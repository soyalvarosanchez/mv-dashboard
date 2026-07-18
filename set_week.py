#!/usr/bin/env python3
"""
Update the 'When are you joining us?' form answer of registrations.

Run via the 'Update Week Field' GitHub Action. ASSIGNMENTS is a
semicolon-separated list of <registration id>=<W1|W2|BOTH>.

Safety design:
- Dry run by default: prints current vs proposed value, changes nothing.
- The PUT replaces the whole form submission, so we first GET the
  registration and resend its FULL properties with only the week answer
  changed — nothing else is touched.
- Canonical week strings are taken live from other registrations in the
  event (never typed by hand), so the value always matches what the
  Bizzabo form itself produces.
- After a live update, each registration is re-fetched to verify the
  new value actually stuck.
"""

import os, sys
import requests

CLIENT_ID     = os.environ["BIZZABO_CLIENT_ID"]
CLIENT_SECRET = os.environ["BIZZABO_CLIENT_SECRET"]
ACCOUNT_ID    = os.environ.get("BIZZABO_ACCOUNT_ID", "129966")
EVENT_ID      = os.environ.get("BIZZABO_EVENT_ID",   "754649")

DRY_RUN     = (os.environ.get("DRY_RUN", "true").strip().lower() != "false")
ASSIGNMENTS = [a.strip() for a in (os.environ.get("ASSIGNMENTS") or "").split(";") if a.strip()]

WEEK_FIELD = "when_are_you_joining"


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


def rec_name(r):
    props = r.get("properties") or {}
    n = f"{(props.get('firstName') or '').strip()} {(props.get('lastName') or '').strip()}".strip()
    return n or "<unnamed>"


def canonical_weeks(regs):
    """Collect the exact strings the Bizzabo form produces, keyed W1/W2/BOTH."""
    out = {}
    for r in regs:
        props = r.get("properties") or {}
        if not isinstance(props, dict):
            continue
        v = (props.get(WEEK_FIELD) or "").strip()
        if not v:
            continue
        vl = v.lower()
        if "both" in vl:                            out.setdefault("BOTH", v)
        elif "week 1" in vl and "week 2" not in vl: out.setdefault("W1", v)
        elif "week 2" in vl and "week 1" not in vl: out.setdefault("W2", v)
    return out


def main():
    if not ASSIGNMENTS:
        sys.exit("ERROR: set ASSIGNMENTS as 'rid=W1;rid=W2;rid=BOTH'")

    parsed = []
    for a in ASSIGNMENTS:
        if "=" not in a:
            sys.exit(f"Bad assignment {a!r} — expected <id>=<W1|W2|BOTH>")
        rid, wk = a.split("=", 1)
        wk = wk.strip().upper()
        if wk not in ("W1", "W2", "BOTH"):
            sys.exit(f"Bad week {wk!r} in {a!r} — use W1, W2 or BOTH")
        parsed.append((rid.strip(), wk))

    print(f"Mode: {'DRY RUN (nothing will be updated)' if DRY_RUN else '⚠️  LIVE — WILL UPDATE'}")
    print(f"Assignments: {parsed}\n")

    token = get_token()
    regs = fetch_all(token)
    valid = {str(r.get("id")): r for r in regs if (r.get("validity") or "").lower() == "valid"}
    weeks = canonical_weeks(regs)
    print(f"Fetched {len(regs)} registrations · canonical week strings: {weeks}\n")
    for wk_key in ("W1", "W2", "BOTH"):
        if wk_key not in weeks:
            sys.exit(f"Could not find a canonical string for {wk_key} in live data — aborting")

    # Verify ALL targets before touching anything
    targets = []
    for rid, wk in parsed:
        r = valid.get(rid)
        if r is None:
            sys.exit(f"ABORT (nothing updated): id {rid} not found among valid registrations")
        props = r.get("properties") or {}
        if not isinstance(props, dict):
            sys.exit(f"ABORT: id {rid} has non-dict properties ({type(props).__name__}) — needs manual handling")
        cur = (props.get(WEEK_FIELD) or "").strip() or "<empty>"
        new = weeks[wk]
        targets.append((r, wk, cur, new))
        same = " (already correct — will skip)" if cur == new else ""
        print(f"  id {rid} · {rec_name(r)} · {r.get('ticketName')}")
        print(f"     current : {cur}")
        print(f"     proposed: {new}{same}\n")

    if DRY_RUN:
        print("DRY RUN complete. Re-run with dry_run=false to apply.")
        return

    done = skipped = 0
    failed = []
    for r, wk, cur, new in targets:
        rid = str(r.get("id"))
        if cur == new:
            skipped += 1
            continue
        new_props = dict(r.get("properties") or {})
        new_props[WEEK_FIELD] = new
        resp = requests.put(
            f"https://api.bizzabo.com/v2/events/{EVENT_ID}/registrations/{rid}/formSubmission",
            headers={"Authorization": f"Bearer {token}",
                     "Content-Type": "application/json",
                     "Accept": "application/json"},
            json={"properties": new_props},
            timeout=30,
        )
        if not resp.ok:
            failed.append((rid, rec_name(r), resp.status_code, resp.text[:300]))
            print(f"❌ id {rid} · {rec_name(r)} · HTTP {resp.status_code}")
            continue
        # Verify: re-fetch this registration
        chk = requests.get(
            f"https://api.bizzabo.com/v2/events/{EVENT_ID}/registrations/{rid}",
            headers={"Authorization": f"Bearer {token}"},
            timeout=30,
        )
        got = ""
        if chk.ok:
            got = ((chk.json().get("properties") or {}).get(WEEK_FIELD) or "").strip()
        if got == new:
            done += 1
            print(f"✅ id {rid} · {rec_name(r)} · verified: {got!r}")
        else:
            failed.append((rid, rec_name(r), "verify", f"expected {new!r}, got {got!r}"))
            print(f"⚠️ id {rid} · {rec_name(r)} · PUT ok but verification got {got!r}")

    print(f"\nUpdated & verified: {done} · already correct: {skipped} · failed: {len(failed)}")
    if failed:
        for f in failed:
            print(f"  FAILED: {f}")
        sys.exit(1)


if __name__ == "__main__":
    main()
