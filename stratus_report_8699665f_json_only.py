
#!/usr/bin/env python3
# STRATUS JSON-only fetcher for a single hardcoded reportId via the Package Dashboard endpoint.
#
# - Hardcoded reportId: 8699665f-d63e-47ab-8c2f-fa05e1547554
# - Only writes raw JSON to ./report_8699665f-d63e-47ab-8c2f-fa05e1547554.json
# - Reads STRATUS creds from environment (.env supported but optional):
#     STRATUS_AUTH_HEADER_NAME (default: app-key)
#     STRATUS_AUTH_HEADER_VALUE (required)
#     STRATUS_COMPANY_ID (required)
#     STRATUS_BASE_URL (default: https://api.gtpstratus.com)

import os, sys, json
from pathlib import Path

import requests

from stratus_logger import get_logger
logger = get_logger(__name__)

# Optional: load .env if available
try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

REPORT_ID = "8699665f-d63e-47ab-8c2f-fa05e1547554"
OUTPUT_FILE = f"report_{REPORT_ID}.json"

def main():
    logger.debug("Entering main()")
    base_url = os.getenv("STRATUS_BASE_URL", "https://api.gtpstratus.com").rstrip("/")
    company_id = os.getenv("STRATUS_COMPANY_ID")
    hdr_name = os.getenv("STRATUS_AUTH_HEADER_NAME", "app-key")
    hdr_value = os.getenv("STRATUS_AUTH_HEADER_VALUE")
    logger.debug("Configuration: base_url=%s, company_id=%s, header_name=%s", base_url, company_id, hdr_name)

    if not company_id or not hdr_value:
        logger.error("Required environment variables STRATUS_COMPANY_ID and STRATUS_AUTH_HEADER_VALUE must be set (e.g., via .env).")
        sys.exit(2)

    url = f"{base_url}/v1/package/dashboard"
    headers = {hdr_name: hdr_value, "Accept": "application/json"}
    params = {
        "companyId": company_id,
        "reportId": REPORT_ID,
        "projectId": 0,
        "modelId": 0,
    }

    logger.debug("API call: GET %s params=%s", url, params)
    try:
        r = requests.get(url, headers=headers, params=params, timeout=300)
    except requests.RequestException as e:
        logger.error("Request failed: %s", e, exc_info=True)
        sys.exit(2)

    logger.debug("Response: status_code=%d, content_type=%s", r.status_code, r.headers.get("Content-Type"))
    if r.status_code != 200:
        logger.error("HTTP %d from %s\nPreview: %s", r.status_code, url, (r.text or "")[:400])
        sys.exit(2)

    try:
        data = r.json()
    except ValueError as e:
        logger.error("Non-JSON response: %s", e, exc_info=True)
        sys.exit(2)

    json_text = json.dumps(data, indent=2)
    Path(OUTPUT_FILE).write_text(json_text, encoding="utf-8")
    logger.debug("Wrote output file: path=%s, size=%d bytes", OUTPUT_FILE, len(json_text))
    logger.info("OK: wrote %s (%d bytes)", OUTPUT_FILE, len(json_text))

if __name__ == "__main__":
    main()
