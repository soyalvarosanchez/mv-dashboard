#!/usr/bin/env python3
"""
Country mix of this year's valid attendees: USA vs Europe vs rest.
Aggregates only — no personal data in the output.

Country source per registration: properties.country, falling back to
billingAddress.country (buyer). Values arrive as names or ISO codes.
"""

import os
import requests

CLIENT_ID     = os.environ["BIZZABO_CLIENT_ID"]
CLIENT_SECRET = os.environ["BIZZABO_CLIENT_SECRET"]
ACCOUNT_ID    = os.environ.get("BIZZABO_ACCOUNT_ID", "129966")
EVENT_ID      = os.environ.get("BIZZABO_EVENT_ID",   "754649")

EUROPE = {
    "albania","andorra","austria","belarus","belgium","bosnia and herzegovina",
    "bulgaria","croatia","cyprus","czech republic","czechia","denmark","estonia",
    "finland","france","germany","greece","hungary","iceland","ireland","italy",
    "kosovo","latvia","liechtenstein","lithuania","luxembourg","malta","moldova",
    "monaco","montenegro","netherlands","north macedonia","norway","poland",
    "portugal","romania","russia","russian federation","san marino","serbia",
    "slovakia","slovenia","spain","sweden","switzerland","ukraine",
    "united kingdom","uk","great britain","vatican city",
    # ISO2
    "al","ad","at","by","be","ba","bg","hr","cy","cz","dk","ee","fi","fr","de",
    "gr","hu","is","ie","it","xk","lv","li","lt","lu","mt","md","mc","me","nl",
    "mk","no","pl","pt","ro","ru","sm","rs","sk","si","es","se","ch","ua","gb",
}
USA = {"united states", "united states of america", "usa", "us", "u.s.", "u.s.a."}


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
    token = get_token()
    valid = [x for x in fetch_all(token) if (x.get("validity") or "").lower() == "valid"]

    from collections import Counter
    dist = Counter()
    for x in valid:
        props = x.get("properties") or {}
        c = (props.get("country") or "").strip()
        if not c:
            c = ((x.get("billingAddress") or {}).get("country") or "").strip()
        dist[c.lower() if c else "<unknown>"] += 1

    us = sum(n for c, n in dist.items() if c in USA)
    eu = sum(n for c, n in dist.items() if c in EUROPE)
    unknown = dist.get("<unknown>", 0)
    total = len(valid)
    rest = total - us - eu - unknown
    known = total - unknown

    print(f"Valid attendees: {total} · with country: {known} · unknown: {unknown}\n")
    print(f"USA:    {us:5d}  ({us/known*100:5.1f}% of known)")
    print(f"Europe: {eu:5d}  ({eu/known*100:5.1f}% of known)")
    print(f"Rest:   {rest:5d}  ({rest/known*100:5.1f}% of known)\n")
    print("Top 25 countries:")
    for c, n in dist.most_common(26):
        if c == "<unknown>":
            continue
        tag = "USA" if c in USA else ("EU" if c in EUROPE else "rest")
        print(f"  {n:5d}  {c}  [{tag}]")


if __name__ == "__main__":
    main()
