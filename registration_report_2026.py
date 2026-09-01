#!/usr/bin/env python3
"""
MVU 2026 Registration Report — same metrics as the Amsterdam 2025 report.
Aggregates only (no personal data in the output).
"""
import csv, io, os, re, unicodedata
from collections import Counter, defaultdict
from datetime import datetime, timedelta
import requests

CLIENT_ID     = os.environ["BIZZABO_CLIENT_ID"]
CLIENT_SECRET = os.environ["BIZZABO_CLIENT_SECRET"]
ACCOUNT_ID    = os.environ.get("BIZZABO_ACCOUNT_ID", "129966")
EVENT_ID      = os.environ.get("BIZZABO_EVENT_ID",   "754649")
SHEET_ID      = "1H5A4kSVUCbvgcHQSLqyOLwhNbnSZAYek8D2crd9t0fo"
EVENTS_GID    = "172132467"

W1_DAYS = [f"2026-07-{d:02d}" for d in range(20, 27)]
W2_DAYS = [f"2026-07-{d:02d}" for d in (27,28,29,30,31)] + ["2026-08-01","2026-08-02"]

def get_token():
    r = requests.post("https://api.bizzabo.com/api/v2/iam/oauth/token",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        data={"grant_type":"client_credentials","client_id":CLIENT_ID,
              "client_secret":CLIENT_SECRET,"account_id":ACCOUNT_ID,
              "audience":"https://api.bizzabo.com/api"}, timeout=30)
    r.raise_for_status(); return r.json()["access_token"]

def fetch_all(token):
    regs, page = [], 0
    while True:
        r = requests.get(f"https://api.bizzabo.com/v2/events/{EVENT_ID}/registrations",
            headers={"Authorization": f"Bearer {token}"},
            params={"size":100,"page":page}, timeout=30)
        r.raise_for_status()
        c = r.json().get("content", [])
        regs.extend(c)
        if not c or len(c) < 100: break
        page += 1
    return regs

def ts(s):
    for f in ("%m/%d/%Y %H:%M:%S","%m/%d/%Y %H:%M"):
        try: return datetime.strptime(s, f) + timedelta(hours=3)
        except ValueError: continue
    return None

def weeks_of(r):
    p = r.get("properties") or {}
    v = (p.get("when_are_you_joining") or "").strip().lower()
    if "both" in v: return "both"
    if "week 1" in v: return "w1"
    if "week 2" in v: return "w2"
    return "unset"

def duration(name):
    n = (name or "").lower()
    if "2 week" in n: return "2 weeks"
    if "1 week" in n: return "1 week"
    if "3 day" in n:  return "3 days"
    return "other"

def is_vip(r):
    t = (r.get("ticketName") or "").lower(); p = (r.get("promoCode") or "").lower()
    if "kid" in t or "teen" in t: return False
    if "speaker" in t or "hexagon" in t or "friends of vishen" in t or "crew" in t: return False
    if "special guest" in t or "vip guest" in t or "vip media" in t or "vip" in t: return True
    return p in ("vipguest","vipmedia")

def is_regular(r):
    t = (r.get("ticketName") or "").lower()
    if "kid" in t or "teen" in t: return False
    return ("adult" in t or "standard" in t) and not is_vip(r)

# ---------- app feeds ----------
base = f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/export?format=csv"
acts_rows = list(csv.DictReader(io.StringIO(requests.get(base, timeout=30).text)))
ev_rows   = list(csv.DictReader(io.StringIO(requests.get(base+"&gid="+EVENTS_GID, timeout=30).text)))
activated_ids = {(r.get("ticketId") or "").strip() for r in acts_rows
                 if (r.get("status") or "").strip().lower() == "activated"}

# ---------- bizzabo ----------
token = get_token(); regs = fetch_all(token)
valid = [r for r in regs if (r.get("validity") or "").lower() == "valid"]
invalid = [r for r in regs if (r.get("validity") or "").lower() != "valid"]

print("="*62); print("MVU 2026 — REGISTRATION REPORT"); print("="*62)

# 1) headline numbers
total_claimed = len(regs)
n_valid, n_cancel = len(valid), len(invalid)
checkins = sum(1 for r in valid if str(r.get("id")) in activated_ids)
def norm(s):
    s = unicodedata.normalize('NFKD', s or '').encode('ASCII','ignore').decode()
    return ' '.join(s.lower().split())
uniq_keys = set()
for r in valid:
    if str(r.get("id")) not in activated_ids: continue
    p = r.get("properties") or {}
    e = (p.get("email") or "").strip().lower()
    n = norm(f"{p.get('firstName','')} {p.get('lastName','')}")
    uniq_keys.add(e or n or str(r.get("id")))
print(f"\n[1] HEADLINE")
print(f"  {total_claimed}  total of tickets claimed")
print(f"  {n_valid}  valid tickets")
print(f"  {n_cancel}  canceled tickets ({n_cancel/total_claimed*100:.0f}%)")
print(f"  {checkins}  check-ins in total")
print(f"  {len(uniq_keys)}  estimated unique attendees")

# 2) daily attendance
per_day = defaultdict(set); acts_per_day = Counter()
for r in ev_rows:
    d = ts(r.get("event_timestamp",""))
    if not d: continue
    day = d.strftime("%Y-%m-%d")
    if r.get("event") == "scan_validation": per_day[day].add(r.get("ticketId"))
    elif r.get("event") == "activated":     acts_per_day[day] += 1
print(f"\n[2] DAILY ATTENDANCE (unique visitors)")
ev_days = [d for d in sorted(per_day) if d in W1_DAYS or d in W2_DAYS]
for d in ev_days:
    lbl = datetime.strptime(d,"%Y-%m-%d").strftime("%d-%b")
    print(f"  {lbl}: {len(per_day[d])}")
peak = max(ev_days, key=lambda d: len(per_day[d]))
print(f"  PEAK: {datetime.strptime(peak,'%Y-%m-%d').strftime('%B %dth')} with {len(per_day[peak])} pax")
w1d = [len(per_day[d]) for d in W1_DAYS if d in per_day]
w2d = [len(per_day[d]) for d in W2_DAYS if d in per_day]
print(f"  Average attendance Week 1: {sum(w1d)/len(w1d):.0f}/day")
print(f"  Average attendance Week 2: {sum(w2d)/len(w2d):.0f}/day")
w1u = set().union(*[per_day[d] for d in W1_DAYS if d in per_day])
w2u = set().union(*[per_day[d] for d in W2_DAYS if d in per_day])
print(f"  Unique W1: {len(w1u)} · Unique W2: {len(w2u)} · Both: {len(w1u&w2u)} · Total unique: {len(w1u|w2u)}")

print(f"\n[3] ACTIVATIONS PER DAY")
for d in sorted(acts_per_day):
    print(f"  {datetime.strptime(d,'%Y-%m-%d').strftime('%d-%b')}: {acts_per_day[d]}")

# 4) VIP / Regular
print(f"\n[4] VIP & REGULAR TICKETS")
for label, fn in (("VIP", is_vip), ("Regular", is_regular)):
    grp = [r for r in valid if fn(r)]
    act = [r for r in grp if str(r.get("id")) in activated_ids]
    print(f"  {label}: {len(act)} / {len(grp)} activated ({len(act)/len(grp)*100:.0f}%)" if grp else f"  {label}: 0")
    for dur in ("1 week","2 weeks","3 days"):
        sub = [r for r in grp if duration(r.get("ticketName")) == dur]
        sact = [r for r in sub if str(r.get("id")) in activated_ids]
        if sub: print(f"     {dur:8s}: {len(sact)} / {len(sub)}")

# 5) no-shows
noshow = [r for r in valid if str(r.get("id")) not in activated_ids]
comped = [r for r in noshow if float(r.get("charge") or 0) == 0]
paid   = [r for r in noshow if float(r.get("charge") or 0) > 0]
print(f"\n[5] NO-SHOWS")
print(f"  {len(noshow)}  total of no-shows")
print(f"  {len(comped)}  were comped")
print(f"  {len(paid)}  were paid tickets")
print(f"  Top promo codes among no-shows:")
for code, n in Counter((r.get("promoCode") or "").strip() for r in noshow if (r.get("promoCode") or "").strip()).most_common(5):
    print(f"     {code}: {n}")

# 6) promo codes
with_promo = [r for r in regs if (r.get("promoCode") or "").strip()]
codes = {(r.get("promoCode") or "").strip().lower() for r in with_promo}
print(f"\n[6] PROMO CODES")
print(f"  {len(codes)}  different promo codes used")
print(f"  {len(with_promo)}  tickets claimed under a promo code ({len(with_promo)/total_claimed*100:.0f}% of total tickets)")

# 7) parties
party = [r for r in valid if "party" in (r.get("ticketName") or "").lower()]
print(f"\n[7] PARTY TICKETS: {len(party)}")
if party:
    for name, n in Counter(r.get("ticketName") for r in party).most_common(): print(f"     {name}: {n}")
else:
    print("     (no dedicated party ticket type in 2026)")

# evening scans as party proxy
print(f"  Evening scans (20:00+) by day:")
even = defaultdict(set)
for r in ev_rows:
    if r.get("event") != "scan_validation": continue
    d = ts(r.get("event_timestamp",""))
    if d and d.hour >= 20: even[d.strftime("%Y-%m-%d")].add(r.get("ticketId"))
for d in sorted(even):
    if len(even[d]) > 30:
        print(f"     {datetime.strptime(d,'%Y-%m-%d').strftime('%a %d-%b')}: {len(even[d])} people")
print("\n" + "="*62)
