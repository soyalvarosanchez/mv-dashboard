#!/usr/bin/env python3
"""
Every Bizzabo registration that is NO LONGER valid — the list the
gatekeeper app must purge (its import only ever adds tickets, so
swapped/cancelled/refunded tickets stay usable at the door).

Cross-referenced against the app's activations sheet: rows flagged
ALREADY_ACTIVATED_IN_APP are dead tickets that have actually been used
to activate a wristband (like the confirmed Keven Thibeault case).

Output: invalid_ticket_ids.csv (private artifact).
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
    app_rows = {}
    for row in csv.DictReader(io.StringIO(r.text)):
        tid = (row.get("ticketId") or "").strip()
        if tid:
            app_rows[tid] = row
    app_activated = {t for t, row in app_rows.items()
                     if (row.get("status") or "").strip().lower() == "activated"}

    token = get_token()
    regs = fetch_all(token)
    invalid = [x for x in regs if (x.get("validity") or "").lower() != "valid"]
    print(f"Total regs: {len(regs)} · invalid: {len(invalid)}")

    out = []
    for x in invalid:
        props = x.get("properties") or {}
        rid = str(x.get("id"))
        name = f"{(props.get('firstName') or '').strip()} {(props.get('lastName') or '').strip()}".strip()
        out.append({
            "ticketId": rid,
            "name": name,
            "ticket": (x.get("ticketName") or "").strip(),
            "paymentStatus": (x.get("paymentStatus") or "").strip(),
            "flag": "ALREADY_ACTIVATED_IN_APP" if rid in app_activated else "",
        })
    # Tickets the app knows that don't exist in Bizzabo AT ALL any more
    # (e.g. upgraded-away originals like the Keven Thibeault case) — these
    # are invisible to the invalid list above but equally usable at the door.
    all_ids = {str(x.get("id")) for x in regs}
    for tid, row in app_rows.items():
        if tid not in all_ids:
            out.append({
                "ticketId": tid,
                "name": (row.get("attendee_name") or "").strip(),
                "ticket": (row.get("type") or "").strip(),
                "paymentStatus": "",
                "flag": ("ORPHAN_NOT_IN_BIZZABO" +
                         ("+ACTIVATED" if tid in app_activated else "")),
            })
    out.sort(key=lambda x: (x["flag"] == "", x["name"].lower()))
    with open("invalid_ticket_ids.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["ticketId", "name", "ticket", "paymentStatus", "flag"])
        w.writeheader()
        w.writerows(out)
    used = sum(1 for o in out if o["flag"])
    print(f"Invalid ids written: {len(out)} · already ACTIVATED in the app: {used}")


if __name__ == "__main__":
    main()
