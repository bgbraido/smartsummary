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

# --- Configuration ---
MENDIX_PAT = os.getenv("MENDIX_PAT", "").strip()
APP_ID = os.getenv("MENDIX_APP_ID", "").strip()

# Base endpoint: set this to the server shown in the Epics API docs/OAS.
# Example placeholder; override with EPICS_API_BASE if your tenant/doc shows a different host.
EPICS_API_BASE = os.getenv("EPICS_API_BASE", "https://epics.api.mendix.com").rstrip("/")

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
    if cat in {"done", "completed", "closed", "resolved"}:
        return True
    return False

def extract_points(story: Dict[str, Any]) -> float:
    """
    Stories may expose points under different keys. Try a few.
    """
    for key in ("points", "storyPoints", "story_points", "estimate", "estimationPoints"):
        val = story.get(key)
        if isinstance(val, (int, float)):
            return float(val)
        # Sometimes nested: { "estimate": { "points": 3 } }
        if isinstance(val, dict):
            for k2 in ("points", "value", "amount"):
                if isinstance(val.get(k2), (int, float)):
                    return float(val[k2])
    return 0.0

def extract_status_id(story: Dict[str, Any]) -> str:
    for key in ("statusId", "status_id", "status", "statusUUID"):
        val = story.get(key)
        if isinstance(val, str):
            return val
        if isinstance(val, dict):
            # e.g., "status": { "id": "...", "name": "Done" }
            for k2 in ("id", "statusId", "uuid", "key"):
                if isinstance(val.get(k2), str):
                    return val[k2]
    return ""

def extract_title(story: Dict[str, Any]) -> str:
    for key in ("title", "name", "summary"):
        if story.get(key):
            return str(story[key]).strip()
    # fallback
    return f"Story {story.get('id') or story.get('storyId') or story.get('uuid') or ''}".strip()

def iterate_all_stories() -> List[Dict[str, Any]]:
    """
    Pull stories with naive pagination.
    The Epics API may return HAL/links; follow `links.next` if present.
    Otherwise, use page/limit if supported (you can adapt as needed).
    """
    all_items: List[Dict[str, Any]] = []

    # First attempt: simple GET with a large page size; adjust if needed.
    params = {"limit": 200}
    data = epics_get(f"/projects/{APP_ID}/stories", params=params)

    def normalize_items(payload):
        # Response could be { "stories": [ ... ], "links": {...} } or direct array
        if isinstance(payload, dict):
            arr = payload.get("stories")
            if isinstance(arr, list):
                return arr
            # sometimes "items"
            arr2 = payload.get("items")
            if isinstance(arr2, list):
                return arr2
        elif isinstance(payload, list):
            return payload
        return []

    def get_next_link(payload) -> str:
        if isinstance(payload, dict):
            links = payload.get("links") or payload.get("_links") or {}
            for key in ("next", "Next", "NEXT"):
                nxt = links.get(key)
                if isinstance(nxt, dict) and nxt.get("href"):
                    href = nxt["href"]
                    # If relative, prefix base
                    if href.startswith("http"):
                        return href
                    return f"{EPICS_API_BASE}{href}"
                if isinstance(nxt, str):
                    return nxt
        return ""

    items = normalize_items(data)
    all_items.extend(items)

    next_url = get_next_link(data)
    while next_url:
        r = requests.get(next_url, headers=auth_headers(), timeout=60)
        r.raise_for_status()
        data = r.json()
        items = normalize_items(data)
        all_items.extend(items)
        next_url = get_next_link(data)

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
        pts = extract_points(st)
        price = pts * PRICE_PER_POINT
        total += price

        price_str = brl_like_currency(price, CURRENCY_SYMBOL)
        lines_txt.append(f"- {title} - {price_str}")
        lines_html.append(f"<li><span>{html.escape(title)}</span> - <strong>{html.escape(price_str)}</strong></li>")

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

    print("Fetching statuses...")
    statuses_by_id = fetch_statuses()
    # Also build a quick lookup by name for convenience
    status_name_map = { (v.get("name") or v.get("displayName") or "").strip().lower(): k
                        for k, v in statuses_by_id.items() if (v.get("name") or v.get("displayName")) }

    print("Determining which statuses are completed...")
    completed_ids = set()
    for sid, sobj in statuses_by_id.items():
        if is_completed_status(sobj):
            completed_ids.add(sid)

    if not completed_ids and COMPLETED_STATUS_NAMES:
        # Fallback: map names to IDs directly if possible
        for nm in COMPLETED_STATUS_NAMES:
            sid = status_name_map.get(nm.strip().lower())
            if sid:
                completed_ids.add(sid)

    if not completed_ids:
        print("WARNING: No completed statuses identified with current configuration.")
        print("Hint: set COMPLETED_STATUS_NAMES env var, e.g. 'Done,Completed,Accepted'")
        # proceed but expect zero matches

    print("Fetching stories...")
    stories = iterate_all_stories()
    print(f"Total stories fetched: {len(stories)}")

    completed_stories = []
    for st in stories:
        st_status_id = extract_status_id(st)
        st_status_obj = statuses_by_id.get(st_status_id, {})
        if (st_status_id and st_status_id in completed_ids) or is_completed_status(st_status_obj):
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