#!/usr/bin/env python3
"""
Update registration form fields (week / first name / last name / email).

Run via the 'Update Registration Fields' GitHub Action. UPDATES format:
  <rid> | field=value | field=value ;; <rid> | field=value ...
Fields: week (W1|W2|BOTH), first, last, email.

Same safety design as set_week.py:
- Dry run by default; prints current vs proposed per field.
- GET-merge-PUT: the full current properties are resent with only the
  requested fields changed — nothing else is touched.
- Canonical week strings read from live data, never hand-typed.
- All targets verified to exist before touching any.
- Every update is re-fetched; each changed field and the property-key
  set are verified. Any mismatch fails the run.
"""

import os, sys
import requests

CLIENT_ID     = os.environ["BIZZABO_CLIENT_ID"]
CLIENT_SECRET = os.environ["BIZZABO_CLIENT_SECRET"]
ACCOUNT_ID    = os.environ.get("BIZZABO_ACCOUNT_ID", "129966")
EVENT_ID      = os.environ.get("BIZZABO_EVENT_ID",   "754649")

DRY_RUN = (os.environ.get("DRY_RUN", "true").strip().lower() != "false")
UPDATES = (os.environ.get("UPDATES") or "").strip()

WEEK_FIELD = "when_are_you_joining"
FIELD_MAP  = {"first": "firstName", "last": "lastName", "email": "email"}


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


def parse_updates(weeks):
    """Returns list of (rid, {property_key: new_value})."""
    parsed = []
    for chunk in UPDATES.split(";;"):
        chunk = chunk.strip()
        if not chunk:
            continue
        parts = [p.strip() for p in chunk.split("|")]
        rid = parts[0]
        changes = {}
        for p in parts[1:]:
            if "=" not in p:
                sys.exit(f"Bad field {p!r} in {chunk!r}")
            k, v = p.split("=", 1)
            k, v = k.strip().lower(), v.strip()
            if k == "week":
                wk = v.upper()
                if wk not in weeks:
                    sys.exit(f"Bad/unavailable week {v!r} in {chunk!r}")
                changes[WEEK_FIELD] = weeks[wk]
            elif k in FIELD_MAP:
                if not v:
                    sys.exit(f"Empty value for {k!r} in {chunk!r}")
                changes[FIELD_MAP[k]] = v
            else:
                sys.exit(f"Unknown field {k!r} in {chunk!r} — use week/first/last/email")
        if not changes:
            sys.exit(f"No fields for id {rid}")
        parsed.append((rid, changes))
    return parsed


def main():
    if not UPDATES:
        sys.exit("ERROR: set UPDATES as '<rid> | field=value ... ;; <rid> | ...'")

    print(f"Mode: {'DRY RUN (nothing will be updated)' if DRY_RUN else '⚠️  LIVE — WILL UPDATE'}\n")
    token = get_token()
    regs = fetch_all(token)
    valid = {str(r.get("id")): r for r in regs if (r.get("validity") or "").lower() == "valid"}
    weeks = canonical_weeks(regs)
    print(f"Fetched {len(regs)} registrations · canonical weeks: {weeks}\n")

    parsed = parse_updates(weeks)

    targets = []
    for rid, changes in parsed:
        r = valid.get(rid)
        if r is None:
            sys.exit(f"ABORT (nothing updated): id {rid} not found among valid registrations")
        props = r.get("properties") or {}
        if not isinstance(props, dict):
            sys.exit(f"ABORT: id {rid} has non-dict properties — needs manual handling")
        targets.append((r, changes))
        print(f"  id {rid} · {rec_name(r)} · {r.get('ticketName')}")
        for k, new in changes.items():
            cur = (props.get(k) or "").strip() or "<empty>"
            same = "  (already correct — will skip field)" if cur == new else ""
            print(f"     {k:22s}: {cur!r} → {new!r}{same}")
        print()

    if DRY_RUN:
        print("DRY RUN complete. Re-run with dry_run=false to apply.")
        return

    done = failed = 0
    for r, changes in targets:
        rid = str(r.get("id"))
        before = dict(r.get("properties") or {})
        new_props = dict(before)
        new_props.update(changes)
        if new_props == before:
            print(f"⏭️  id {rid} · {rec_name(r)} · nothing to change")
            continue
        resp = requests.put(
            f"https://api.bizzabo.com/v2/events/{EVENT_ID}/registrations/{rid}/formSubmission",
            headers={"Authorization": f"Bearer {token}",
                     "Content-Type": "application/json",
                     "Accept": "application/json"},
            json={"properties": new_props},
            timeout=30,
        )
        if not resp.ok:
            failed += 1
            print(f"❌ id {rid} · {rec_name(r)} · HTTP {resp.status_code} · {resp.text[:200]}")
            continue
        chk = requests.get(
            f"https://api.bizzabo.com/v2/events/{EVENT_ID}/registrations/{rid}",
            headers={"Authorization": f"Bearer {token}"},
            timeout=30,
        )
        chk_props = (chk.json().get("properties") or {}) if chk.ok else {}
        bad_fields = {k: (chk_props.get(k) or "").strip()
                      for k, v in changes.items() if (chk_props.get(k) or "").strip() != v}
        lost = set(before.keys()) - set(chk_props.keys())
        if not bad_fields and not lost:
            done += 1
            print(f"✅ id {rid} · {rec_name(chk.json())} · all fields verified · props {len(before)}→{len(chk_props)} keys intact")
        else:
            failed += 1
            print(f"⚠️ id {rid} · verification problem · bad_fields={bad_fields} · lost_keys={sorted(lost)}")

    print(f"\nUpdated & verified: {done} · failed: {failed}")
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
