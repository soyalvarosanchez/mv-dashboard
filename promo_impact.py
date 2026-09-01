#!/usr/bin/env python3
"""Sales impact of the promo-code system: share of tickets and income
that came through codes, broken down by code type. Aggregates only."""
import csv, os
from collections import defaultdict
import requests
CID=os.environ["BIZZABO_CLIENT_ID"]; CS=os.environ["BIZZABO_CLIENT_SECRET"]
AID=os.environ.get("BIZZABO_ACCOUNT_ID","129966"); EID=os.environ.get("BIZZABO_EVENT_ID","754649")
types={}
with open('promo_code_types.csv') as f:
    for row in csv.DictReader(f): types[row['code']] = row['type']
tok=requests.post("https://api.bizzabo.com/api/v2/iam/oauth/token",
    headers={"Content-Type":"application/x-www-form-urlencoded"},
    data={"grant_type":"client_credentials","client_id":CID,"client_secret":CS,
          "account_id":AID,"audience":"https://api.bizzabo.com/api"},timeout=30).json()["access_token"]
regs=[];page=0
while True:
    c=requests.get(f"https://api.bizzabo.com/v2/events/{EID}/registrations",
        headers={"Authorization":f"Bearer {tok}"},params={"size":100,"page":page},timeout=30).json().get("content",[])
    regs+=c
    if not c or len(c)<100: break
    page+=1
valid=[r for r in regs if (r.get("validity") or "").lower()=="valid"]
LBL={"Invisible Tickets":"Invisible","Ticket Discount":"Discount","Tracking Only":"Tracking"}
def bucket(r):
    p=(r.get("promoCode") or "").strip().lower()
    if not p: return "No code"
    return LBL.get(types.get(p,""), "Code (type unknown)")
# tickets: sobre TOTAL CLAIMED
tc=defaultdict(int)
for r in regs: tc[bucket(r)]+=1
TOT=len(regs)
print("="*66); print("PROMO CODE IMPACT — MVU 2026"); print("="*66)
print(f"\nTICKETS CLAIMED: {TOT}")
order=["Invisible","Discount","Tracking","Code (type unknown)","No code"]
with_code=sum(v for k,v in tc.items() if k!="No code")
for k in order:
    if tc.get(k): print(f"  {k:22s} {tc[k]:5d}  ({tc[k]/TOT*100:4.1f}%)")
print(f"  {'— via any code':22s} {with_code:5d}  ({with_code/TOT*100:4.1f}%)")
# revenue: sobre VALID
rev=defaultdict(float)
for r in valid: rev[bucket(r)]+=float(r.get("charge") or 0)/100
TOTREV=sum(rev.values()); code_rev=sum(v for k,v in rev.items() if k!="No code")
print(f"\nINCOME (valid tickets, charge collected): ${TOTREV:,.0f}")
for k in order:
    if rev.get(k): print(f"  {k:22s} ${rev[k]:12,.0f}  ({rev[k]/TOTREV*100:4.1f}%)")
print(f"  {'— via any code':22s} ${code_rev:12,.0f}  ({code_rev/TOTREV*100:4.1f}%)")
# paid vs comped dentro de códigos
paid_code=sum(1 for r in valid if bucket(r)!="No code" and float(r.get("charge") or 0)>0)
comp_code=sum(1 for r in valid if bucket(r)!="No code" and float(r.get("charge") or 0)==0)
print(f"\nValid tickets via codes: paid {paid_code} · comped {comp_code}")
unk=sorted({(r.get('promoCode') or '').strip() for r in regs if (r.get('promoCode') or '').strip() and (r.get('promoCode') or '').strip().lower() not in types})
print(f"\nCódigos en Bizzabo sin tipo en el export: {len(unk)} → {unk[:10]}")
