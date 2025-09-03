#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Fetch completed stories from Mendix Epics, price each at a fixed rate per story point,
and build an email body that matches the requested PDF-like list formatting.

Requires:
  - requests (HTTP)
  - msal (only if SEND_VIA_GRAPH=true to send mail via Microsoft Graph)

Config via environment variables:
  MENDIX_PAT, MENDIX_APP_ID, EPICS_API_BASE (default: https://epics.api.mendix.com),
  PRICE_PER_POINT (default 55.00), CURRENCY_SYMBOL (default $),
  EMAIL_TO, EMAIL_FROM, SEND_VIA_GRAPH (true/false),
  TENANT_ID, CLIENT_ID, CLIENT_SECRET (if sending via Graph)

Epics API notes:
  - Auth: Authorization: MxToken <PAT>
  - Endpoints used:
      GET /projects/{appId}/statuses
      GET /projects/{appId}/stories
  - API details & scopes: https://docs.mendix.com/apidocs-mxsdk/apidocs/epics-api/
  - Swagger UI cannot be used to call endpoints due to CORS; use Postman/script instead.
"""

import os
import sys
import math
import json
import time
import html
from typing import Dict, List, Any
import requests

# Add dotenv support
from dotenv import load_dotenv
load_dotenv()

# --- Configuration ---
MENDIX_PAT = os.getenv("MENDIX_PAT", "").strip()
APP_ID = os.getenv("MENDIX_APP_ID", "").strip()

# Base endpoint: set this to the server shown in the Epics API docs/OAS.
# Example placeholder; override with EPICS_API_BASE if your tenant/doc shows a different host.
EPICS_API_BASE = os.getenv("EPICS_API_BASE", "https://epics.api.mendix.com/v1").rstrip("/")

PRICE_PER_POINT = float(os.getenv("PRICE_PER_POINT", "55.00"))
CURRENCY_SYMBOL = os.getenv("CURRENCY_SYMBOL", "$")

# Completed statuses (names). You can override with a comma-separated list in env:
# e.g., COMPLETED_STATUS_NAMES="Done,Completed,Accepted"
COMPLETED_STATUS_NAMES = [s.strip() for s in os.getenv(
    "COMPLETED_STATUS_NAMES",
    "Done,Completed,Accepted,Closed Resolved,Closed,Resolved"
).replace("  ", " ").split(",") if s.strip()]

# Email settings
EMAIL_TO = os.getenv("EMAIL_TO", "").strip()
EMAIL_FROM = os.getenv("EMAIL_FROM", "").strip()
SEND_VIA_GRAPH = os.getenv("SEND_VIA_GRAPH", "false").strip().lower() == "true"

# Graph config (only used if SEND_VIA_GRAPH=true)
TENANT_ID = os.getenv("TENANT_ID", "").strip()
CLIENT_ID = os.getenv("CLIENT_ID", "").strip()
CLIENT_SECRET = os.getenv("CLIENT_SECRET", "").strip()

# --- Helpers ---

def brl_like_currency(amount: float, symbol: str = "$") -> str:
    """
    Format like the example: $110,00 (comma decimal). We keep the $ symbol and use comma decimals.
    """
    # Format with two decimals using dot, then swap dot->comma
    s = f"{amount:,.2f}"
    # Replace thousands separator ',' with temporary, then '.' to ',', then temp to '.'
    # This keeps thousands grouping but flips decimal char.
    s = s.replace(",", "_").replace(".", ",").replace("_", ".")
    return f"{symbol}{s}"

def auth_headers() -> Dict[str, str]:
    if not MENDIX_PAT:
        raise RuntimeError("Missing MENDIX_PAT.")
    return {
        "Authorization": f"MxToken {MENDIX_PAT}",
        "Accept": "application/json",
    }

def epics_get(path: str, params: Dict[str, Any] = None) -> Dict[str, Any]:
    url = f"{EPICS_API_BASE}{path}"
    r = requests.get(url, headers=auth_headers(), params=params, timeout=60)
    r.raise_for_status()
    return r.json()

def fetch_statuses() -> Dict[str, Dict[str, Any]]:
    """
    Returns a dict keyed by statusId with the status object.
    Endpoint: GET /projects/{appId}/statuses
    """
    data = epics_get(f"/projects/{APP_ID}/statuses")
    statuses = {}
    # Response shape may be { "statuses": [ ... ] } OR direct array; handle both
    items = data.get("statuses") if isinstance(data, dict) else data
    if items is None:
        items = []
    for st in items:
        sid = str(st.get("id") or st.get("statusId") or st.get("uuid") or st.get("key") or "")
        if sid:
            statuses[sid] = st
    return statuses

def is_completed_status(status_obj: Dict[str, Any]) -> bool:
    """
    Check by name against COMPLETED_STATUS_NAMES.
    Also attempt category flags if available (e.g., category == 'DONE').
    """
    name = (status_obj.get("name") or status_obj.get("displayName") or "").strip()
    if name:
        for target in COMPLETED_STATUS_NAMES:
            if name.lower() == target.lower():
                return True
    # Try category if present
    cat = (status_obj.get("category") or "").strip().lower()
    if cat in {"Done"}:
        return True
    return False

def extract_points(story: Dict[str, Any]) -> float:
    """
    Extract story points using OpenAPI spec key.
    """
    val = story.get("storyPoints")
    if isinstance(val, (int, float)):
        return float(val)
    return 0.0

def extract_title(story: Dict[str, Any]) -> str:
    """
    Extract story title using OpenAPI spec key.
    """
    return str(story.get("title", "")).strip()

def extract_description(story: Dict[str, Any]) -> str:
    """
    Extract story description using OpenAPI spec key.
    """
    return str(story.get("descriptionPlain", "")).strip()

def extract_status(story: Dict[str, Any]) -> str:
    """
    Extract story status using OpenAPI spec key.
    """
    return str(story.get("status", "")).strip()

def iterate_all_stories() -> List[Dict[str, Any]]:
    """
    Pull stories with naive pagination.
    """
    all_items: List[Dict[str, Any]] = []
    params = {"limit": 100, "offset": 0}
    while True:
        data = epics_get(f"/projects/{APP_ID}/stories", params=params)
        # Mendix Epics API always returns a dict with 'stories' key
        items = data.get("stories", [])
        all_items.extend(items)

        # Pagination: look for 'links' array with rel: next
        next_offset = None
        links = data.get("links", [])
        for link in links:
            if link.get("rel") == "next" and link.get("hRef"):
                # Extract offset from hRef if present
                import re
                m = re.search(r"offset=(\d+)", link["hRef"])
                if m:
                    next_offset = int(m.group(1))
                break

        if next_offset is not None and next_offset != params["offset"]:
            params["offset"] = next_offset
        else:
            break

    return all_items

def build_email_lines(completed_stories: List[Dict[str, Any]]) -> (str, str, float):
    """
    Build plain-text and HTML email bodies and return (text, html, total_amount)
    """
    lines_txt: List[str] = []
    lines_html: List[str] = []

    total = 0.0
    for st in completed_stories:
        title = extract_title(st)
        desc = extract_description(st)
        pts = extract_points(st)
        price = pts * PRICE_PER_POINT
        total += price

        price_str = brl_like_currency(price, CURRENCY_SYMBOL)
        lines_txt.append(f"- {title}\n  {desc}\n  {pts} story point(s) - {price_str}")
        lines_html.append(
            f"<li><span>{html.escape(title)}</span><br>"
            f"<em>{html.escape(desc)}</em><br>"
            f"<strong>{pts} story point(s) - {html.escape(price_str)}</strong></li>"
        )

    total_str = brl_like_currency(total, CURRENCY_SYMBOL)

    text_body = "\n".join(lines_txt + [f"\nTotal - {total_str}"])
    html_body = f"""
    <div>
      <ul>
        {''.join(lines_html)}
      </ul>
      <p><strong>Total - {html.escape(total_str)}</strong></p>
    </div>
    """.strip()

    return text_body, html_body, total

def send_via_graph(subject: str, body_html: str, body_text: str):
    """
    Send email with Microsoft Graph (client credentials).
    Requires: TENANT_ID, CLIENT_ID, CLIENT_SECRET, EMAIL_FROM, EMAIL_TO
    """
    import msal
    if not all([TENANT_ID, CLIENT_ID, CLIENT_SECRET, EMAIL_FROM, EMAIL_TO]):
        raise RuntimeError("Graph send requires TENANT_ID, CLIENT_ID, CLIENT_SECRET, EMAIL_FROM, EMAIL_TO.")

    authority = f"https://login.microsoftonline.com/{TENANT_ID}"
    scope = ["https://graph.microsoft.com/.default"]
    app = msal.ConfidentialClientApplication(CLIENT_ID, authority=authority, client_credential=CLIENT_SECRET)
    result = app.acquire_token_for_client(scopes=scope)
    if "access_token" not in result:
        raise RuntimeError(f"Failed to get Graph token: {result}")

    msg = {
        "message": {
            "subject": subject,
            "body": {
                "contentType": "HTML",
                "content": body_html
            },
            "toRecipients": [{"emailAddress": {"address": EMAIL_TO}}],
            "from": {"emailAddress": {"address": EMAIL_FROM}}
        },
        "saveToSentItems": "true"
    }

    # Use the /sendMail action on the sender mailbox
    url = f"https://graph.microsoft.com/v1.0/users/{EMAIL_FROM}/sendMail"
    r = requests.post(url, headers={"Authorization": f"Bearer {result['access_token']}",
                                    "Content-Type": "application/json"}, data=json.dumps(msg), timeout=60)
    if r.status_code not in (200, 202):
        raise RuntimeError(f"Graph send failed: {r.status_code} {r.text}")

def main():
    if not APP_ID:
        raise RuntimeError("Missing MENDIX_APP_ID (your Mendix app/project ID).")

    print("Fetching stories...")
    stories = iterate_all_stories()
    print(f"Total stories fetched: {len(stories)}")

    completed_stories = []
    for st in stories:
        status = extract_status(st)
        if status.lower() == "done":
            completed_stories.append(st)

    print(f"Completed stories found: {len(completed_stories)}")

    text_body, html_body, total = build_email_lines(completed_stories)
    subject = "Completed Stories — Billing Summary"

    # Print to console (always)
    print("\n=== EMAIL (Plain Text) ===")
    print(text_body)
    print("\n=== EMAIL (HTML) ===")
    print(html_body)

    if SEND_VIA_GRAPH:
        print("Sending email via Microsoft Graph...")
        send_via_graph(subject, html_body, text_body)
        print("Email sent.")
    else:
        print("\nEmail NOT sent (SEND_VIA_GRAPH=false). Copy the body above into your email client or enable Graph sending.")

if __name__ == "__main__":
    try:
        main()
    except Exception as ex:
        print(f"ERROR: {ex}", file=sys.stderr)
        sys.exit(1)