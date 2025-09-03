# Instructions to Run the Mendix Epics Completed Stories Script

This guide explains how to set up and run the Python script that fetches completed stories from Mendix Epics, calculates billing based on story points, and generates an email summary.

---

## ✅ 1. Create a `.env` File

Create a file named `.env` in the same directory as the script and add:


# ---- Mendix Epics API ----
MENDIX_PAT=your_mendix_pat_here
MENDIX_APP_ID=your-app-uuid-here
EPICS_API_BASE=https://epics.api.mendix.com

# Completed statuses (comma-separated)
COMPLETED_STATUS_NAMES=Done,Completed,Accepted,Closed,Resolved

# ---- Pricing ----
PRICE_PER_POINT=55.00
CURRENCY_SYMBOL=$

# ---- Email ----
EMAIL_TO=recipient@example.com
EMAIL_FROM=sender@example.com
SEND_VIA_GRAPH=false

# ---- Microsoft Graph (only if SEND_VIA_GRAPH=true) ----
TENANT_ID=your-tenant-id
CLIENT_ID=your-client-id
CLIENT_SECRET=your-client-secret

> **Important:** Add `.env` to `.gitignore` to keep secrets safe.

---

## ✅ 2. Install Dependencies

```bash
python -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install requests msal python-dotenv
```

---

## ✅ 3. Run the Script

```bash
python epics_completed_stories_email.py
```

- If `SEND_VIA_GRAPH=false`, the script **prints the email body** (Plain Text + HTML) so you can copy it into your email client.
- If `SEND_VIA_GRAPH=true`, it sends the email via Microsoft Graph using the credentials in `.env`.

---

## ✅ 4. Output Example

Plain text:

```
- Multi selection Client and State - $110,00
- SignType based on Ref Name - $55,00
- Only show PDF Button when there is a file associated. - $55,00
- New Layout for PDF print - $110,00

Total - $330,00
```

---

## ✅ 5. Optional Enhancements

- Filter by **date range** (e.g., completed this month).
- Group by **Epic** or **Assignee**.
- Generate a **PDF invoice** automatically.

---

### 🔗 References

- [Mendix Epics API Documentation](https://docs.mendix.com/apidocs-mxsdk/apidocs/epics-api/)
- Microsoft Graph API
