#!/usr/bin/env python3
"""
Export attendee emails for every activated wristband.

Joins the check-in app's activations sheet (ticketId == Bizzabo
registration id) with Bizzabo registrations and writes
activated_emails.csv, which the workflow uploads as a PRIVATE artifact —
personal data never goes into logs, commits or GitHub Pages. Orphan
activation ids (no valid Bizzabo registration — swapped/cancelled after
the app synced) are listed with an empty email so nobody vanishes
silently.
"""

import csv, io, os
import requests

CLIENT_ID     = os.environ["BIZZABO_CLIENT_ID"]
CLIENT_SECRET = os.environ["BIZZABO_CLIENT_SECRET"]
ACCOUNT_ID    = os.environ.get("BIZZABO_ACCOUNT_ID", "129966")
EVENT_ID      = os.environ.get("BIZZABO_EVENT_ID",   "754649")
SHEET_ID      = "1H5A4kSVUCbvgcHQSLqyOLwhNbnSZAYek8D2crd9t0fo"


def get_token():
    r = requests.post(
        "https://api.bizzabo.com/api/v2/iam/oauth/token",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        data={"grant_type": "client_credentials", "client_id": CLIENT_ID,
              "client_secret": CLIENT_SECRET, "account_id": ACCOUNT_ID,
              "audience": "https://api.bizzabo.com/api"},
        timeout=30,
    )
    r.raise_for_status()
    return r.json()["access_token"]


def fetch_all(token):
    regs, page = [], 0
    while True:
        r = requests.get(
            f"https://api.bizzabo.com/v2/events/{EVENT_ID}/registrations",
            headers={"Authorization": f"Bearer {token}"},
            params={"size": 100, "page": page}, timeout=30)
        r.raise_for_status()
        content = r.json().get("content", [])
        regs.extend(content)
        if not content or len(content) < 100:
            break
        page += 1
    return regs


def main():
    r = requests.get(f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/export?format=csv", timeout=30)
    r.raise_for_status()
    activated = {}
    for row in csv.DictReader(io.StringIO(r.text)):
        if (row.get("status") or "").strip().lower() == "activated":
            activated[(row.get("ticketId") or "").strip()] = {
                "app_name": (row.get("attendee_name") or "").strip(),
                "activated_at_utc": (row.get("activated_at") or "").strip(),
                "type": (row.get("type") or "").strip(),
            }
    print(f"Activated rows in app sheet: {len(activated)}")

    token = get_token()
    regs = {str(x.get("id")): x for x in fetch_all(token)
            if (x.get("validity") or "").lower() == "valid"}
    print(f"Valid Bizzabo registrations: {len(regs)}")

    out_rows, orphans = [], 0
    emails = set()
    for tid, a in activated.items():
        reg = regs.get(tid)
        if reg:
            props = reg.get("properties") or {}
            email = (props.get("email") or "").strip()
            first = (props.get("firstName") or "").strip()
            last  = (props.get("lastName") or "").strip()
            if not first and not last:
                # fall back to the app's single-field name: first word / rest
                parts = a["app_name"].split(None, 1)
                first, last = (parts + ["", ""])[:2]
            if email:
                emails.add(email.lower())
            out_rows.append({"ticketId": tid, "first_name": first, "last_name": last,
                             "email": email,
                             "ticket": reg.get("ticketName") or a["type"],
                             "activated_at_utc": a["activated_at_utc"]})
        else:
            orphans += 1
            parts = a["app_name"].split(None, 1)
            first, last = (parts + ["", ""])[:2]
            out_rows.append({"ticketId": tid, "first_name": first, "last_name": last,
                             "email": "",
                             "ticket": a["type"] + "  [NO VALID BIZZABO REG — swapped/cancelled?]",
                             "activated_at_utc": a["activated_at_utc"]})

    out_rows.sort(key=lambda x: (x["last_name"].lower(), x["first_name"].lower()))
    with open("activated_emails.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["ticketId", "first_name", "last_name", "email", "ticket", "activated_at_utc"])
        w.writeheader()
        w.writerows(out_rows)

    print(f"Rows written: {len(out_rows)} · unique emails: {len(emails)} · orphans (no email): {orphans}")


if __name__ == "__main__":
    main()
