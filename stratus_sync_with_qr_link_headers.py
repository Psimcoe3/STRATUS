#!/usr/bin/env python3
"""
STRATUS report (hardcoded) -> Smartsheet sync with QR-code hyperlink

This script downloads a STRATUS dashboard report, normalizes the data and
uploads it into a Smartsheet sheet. It automatically creates any missing
columns and uses a package ID field as the unique key for upserts.  A
special case is handled for the `STRATUS.Package.QRCode` field: the
generated column is labelled ``STRATUS PACKAGE NAME`` and the value is
replaced by the package name while attaching the original QR code URL as
a hyperlink.  An audit timestamp column records the last sync time in UTC.

Additionally, this version filters out any records whose package status
matches a set of excluded values (see ``EXCLUDED_STATUSES_RAW``).  Status
values can arrive as simple strings, dictionaries or lists, and the
normalization helper handles these shapes.  If excluded packages already
exist in the target Smartsheet, they can optionally be deleted during
synchronization.

Environment variables (``.env`` file is supported):

  STRATUS_AUTH_HEADER_NAME   Name of header for STRATUS API authentication (default: ``app-key``)
  STRATUS_AUTH_HEADER_VALUE  Value for the authentication header (required)
  STRATUS_COMPANY_ID         STRATUS company identifier (required)
  STRATUS_BASE_URL           Base URL for STRATUS API (default: ``https://api.gtpstratus.com``)
  SMARTSHEET_ACCESS_TOKEN    Smartsheet API token (required)
  SMARTSHEET_SHEET_ID        Smartsheet sheet ID to update (required, integer)

"""

import os
import sys
import json
from datetime import datetime, timezone
from typing import List, Dict, Optional, Set

import re
import requests
import pandas as pd

try:
    # Load environment variables from .env if present
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

import smartsheet

###############################################################################
# Configuration
###############################################################################

# Hard-coded STRATUS report ID
REPORT_ID = "8699665f-d63e-47ab-8c2f-fa05e1547554"

# Name of column used to store the unique package identifier in Smartsheet
REQUIRED_ID_COL = "STRATUS_PackageId"

# Column where the sync timestamp is recorded
AUDIT_COL = "Last Sync From STRATUS (UTC)"

# Whether to automatically create missing columns in the target Smartsheet
AUTO_CREATE_COLUMNS = True

# Maximum length for generated column titles
MAX_COLUMN_TITLE_LEN = 200

# Status-based exclusion before upload.  Rows whose normalized status
# matches one of these values will be filtered out of the DataFrame
# prior to synchronization.  If DELETE_EXCLUDED_FROM_SHEET is True, any
# existing rows in Smartsheet with those package IDs will also be deleted.
DELETE_EXCLUDED_FROM_SHEET = True

EXCLUDED_STATUSES_RAW = [
    "Shipped to Jobsite",
    "Received on Jobsite",
    "Jobsite",
    "Issued for Installation",
    "Installed",
    "Wire Pulled",
    "Trim and Terminations Complete",
    "Point List Ready",
    "FAB CANCELLED",
    "NO PREFAB (FIELD INSTALL)",
    "Returned/Received",
]

###############################################################################
# STRATUS API fetch
###############################################################################

def fetch_report_json() -> list:
    """Fetch the dashboard report from STRATUS and return it as a list of records.

    This function constructs the request URL and headers based on environment
    variables.  The response JSON can be returned as a list or a dict; both
    cases are handled and the function always returns a list of records.
    """
    base_url = os.getenv("STRATUS_BASE_URL", "https://api.gtpstratus.com").rstrip("/")
    company_id = os.getenv("STRATUS_COMPANY_ID")
    hdr_name = os.getenv("STRATUS_AUTH_HEADER_NAME", "app-key")
    hdr_value = os.getenv("STRATUS_AUTH_HEADER_VALUE")

    if not company_id or not hdr_value:
        raise RuntimeError(
            "STRATUS_COMPANY_ID and STRATUS_AUTH_HEADER_VALUE must be set (.env)"
        )

    url = f"{base_url}/v1/package/dashboard"
    headers = {hdr_name: hdr_value, "Accept": "application/json"}
    params = {
        "companyId": company_id,
        "reportId": REPORT_ID,
        "projectId": 0,
        "modelId": 0,
    }

    resp = requests.get(url, headers=headers, params=params, timeout=300)
    if resp.status_code != 200:
        raise RuntimeError(
            f"HTTP {resp.status_code} from {url}: {(resp.text or '')[:400]}"
        )

    data = resp.json()
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        # In some cases the list may be nested under an arbitrary key
        for v in data.values():
            if isinstance(v, list):
                return v
        return [data]
    raise RuntimeError("Unexpected JSON shape from STRATUS")

###############################################################################
# Helper functions
###############################################################################

def pick_col(df: pd.DataFrame, names: List[str]) -> Optional[str]:
    """Return the first column in df matching one of the names, or None."""
    for n in names:
        if n in df.columns:
            return n
    return None


def normalize_title(key: str) -> str:
    """Generate a Smartsheet column title from a JSON key.

    Special cases:

    - ``Id``/``id`` become ``REQUIRED_ID_COL``.
    - ``STRATUS.Package.QRCode`` becomes "STRATUS PACKAGE NAME".
    - All other keys are prefaced with ``STRATUS `` and dots are replaced with spaces.

    Long titles are truncated to MAX_COLUMN_TITLE_LEN characters.
    """
    if key in ("Id", "id"):
        return REQUIRED_ID_COL
    if key == "STRATUS.Package.QRCode":
        return "STRATUS PACKAGE NAME"
    t = str(key).replace("\n", " ").replace("\r", " ")
    t = " ".join(t.split())[:MAX_COLUMN_TITLE_LEN]
    t = t.replace(".", " ")
    return f"STRATUS {t}"


def collect_keys(df: pd.DataFrame) -> List[str]:
    """Order JSON keys so that package IDs and generic Id fields come first."""
    cols = list(df.columns)
    ordered: List[str] = []
    if "STRATUS.Package.Id" in cols:
        ordered.append("STRATUS.Package.Id")
        cols.remove("STRATUS.Package.Id")
    for id_key in ("Id", "id"):
        if id_key in cols:
            ordered.append(id_key)
            cols.remove(id_key)
            break
    ordered.extend(cols)
    return ordered


def dataframe_from_records(rows: list) -> pd.DataFrame:
    """Build a DataFrame from a list of JSON records, flattening nested keys."""
    if not rows:
        return pd.DataFrame()
    return pd.json_normalize(rows)


def get_sheet_and_columns(smartsheet_client, sheet_id):
    """Retrieve the Smartsheet and build a mapping from column title to column ID."""
    sheet = smartsheet_client.Sheets.get_sheet(sheet_id)
    col_id_by_name = {c.title: c.id for c in sheet.columns}
    return sheet, col_id_by_name


def build_existing_row_map(sheet) -> Dict[str, dict]:
    """Build a mapping of existing rows keyed by package ID.

    The returned dictionary maps package ID strings to a dict containing the
    row ID, existing cell values keyed by column title, and existing
    hyperlinks keyed by column title.  Rows without a valid package ID are
    ignored.
    """
    id_col_id = None
    for c in sheet.columns:
        if c.title == REQUIRED_ID_COL:
            id_col_id = c.id
            break
    if not id_col_id:
        raise RuntimeError(f"Smartsheet must have a '{REQUIRED_ID_COL}' column.")

    existing: Dict[str, dict] = {}
    title_by_colid = {c.id: c.title for c in sheet.columns}

    for row in sheet.rows:
        key = None
        values_by_title: Dict[str, object] = {}
        links_by_title: Dict[str, str] = {}
        for cell in row.cells:
            title = title_by_colid.get(cell.column_id)
            # capture value and display_value fallback
            val = getattr(cell, "value", None)
            if val is None:
                val = getattr(cell, "display_value", None)
            if title:
                values_by_title[title] = val
            # capture hyperlink URL if present
            link_obj = getattr(cell, "hyperlink", None)
            link_url = getattr(link_obj, "url", None) if link_obj else None
            if link_url:
                links_by_title[title] = link_url
            # detect row key
            if title == REQUIRED_ID_COL:
                key = str(val) if val is not None else None
        if key:
            existing[key] = {
                "rowId": row.id,
                "cells": values_by_title,
                "hyperlinks": links_by_title,
            }
    return existing


def cells_for_row_update(
    col_id_by_name: Dict[str, int],
    data_by_colname: Dict[str, object],
    hyperlinks: Optional[Dict[str, str]] = None,
) -> List[smartsheet.models.Cell]:
    """Build a list of Smartsheet Cell objects for an update or add operation."""
    cells = []
    for name, value in data_by_colname.items():
        if name not in col_id_by_name:
            continue
        cell = smartsheet.models.Cell()
        cell.column_id = col_id_by_name[name]
        # Convert NaN to None for Smartsheet
        cell.value = None if (isinstance(value, float) and pd.isna(value)) else value
        # Attach hyperlink if provided
        link_url = (hyperlinks or {}).get(name)
        if link_url:
            hl = smartsheet.models.Hyperlink()
            hl.url = str(link_url)
            cell.hyperlink = hl
        cells.append(cell)
    return cells


def merge_updates_by_rowid(
    rows: List[smartsheet.models.Row],
) -> List[smartsheet.models.Row]:
    """Merge multiple updates targeting the same row ID into a single Row object."""
    merged: Dict[int, smartsheet.models.Row] = {}
    for r in rows:
        rid = r.id
        if rid not in merged:
            merged[rid] = r
        else:
            by_col = {c.column_id: c for c in merged[rid].cells}
            for c in r.cells:
                by_col[c.column_id] = c
            merged[rid].cells = list(by_col.values())
    return list(merged.values())

###############################################################################
# Status filtering helpers
###############################################################################

def _norm_ws(s: str) -> str:
    """Collapse consecutive whitespace into single spaces and strip."""
    return re.sub(r"\s+", " ", s).strip()


def normalize_status_value(x) -> str:
    """Normalize a status value for comparison.

    Status values can be provided as strings, dictionaries or lists.  This
    function attempts to extract a human-readable text by looking for common
    keys such as Name, Status, Value, Text, Label or Title.  The result is
    then normalized: newlines and carriage returns are replaced with spaces,
    extra whitespace collapsed, and the string case-folded for case-insensitive
    comparison.
    """
    if x is None:
        return ""
    # If the value is a dict, try to pull out a human-readable field
    if isinstance(x, dict):
        for k in (
            "Name",
            "name",
            "Status",
            "status",
            "Value",
            "value",
            "Text",
            "text",
            "Label",
            "label",
            "Title",
            "title",
        ):
            if k in x and x[k] is not None:
                x = x[k]
                break
        else:
            # single-key dict fallback
            if len(x) == 1:
                x = next(iter(x.values()))
            else:
                # if multiple keys, fall back to JSON string
                x = json.dumps(x, ensure_ascii=False)
    # If it's a list, pick the first element
    if isinstance(x, list) and x:
        x = x[0]
    s = str(x).replace("\n", " ").replace("\r", " ")
    s = _norm_ws(s).casefold()
    return s


def build_excluded_status_set() -> Set[str]:
    """Return a set of normalized excluded status values."""
    return {normalize_status_value(s) for s in EXCLUDED_STATUSES_RAW}


def find_best_status_column(df: pd.DataFrame, excluded_set: Set[str]) -> Optional[str]:
    """Select the column containing status values to filter against.

    This function first attempts to pick columns whose names contain
    ``status``.  It then ranks candidate columns by the number of rows
    matching one of the excluded status values.  If multiple columns match
    equally, secondary heuristics favour names containing ``trackingstatus`` or
    ending in ``.name``.  A candidate must match at least one excluded
    status value within a sample of the DataFrame or no column is selected.
    """
    if df is None or df.empty:
        return None
    # Sample a subset for efficiency
    sample = df.head(5000)
    # Initial candidates: any column with 'status' in its name (case-insensitive)
    candidates = [
        c
        for c in df.columns
        if "status" in str(c).casefold()
    ]
    # Evaluate candidates
    best_col: Optional[str] = None
    best_matches = -1
    best_name_score = -1
    for col in candidates:
        lname = str(col).casefold()
        # Skip columns that likely hold IDs
        if lname.endswith("id") or "statusid" in lname or ("id" in lname and "name" not in lname):
            continue
        # Count how many rows match an excluded status in the sample
        vals = sample[col].apply(normalize_status_value)
        matches = int(vals.isin(excluded_set).sum())
        # Secondary scoring based on name
        name_score = 0
        if "trackingstatus" in lname:
            name_score += 5
        if lname.endswith(".name") or lname.endswith("name"):
            name_score += 2
        if "package" in lname:
            name_score += 1
        if matches > best_matches or (matches == best_matches and name_score > best_name_score):
            best_col = col
            best_matches = matches
            best_name_score = name_score
    # Only accept if at least one match was found
    return best_col if best_matches >= 1 else None

###############################################################################
# Main synchronization logic
###############################################################################

def main():
    """Entry point for the script."""
    token = os.getenv("SMARTSHEET_ACCESS_TOKEN")
    sheet_id_str = os.getenv("SMARTSHEET_SHEET_ID")
    if not token or not sheet_id_str:
        print(
            "ERROR: Set SMARTSHEET_ACCESS_TOKEN and SMARTSHEET_SHEET_ID in .env",
            file=sys.stderr,
        )
        sys.exit(2)
    try:
        sheet_id = int(sheet_id_str)
    except Exception:
        print("ERROR: SMARTSHEET_SHEET_ID must be an integer", file=sys.stderr)
        sys.exit(2)

    smartsheet_client = smartsheet.Smartsheet(token)
    smartsheet_client.errors_as_exceptions(True)

    print("[1/6] Fetching STRATUS report JSON (hardcoded)...")
    rows = fetch_report_json()
    print(f"Loaded {len(rows)} records for reportId={REPORT_ID}")

    print("[2/6] Converting to DataFrame...")
    df = dataframe_from_records(rows)
    if df.empty:
        print("No data returned from STRATUS for this report.")
        return

    # Determine JSON keys and build write_df template
    json_keys = collect_keys(df)
    write_df = pd.DataFrame()

    # Unique key: STRATUS.Package.Id
    pkg_id_col = pick_col(df, ["STRATUS.Package.Id"])
    if not pkg_id_col:
        available = ", ".join(df.columns.tolist())
        raise RuntimeError(
            "Required 'STRATUS.Package.Id' not found in data. Available: " + available
        )

    # Build excluded set and detect status column
    excluded_set = build_excluded_status_set()
    status_col = find_best_status_column(df, excluded_set)

    excluded_pkg_ids: Set[str] = set()
    if status_col:
        # Normalize status values and build exclusion mask
        status_norm = df[status_col].apply(normalize_status_value)
        exclude_mask = status_norm.apply(lambda s: (s in excluded_set) if s else False)
        excluded_pkg_ids = set(df.loc[exclude_mask, pkg_id_col].astype(str).tolist())
        removed = int(exclude_mask.sum())
        if removed:
            print(
                f"[FILTER] Excluding {removed} packages by status using column '{status_col}':"
            )
            # Show a breakdown of excluded statuses for logging
            try:
                counts = df.loc[exclude_mask, status_col].astype(str).value_counts().head(20)
                for k, v in counts.items():
                    print(f"  - {k}: {v}")
            except Exception:
                pass
        # Filter df in place
        df = df.loc[~exclude_mask].reset_index(drop=True)
    else:
        print(
            "[FILTER] WARNING: Could not confidently detect a Status/TrackingStatus column in this report. "
            "No status filtering applied."
        )

    # Build Smartsheet-facing DataFrame after filtering
    write_df[REQUIRED_ID_COL] = df[pkg_id_col].astype(str)
    json_to_sheet: Dict[str, str] = {}
    for key in json_keys:
        if key in ("Id", "id", "STRATUS.Package.Id"):
            continue
        title = normalize_title(key)
        json_to_sheet[key] = title
        if key in df.columns:
            write_df[title] = df[key]
        else:
            write_df[title] = None

    # Ensure the QR code display column exists in write_df
    qrcode_col_title = normalize_title("STRATUS.Package.QRCode")  # "STRATUS PACKAGE NAME"
    if qrcode_col_title not in write_df.columns:
        write_df[qrcode_col_title] = None

    # Add audit timestamp
    now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    write_df[AUDIT_COL] = now_utc

    print("[3/6] Loading Smartsheet and columns...")
    sheet, col_id_by_name = get_sheet_and_columns(smartsheet_client, sheet_id)

    # Determine which columns are needed
    required_titles = {REQUIRED_ID_COL, AUDIT_COL}
    dynamic_titles = set(json_to_sheet.values())
    dynamic_titles.add(qrcode_col_title)
    needed = list(required_titles | dynamic_titles)

    # Add missing columns if configured
    if AUTO_CREATE_COLUMNS:
        missing = [t for t in needed if t not in col_id_by_name]
        if missing:
            cols_to_add = []
            anchor_index = len(col_id_by_name)
            for t in missing:
                col = smartsheet.models.Column()
                col.title = t
                col.type = "TEXT_NUMBER"
                col.index = anchor_index
                col.primary = False
                cols_to_add.append(col)
            if cols_to_add:
                _ = smartsheet_client.Sheets.add_columns(sheet_id, cols_to_add)
                sheet, col_id_by_name = get_sheet_and_columns(smartsheet_client, sheet_id)
    else:
        missing = [t for t in needed if t not in col_id_by_name]
        if missing:
            print(
                "ERROR: Missing columns and AUTO_CREATE_COLUMNS=False:",
                missing,
            )
            sys.exit(2)

    print("[4/6] Building existing row map...")
    existing = build_existing_row_map(sheet)

    # Optionally delete excluded packages already present in Smartsheet
    if DELETE_EXCLUDED_FROM_SHEET and excluded_pkg_ids:
        delete_row_ids = [existing[k]["rowId"] for k in excluded_pkg_ids if k in existing]
        if delete_row_ids:
            print(
                f"[DELETE] Removing {len(delete_row_ids)} excluded packages from Smartsheet..."
            )
            def chunk_iterable(lst, n):
                for i in range(0, len(lst), n):
                    yield lst[i : i + n]
            for batch in chunk_iterable(delete_row_ids, 400):
                try:
                    smartsheet_client.Sheets.delete_rows(sheet_id, batch)
                except TypeError:
                    # some SDK versions require a comma-separated string
                    smartsheet_client.Sheets.delete_rows(sheet_id, ",".join(str(x) for x in batch))

    # Identify columns that can be updated (skip the key column)
    writable_cols = [c for c in write_df.columns if c in col_id_by_name and c != REQUIRED_ID_COL]

    # Precompute name and QR code source columns
    name_src_df = pick_col(
        df,
        [
            "Name",
            "STRATUS.Name",
            "Stratus.Name",
            "STRATUS.Package.Name",
            "Package Name",
            "Package.Name",
        ],
    )
    qrcode_src_df = pick_col(df, ["STRATUS.Package.QRCode"])

    print("[5/6] Preparing row diffs...")
    adds: List[smartsheet.models.Row] = []
    updates: List[smartsheet.models.Row] = []
    for idx, r in write_df.iterrows():
        key = str(r.get(REQUIRED_ID_COL, "")).strip()
        if not key:
            continue
        # Build a row's data dict for update/add
        data = {
            col: (None if (isinstance(r.get(col), float) and pd.isna(r.get(col))) else r.get(col))
            for col in writable_cols
        }
        # Build hyperlink mapping for QR code column
        hyperlinks: Dict[str, str] = {}
        if qrcode_col_title in writable_cols:
            # Use Name field as display text
            display_name = None
            if name_src_df and name_src_df in df.columns:
                display_name = df.loc[idx, name_src_df]
            if display_name is not None:
                data[qrcode_col_title] = display_name
            # Attach QR code URL as hyperlink if valid
            if qrcode_src_df and qrcode_src_df in df.columns:
                link_url = df.loc[idx, qrcode_src_df]
                if isinstance(link_url, str) and link_url.lower().startswith(("http://", "https://")):
                    hyperlinks[qrcode_col_title] = link_url
        # Determine if this row exists and build add/update objects
        if key in existing:
            current = existing[key]["cells"]
            current_links = existing[key].get("hyperlinks", {})
            changed: Dict[str, object] = {}
            for col, new_val in data.items():
                cur_val = current.get(col)
                if isinstance(new_val, float) and pd.isna(new_val):
                    new_val = None
                if new_val != cur_val:
                    changed[col] = new_val
            # Only update hyperlink if it has changed
            for col, url in hyperlinks.items():
                if col in col_id_by_name:
                    if current_links.get(col) != url:
                        # Retain existing cell value if not already in changed
                        changed.setdefault(col, current.get(col))
            if changed:
                row = smartsheet.models.Row()
                row.id = existing[key]["rowId"]
                row.cells = cells_for_row_update(col_id_by_name, changed, hyperlinks=hyperlinks)
                updates.append(row)
        else:
            row = smartsheet.models.Row()
            row.to_top = True
            row_data = dict(data)
            row_data[REQUIRED_ID_COL] = key
            row.cells = cells_for_row_update(col_id_by_name, row_data, hyperlinks=hyperlinks)
            adds.append(row)

    print(f"[6/6] Writing to Smartsheet: {len(adds)} adds, {len(updates)} updates")
    # Write adds in batches
    def chunk_iter(lst, n):
        for i in range(0, len(lst), n):
            yield lst[i : i + n]

    added = 0
    for batch in chunk_iter(adds, 400):
        if not batch:
            continue
        resp = smartsheet_client.Sheets.add_rows(sheet_id, batch)
        added += len(resp.result)

    updated_count = 0
    for batch in chunk_iter(updates, 400):
        if not batch:
            continue
        batch = merge_updates_by_rowid(batch)
        if not batch:
            continue
        resp = smartsheet_client.Sheets.update_rows(sheet_id, batch)
        updated_count += len(resp.result)

    print(f"Done. Added {added}, Updated {updated_count}. Timestamp (UTC): {now_utc}")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(2)
