#!/usr/bin/env python3
"""
VIP ticket holders who had NOT activated their wristband before the
cutoff (Mon 27 Jul 2026, 06:00 Tallinn = 03:00 UTC).

'VIP' means the dashboard's VIP category: every VIP* sales ticket
(incl. VIP Comped / MV Family / VIP | Adult 3 days), the VIP Guest and
Special Guest ticket types, and the vipguest/vipmedia promo riders.
Speakers/Hexagon/Non-Hex Friends (VVIPs) are NOT included.

Someone counts as 'activated before cutoff' if their row in the app's
activations sheet carries an activated_at earlier than the cutoff —
even if later deactivated. Everyone else (never activated, or activated
after the cutoff) lands in the CSV.

Output: vip_not_activated.csv (First Name, Last Name, Email address,
Ticket type) uploaded as a private artifact.
"""

import csv, io, os
from datetime import datetime
import requests

CLIENT_ID     = os.environ["BIZZABO_CLIENT_ID"]
CLIENT_SECRET = os.environ["BIZZABO_CLIENT_SECRET"]
ACCOUNT_ID    = os.environ.get("BIZZABO_ACCOUNT_ID", "129966")
EVENT_ID      = os.environ.get("BIZZABO_EVENT_ID",   "754649")
SHEET_ID      = "1H5A4kSVUCbvgcHQSLqyOLwhNbnSZAYek8D2crd9t0fo"

CUTOFF_UTC = datetime(2026, 7, 27, 3, 0, 0)   # 06:00 Tallinn (EEST)


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


def is_vip(r):
    t = (r.get("ticketName") or "").lower()
    p = (r.get("promoCode") or "").strip().lower()
    if "kid" in t or "teen" in t:
        return False
    if "speaker" in t or "hexagon" in t or "friends of vishen" in t or "crew" in t:
        return False
    if "special guest" in t or "vip guest" in t or "vip media" in t or "vip" in t:
        return True
    return p in ("vipguest", "vipmedia")


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
    activated_before = set()
    for row in csv.DictReader(io.StringIO(r.text)):
        ts = parse_ts((row.get("activated_at") or "").strip())
        if ts and ts < CUTOFF_UTC:
            activated_before.add((row.get("ticketId") or "").strip())
    print(f"Activated before cutoff (any ticket): {len(activated_before)}")

    token = get_token()
    valid = [x for x in fetch_all(token) if (x.get("validity") or "").lower() == "valid"]
    vips = [x for x in valid if is_vip(x)]
    print(f"Valid regs: {len(valid)} · VIP category: {len(vips)}")

    out = []
    for x in vips:
        if str(x.get("id")) in activated_before:
            continue
        props = x.get("properties") or {}
        out.append({
            "First Name": (props.get("firstName") or "").strip(),
            "Last Name":  (props.get("lastName") or "").strip(),
            "Email address": (props.get("email") or "").strip(),
            "Ticket type": (x.get("ticketName") or "").strip(),
        })
    out.sort(key=lambda x: (x["Last Name"].lower(), x["First Name"].lower()))
    with open("vip_not_activated.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["First Name", "Last Name", "Email address", "Ticket type"])
        w.writeheader()
        w.writerows(out)
    print(f"VIPs NOT activated before Mon 27 Jul 06:00 Tallinn: {len(out)}")


if __name__ == "__main__":
    main()
