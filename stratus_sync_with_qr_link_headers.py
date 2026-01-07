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
###############################################################################
# Fallback file handling
###############################################################################

def _prompt_user_on_throttle() -> str:
    """Prompt the user to choose an action when the STRATUS API is throttled.

    This function prints a message informing the user that the API returned
    HTTP 429 and asks whether to quit or proceed by uploading an existing
    cached JSON file.  The user has 60 seconds to respond; if no input is
    received within that time, an empty string is returned, signalling that
    the default action (upload) should be taken.

    :return: The user's input lower-cased (e.g. 'q' to quit or 'u' to
        upload).  An empty string indicates no response within the timeout.
    """
    import sys
    try:
        import select  # Linux/Unix select for timeout support
    except ImportError:
        # Fallback: no select available, just read input without timeout
        select = None  # type: ignore

    msg = (
        "\nThe STRATUS API returned an HTTP 429 response (throttled due to excessive usage).\n"
        "You may either quit and try again later or proceed by uploading the existing\n"
        "cached JSON file to Smartsheet.  Enter 'q' to quit or 'u' to upload.\n"
        "If no input is provided within 60 seconds, the script will default to uploading\n"
        "the cached JSON file.\n"
    )
    print(msg)
    sys.stdout.write("Enter choice [u/q] and press Enter (default: upload): ")
    sys.stdout.flush()
    if select is not None:
        # Wait up to 60 seconds for input
        try:
            ready, _, _ = select.select([sys.stdin], [], [], 60)
        except Exception:
            ready = None
        if ready:
            try:
                choice = sys.stdin.readline().strip().lower()
            except Exception:
                choice = ""
        else:
            choice = ""
    else:
        try:
            # Without select, just block for input (no timeout)
            choice = sys.stdin.readline().strip().lower()
        except Exception:
            choice = ""
    return choice

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

# ----------------------------------------------------------------------------
# Additional configuration for excluded status synchronization
#
# The primary Smartsheet specified by SMARTSHEET_SHEET_ID is used to store all
# active packages after filtering based on the status list above.  Some
# workflows may require that the filtered (excluded) packages are tracked
# elsewhere.  To support this, the script can optionally push all packages
# whose status matches one of the values in ``EXCLUDED_STATUSES_RAW`` to a
# secondary Smartsheet.  Set ``SMARTSHEET_EXCLUDED_SHEET_ID`` in the
# environment or modify ``EXCLUDED_STATUSES_SHEET_ID`` below to the ID of
# that sheet.  If neither is provided the secondary sync is skipped.

# Default secondary sheet ID for excluded packages.  This can be overridden
# by setting SMARTSHEET_EXCLUDED_SHEET_ID in the environment.
EXCLUDED_STATUSES_SHEET_ID = os.getenv("SMARTSHEET_EXCLUDED_SHEET_ID", "2819292992065412")

#
# Fallback JSON file configuration
#
# When the STRATUS API is throttled (HTTP 429) the script will offer the user
# the option to quit or proceed with a cached JSON file.  The cached file
# location can be configured via the STRATUS_FALLBACK_JSON_PATH environment
# variable.  If unset, we construct a default path based on this script's
# location and the hard-coded REPORT_ID.  The default path follows the
# convention used in some deployments where JSON reports are stored under
# a ``stratus_reports_by_id`` directory and named ``report_<REPORT_ID>.json``.
_script_dir = os.path.dirname(os.path.abspath(__file__))
_default_cache_dir = os.path.join(_script_dir, "stratus_reports_by_id")
_default_cache_name = f"report_{REPORT_ID}.json"
_default_cache_path = os.path.join(_default_cache_dir, _default_cache_name)
FALLBACK_JSON_PATH = os.getenv("STRATUS_FALLBACK_JSON_PATH", _default_cache_path)

###############################################################################
# Excluded status synchronization helpers
###############################################################################

def sync_excluded_to_sheet(
    excluded_df: pd.DataFrame,
    smartsheet_client,
    status_col: Optional[str],
    now_utc: str,
    *,
    json_keys: Optional[List[str]] = None,
    create_missing_columns: bool = True,
) -> None:
    """Synchronize excluded packages to a secondary Smartsheet.

    This helper creates or updates rows in another Smartsheet to record
    information about packages whose status placed them on the exclusion
    list.  Only a subset of columns is written: the unique package ID
    (``REQUIRED_ID_COL``), the human-readable status text, the package name
    with a hyperlink pointing to the QR code (if available), and the audit
    timestamp column (``AUDIT_COL``).

    :param excluded_df: DataFrame containing only the rows that were
        excluded from the primary sheet due to their status.  Each row
        should include at least ``STRATUS.Package.Id`` and the column
        identified by ``status_col``.  Additional fields used to populate
        the package name and QR code hyperlink will be discovered
        automatically.
    :param smartsheet_client: Authenticated Smartsheet client instance.
    :param status_col: Name of the DataFrame column containing the raw
        status values.  The raw values will be converted to strings for
        display in the secondary sheet.  If None, the status field is
        omitted.
    :param now_utc: Timestamp string used for the audit column.
    :param create_missing_columns: Whether to create any columns missing from
        the secondary sheet.  Defaults to True.
    :param json_keys: Optional ordered list of keys from the STRATUS JSON
        records.  If provided, these keys (except for Id and package id)
        will be normalized to column titles and used to build the secondary
        sheet.  If not provided, the keys will be derived from the
        ``excluded_df``.
    """
    # Determine the target sheet ID from environment or constant.  Abort if
    # not configured.
    sheet_id_env = os.getenv("SMARTSHEET_EXCLUDED_SHEET_ID")
    try:
        sheet_id = int(sheet_id_env) if sheet_id_env else int(EXCLUDED_STATUSES_SHEET_ID)
    except Exception:
        print(
            "[EXCLUDED] WARNING: Invalid secondary sheet ID provided; skipping excluded sync.",
            file=sys.stderr,
        )
        return
    # If there is no data or an empty DataFrame, there is nothing to sync
    if excluded_df is None or excluded_df.empty:
        print("[EXCLUDED] No excluded packages to sync to secondary sheet.")
        return
    print(f"[EXCLUDED] Syncing {len(excluded_df)} excluded packages to Smartsheet {sheet_id}...")

    # Identify columns for name and QR code within the excluded dataframe
    name_src = pick_col(
        excluded_df,
        [
            "Name",
            "STRATUS.Name",
            "Stratus.Name",
            "STRATUS.Package.Name",
            "Package Name",
            "Package.Name",
        ],
    )
    qrcode_src = pick_col(excluded_df, ["STRATUS.Package.QRCode"])
    # Title for the display column containing the package name.  This must
    # match the logic used in the main sync to ensure consistent labelling.
    qrcode_col_title = normalize_title("STRATUS.Package.QRCode")

    # Fetch the target sheet and existing column metadata
    try:
        sheet, col_id_by_name = get_sheet_and_columns(smartsheet_client, sheet_id)
    except Exception as e:
        print(
            f"[EXCLUDED] ERROR retrieving secondary sheet {sheet_id}: {e}",
            file=sys.stderr,
        )
        return

    # Build a mapping of column titles to their type for the secondary sheet
    col_type_by_name = {c.title: getattr(c, "type", None) for c in sheet.columns}

    # Determine which columns are required for excluded rows
    # Always require the unique ID and audit timestamp columns
    required_titles: set = {REQUIRED_ID_COL, AUDIT_COL}
    # Derive a list of JSON keys to include in the secondary sheet.  If
    # json_keys was not provided, collect keys from the excluded DataFrame.
    if json_keys is not None:
        json_keys_local = list(json_keys)
    else:
        json_keys_local = collect_keys(excluded_df) if excluded_df is not None else []
    # Build a mapping from JSON keys to column titles using the same
    # normalization as the main sync.  Skip Id and package Id keys.
    json_to_title: Dict[str, str] = {}
    for key in json_keys_local:
        if key in ("Id", "id", "STRATUS.Package.Id"):
            continue
        title = normalize_title(key)
        json_to_title[key] = title
        required_titles.add(title)
    # Add the QR code display column if name information is available
    if name_src:
        required_titles.add(qrcode_col_title)

    # Create any missing columns if permitted
    if create_missing_columns:
        missing_cols = [t for t in required_titles if t not in col_id_by_name]
        if missing_cols:
            # Add each missing column individually.  When adding columns via
            # Smartsheet API, all columns in a single request must specify
            # the same index.  To avoid index mismatch errors, create one
            # column per request and refresh sheet metadata after each.
            anchor_index = len(col_id_by_name)
            for t in missing_cols:
                col = smartsheet.models.Column()
                col.title = t
                col.type = "TEXT_NUMBER"
                col.index = anchor_index  # insert new columns at the end
                col.primary = False
                try:
                    _ = smartsheet_client.Sheets.add_columns(sheet_id, [col])
                except Exception as e:
                    print(
                        f"[EXCLUDED] ERROR creating column '{t}' in secondary sheet: {e}",
                        file=sys.stderr,
                    )
                    # Continue with the next column
                    continue
                # Refresh sheet metadata after adding this column
                try:
                    sheet, col_id_by_name = get_sheet_and_columns(smartsheet_client, sheet_id)
                    col_type_by_name = {c.title: getattr(c, "type", None) for c in sheet.columns}
                except Exception:
                    pass

    # Build a mapping of existing rows in the secondary sheet keyed by package ID
    try:
        existing = build_existing_row_map(sheet)
    except Exception as e:
        print(
            f"[EXCLUDED] ERROR building row map for secondary sheet: {e}",
            file=sys.stderr,
        )
        return

    adds: List[smartsheet.models.Row] = []
    updates: List[smartsheet.models.Row] = []

    # Loop over each excluded package and prepare row data
    for idx, row in excluded_df.iterrows():
        pkg_id = str(row.get("STRATUS.Package.Id", "")).strip()
        if not pkg_id:
            continue
        # Construct data dictionary for the row
        data_by_colname: Dict[str, object] = {
            REQUIRED_ID_COL: pkg_id,
            AUDIT_COL: now_utc,
        }
        # Populate all dynamic fields from the JSON mapping
        for key, title in json_to_title.items():
            try:
                val = row.get(key)
            except Exception:
                val = None
            # Normalize pandas NaN to None
            if isinstance(val, float) and pd.isna(val):
                val = None
            data_by_colname[title] = val
        # Prepare optional hyperlink for package name
        hyperlinks: Dict[str, str] = {}
        if name_src:
            display_name = row.get(name_src)
            # Override the QR code display column with the package name
            data_by_colname[qrcode_col_title] = display_name
            # If there is a QR code URL and it looks like a URL, attach as hyperlink
            if qrcode_src:
                link_url = row.get(qrcode_src)
                if isinstance(link_url, str) and link_url.lower().startswith(("http://", "https://")):
                    hyperlinks[qrcode_col_title] = link_url
        # Decide whether to add or update
        if pkg_id in existing:
            current = existing[pkg_id]["cells"]
            current_links = existing[pkg_id].get("hyperlinks", {})
            changed: Dict[str, object] = {}
            # Determine changed values
            for col_name, new_val in data_by_colname.items():
                cur_val = current.get(col_name)
                # Normalize floats representing NaN to None
                if isinstance(new_val, float) and pd.isna(new_val):
                    new_val = None
                if new_val != cur_val:
                    changed[col_name] = new_val
            # Only update hyperlink if it has changed
            for col_name, url in hyperlinks.items():
                if col_name in col_id_by_name:
                    if current_links.get(col_name) != url:
                        changed.setdefault(col_name, current.get(col_name))
            if changed:
                update_row = smartsheet.models.Row()
                update_row.id = existing[pkg_id]["rowId"]
                update_row.cells = cells_for_row_update(col_id_by_name, changed, hyperlinks=hyperlinks)
                updates.append(update_row)
        else:
            add_row = smartsheet.models.Row()
            add_row.to_top = True
            add_row_data = dict(data_by_colname)
            add_row.cells = cells_for_row_update(col_id_by_name, add_row_data, hyperlinks=hyperlinks)
            adds.append(add_row)

    # Write rows in batches of up to 400 (Smartsheet API limit)
    def chunk(seq, n):
        for i in range(0, len(seq), n):
            yield seq[i : i + n]

    added_count = 0
    for batch in chunk(adds, 400):
        if not batch:
            continue
        try:
            resp = smartsheet_client.Sheets.add_rows(sheet_id, batch)
            added_count += len(resp.result)
        except Exception as e:
            print(f"[EXCLUDED] ERROR adding rows: {e}", file=sys.stderr)

    updated_count = 0
    for batch in chunk(updates, 400):
        if not batch:
            continue
        try:
            batch = merge_updates_by_rowid(batch)
            if not batch:
                continue
            resp = smartsheet_client.Sheets.update_rows(sheet_id, batch)
            updated_count += len(resp.result)
        except Exception as e:
            print(f"[EXCLUDED] ERROR updating rows: {e}", file=sys.stderr)

    print(
        f"[EXCLUDED] Sync complete. Added {added_count}, Updated {updated_count} in secondary sheet {sheet_id}."
    )

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
    # Attempt to fetch the report JSON.  If the STRATUS API is being throttled
    # (HTTP 429), prompt the user for a course of action.  The user can quit
    # or choose to upload a cached JSON file.  If no response is received
    # within 60 seconds, the cached file will be used automatically.
    try:
        rows = fetch_report_json()
    except RuntimeError as _e:
        err_msg = str(_e)
        if "HTTP 429" in err_msg or "throttled" in err_msg.lower():
            choice = _prompt_user_on_throttle()
            # Interpret the choice: any input starting with 'q' is a quit
            if choice and choice.startswith("q"):
                print("User chose to quit due to STRATUS API throttling.")
                return
            # Default or 'u' triggers upload of cached JSON
            fallback_path = FALLBACK_JSON_PATH
            if not os.path.exists(fallback_path):
                # Prompt the user for an alternate JSON file path
                print(
                    f"WARNING: The default cached JSON file '{fallback_path}' was not found.",
                    file=sys.stderr,
                )
                # Ask for a file path with a timeout of 60 seconds.  If the user
                # provides nothing, the script will abort.  Use select for a
                # timeout if available.
                print(
                    "Please enter the path to an existing JSON file to upload, or press Enter to quit."
                )
                sys.stdout.write("JSON file path: ")
                sys.stdout.flush()
                try:
                    import select as _sel  # type: ignore
                except Exception:
                    _sel = None  # type: ignore
                user_path = ""
                if _sel is not None:
                    try:
                        ready, _, _ = _sel.select([sys.stdin], [], [], 60)
                    except Exception:
                        ready = None
                    if ready:
                        try:
                            user_path = sys.stdin.readline().strip()
                        except Exception:
                            user_path = ""
                    else:
                        user_path = ""
                else:
                    # Without select, just read input (blocking)
                    try:
                        user_path = sys.stdin.readline().strip()
                    except Exception:
                        user_path = ""
                if not user_path:
                    print("No JSON file provided. Exiting due to throttled API.")
                    return
                fallback_path = user_path
                if not os.path.exists(fallback_path):
                    print(
                        f"The specified JSON file '{fallback_path}' does not exist. Exiting.",
                        file=sys.stderr,
                    )
                    return
            try:
                with open(fallback_path, "r", encoding="utf-8") as f:
                    rows = json.load(f)
                print(
                    f"Loaded {len(rows)} records from JSON file '{fallback_path}' due to API throttling."
                )
            except Exception as exc:
                print(
                    f"ERROR: Failed to read JSON file '{fallback_path}': {exc}",
                    file=sys.stderr,
                )
                return
        else:
            # Not a throttling error; re-raise
            raise
    # Persist the fetched JSON to the fallback cache for future runs.  Ensure
    # that the parent directory exists before writing the file.
    try:
        cache_dir = os.path.dirname(FALLBACK_JSON_PATH)
        if cache_dir and not os.path.isdir(cache_dir):
            os.makedirs(cache_dir, exist_ok=True)
        with open(FALLBACK_JSON_PATH, "w", encoding="utf-8") as f:
            json.dump(rows, f)
    except Exception as exc:
        print(
            f"WARNING: Could not write fallback JSON cache to '{FALLBACK_JSON_PATH}': {exc}",
            file=sys.stderr,
        )
    print(f"Loaded {len(rows)} records for reportId={REPORT_ID}")

    print("[2/6] Converting to DataFrame...")
    df = dataframe_from_records(rows)
    if df.empty:
        print("No data returned from STRATUS for this report.")
        return

    # Keep a copy of the full DataFrame before filtering so that excluded
    # packages can be synchronized to a secondary sheet later on.
    full_df = df.copy()
    # Collect JSON keys from the full DataFrame to ensure all fields are
    # available for the excluded sheet.  These keys will be passed to the
    # excluded sync so that all columns from the STRATUS report are
    # represented in the secondary sheet.
    json_keys_all = collect_keys(full_df)

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

    # Build excluded set and detect status column using the full DataFrame
    excluded_set = build_excluded_status_set()
    status_col = find_best_status_column(full_df, excluded_set)

    # Identify packages to exclude and split the DataFrame accordingly
    excluded_pkg_ids: Set[str] = set()
    excluded_df: pd.DataFrame = pd.DataFrame()
    if status_col:
        status_norm = full_df[status_col].apply(normalize_status_value)
        exclude_mask = status_norm.apply(lambda s: (s in excluded_set) if s else False)
        excluded_df = full_df.loc[exclude_mask].reset_index(drop=True)
        excluded_pkg_ids = set(excluded_df[pkg_id_col].astype(str).tolist())
        removed = int(exclude_mask.sum())
        if removed:
            print(
                f"[FILTER] Excluding {removed} packages by status using column '{status_col}':"
            )
            # Show a breakdown of excluded statuses for logging
            try:
                counts = full_df.loc[exclude_mask, status_col].astype(str).value_counts().head(20)
                for k, v in counts.items():
                    print(f"  - {k}: {v}")
            except Exception:
                pass
        # Build df from only non-excluded packages
        df = full_df.loc[~exclude_mask].reset_index(drop=True)
    else:
        print(
            "[FILTER] WARNING: Could not confidently detect a Status/TrackingStatus column in this report. "
            "No status filtering applied."
        )
        df = full_df

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
    # Build a mapping from column title to column type.  This will be used
    # later to convert values into appropriate types before sending to
    # Smartsheet.  For example, date columns require values in ISO date
    # format (YYYY-MM-DD).
    col_type_by_name = {c.title: getattr(c, "type", None) for c in sheet.columns}

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
                # also update col_type_by_name after adding
                col_type_by_name = {c.title: getattr(c, "type", None) for c in sheet.columns}
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
        # Build a row's data dict for update/add.  Normalize NaN to None,
        # and convert date columns to the expected ISO date format.  Smartsheet
        # expects date-only columns to be provided as 'YYYY-MM-DD'.  Values that
        # cannot be parsed as dates will be set to None to avoid API errors.
        data = {}
        for col in writable_cols:
            val = r.get(col)
            # Convert pandas NaN to None
            if isinstance(val, float) and pd.isna(val):
                data[col] = None
            else:
                col_type = col_type_by_name.get(col)
                if col_type and str(col_type).upper() == "DATE" and val not in (None, ""):
                    try:
                        dt = pd.to_datetime(val, errors="coerce")
                        if pd.notna(dt):
                            data[col] = dt.date().isoformat()
                        else:
                            data[col] = None
                    except Exception:
                        data[col] = None
                else:
                    data[col] = val
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
                # Convert NaN represented as float to None
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

    # Synchronize excluded packages to the secondary sheet, if configured.  This
    # occurs after updates to the primary sheet so that the audit timestamp
    # remains consistent across both operations.  Pass along the same
    # status column and timestamp used earlier.
    try:
        if 'excluded_df' in locals() and excluded_df is not None and not excluded_df.empty:
            # Pass the full set of JSON keys so that the secondary sheet
            # includes all available columns from the STRATUS report.
            sync_excluded_to_sheet(
                excluded_df,
                smartsheet_client,
                status_col,
                now_utc,
                json_keys=json_keys_all,
            )
    except Exception as exc:
        print(f"[EXCLUDED] ERROR during secondary sync: {exc}", file=sys.stderr)

    print(f"Done. Added {added}, Updated {updated_count}. Timestamp (UTC): {now_utc}")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(2)

