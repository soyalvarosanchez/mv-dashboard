import csv, io, os
from collections import Counter
import requests
CID=os.environ["BIZZABO_CLIENT_ID"]; CS=os.environ["BIZZABO_CLIENT_SECRET"]
AID=os.environ.get("BIZZABO_ACCOUNT_ID","129966"); EID=os.environ.get("BIZZABO_EVENT_ID","754649")
SHEET="1H5A4kSVUCbvgcHQSLqyOLwhNbnSZAYek8D2crd9t0fo"
r=requests.post("https://api.bizzabo.com/api/v2/iam/oauth/token",headers={"Content-Type":"application/x-www-form-urlencoded"},
  data={"grant_type":"client_credentials","client_id":CID,"client_secret":CS,"account_id":AID,"audience":"https://api.bizzabo.com/api"},timeout=30)
tok=r.json()["access_token"]
regs=[];page=0
while True:
    q=requests.get(f"https://api.bizzabo.com/v2/events/{EID}/registrations",headers={"Authorization":f"Bearer {tok}"},params={"size":100,"page":page},timeout=30)
    c=q.json().get("content",[]); regs+=c
    if not c or len(c)<100: break
    page+=1
acts={(x.get("ticketId") or "").strip() for x in csv.DictReader(io.StringIO(requests.get(f"https://docs.google.com/spreadsheets/d/{SHEET}/export?format=csv",timeout=30).text)) if (x.get("status") or "").strip().lower()=="activated"}
valid=[x for x in regs if (x.get("validity") or "").lower()=="valid"]
def vip(r):
    t=(r.get("ticketName") or "").lower(); p=(r.get("promoCode") or "").lower()
    if "kid" in t or "teen" in t: return False
    if any(k in t for k in ("speaker","hexagon","friends of vishen","crew")): return False
    if any(k in t for k in ("special guest","vip guest","vip media","vip")): return True
    return p in ("vipguest","vipmedia")
def reg_(r):
    t=(r.get("ticketName") or "").lower()
    if "kid" in t or "teen" in t: return False
    return ("adult" in t or "standard" in t) and not vip(r)
def dur(n):
    n=(n or "").lower()
    if "2 week" in n: return "2 weeks"
    if "1 week" in n: return "1 week"
    if "3 day" in n: return "3 days"
    return "other"
print("=== 4) VIP & REGULAR (con 'other' y combinado) ===")
tot_t=tot_a=0
for lab,fn in (("VIP",vip),("Regular",reg_)):
    g=[x for x in valid if fn(x)]; a=[x for x in g if str(x.get("id")) in acts]
    tot_t+=len(g); tot_a+=len(a)
    print(f"{lab}: {len(a)}/{len(g)} ({len(a)/len(g)*100:.0f}%)")
    for d in ("1 week","2 weeks","3 days","other"):
        s=[x for x in g if dur(x.get("ticketName"))==d]
        sa=[x for x in s if str(x.get("id")) in acts]
        if s:
            names=Counter(x.get("ticketName") for x in s) if d=="other" else None
            print(f"   {d:8s}: {len(sa)}/{len(s)}" + (f"  → {dict(names)}" if names else ""))
print(f"COMBINED VIP+Regular: {tot_a}/{tot_t} ({tot_a/tot_t*100:.0f}%)")

print("\n=== 5) NO-SHOWS con % ===")
ns=[x for x in valid if str(x.get("id")) not in acts]
comp=[x for x in ns if float(x.get("charge") or 0)==0]; paid=[x for x in ns if float(x.get("charge") or 0)>0]
print(f"total no-shows: {len(ns)} = {len(ns)/len(valid)*100:.1f}% de {len(valid)} valid tickets")
print(f"  comped: {len(comp)} ({len(comp)/len(ns)*100:.0f}% de los no-shows)")
print(f"  paid:   {len(paid)} ({len(paid)/len(ns)*100:.0f}% de los no-shows)")
print("  show-up rate global: %.1f%%" % ((len(valid)-len(ns))/len(valid)*100))
print("  TOP 5 promo codes por no-shows:")
for c,n in Counter((x.get("promoCode") or "").strip() for x in ns if (x.get("promoCode") or "").strip()).most_common(5):
    tot_code=sum(1 for x in valid if (x.get("promoCode") or "").strip()==c)
    print(f"     {c}: {n} no-shows (de {tot_code} tickets con ese código = {n/tot_code*100:.0f}%)")

print("\n=== 7) PARTIES: tickets + revenue ===")
party=[x for x in valid if "party" in (x.get("ticketName") or "").lower()]
byname=Counter(x.get("ticketName") for x in party)
tot_rev=0
for name,n in byname.most_common():
    rev=sum(float(x.get("charge") or 0) for x in party if x.get("ticketName")==name)/100
    tot_rev+=rev
    act=sum(1 for x in party if x.get("ticketName")==name and str(x.get("id")) in acts)
    print(f"  {name}: {n} tickets · ${rev:,.0f} · {act} activados")
print(f"  TOTAL: {len(party)} tickets · ${tot_rev:,.0f} revenue")
# también los invalid (cancelados) por si acaso
pinv=[x for x in regs if "party" in (x.get("ticketName") or "").lower() and (x.get("validity") or "").lower()!="valid"]
print(f"  (party tickets cancelados/inválidos: {len(pinv)})")
