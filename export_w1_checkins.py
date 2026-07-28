#!/usr/bin/env python3
"""
Everyone who checked in (wristband activation) during Week 1:
activated_at between Mon 20 Jul 00:00 and Mon 27 Jul 06:00 Tallinn
(= 19 Jul 21:00 UTC → 27 Jul 03:00 UTC; sheet timestamps are UTC).

Output: w1_checkins.csv (First name, Last name, Email Address) as a
private artifact. Activations whose registration no longer exists in
Bizzabo (swapped/cancelled after activating) are kept, using the app's
name and an empty email, so the total matches reality at the door.
"""

import csv, io, os
from datetime import datetime
import requests

CLIENT_ID     = os.environ["BIZZABO_CLIENT_ID"]
CLIENT_SECRET = os.environ["BIZZABO_CLIENT_SECRET"]
ACCOUNT_ID    = os.environ.get("BIZZABO_ACCOUNT_ID", "129966")
EVENT_ID      = os.environ.get("BIZZABO_EVENT_ID",   "754649")
SHEET_ID      = "1H5A4kSVUCbvgcHQSLqyOLwhNbnSZAYek8D2crd9t0fo"

WINDOW_START_UTC = datetime(2026, 7, 19, 21, 0, 0)   # Mon 20 Jul 00:00 Tallinn
WINDOW_END_UTC   = datetime(2026, 7, 27,  3, 0, 0)   # Mon 27 Jul 06:00 Tallinn


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


def parse_ts(s):
    for fmt in ("%m/%d/%Y %H:%M:%S", "%m/%d/%Y %H:%M"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


def main():
    r = requests.get(f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/export?format=csv", timeout=30)
    r.raise_for_status()
    in_window = {}
    for row in csv.DictReader(io.StringIO(r.text)):
        ts = parse_ts((row.get("activated_at") or "").strip())
        if ts and WINDOW_START_UTC <= ts < WINDOW_END_UTC:
            in_window[(row.get("ticketId") or "").strip()] = (row.get("attendee_name") or "").strip()
    print(f"Activations in W1 window: {len(in_window)}")

    token = get_token()
    regs = {str(x.get("id")): x for x in fetch_all(token)}
    out, orphans = [], 0
    for tid, app_name in in_window.items():
        reg = regs.get(tid)
        first = last = email = ""
        if reg:
            props = reg.get("properties") or {}
            first = (props.get("firstName") or "").strip()
            last  = (props.get("lastName") or "").strip()
            email = (props.get("email") or "").strip()
            if not (first or last or email):
                bill = reg.get("billingAddress") or {}
                first = (bill.get("firstName") or "").strip()
                last  = (bill.get("lastName") or "").strip()
                email = (bill.get("email") or "").strip()
        if not (first or last):
            parts = app_name.split(None, 1)
            first, last = (parts + ["", ""])[:2]
            orphans += 1
        out.append({"First name": first, "Last name": last, "Email Address": email})

    out.sort(key=lambda x: (x["Last name"].lower(), x["First name"].lower()))
    with open("w1_checkins.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["First name", "Last name", "Email Address"])
        w.writeheader()
        w.writerows(out)
    print(f"Rows written: {len(out)} · fallback-name rows (no Bizzabo match): {orphans}")


if __name__ == "__main__":
    main()
