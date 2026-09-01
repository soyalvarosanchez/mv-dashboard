#!/usr/bin/env python3
"""
Two CSVs for cross-referencing against Bizzabo exports and survey results.

  attendance_cohorts.csv : email, cohort, checkin_days_w1, checkin_days_w2
  volunteers_staff.csv   : email, category, weeks

Sources
  - Hub entries: check-in app's Google Sheet, 'Events' tab, scan_validation
    rows (ticketId + timestamp, UTC -> Tallinn +3h).
  - Emails, ticket types, promo codes, week selection: Bizzabo API,
    event 754649 registrations (validity = valid).

Weeks: W1 = 20-26 Jul 2026 · W2 = 27 Jul - 2 Aug 2026.
Nothing is estimated: rows that cannot be resolved are reported, not filled.
"""
import csv, io, os, unicodedata
from collections import defaultdict
from datetime import datetime, timedelta
import requests

CID=os.environ["BIZZABO_CLIENT_ID"]; CS=os.environ["BIZZABO_CLIENT_SECRET"]
AID=os.environ.get("BIZZABO_ACCOUNT_ID","129966"); EID=os.environ.get("BIZZABO_EVENT_ID","754649")
SHEET="1H5A4kSVUCbvgcHQSLqyOLwhNbnSZAYek8D2crd9t0fo"; GID="172132467"
W1={f"2026-07-{d:02d}" for d in range(20,27)}
W2={f"2026-07-{d:02d}" for d in (27,28,29,30,31)}|{"2026-08-01","2026-08-02"}

def token():
    return requests.post("https://api.bizzabo.com/api/v2/iam/oauth/token",
        headers={"Content-Type":"application/x-www-form-urlencoded"},
        data={"grant_type":"client_credentials","client_id":CID,"client_secret":CS,
              "account_id":AID,"audience":"https://api.bizzabo.com/api"},timeout=30).json()["access_token"]

def fetch(tok):
    out=[];p=0
    while True:
        c=requests.get(f"https://api.bizzabo.com/v2/events/{EID}/registrations",
            headers={"Authorization":f"Bearer {tok}"},params={"size":100,"page":p},timeout=30).json().get("content",[])
        out+=c
        if not c or len(c)<100: break
        p+=1
    return out

def norm(s):
    s=unicodedata.normalize('NFKD',s or '').encode('ASCII','ignore').decode()
    return ' '.join(s.lower().split())

def email_of(r):
    p=r.get("properties") or {}
    e=(p.get("email") or "").strip().lower()
    if e: return e
    return ((r.get("billingAddress") or {}).get("email") or "").strip().lower()

def name_of(r):
    p=r.get("properties") or {}
    return norm(f"{p.get('firstName','')} {p.get('lastName','')}")

def weeks_of(r):
    v=((r.get("properties") or {}).get("when_are_you_joining") or "").strip().lower()
    if "both" in v: return "BOTH"
    if "week 1" in v: return "W1"
    if "week 2" in v: return "W2"
    return "NA"

def category(r):
    t=(r.get("ticketName") or "").lower(); p=(r.get("promoCode") or "").strip().lower()
    if p in ("volunteer1week","volunteer2weeks"): return "VOLUNTEER"
    if p=="mycrewpass" or "crew" in t:            return "CREW"
    if "speaker" in t:                            return "SPEAKER"
    if "hexagon" in t or "friends of vishen" in t: return "VVIP"
    if "comped" in t or float(r.get("charge") or 0)==0: return "COMP"
    return None

# ---------- sources ----------
ev=list(csv.DictReader(io.StringIO(requests.get(
    f"https://docs.google.com/spreadsheets/d/{SHEET}/export?format=csv&gid={GID}",timeout=30).text)))
tok=token(); regs=fetch(tok)
valid=[r for r in regs if (r.get("validity") or "").lower()=="valid"]
by_id={str(r.get("id")):r for r in regs}
print(f"SOURCES: Bizzabo event {EID} -> {len(regs)} regs ({len(valid)} valid) · app sheet -> {len(ev)} events")

# ---------- ticket-level scan days ----------
tdays=defaultdict(lambda: {"w1":set(),"w2":set()})
outside=0
for r in ev:
    if r.get("event")!="scan_validation": continue
    try: d=datetime.strptime(r["event_timestamp"],"%m/%d/%Y %H:%M:%S")+timedelta(hours=3)
    except (ValueError,KeyError): continue
    day=d.strftime("%Y-%m-%d"); tid=(r.get("ticketId") or "").strip()
    if   day in W1: tdays[tid]["w1"].add(day)
    elif day in W2: tdays[tid]["w2"].add(day)
    else: outside+=1
def coh(w1,w2):
    return "BOTH" if (w1 and w2) else ("W1_ONLY" if w1 else "W2_ONLY")
tick_coh={t:coh(v["w1"],v["w2"]) for t,v in tdays.items() if v["w1"] or v["w2"]}
tc=defaultdict(int)
for c in tick_coh.values(): tc[c]+=1
print(f"\nTICKET-LEVEL (pre-dedupe): W1_ONLY={tc['W1_ONLY']} W2_ONLY={tc['W2_ONLY']} BOTH={tc['BOTH']} TOTAL={len(tick_coh)}")
print(f"  scans outside both weeks (pre-event): {outside}")

# ---------- file 1: dedupe by email ----------
per_email=defaultdict(lambda: {"w1":set(),"w2":set()})
no_email=[]
for tid,v in tdays.items():
    if not (v["w1"] or v["w2"]): continue
    r=by_id.get(tid)
    e=email_of(r) if r else ""
    if not e:
        no_email.append(tid); continue
    per_email[e]["w1"]|=v["w1"]; per_email[e]["w2"]|=v["w2"]
rows1=[]
for e,v in per_email.items():
    rows1.append({"email":e,"cohort":coh(v["w1"],v["w2"]),
                  "checkin_days_w1":len(v["w1"]),"checkin_days_w2":len(v["w2"])})
rows1.sort(key=lambda x:x["email"])
with open("attendance_cohorts.csv","w",newline="",encoding="utf-8") as f:
    w=csv.DictWriter(f,fieldnames=["email","cohort","checkin_days_w1","checkin_days_w2"]); w.writeheader(); w.writerows(rows1)
ec=defaultdict(int)
for r_ in rows1: ec[r_["cohort"]]+=1
print(f"\nFILE 1 attendance_cohorts.csv -> {len(rows1)} rows (deduped by email)")
print(f"  W1_ONLY={ec['W1_ONLY']} W2_ONLY={ec['W2_ONLY']} BOTH={ec['BOTH']}")
print(f"  scanned tickets with NO email in Bizzabo (excluded): {len(no_email)} -> {no_email[:12]}")

# ---------- file 2 ----------
staff=defaultdict(lambda: {"cats":set(),"weeks":set(),"ci":False})
for r in valid:
    c=category(r)
    if not c: continue
    e=email_of(r)
    if not e: continue
    s=staff[e]; s["cats"].add(c); s["weeks"].add(weeks_of(r))
    if str(r.get("id")) in tick_coh: s["ci"]=True
PRIO=["SPEAKER","VVIP","CREW","VOLUNTEER","COMP"]
rows2=[]
for e,s in staff.items():
    cat=next(c for c in PRIO if c in s["cats"])
    wk = "BOTH" if ("BOTH" in s["weeks"] or {"W1","W2"}<=s["weeks"]) else \
         ("W1" if "W1" in s["weeks"] else ("W2" if "W2" in s["weeks"] else "NA"))
    rows2.append({"email":e,"category":cat,"weeks":wk})
rows2.sort(key=lambda x:(x["category"],x["email"]))
with open("volunteers_staff.csv","w",newline="",encoding="utf-8") as f:
    w=csv.DictWriter(f,fieldnames=["email","category","weeks"]); w.writeheader(); w.writerows(rows2)
print(f"\nFILE 2 volunteers_staff.csv -> {len(rows2)} rows (deduped by email)")
cc=defaultdict(int); ci=defaultdict(int)
for e,s in staff.items():
    cat=next(c for c in PRIO if c in s["cats"]); cc[cat]+=1
    if s["ci"]: ci[cat]+=1
for c in PRIO: print(f"  {c:10s} {cc[c]:4d} people · {ci[c]:4d} checked in")
wk=defaultdict(int)
for r_ in rows2: wk[r_["weeks"]]+=1
print(f"  weeks: {dict(wk)}")

# ticket-level counts for validation vs dashboard (W1 registered)
print("\n  VALIDATION vs dashboard (ticket-level, registered for W1 incl. BOTH):")
for c in PRIO:
    n=sum(1 for r in valid if category(r)==c and weeks_of(r) in ("W1","BOTH"))
    a=sum(1 for r in valid if category(r)==c and weeks_of(r) in ("W1","BOTH") and str(r.get("id")) in tick_coh)
    print(f"    {c:10s} {n:4d} registered · {a:4d} checked in")

# ---------- same person, multiple emails ----------
nm=defaultdict(set)
for r in valid:
    n=name_of(r); e=email_of(r)
    if n and e: nm[n].add(e)
multi={n:v for n,v in nm.items() if len(v)>1}
print(f"\nSAME NAME, MULTIPLE EMAILS: {len(multi)} names (both rows kept)")
for n in sorted(multi)[:15]: print(f"    {len(multi[n])} emails for one name")
