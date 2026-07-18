#!/usr/bin/env python3
"""
Bizzabo ticket cancellation — manual, one at a time, dry-run first.

Run via the 'Cancel Ticket' GitHub Action (workflow_dispatch). Flow:
  1. DRY_RUN=true (default): looks up the target's registrations by email
     (name fallback), prints every match with its IDs, cancels NOTHING.
  2. Re-run with DRY_RUN=false and TICKET_ID=<id from the dry run>:
     cancels exactly that one registration. Never cancels by name match
     alone — the explicit TICKET_ID is required.

refundAmount is hardcoded to 0: refunds are handled manually in Bizzabo,
never through this script.
"""

import os, sys, unicodedata
import requests

CLIENT_ID     = os.environ["BIZZABO_CLIENT_ID"]
CLIENT_SECRET = os.environ["BIZZABO_CLIENT_SECRET"]
ACCOUNT_ID    = os.environ.get("BIZZABO_ACCOUNT_ID", "129966")
EVENT_ID      = os.environ.get("BIZZABO_EVENT_ID",   "754649")

TARGET_EMAIL = (os.environ.get("TARGET_EMAIL") or "").strip().lower()
TARGET_NAME  = (os.environ.get("TARGET_NAME")  or "").strip()
TICKET_ID    = (os.environ.get("TICKET_ID")    or "").strip()
DRY_RUN      = (os.environ.get("DRY_RUN", "true").strip().lower() != "false")
SEND_EMAIL   = (os.environ.get("SEND_EMAIL", "true").strip().lower() != "false")


def get_token():
    r = requests.post(
        "https://api.bizzabo.com/api/v2/iam/oauth/token",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        data={
            "grant_type":    "client_credentials",
            "client_id":     CLIENT_ID,
            "client_secret": CLIENT_SECRET,
            "account_id":    ACCOUNT_ID,
            "audience":      "https://api.bizzabo.com/api",
        },
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
            params={"size": 100, "page": page},
            timeout=30,
        )
        r.raise_for_status()
        data = r.json()
        content = data.get("content", [])
        regs.extend(content)
        if not content or len(content) < 100:
            break
        total_pages = data.get("totalPages")
        if total_pages and page >= total_pages - 1:
            break
        page += 1
    return regs


def norm(s):
    s = unicodedata.normalize("NFKD", s or "").encode("ASCII", "ignore").decode()
    return " ".join(s.lower().split())


def rec_emails(r):
    out = []
    for src in (r.get("properties") or {}, r.get("billingAddress") or {}):
        e = (src.get("email") or "").strip().lower()
        if e:
            out.append(e)
    return out


def rec_name(r):
    props = r.get("properties") or {}
    n = f"{(props.get('firstName') or '').strip()} {(props.get('lastName') or '').strip()}".strip()
    if n:
        return n
    bill = r.get("billingAddress") or {}
    return f"{(bill.get('firstName') or '').strip()} {(bill.get('lastName') or '').strip()}".strip()


def describe(r):
    week = ""
    props = r.get("properties") or {}
    if isinstance(props, dict):
        week = (props.get("when_are_you_joining") or "").strip()
    return (f"    registration id : {r.get('id')}\n"
            f"    orderId         : {r.get('orderId')}\n"
            f"    ticketId (type) : {r.get('ticketId')}\n"
            f"    name            : {rec_name(r)}\n"
            f"    email(s)        : {', '.join(rec_emails(r)) or '<none>'}\n"
            f"    ticket          : {r.get('ticketName')}\n"
            f"    week            : {week or '<none>'}\n"
            f"    charge          : {r.get('charge')} · paymentStatus: {r.get('paymentStatus')}"
            f" · validity: {r.get('validity')} · promo: {r.get('promoCode') or '<none>'}")


def main():
    if not TARGET_EMAIL and not TARGET_NAME:
        sys.exit("ERROR: set TARGET_EMAIL (preferred) or TARGET_NAME")

    print(f"Mode: {'DRY RUN (nothing will be cancelled)' if DRY_RUN else '⚠️  LIVE — WILL CANCEL'}")
    print(f"Target: email={TARGET_EMAIL or '-'} name={TARGET_NAME or '-'} ticket_id={TICKET_ID or '-'}")
    print(f"sendEmail on cancel: {SEND_EMAIL} · refundAmount: 0 (always)\n")

    token = get_token()
    regs = fetch_all(token)
    valid = [r for r in regs if (r.get("validity") or "").lower() == "valid"]
    print(f"Fetched {len(regs)} registrations ({len(valid)} valid)\n")

    matches = []
    if TARGET_EMAIL:
        matches = [r for r in valid if TARGET_EMAIL in rec_emails(r)]
    if not matches and TARGET_NAME:
        tn = norm(TARGET_NAME)
        matches = [r for r in valid if norm(rec_name(r)) == tn]

    if not matches:
        sys.exit("No valid registrations matched — nothing to do.")

    print(f"Matched {len(matches)} valid registration(s):")
    for r in matches:
        print(describe(r))
        print()

    if DRY_RUN:
        print("DRY RUN complete. To cancel one of the above, re-run with:")
        print("  dry_run=false · ticket_id=<registration id shown above>")
        return

    if not TICKET_ID:
        sys.exit("LIVE mode requires ticket_id — take it from a dry run. Aborting.")

    target = [r for r in matches if str(r.get("id")) == TICKET_ID]
    if len(target) != 1:
        sys.exit(f"ticket_id {TICKET_ID} matched {len(target)} of the {len(matches)} "
                 f"registrations found for this person. Aborting.")
    r = target[0]

    print("Cancelling this registration:")
    print(describe(r))
    url = (f"https://api.bizzabo.com/v2/events/{EVENT_ID}/orders/{r.get('orderId')}"
           f"/registrations/{r.get('id')}/cancel")
    print(f"\nPOST {url}")
    resp = requests.post(
        url,
        headers={"Authorization": f"Bearer {token}",
                 "Content-Type": "application/json",
                 "Accept": "application/json"},
        json={"refundAmount": 0, "sendEmail": SEND_EMAIL},
        timeout=30,
    )
    print(f"HTTP {resp.status_code}")
    print(resp.text[:2000])
    resp.raise_for_status()
    print("\n✅ Ticket cancelled.")


if __name__ == "__main__":
    main()
