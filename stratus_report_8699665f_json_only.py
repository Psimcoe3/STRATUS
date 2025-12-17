
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

# Optional: load .env if available
try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

REPORT_ID = "8699665f-d63e-47ab-8c2f-fa05e1547554"
OUTPUT_FILE = f"report_{REPORT_ID}.json"

def main():
    base_url = os.getenv("STRATUS_BASE_URL", "https://api.gtpstratus.com").rstrip("/")
    company_id = os.getenv("STRATUS_COMPANY_ID")
    hdr_name = os.getenv("STRATUS_AUTH_HEADER_NAME", "app-key")
    hdr_value = os.getenv("STRATUS_AUTH_HEADER_VALUE")

    if not company_id or not hdr_value:
        print("ERROR: STRATUS_COMPANY_ID and STRATUS_AUTH_HEADER_VALUE must be set (e.g., via .env).", file=sys.stderr)
        sys.exit(2)

    url = f"{base_url}/v1/package/dashboard"
    headers = {hdr_name: hdr_value, "Accept": "application/json"}
    params = {
        "companyId": company_id,
        "reportId": REPORT_ID,
        "projectId": 0,
        "modelId": 0,
    }

    try:
        r = requests.get(url, headers=headers, params=params, timeout=300)
    except requests.RequestException as e:
        print(f"ERROR: request failed: {e}", file=sys.stderr)
        sys.exit(2)

    if r.status_code != 200:
        print(f"ERROR: HTTP {r.status_code} from {url}\nPreview: {(r.text or '')[:400]}", file=sys.stderr)
        sys.exit(2)

    try:
        data = r.json()
    except ValueError as e:
        print(f"ERROR: Non-JSON response: {e}", file=sys.stderr)
        sys.exit(2)

    Path(OUTPUT_FILE).write_text(json.dumps(data, indent=2), encoding="utf-8")
    print(f"OK: wrote {OUTPUT_FILE} ({len(json.dumps(data))} bytes)")

if __name__ == "__main__":
    main()
