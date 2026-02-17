
#!/usr/bin/env python3
"""
STRATUS -> Smartsheet daily sync @ 4:00 PM
------------------------------------------
- Pull a STRATUS Packages CSV via API (preferred) or local fallback
- Upsert rows into Smartsheet sheet 750890915942276 using STRATUS_PackageId as the unique key

Setup:
  pip install smartsheet-python-sdk pandas requests python-dotenv

Env vars (recommended: put in a `.env` file in the same directory; **do not include inline comments**):
  SMARTSHEET_ACCESS_TOKEN = your Smartsheet API token
  STRATUS_API_KEY        = your STRATUS API key
  STRATUS_COMPANY_ID     = your STRATUS company GUID (required by most endpoints)
  STRATUS_PROJECT_ID     = project ID for dashboard exports (0 means "All Projects")
  STRATUS_MODEL_ID       = model ID for dashboard exports (0 means "All Models")
  STRATUS_REPORT_ID      = optional report GUID if you use the report export API
  STRATUS_BASE_URL       = https://api.gtpstratus.com
  STRATUS_COOKIE         = optional session cookie (e.g., gtp.auth=...) to emulate browser auth
  STRATUS_CSV_FALLBACK   = optional absolute path to a local CSV file used if API calls fail

Notes:
- The STRATUS API surface for CSV exports differs by tenant and API version. This script attempts
  multiple dashboard (orders) and report endpoints with various header permutations to find one
  that works for your environment. See the `fetch_stratus_csv()` docstring for details.
"""

import os
import io
import sys
import time
import csv
import json
import uuid
import argparse
from datetime import datetime, timezone
import requests
import pandas as pd

try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

import smartsheet

from stratus_logger import get_logger
logger = get_logger(__name__)

SHEET_ID = 750890915942276

# Column names in Smartsheet (exact text) we plan to write to.
# Add to this mapping if you want to include more fields from STRATUS.
TARGET_COLUMNS = [
    "STRATUS_PackageId",
    "Package Name",
    "Tracking Status",
    "%Complete",
    "Hours Estimated",
    "Actual Hours",
    "Division",
    "Last Sync From STRATUS (UTC)"
]

# Map STRATUS CSV columns -> Smartsheet column names
STRATUS_TO_SHEET = {
    "Id": "STRATUS_PackageId",
    "Package Name": "Package Name",
    "Status": "Tracking Status",
    "%Complete": "%Complete",
    "Hours Estimated": "Hours Estimated",
    "Actual Hours": "Actual Hours",
    "Division": "Division",
}

def fetch_stratus_csv():
    """
    Fetch the STRATUS Packages CSV via API.

    This helper will iterate over a series of candidate endpoints and authentication header
    permutations to find a combination that returns a 200 OK and non-empty content.

    Supported configuration via environment variables (recommended to store in a `.env` file):

      STRATUS_BASE_URL       – Base API URL, e.g. https://api.gtpstratus.com
      STRATUS_COMPANY_ID     – Your company GUID, required by many endpoints
      STRATUS_PROJECT_ID     – Project ID for dashboard exports; 0 for “All Projects”
      STRATUS_MODEL_ID       – Model ID for dashboard exports; 0 for “All Models”
      STRATUS_REPORT_ID      – Report GUID for report exports (if you capture via DevTools)
      STRATUS_API_KEY        – API key issued by STRATUS
      STRATUS_COOKIE         – Optional session cookie (e.g., gtp.auth=<value>) to mimic browser requests
      STRATUS_CSV_FALLBACK   – Path to a local CSV file as a last resort fallback

    The function prints each attempted URL and header combination to aid troubleshooting.
    """
    logger.debug("Entering fetch_stratus_csv()")
    base_url = os.getenv("STRATUS_BASE_URL", "https://api.gtpstratus.com").rstrip("/")
    report_id = os.getenv("STRATUS_REPORT_ID")
    company_id = os.getenv("STRATUS_COMPANY_ID")
    api_key = os.getenv("STRATUS_API_KEY")
    project_id = os.getenv("STRATUS_PROJECT_ID", "0")
    model_id = os.getenv("STRATUS_MODEL_ID", "0")
    session_cookie = os.getenv("STRATUS_COOKIE", "")
    logger.debug("base_url=%s, company_id=%s, project_id=%s, model_id=%s, report_id=%s",
                 base_url, company_id, project_id, model_id, report_id)

    # Build a list of authentication header sets. Some tenants require different header names.
    header_sets = []
    if api_key:
        header_sets.extend([
            {"Authorization": f"Bearer {api_key}", "x-api-key": api_key, "Accept": "text/csv"},
            {"x-api-key": api_key, "Accept": "text/csv"},
            {"X-API-KEY": api_key, "Accept": "text/csv"},
            {"Api-Key": api_key, "Accept": "text/csv"},
            {"X-STRATUS-API-KEY": api_key, "Accept": "text/csv"},
            # Include Origin/Referer for stricter CORS policies
            {"x-api-key": api_key, "Accept": "text/csv",
             "Origin": "https://www.gtpstratus.com",
             "Referer": "https://www.gtpstratus.com/"},
        ])
    # If a session cookie is provided, add it as a last resort header set
    if session_cookie:
        header_sets.append({
            "Accept": "text/csv",
            "Cookie": f"gtp.auth={session_cookie}",
            "Origin": "https://www.gtpstratus.com",
            "Referer": "https://www.gtpstratus.com/"
        })

    # Build candidate endpoint URLs. We try a variety of patterns because different tenants
    # expose CSV exports under slightly different paths.
    candidates = []
    # Dashboard export (project/model) endpoints
    if company_id:
        candidates.extend([
            # Specific project/model
            f"{base_url}/v1/company/{company_id}/project/{project_id}/model/{model_id}/orders/output?format=csv",
            f"{base_url}/v1/orders/output?companyId={company_id}&projectId={project_id}&modelId={model_id}&format=csv",
            # Company-level export without project/model params
            f"{base_url}/v1/company/{company_id}/orders/output?format=csv",
            f"{base_url}/v1/orders/output?companyId={company_id}&format=csv",
        ])
    else:
        # If no company_id is provided, still try generic orders endpoint
        candidates.extend([
            f"{base_url}/v1/orders/output?format=csv"
        ])
    # Report-based exports
    if report_id:
        if company_id:
            candidates.append(f"{base_url}/v1/company/{company_id}/report/{report_id}/output?format=csv")
        candidates.append(f"{base_url}/v1/report/{report_id}/output?format=csv")

    logger.debug("Built %d candidate URLs and %d header sets", len(candidates), len(header_sets))

    last_error = None
    # Attempt each URL with each header permutation
    for url in candidates:
        for headers in header_sets:
            try:
                logger.debug("Trying: %s with headers: %s", url, list(headers.keys()))
                resp = requests.get(url, headers=headers, timeout=60)
                logger.debug("Response: status=%s, content-type=%s, body_preview=%s", resp.status_code, resp.headers.get("content-type", ""), resp.text[:200])
                # Accept 200 responses with content as success
                if resp.ok and resp.content:
                    logger.info("Successfully fetched STRATUS CSV from %s (%d bytes)", url, len(resp.content))
                    return resp.content
                last_error = f"{resp.status_code} {resp.text[:300]}"
            except Exception as e:
                logger.error("Request to %s failed: %s", url, e, exc_info=True)
                last_error = str(e)
    # Fallback to a local CSV file if configured
    fallback = os.getenv("STRATUS_CSV_FALLBACK")
    if fallback and os.path.exists(fallback):
        logger.info("Using local CSV fallback: %s", fallback)
        with open(fallback, "rb") as f:
            return f.read()
    # If nothing worked, raise an error with the last encountered message
    logger.error("All STRATUS CSV fetch attempts failed. Last error: %s", last_error)
    raise RuntimeError(f"Failed to fetch STRATUS CSV. Last error: {last_error or 'no candidates tried'}")

def get_sheet_and_columns(smartsheet_client, sheet_id):
    logger.debug("Entering get_sheet_and_columns(sheet_id=%s)", sheet_id)
    sheet = smartsheet_client.Sheets.get_sheet(sheet_id)
    col_id_by_name = {c.title: c.id for c in sheet.columns}
    logger.debug("Sheet loaded: %d columns, %d rows", len(col_id_by_name), len(sheet.rows))
    return sheet, col_id_by_name

def dataframe_from_csv_bytes(csv_bytes):
    logger.debug("Entering dataframe_from_csv_bytes(csv_bytes length=%d)", len(csv_bytes))
    # Handle potential BOM and encoding issues robustly
    text = csv_bytes.decode("utf-8-sig", errors="replace")
    df = pd.read_csv(io.StringIO(text))
    # Normalize column names (strip)
    df.columns = [c.strip() for c in df.columns]
    logger.debug("DataFrame created: shape=%s, columns=%s", df.shape, list(df.columns))
    return df

def build_existing_row_map(sheet):
    """
    Returns dict: {STRATUS_PackageId: {"rowId": id, "cells": {colName: value}}}
    """
    logger.debug("Entering build_existing_row_map(sheet rows=%d)", len(sheet.rows))
    # Find the column index for STRATUS_PackageId
    id_col = None
    for c in sheet.columns:
        if c.title == "STRATUS_PackageId":
            id_col = c.id
            break
    if not id_col:
        logger.error("Smartsheet missing required 'STRATUS_PackageId' column")
        raise RuntimeError("Smartsheet must have a 'STRATUS_PackageId' column.")

    existing = {}
    for row in sheet.rows:
        key = None
        by_name = {}
        for cell in row.cells:
            # Build a by-name map for quick diffs later
            col_title = None
            for c in sheet.columns:
                if c.id == cell.column_id:
                    col_title = c.title
                    break
            val = cell.value if hasattr(cell, 'value') else cell.display_value
            by_name[col_title] = val
            if cell.column_id == id_col:
                key = val
        if key:
            existing[str(key)] = {"rowId": row.id, "cells": by_name}
    logger.debug("Built existing row map with %d entries", len(existing))
    return existing

def cells_for_row_update(col_id_by_name, data_by_colname):
    """
    data_by_colname: dict of {sheet_col_name: value}
    Returns list of smartsheet.models.Cell for an update.
    """
    logger.debug("Entering cells_for_row_update(columns=%s)", list(data_by_colname.keys()))
    cells = []
    for name, value in data_by_colname.items():
        if name not in col_id_by_name:
            continue
        cell = smartsheet.models.Cell()
        cell.column_id = col_id_by_name[name]
        cell.value = None if (value is None or (isinstance(value, float) and pd.isna(value))) else value
        cells.append(cell)
    return cells

def main():
    logger.debug("Entering main()")
    token = os.getenv("SMARTSHEET_ACCESS_TOKEN")
    if not token:
        logger.error("SMARTSHEET_ACCESS_TOKEN environment variable is not set.")
        sys.exit(2)

    smartsheet_client = smartsheet.Smartsheet(token)
    smartsheet_client.errors_as_exceptions(True)

    logger.info("[1/5] Fetching STRATUS CSV...")
    csv_bytes = fetch_stratus_csv()
    stratus_df = dataframe_from_csv_bytes(csv_bytes)

    # Ensure required columns exist
    missing = [k for k in STRATUS_TO_SHEET.keys() if k not in stratus_df.columns]
    if missing:
        logger.warning("STRATUS CSV missing expected columns: %s", missing)

    # Keep only columns we care about
    subset_cols = [c for c in STRATUS_TO_SHEET.keys() if c in stratus_df.columns]
    work_df = stratus_df[subset_cols].copy()

    logger.debug("Working DataFrame shape: %s, columns: %s", work_df.shape, list(work_df.columns))

    # Convert %Complete like "10.0 %" to numeric if needed
    if "%Complete" in work_df.columns:
        def clean_pct(v):
            if pd.isna(v):
                return None
            if isinstance(v, (int, float)):
                return v
            s = str(v).strip().replace("%","").replace(" %","")
            try:
                return float(s)
            except:
                return s
        work_df["%Complete"] = work_df["%Complete"].map(clean_pct)

    # Prepare mapping to sheet column names
    work_df.rename(columns=STRATUS_TO_SHEET, inplace=True)

    # Add audit timestamp
    now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    work_df["Last Sync From STRATUS (UTC)"] = now_utc

    logger.info("[2/5] Loading Smartsheet and columns...")
    sheet, col_id_by_name = get_sheet_and_columns(smartsheet_client, SHEET_ID)

    logger.info("[3/5] Building existing row map...")
    existing = build_existing_row_map(sheet)

    # Validate required columns exist in Smartsheet
    for col in TARGET_COLUMNS:
        if col not in col_id_by_name:
            logger.warning("Column '%s' not found in Smartsheet; it will be skipped.", col)

    # Prepare adds/updates
    adds = []
    updates = []

    # We only write the columns that exist in Smartsheet
    writable_cols = [c for c in work_df.columns if c in col_id_by_name]

    for _, r in work_df.iterrows():
        key = str(r.get("STRATUS_PackageId", "")).strip()
        if not key:
            continue
        data = {col: (None if (isinstance(r.get(col), float) and pd.isna(r.get(col))) else r.get(col)) for col in writable_cols}

        if key in existing:
            # Diff against current cells by name and only send changed values
            current = existing[key]["cells"]
            changed = {}
            for col in writable_cols:
                new_val = data.get(col)
                cur_val = current.get(col)
                if isinstance(new_val, float) and pd.isna(new_val):
                    new_val = None
                # Normalize %Complete comparison (Smartsheet may store as number)
                if col == "%Complete":
                    try:
                        cur_val = float(cur_val) if cur_val is not None else None
                    except:
                        pass
                if new_val != cur_val:
                    changed[col] = new_val
            if changed:
                row = smartsheet.models.Row()
                row.id = existing[key]["rowId"]
                row.cells = cells_for_row_update(col_id_by_name, changed)
                updates.append(row)
        else:
            # New row
            row = smartsheet.models.Row()
            row.to_top = True
            row.cells = cells_for_row_update(col_id_by_name, data)
            adds.append(row)

    logger.info("[4/5] Preparing to write: %d adds, %d updates", len(adds), len(updates))

    # Batch write in chunks (Smartsheet limits)
    def chunks(lst, n):
        for i in range(0, len(lst), n):
            yield lst[i:i+n]

    # Adds
    added = 0
    for batch in chunks(adds, 400):  # safe batch size
        logger.debug("Adding batch of %d rows to Smartsheet", len(batch))
        resp = smartsheet_client.Sheets.add_rows(SHEET_ID, batch)
        added += len(resp.result)
        logger.debug("Batch add complete, %d rows added so far", added)
    # Updates
    updated = 0
    for batch in chunks(updates, 400):
        logger.debug("Updating batch of %d rows in Smartsheet", len(batch))
        resp = smartsheet_client.Sheets.update_rows(SHEET_ID, batch)
        updated += len(resp.result)
        logger.debug("Batch update complete, %d rows updated so far", updated)

    logger.info("[5/5] Done. Added %d, Updated %d. Timestamp: %s", added, updated, now_utc)

if __name__ == "__main__":
    main()
