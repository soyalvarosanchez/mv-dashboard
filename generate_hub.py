#!/usr/bin/env python3
"""
Generates the Mindvalley Events Hub global dashboard.
Fetches registration data for all events from Bizzabo API,
aggregates metrics, and writes a static HTML file with data pre-baked.
"""

import csv, json, os, re, sys, time
from datetime import datetime, timezone
from pathlib import Path

import requests

# --- Config ---

CLIENT_ID = os.environ.get("BIZZABO_CLIENT_ID", "")
CLIENT_SECRET = os.environ.get("BIZZABO_CLIENT_SECRET", "")
ACCOUNT_ID = 129966
AUTH_URL = "https://auth.bizzabo.com/oauth/token"
API_BASE = "https://api.bizzabo.com/v1"
CSV_PATH = Path(__file__).parent / "events.csv"
OUTPUT_PATH = Path(__file__).parent / "views" / "global" / "index.html"
TEMPLATE_PATH = Path(__file__).parent / "views" / "global" / "_template.html"

EXCLUDE_PATTERNS = [
    re.compile(r"\btest\b", re.I),
    re.compile(r"\bdummy\b", re.I),
    re.compile(r"\barchived\b", re.I),
    re.compile(r"\bbeta test\b", re.I),
    re.compile(r"\btesting\b", re.I),
]

# --- Auth ---

def get_token():
    resp = requests.post(AUTH_URL, json={
        "grant_type": "client_credentials",
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "audience": "https://api.bizzabo.com/api",
        "account_id": ACCOUNT_ID,
    })
    resp.raise_for_status()
    return resp.json()["access_token"]

# --- API helpers ---

def api_get(path, token):
    resp = requests.get(f"{API_BASE}{path}", headers={
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
    })
    resp.raise_for_status()
    return resp.json()

def fetch_event_details(event_id, token):
    try:
        return api_get(f"/events/{event_id}", token)
    except Exception as e:
        print(f"  Failed to get event {event_id} details: {e}")
        return None

def fetch_all_registrations(event_id, token):
    registrations = []
    page, size, total_pages = 0, 200, 1
    while page < total_pages:
        try:
            data = api_get(f"/events/{event_id}/registrations?page={page}&size={size}", token)
            registrations.extend(data.get("content", []))
            p = data.get("page", {})
            total_pages = p.get("totalPages", 1)
            total = p.get("totalElements", 0)
            print(f"  Page {page+1}/{total_pages} ({total} total)")
            page += 1
            if page < total_pages:
                time.sleep(0.1)
        except Exception as e:
            print(f"  Error page {page}: {e}")
            break
    return registrations

# --- CSV parsing ---

def parse_csv():
    events = []
    with open(CSV_PATH, newline="", encoding="utf-8") as f:
        reader = csv.reader(f)
        next(reader)  # skip header
        for row in reader:
            if len(row) < 7:
                continue
            event_id = row[1].strip()
            name = row[2].strip()
            is_archived = row[3].strip() == "Yes"
            start_date = row[4].strip()
            end_date = row[5].strip()

            if not event_id.isdigit():
                continue
            if is_archived:
                continue
            if any(p.search(name) for p in EXCLUDE_PATTERNS):
                continue

            events.append({
                "eventId": int(event_id),
                "name": name,
                "startDate": start_date,
                "endDate": end_date,
            })
    return events

# --- Aggregation ---

def is_virtual(ticket_name):
    return bool(re.search(r"virtual", ticket_name or "", re.I))

def aggregate(event, details, registrations):
    # Filter out virtual tickets
    regs = [r for r in registrations if not is_virtual(r.get("ticketName", ""))]

    total = len(regs)
    checked_in = sum(1 for r in regs if r.get("checkedin"))
    revenue = sum(r.get("price", 0) for r in regs) / 100
    charges = sum(r.get("charge", 0) for r in regs) / 100
    fees = sum(r.get("fees", 0) for r in regs) / 100

    payment_statuses = {}
    for r in regs:
        st = r.get("paymentStatus") or "unknown"
        payment_statuses[st] = payment_statuses.get(st, 0) + 1

    ticket_types = {}
    for r in regs:
        t = r.get("ticketName") or "Unknown"
        ticket_types[t] = ticket_types.get(t, 0) + 1

    reg_dates = [
        r["registrationDate"][:10]
        for r in regs
        if r.get("registrationDate")
    ]

    order_types = {}
    for r in regs:
        ot = r.get("orderType") or "unknown"
        order_types[ot] = order_types.get(ot, 0) + 1

    venue = details.get("venue") if details else None
    return {
        "eventId": event["eventId"],
        "name": event["name"],
        "startDate": event["startDate"],
        "endDate": event["endDate"],
        "venue": venue,
        "timezone": details.get("timezone") if details else None,
        "attendanceType": details.get("attendanceType") if details else None,
        "status": details.get("status") if details else None,
        "totalRegistrations": total,
        "checkedIn": checked_in,
        "checkinRate": round(checked_in / total * 100, 1) if total > 0 else 0,
        "revenue": round(revenue, 2),
        "charges": round(charges, 2),
        "fees": round(fees, 2),
        "currency": regs[0].get("currency", "USD") if regs else "USD",
        "paymentStatuses": payment_statuses,
        "ticketTypes": ticket_types,
        "orderTypes": order_types,
        "regDates": reg_dates,
    }

# --- Main ---

def main():
    if not CLIENT_ID or not CLIENT_SECRET:
        sys.exit("Missing BIZZABO_CLIENT_ID or BIZZABO_CLIENT_SECRET env vars")

    print("Authenticating with Bizzabo...")
    token = get_token()
    print("Authenticated.")

    events = parse_csv()
    print(f"Found {len(events)} real events (after filtering).")

    results = []
    spender_map = {}

    for i, event in enumerate(events):
        eid = event["eventId"]
        print(f"[{i+1}/{len(events)}] Fetching: {event['name']} ({eid})...")

        details = fetch_event_details(eid, token)
        registrations = fetch_all_registrations(eid, token)

        # Track spenders (exclude virtual)
        for r in registrations:
            if is_virtual(r.get("ticketName", "")):
                continue
            props = r.get("properties") or {}
            billing = r.get("billingAddress") or {}
            email = props.get("email") or billing.get("email")
            price = r.get("price", 0)
            if not email or not price or r.get("paymentStatus") == "refunded":
                continue
            if email not in spender_map:
                fn = props.get("firstName", "")
                ln = props.get("lastName", "")
                spender_map[email] = {
                    "name": f"{fn} {ln}".strip(),
                    "email": email,
                    "totalSpent": 0,
                    "events": [],
                    "registrations": 0,
                }
            spender_map[email]["totalSpent"] += price / 100
            spender_map[email]["registrations"] += 1
            if event["name"] not in spender_map[email]["events"]:
                spender_map[email]["events"].append(event["name"])

        agg = aggregate(event, details, registrations)
        results.append(agg)
        print(f"  -> {agg['totalRegistrations']} regs, ${agg['revenue']:,.0f} revenue")

        if i < len(events) - 1:
            time.sleep(0.3)

    # Top 15 spenders
    top_spenders = sorted(spender_map.values(), key=lambda s: s["totalSpent"], reverse=True)[:15]
    for s in top_spenders:
        s["totalSpent"] = round(s["totalSpent"], 2)

    dashboard_data = {
        "fetchedAt": datetime.now(timezone.utc).isoformat(),
        "totalEvents": len(results),
        "events": results,
        "topSpenders": top_spenders,
    }

    # Read template and inject data
    template = TEMPLATE_PATH.read_text(encoding="utf-8")
    data_json = json.dumps(dashboard_data, ensure_ascii=False)
    html = template.replace("/*__DASHBOARD_DATA__*/", f"window.__DASHBOARD_DATA__ = {data_json};")

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(html, encoding="utf-8")
    print(f"\nDashboard written to {OUTPUT_PATH} ({len(html)//1024} KB)")
    print(f"Total: {len(results)} events, {sum(e['totalRegistrations'] for e in results)} registrations")

if __name__ == "__main__":
    main()
