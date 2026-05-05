#!/usr/bin/env python3
"""
Mindvalley U 2026 – Dashboard Generator
Fetches live data from Bizzabo and writes index.html
"""

import os, re, json, requests
from datetime import datetime, timezone, timedelta

# ── Credentials (from environment / GitHub Secrets) ──────────────────────────
CLIENT_ID     = os.environ["BIZZABO_CLIENT_ID"]
CLIENT_SECRET = os.environ["BIZZABO_CLIENT_SECRET"]
ACCOUNT_ID    = os.environ.get("BIZZABO_ACCOUNT_ID", "129966")
EVENT_ID      = os.environ.get("BIZZABO_EVENT_ID",   "754649")  # MVU 2026
EVENT_ID_2025 = os.environ.get("BIZZABO_EVENT_ID_2025", "619441")  # MVU 2025 (for YoY)
CAPACITY      = 70   # pax per youth category per week

# ── Auth ──────────────────────────────────────────────────────────────────────
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

# ── Fetch all registrations ───────────────────────────────────────────────────
def fetch_all(token, event_id=EVENT_ID):
    regs, page = [], 0
    while True:
        r = requests.get(
            f"https://api.bizzabo.com/v2/events/{event_id}/registrations",
            headers={"Authorization": f"Bearer {token}"},
            params={"size": 100, "page": page},
            timeout=30,
        )
        r.raise_for_status()
        data    = r.json()
        content = data.get("content", [])
        regs.extend(content)
        total_pages = data.get("totalPages", None)
        print(f"  page {page+1}/{total_pages or '?'} – {len(content)} records (running total: {len(regs)})")
        # Stop if: empty page, less than full page, or totalPages says we're done
        if len(content) == 0:
            break
        if total_pages and page >= total_pages - 1:
            break
        if len(content) < 100:
            break
        page += 1
    print(f"  Fetched {len(regs)} total registrations across {page+1} pages")
    return regs

# ── Date parsing ──────────────────────────────────────────────────────────────
def parse_date(s):
    if not s:
        return None
    try:
        s = s.replace(".000", "")
        s = re.sub(r"\+(\d{2})(\d{2})$", r"+\1:\2", s)
        return datetime.fromisoformat(s)
    except Exception:
        return None

# ── Paid vs Comped helper ─────────────────────────────────────────────────────
def is_paid(r):
    """A valid ticket is 'paid' if Bizzabo's top-level `price` field is > 0
    (it's stored in cents, e.g. 119900 = $1199.00). 0 / missing = comped."""
    try:
        return float(r.get("price") or 0) > 0
    except (TypeError, ValueError):
        return False

# ── Monthly buckets for YoY charts ────────────────────────────────────────────
_MONTH_NAMES = ['', 'Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']

def monthly_buckets_full(event_year):
    """
    Returns 12 (label, start, end) tuples covering the full sales season:
    Aug (event_year - 1) through Jul (event_year).
    """
    out = []
    y, m = event_year - 1, 8
    for _ in range(12):
        start = datetime(y, m, 1, tzinfo=timezone.utc)
        ny, nm = (y + 1, 1) if m == 12 else (y, m + 1)
        end = datetime(ny, nm, 1, tzinfo=timezone.utc) - timedelta(seconds=1)
        out.append((_MONTH_NAMES[m], start, end))
        y, m = ny, nm
    return out

def per_month_count(records, buckets, date_getter, clamp_end=None):
    """
    For each (label, start, end) bucket, count records whose date falls in it.
    If `clamp_end` is provided (e.g. today), buckets that start after it return None
    (so the chart can break the line for future/missing months) and the bucket
    containing clamp_end is truncated at it.
    """
    parsed = [date_getter(r) for r in records]
    out = []
    for (_, start, end) in buckets:
        if clamp_end is not None and start > clamp_end:
            out.append(None)
            continue
        eff_end = min(end, clamp_end) if clamp_end is not None else end
        out.append(sum(1 for d in parsed if d and start <= d <= eff_end))
    return out

# ── Week assignment helper ────────────────────────────────────────────────────
def get_week(reg):
    """Return 'Week 1', 'Week 2', 'Both Weeks', or None."""
    props = reg.get("properties", {})
    # Properties can be a dict (key→value) or a list
    if isinstance(props, dict):
        val = (props.get("when_are_you_joining") or "").strip()
        if not val:
            # Try alternate keys
            for k, v in props.items():
                if "when" in k.lower() and "joining" in k.lower():
                    val = str(v).strip()
                    break
        if val:
            val_lower = val.lower()
            if "both" in val_lower or ("week 1" in val_lower and "week 2" in val_lower):
                return "Both Weeks"
            elif "week 1" in val_lower:
                return "Week 1"
            elif "week 2" in val_lower:
                return "Week 2"
            return val  # return raw value if doesn't match patterns
    elif isinstance(props, list):
        for prop in props:
            if not isinstance(prop, dict):
                continue
            sys_id = (prop.get("systemFieldId") or "").upper()
            label  = (prop.get("label") or "").upper()
            if "WHEN_ARE_YOU_JOINING" in sys_id or "WHEN_ARE_YOU_JOINING" in label:
                val = (prop.get("value") or "").strip()
                if val:
                    return val
    return None

# ── Main compute ─────────────────────────────────────────────────────────────
def compute(regs):
    now   = datetime.now(tz=timezone.utc)
    d7    = now - timedelta(days=7)
    d24   = now - timedelta(hours=24)

    valid     = [r for r in regs if r.get("validity","").lower() == "valid"]
    refunded  = [r for r in regs if (r.get("paymentStatus") or "").lower() == "refunded"]
    unassigned_tickets = [r for r in valid if (r.get("formSubmissionStatus") or "").lower() == "unassigned"]
    paid      = [r for r in valid if is_paid(r)]
    comped    = [r for r in valid if not is_paid(r)]

    def recent(lst, since):
        return sum(1 for r in lst if (d := parse_date(r.get("registrationDate"))) and d >= since)

    # ── hero counts ──
    hero = {
        "valid_total":    len(valid),
        "valid_7d":       recent(valid, d7),
        "valid_24h":      recent(valid, d24),
        "paid_total":     len(paid),
        "paid_7d":        recent(paid, d7),
        "paid_24h":       recent(paid, d24),
        "comped_total":   len(comped),
        "refund_total":   len(refunded),
        "refund_7d":      recent(refunded, d7),
        "refund_24h":     recent(refunded, d24),
        "unassigned":     len(unassigned_tickets),
    }

    # ── category builder ──
    def cat_stats(lst, keyword):
        kw = keyword.lower()
        hits = [r for r in lst if kw in (r.get("ticketName") or "").lower()]
        w1 = w2 = unass = 0
        for r in hits:
            w = get_week(r)
            if w in ("Week 1", "Both Weeks"):  w1   += 1
            if w in ("Week 2", "Both Weeks"):  w2   += 1
            if not w:                          unass += 1
        return {"total": len(hits), "w1": w1, "w2": w2, "unassigned": unass}

    kids  = cat_stats(valid, "kid")
    teens = cat_stats(valid, "teen")
    vip   = cat_stats(valid, "vip")
    fc    = cat_stats(valid, "first class")
    reg   = cat_stats(valid, "adult")

    # ── capacity semaphore ──
    def semaphore(confirmed, unassigned_count):
        worst = confirmed + unassigned_count
        if worst >= CAPACITY:     return "red",    "At Risk",  f"Overflow risk: +{worst - CAPACITY}"
        elif worst >= 60:         return "yellow",  "Watch",   f"{CAPACITY - worst} spots left in worst case"
        else:                     return "green",   "Safe",    f"{CAPACITY - worst} spots available"

    cap = {
        "kids_w1":  semaphore(kids["w1"],  kids["unassigned"]),
        "kids_w2":  semaphore(kids["w2"],  kids["unassigned"]),
        "teens_w1": semaphore(teens["w1"], teens["unassigned"]),
        "teens_w2": semaphore(teens["w2"], teens["unassigned"]),
    }

    # ── promo code lists ──
    def promo_list(promo_code, lst=valid):
        results = []
        for r in lst:
            if (r.get("promoCode") or "").lower() != promo_code.lower():
                continue
            props = r.get("properties") or {}
            email = props.get("email", "")
            name = f'{props.get("firstName", "")} {props.get("lastName", "")}'.strip()
            week = get_week(r)
            week_label = week if week else "Unassigned"
            is_mv = "@mindvalley" in email.lower()
            results.append({"name": name, "email": email, "week": week_label, "is_mv": is_mv,
                            "ticket": r.get("ticketName", "")})
        return results

    crew_list  = promo_list("MyCrewPass")
    vol_list   = promo_list("Volunteer2Weeks")
    hex_list   = promo_list("hexagon")

    return hero, kids, teens, vip, fc, reg, cap, crew_list, vol_list, hex_list

# ── Year-over-year time series ───────────────────────────────────────────────
def compute_yoy(regs_2026, regs_2025=None):
    """
    Build the per-month paid-tickets series for the YoY chart, comparing
    2025 (event 619441) and 2026 (event 754649). Each line covers Aug of the
    prior year through Jul of the event year; the 2026 line clamps at today
    so future months render as a gap rather than zero.
    """
    today = datetime.now(tz=timezone.utc)

    paid_2026 = [r for r in regs_2026 if r.get("validity","").lower() == "valid" and is_paid(r)]
    paid_2025 = [r for r in regs_2025 if r.get("validity","").lower() == "valid" and is_paid(r)] if regs_2025 else []

    buckets_2026 = monthly_buckets_full(2026)
    buckets_2025 = monthly_buckets_full(2025)

    reg_date = lambda r: parse_date(r.get("registrationDate"))

    # Apples-to-apples "to date" totals: Aug 1 → today's month/day in each event year
    def to_date_count(records, event_year, date_getter):
        start = datetime(event_year - 1, 8, 1, tzinfo=timezone.utc)
        end   = datetime(event_year, today.month, today.day, 23, 59, 59, tzinfo=timezone.utc)
        return sum(1 for r in records if (d := date_getter(r)) and start <= d <= end)

    return {
        "labels":        [b[0] for b in buckets_2026],
        "paid_2025":         per_month_count(paid_2025, buckets_2025, reg_date),
        "paid_2026":         per_month_count(paid_2026, buckets_2026, reg_date, clamp_end=today),
        "paid_2025_to_date": to_date_count(paid_2025, 2025, reg_date),
        "paid_2026_to_date": to_date_count(paid_2026, 2026, reg_date),
        "available_2025":    bool(regs_2025),
    }

# ── HTML generation ───────────────────────────────────────────────────────────
def render_html(hero, kids, teens, vip, fc, reg, cap, crew_list, vol_list, hex_list, yoy=None):
    now_str = datetime.now(tz=timezone.utc).strftime("%B %d, %Y at %H:%M UTC")
    yoy_json = json.dumps(yoy or {"labels":[],"paid_2025":[],"paid_2026":[],"available_2025":False})

    def _delta(prev, curr):
        if prev <= 0:
            return ('<div class="chart-delta">—</div>' if curr == 0 else '')
        pct = (curr - prev) / prev * 100
        cls = "up" if curr >= prev else "down"
        arrow = "▲" if curr >= prev else "▼"
        return f'<div class="chart-delta {cls}">{arrow} {pct:+.1f}% YoY</div>'

    paid_2025_total = (yoy.get("paid_2025_to_date", 0) if yoy else 0) if (yoy and yoy.get("available_2025")) else 0
    paid_2026_total = (yoy.get("paid_2026_to_date") if yoy else None) or hero.get("paid_total", 0)
    paid_delta = _delta(paid_2025_total, paid_2026_total) if (yoy and yoy.get("available_2025")) else ""

    def cap_card(emoji, name, week_label, confirmed, unassigned_count, cap_tuple):
        level, status, subtitle = cap_tuple
        worst = confirmed + unassigned_count
        overflow = worst - CAPACITY
        overflow_html = (
            f'<strong style="color:#f87171">+{overflow}</strong>'
            if overflow > 0 else
            f'<strong style="color:#{"34d399" if level=="green" else "fbbf24"}">{CAPACITY - worst} spots</strong>'
        )
        note_label = "Overflow risk:" if overflow > 0 else "Buffer:"
        TRACK = 82
        conf_pct  = min((confirmed / CAPACITY) * TRACK, 100)
        unass_pct = min((unassigned_count / CAPACITY) * TRACK, 100 - conf_pct)
        return f"""
    <div class="cap-card risk-{level}">
      <div class="cap-header">
        <div class="cap-title">{emoji} {name}</div>
        <div class="cap-week-badge">{week_label}</div>
      </div>
      <div class="traffic-light">
        <div class="tl-dot"></div>
        <div class="tl-status">{status}</div>
        <div class="tl-sub">{subtitle}</div>
      </div>
      <div class="cap-bar-wrap">
        <div class="cap-bar-labels"><span>0</span><span>Capacity</span></div>
        <div class="cap-bar-track">
          <div class="cap-bar-confirmed"  style="width:{conf_pct:.1f}%"></div>
          <div class="cap-bar-unassigned" style="left:{conf_pct:.1f}%;width:{unass_pct:.1f}%"></div>
          <div class="cap-bar-marker"     style="left:{TRACK}%"></div>
        </div>
      </div>
      <div class="cap-numbers">
        <div class="cap-num-item"><div class="cap-num-val">{confirmed}</div><div class="cap-num-label">Confirmed</div></div>
        <div class="cap-num-item"><div class="cap-num-val">{unassigned_count}</div><div class="cap-num-label">No Week Sel.</div></div>
        <div class="cap-num-item worst"><div class="cap-num-val">{worst}</div><div class="cap-num-label">Worst Case</div></div>
      </div>
      <div class="cap-capacity-note">Capacity: <strong>{CAPACITY}</strong> · {note_label} {overflow_html}</div>
    </div>"""

    def cat_card(emoji, label, stats):
        return f"""
    <div class="cat-card">
      <div class="cat-icon">{emoji}</div>
      <div class="cat-label">{label}</div>
      <div class="cat-value" data-target="{stats['total']}">0</div>
      <div class="cat-breakdown">
        <div class="item"><div class="item-val" data-target="{stats['w1']}">0</div><div class="item-label">Week 1</div></div>
        <div class="item"><div class="item-val" data-target="{stats['w2']}">0</div><div class="item-label">Week 2</div></div>
        <div class="item"><div class="item-val" data-target="{stats['unassigned']}">0</div><div class="item-label">No Week Selected</div></div>
      </div>
    </div>"""

    def promo_section(emoji, title, plist, flag_non_mv=False):
        if not plist:
            return f"""
  <div class="section-label">{emoji} {title}</div>
  <div class="promo-empty">No registrations yet</div>"""
        rows = ""
        for p in plist:
            flag = ' <span class="flag-ext">external</span>' if (flag_non_mv and not p["is_mv"]) else ""
            rows += f"""
        <tr>
          <td>{p["name"]}{flag}</td>
          <td>{p["email"]}</td>
          <td>{p["week"]}</td>
        </tr>"""
        return f"""
  <div class="section-label">{emoji} {title} <span class="section-count">{len(plist)}</span></div>
  <div class="promo-table-wrap">
    <table class="promo-table">
      <thead><tr><th>Name</th><th>Email</th><th>Weeks</th></tr></thead>
      <tbody>{rows}
      </tbody>
    </table>
  </div>"""

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Mindvalley U 2026 — Registration Dashboard</title>
<style>
  *,*::before,*::after{{box-sizing:border-box;margin:0;padding:0}}
  :root{{
    --bg:#0b0a1a;--card:#14122a;--card-border:#2a2650;
    --gold:#d4a843;--gold-dim:#a07e30;
    --purple:#7c3aed;--purple-light:#a78bfa;
    --text:#e8e4f0;--text-dim:#9a93b0;
    --green:#34d399;--red:#f87171;--orange:#fb923c;
  }}
  body{{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;background:var(--bg);color:var(--text);min-height:100vh;overflow-x:hidden;position:relative}}
  .orb{{position:fixed;border-radius:50%;filter:blur(120px);opacity:.25;pointer-events:none;z-index:0}}
  .orb-1{{width:600px;height:600px;background:radial-gradient(circle,#7c3aed,transparent);top:-200px;left:-100px;animation:float1 18s ease-in-out infinite}}
  .orb-2{{width:500px;height:500px;background:radial-gradient(circle,#d4a843,transparent);bottom:-150px;right:-100px;animation:float2 22s ease-in-out infinite}}
  .orb-3{{width:400px;height:400px;background:radial-gradient(circle,#6d28d9,transparent);top:40%;left:50%;animation:float3 15s ease-in-out infinite}}
  @keyframes float1{{0%,100%{{transform:translate(0,0)}}50%{{transform:translate(80px,60px)}}}}
  @keyframes float2{{0%,100%{{transform:translate(0,0)}}50%{{transform:translate(-60px,-80px)}}}}
  @keyframes float3{{0%,100%{{transform:translate(-50%,-50%)}}50%{{transform:translate(-30%,-30%)}}}}
  .container{{max-width:1200px;margin:0 auto;padding:32px 20px;position:relative;z-index:1}}
  header{{text-align:center;margin-bottom:40px}}
  header h1{{font-size:2.2rem;font-weight:800;background:linear-gradient(135deg,var(--gold),var(--purple-light));-webkit-background-clip:text;-webkit-text-fill-color:transparent;background-clip:text;letter-spacing:-.02em}}
  header p{{color:var(--text-dim);margin-top:6px;font-size:.95rem}}
  .timestamp{{display:inline-block;margin-top:10px;padding:4px 14px;border-radius:20px;background:rgba(124,58,237,.15);border:1px solid rgba(124,58,237,.3);font-size:.8rem;color:var(--purple-light)}}
  .section-label{{font-size:1.1rem;font-weight:700;color:var(--gold);margin:32px 0 16px;text-transform:uppercase;letter-spacing:.08em;display:flex;align-items:center;gap:8px}}
  .section-label::after{{content:'';flex:1;height:1px;background:linear-gradient(90deg,var(--gold-dim),transparent)}}
  .hero-grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:16px;margin-bottom:12px}}
  .hero-card{{background:var(--card);border:1px solid var(--card-border);border-radius:16px;padding:24px;position:relative;overflow:hidden;transition:transform .2s,box-shadow .2s}}
  .hero-card:hover{{transform:translateY(-3px);box-shadow:0 12px 40px rgba(124,58,237,.15)}}
  .hero-card::before{{content:'';position:absolute;top:0;left:0;right:0;height:3px;border-radius:16px 16px 0 0}}
  .hero-card.valid::before{{background:linear-gradient(90deg,var(--green),#059669)}}
  .hero-card.paid::before{{background:linear-gradient(90deg,var(--purple-light),var(--purple))}}
  .hero-card.refund::before{{background:linear-gradient(90deg,var(--red),#dc2626)}}
  .hero-card.unassigned::before{{background:linear-gradient(90deg,var(--orange),#ea580c)}}
  .hero-icon{{font-size:1.8rem;margin-bottom:8px}}
  .hero-label{{font-size:.85rem;color:var(--text-dim);font-weight:500;text-transform:uppercase;letter-spacing:.06em}}
  .hero-value{{font-size:2.8rem;font-weight:800;line-height:1.1;margin:6px 0}}
  .hero-card.valid .hero-value{{color:var(--green)}}
  .hero-card.paid .hero-value{{color:var(--purple-light)}}
  .hero-card.refund .hero-value{{color:var(--red)}}
  .hero-card.unassigned .hero-value{{color:var(--orange)}}
  .hero-sub{{display:flex;gap:16px;margin-top:10px;font-size:.82rem;color:var(--text-dim)}}
  .hero-sub span{{display:flex;align-items:center;gap:4px}}
  .hero-sub .num{{font-weight:700;color:var(--text)}}
  .cat-grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:16px}}
  .cat-card{{background:var(--card);border:1px solid var(--card-border);border-radius:16px;padding:22px;position:relative;overflow:hidden;transition:transform .2s,box-shadow .2s}}
  .cat-card:hover{{transform:translateY(-3px);box-shadow:0 12px 40px rgba(212,168,67,.1)}}
  .cat-card::before{{content:'';position:absolute;top:0;left:0;right:0;height:3px;background:linear-gradient(90deg,var(--gold),var(--purple));border-radius:16px 16px 0 0}}
  .cat-icon{{font-size:1.6rem;margin-bottom:6px}}
  .cat-label{{font-size:.82rem;color:var(--text-dim);font-weight:500;text-transform:uppercase;letter-spacing:.06em}}
  .cat-value{{font-size:2.4rem;font-weight:800;color:var(--gold);line-height:1.1;margin:4px 0 12px}}
  .cat-breakdown{{display:grid;grid-template-columns:1fr 1fr 1fr;gap:6px}}
  .cat-breakdown .item{{text-align:center;padding:6px 0;border-radius:8px;background:rgba(255,255,255,.03)}}
  .cat-breakdown .item-val{{font-size:1.15rem;font-weight:700;color:var(--text)}}
  .cat-breakdown .item-label{{font-size:.7rem;color:var(--text-dim);text-transform:uppercase;letter-spacing:.04em;margin-top:1px}}
  .cap-grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:16px;margin-bottom:12px}}
  .cap-card{{background:var(--card);border:1px solid var(--card-border);border-radius:16px;padding:22px;position:relative;overflow:hidden;transition:transform .2s,box-shadow .2s}}
  .cap-card:hover{{transform:translateY(-3px)}}
  .cap-card::before{{content:'';position:absolute;top:0;left:0;right:0;height:3px;border-radius:16px 16px 0 0}}
  .cap-card.risk-green::before{{background:linear-gradient(90deg,#34d399,#059669)}}
  .cap-card.risk-yellow::before{{background:linear-gradient(90deg,#fbbf24,#d97706)}}
  .cap-card.risk-red::before{{background:linear-gradient(90deg,#f87171,#dc2626)}}
  .cap-header{{display:flex;align-items:center;justify-content:space-between;margin-bottom:14px}}
  .cap-title{{font-size:.9rem;font-weight:700;color:var(--text);letter-spacing:.03em}}
  .cap-week-badge{{font-size:.72rem;font-weight:700;padding:3px 10px;border-radius:20px;letter-spacing:.05em;text-transform:uppercase}}
  .cap-card.risk-green .cap-week-badge{{background:rgba(52,211,153,.15);color:#34d399;border:1px solid rgba(52,211,153,.3)}}
  .cap-card.risk-yellow .cap-week-badge{{background:rgba(251,191,36,.15);color:#fbbf24;border:1px solid rgba(251,191,36,.3)}}
  .cap-card.risk-red .cap-week-badge{{background:rgba(248,113,113,.15);color:#f87171;border:1px solid rgba(248,113,113,.3)}}
  .traffic-light{{display:flex;align-items:center;gap:10px;margin-bottom:16px}}
  .tl-dot{{width:14px;height:14px;border-radius:50%;flex-shrink:0;box-shadow:0 0 8px currentColor}}
  .risk-green .tl-dot{{background:#34d399;color:#34d399}}
  .risk-yellow .tl-dot{{background:#fbbf24;color:#fbbf24;animation:pulse-yellow 2s ease-in-out infinite}}
  .risk-red .tl-dot{{background:#f87171;color:#f87171;animation:pulse-red 1.4s ease-in-out infinite}}
  @keyframes pulse-yellow{{0%,100%{{box-shadow:0 0 6px #fbbf24}}50%{{box-shadow:0 0 16px #fbbf24}}}}
  @keyframes pulse-red{{0%,100%{{box-shadow:0 0 6px #f87171}}50%{{box-shadow:0 0 20px #f87171}}}}
  .tl-status{{font-size:.82rem;font-weight:700;text-transform:uppercase;letter-spacing:.08em}}
  .risk-green .tl-status{{color:#34d399}}
  .risk-yellow .tl-status{{color:#fbbf24}}
  .risk-red .tl-status{{color:#f87171}}
  .tl-sub{{font-size:.75rem;color:var(--text-dim);margin-left:auto}}
  .cap-bar-wrap{{margin-bottom:14px}}
  .cap-bar-labels{{display:flex;justify-content:space-between;font-size:.72rem;color:var(--text-dim);margin-bottom:5px}}
  .cap-bar-track{{width:100%;height:12px;border-radius:6px;background:rgba(255,255,255,.06);position:relative;overflow:visible}}
  .cap-bar-confirmed{{height:100%;border-radius:6px 0 0 6px;position:absolute;left:0;top:0}}
  .cap-bar-unassigned{{height:100%;position:absolute;top:0;background-image:repeating-linear-gradient(45deg,transparent,transparent 3px,rgba(0,0,0,.25) 3px,rgba(0,0,0,.25) 6px)}}
  .risk-green .cap-bar-confirmed{{background:linear-gradient(90deg,#34d399,#059669)}}
  .risk-green .cap-bar-unassigned{{background-color:rgba(52,211,153,.35)}}
  .risk-yellow .cap-bar-confirmed{{background:linear-gradient(90deg,#fbbf24,#d97706)}}
  .risk-yellow .cap-bar-unassigned{{background-color:rgba(251,191,36,.35)}}
  .risk-red .cap-bar-confirmed{{background:linear-gradient(90deg,#f87171,#dc2626)}}
  .risk-red .cap-bar-unassigned{{background-color:rgba(248,113,113,.35)}}
  .cap-bar-marker{{position:absolute;top:-4px;height:20px;width:2px;background:var(--gold);border-radius:2px;z-index:2}}
  .cap-bar-marker::after{{content:'70';position:absolute;top:-16px;left:50%;transform:translateX(-50%);font-size:.65rem;color:var(--gold);font-weight:700;white-space:nowrap}}
  .cap-numbers{{display:grid;grid-template-columns:1fr 1fr 1fr;gap:6px;text-align:center}}
  .cap-num-item{{padding:6px 2px;border-radius:8px;background:rgba(255,255,255,.03)}}
  .cap-num-val{{font-size:1.2rem;font-weight:800;color:var(--text)}}
  .cap-num-label{{font-size:.68rem;color:var(--text-dim);text-transform:uppercase;letter-spacing:.04em;margin-top:1px}}
  .cap-num-item.worst .cap-num-val{{font-size:1.35rem}}
  .risk-green .cap-num-item.worst .cap-num-val{{color:#34d399}}
  .risk-yellow .cap-num-item.worst .cap-num-val{{color:#fbbf24}}
  .risk-red .cap-num-item.worst .cap-num-val{{color:#f87171}}
  .cap-capacity-note{{text-align:center;font-size:.72rem;color:var(--text-dim);margin-top:10px}}
  .cap-capacity-note strong{{color:var(--gold)}}
  .promo-table-wrap{{overflow-x:auto;margin-bottom:8px}}
  .promo-table{{width:100%;border-collapse:collapse;font-size:.85rem}}
  .promo-table th{{text-align:left;padding:8px 12px;color:var(--text-dim);font-size:.72rem;text-transform:uppercase;letter-spacing:.05em;border-bottom:1px solid rgba(255,255,255,.08)}}
  .promo-table td{{padding:8px 12px;border-bottom:1px solid rgba(255,255,255,.04);color:var(--text)}}
  .promo-table tr:hover td{{background:rgba(255,255,255,.03)}}
  .promo-empty{{text-align:center;padding:24px;color:var(--text-dim);font-size:.9rem;background:var(--card);border-radius:16px;border:1px solid rgba(255,255,255,.06);margin-bottom:8px}}
  .chart-grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(420px,1fr));gap:16px;margin-bottom:12px}}
  .chart-card{{background:var(--card);border:1px solid var(--card-border);border-radius:16px;padding:22px;position:relative;overflow:hidden;transition:transform .2s,box-shadow .2s}}
  .chart-card:hover{{transform:translateY(-3px);box-shadow:0 12px 40px rgba(124,58,237,.12)}}
  .chart-card::before{{content:'';position:absolute;top:0;left:0;right:0;height:3px;background:linear-gradient(90deg,var(--purple),var(--gold));border-radius:16px 16px 0 0}}
  .chart-header{{display:flex;justify-content:space-between;align-items:center;margin-bottom:14px;flex-wrap:wrap;gap:8px}}
  .chart-title{{font-size:.95rem;font-weight:700;color:var(--text);letter-spacing:.02em}}
  .chart-legend{{display:flex;gap:14px;font-size:.72rem;color:var(--text-dim)}}
  .chart-legend .leg{{display:flex;align-items:center;gap:6px}}
  .chart-legend .swatch{{width:16px;height:3px;border-radius:2px}}
  .chart-legend .swatch.s2025{{background:var(--purple-light);opacity:.7}}
  .chart-legend .swatch.s2026{{background:var(--gold)}}
  .chart-svg{{width:100%;height:auto;display:block;overflow:visible}}
  .chart-grid-line{{stroke:rgba(255,255,255,.06);stroke-width:1}}
  .chart-axis-label{{fill:var(--text-dim);font-size:10px;font-family:-apple-system,sans-serif}}
  .chart-line-2025{{fill:none;stroke:var(--purple-light);stroke-width:2;stroke-linecap:round;stroke-linejoin:round;opacity:.7;stroke-dasharray:4 3}}
  .chart-line-2026{{fill:none;stroke:var(--gold);stroke-width:2.5;stroke-linecap:round;stroke-linejoin:round;filter:drop-shadow(0 0 4px rgba(212,168,67,.4))}}
  .chart-dot-2025{{fill:var(--purple-light);opacity:.7}}
  .chart-dot-2026{{fill:var(--gold)}}
  .chart-totals{{display:flex;justify-content:space-around;margin-top:14px;padding-top:14px;border-top:1px solid rgba(255,255,255,.06)}}
  .chart-total{{text-align:center}}
  .chart-total-label{{font-size:.7rem;color:var(--text-dim);text-transform:uppercase;letter-spacing:.05em}}
  .chart-total-val{{font-size:1.4rem;font-weight:800;margin-top:2px}}
  .chart-total.t2025 .chart-total-val{{color:var(--purple-light)}}
  .chart-total.t2026 .chart-total-val{{color:var(--gold)}}
  .chart-delta{{font-size:.7rem;margin-top:2px;font-weight:600}}
  .chart-delta.up{{color:var(--green)}}
  .chart-delta.down{{color:var(--red)}}
  .chart-empty{{text-align:center;padding:32px 16px;color:var(--text-dim);font-size:.85rem}}
  .section-count{{display:inline-block;background:var(--purple);color:#fff;font-size:.75rem;padding:2px 8px;border-radius:10px;margin-left:6px;font-weight:700}}
  .flag-ext{{display:inline-block;background:#f87171;color:#fff;font-size:.65rem;padding:1px 6px;border-radius:4px;margin-left:6px;font-weight:600;vertical-align:middle}}
  @media(max-width:600px){{
    header h1{{font-size:1.6rem}}.hero-value{{font-size:2rem}}.cat-value{{font-size:1.8rem}}
    .hero-grid,.cat-grid,.cap-grid{{grid-template-columns:1fr}}
  }}
</style>
</head>
<body>
<div class="orb orb-1"></div><div class="orb orb-2"></div><div class="orb orb-3"></div>
<div class="container">
  <header>
    <h1>Mindvalley U 2026</h1>
    <p>Registration Dashboard — Tallinn, Estonia</p>
    <div class="timestamp">Data snapshot: {now_str}</div>
  </header>

  <div class="section-label">Ticket Overview</div>
  <div class="hero-grid">
    <div class="hero-card valid">
      <div class="hero-icon">🎟️</div>
      <div class="hero-label">Valid Tickets</div>
      <div class="hero-value" data-target="{hero['valid_total']}">0</div>
      <div class="hero-sub">
        <span>7d: <span class="num" data-target="{hero['valid_7d']}">0</span></span>
        <span>24h: <span class="num" data-target="{hero['valid_24h']}">0</span></span>
      </div>
    </div>
    <div class="hero-card paid">
      <div class="hero-icon">💳</div>
      <div class="hero-label">Paid Tickets</div>
      <div class="hero-value" data-target="{hero['paid_total']}">0</div>
      <div class="hero-sub">
        <span>7d: <span class="num" data-target="{hero['paid_7d']}">0</span></span>
        <span>24h: <span class="num" data-target="{hero['paid_24h']}">0</span></span>
      </div>
    </div>
    <div class="hero-card refund">
      <div class="hero-icon">🔄</div>
      <div class="hero-label">Refunded Tickets</div>
      <div class="hero-value" data-target="{hero['refund_total']}">0</div>
      <div class="hero-sub">
        <span>7d: <span class="num" data-target="{hero['refund_7d']}">0</span></span>
        <span>24h: <span class="num" data-target="{hero['refund_24h']}">0</span></span>
      </div>
    </div>
    <div class="hero-card unassigned">
      <div class="hero-icon">🔔</div>
      <div class="hero-label">Unassigned Tickets</div>
      <div class="hero-value" data-target="{hero['unassigned']}">0</div>
    </div>
  </div>

  <div class="section-label">Year-over-Year Trend <span style="font-size:.75rem;font-weight:400;color:var(--text-dim);text-transform:none;letter-spacing:0;margin-left:8px">From sales open (Aug) through today · 2025 vs 2026</span></div>
  <div class="chart-grid">
    <div class="chart-card">
      <div class="chart-header">
        <div class="chart-title">💳 Paid Tickets — Monthly</div>
        <div class="chart-legend">
          <div class="leg"><span class="swatch s2025"></span>2025</div>
          <div class="leg"><span class="swatch s2026"></span>2026</div>
        </div>
      </div>
      <svg class="chart-svg" id="chartPaid" viewBox="0 0 600 280" preserveAspectRatio="xMidYMid meet"></svg>
      <div class="chart-totals">
        <div class="chart-total t2025">
          <div class="chart-total-label">2025 to date</div>
          <div class="chart-total-val">{paid_2025_total}</div>
        </div>
        <div class="chart-total t2026">
          <div class="chart-total-label">2026 to date</div>
          <div class="chart-total-val">{paid_2026_total}</div>
          {paid_delta}
        </div>
      </div>
    </div>
  </div>

  <div class="section-label">Kids &amp; Teens Program</div>
  <div class="cat-grid">
    {cat_card("🧒", "Kids (6-12)", kids)}
    {cat_card("🧑", "Teens (13-17)", teens)}
  </div>

  <div class="section-label">⚠️ Kids &amp; Teens Program — Capacity Risk <span style="font-size:.75rem;font-weight:400;color:var(--text-dim);text-transform:none;letter-spacing:0;margin-left:8px">Cap. {CAPACITY} pax / category / week</span></div>
  <div class="cap-grid">
    {cap_card("🧒", "Kids",  "Week 1", kids["w1"],  kids["unassigned"],  cap["kids_w1"])}
    {cap_card("🧒", "Kids",  "Week 2", kids["w2"],  kids["unassigned"],  cap["kids_w2"])}
    {cap_card("🧑", "Teens", "Week 1", teens["w1"], teens["unassigned"], cap["teens_w1"])}
    {cap_card("🧑", "Teens", "Week 2", teens["w2"], teens["unassigned"], cap["teens_w2"])}
  </div>

  <div class="section-label">Ticket Breakdown</div>
  <div class="cat-grid">
    {cat_card("👑", "VIP", vip)}
    {cat_card("💎", "First Class", fc)}
    {cat_card("🎫", "Regular (Adult)", reg)}
  </div>


</div>

<script>
  function animateCounters() {{
    const els = document.querySelectorAll('[data-target]');
    els.forEach((el, i) => {{
      const target = parseInt(el.dataset.target, 10);
      if (target === 0) {{ el.textContent = '0'; return; }}
      const duration = 1400;
      const start = performance.now();
      const delay = i * 40;
      setTimeout(() => {{
        function tick(now) {{
          const elapsed = now - start - delay;
          if (elapsed < 0) {{ requestAnimationFrame(tick); return; }}
          const progress = Math.min(elapsed / duration, 1);
          const eased = 1 - Math.pow(1 - progress, 3);
          el.textContent = Math.round(eased * target).toLocaleString();
          if (progress < 1) requestAnimationFrame(tick);
        }}
        requestAnimationFrame(tick);
      }}, delay);
    }});
  }}
  animateCounters();

  // Year-over-year line charts (data injected from Python)
  const yoy = {yoy_json};

  function renderLineChart(svgId, dataA, dataB, labels, available2025) {{
    const svg = document.getElementById(svgId);
    if (!svg) return;
    const W = 600, H = 280;
    const pad = {{ top: 18, right: 18, bottom: 32, left: 44 }};
    const innerW = W - pad.left - pad.right;
    const innerH = H - pad.top - pad.bottom;

    if (!labels || labels.length === 0 || !dataB || dataB.length === 0) {{
      svg.innerHTML = '<text class="chart-axis-label" x="50%" y="50%" text-anchor="middle">No data yet</text>';
      return;
    }}

    const safeNums = arr => (arr || []).filter(v => v != null && !isNaN(v));
    const allVals = [...safeNums(dataB), ...(available2025 ? safeNums(dataA) : [])];
    const maxVal = Math.max(1, ...allVals);
    const niceStep = (() => {{
      const raw = maxVal / 4;
      const mag = Math.pow(10, Math.floor(Math.log10(raw || 1)));
      const norm = raw / mag;
      const nice = norm < 1.5 ? 1 : norm < 3 ? 2 : norm < 7 ? 5 : 10;
      return nice * mag;
    }})();
    const yMax = Math.max(niceStep, Math.ceil(maxVal / niceStep) * niceStep);
    const xStep = innerW / Math.max(1, labels.length - 1);
    const xAt = i => pad.left + i * xStep;
    const yAt = v => pad.top + innerH - (v / yMax) * innerH;

    let parts = [];
    const ticks = Math.max(1, Math.round(yMax / niceStep));
    const fmt = v => Number(v.toFixed(2)).toString();
    for (let i = 0; i <= ticks; i++) {{
      const v = niceStep * i;
      const y = yAt(v);
      parts.push('<line class="chart-grid-line" x1="' + pad.left + '" x2="' + (W - pad.right) + '" y1="' + y + '" y2="' + y + '"/>');
      parts.push('<text class="chart-axis-label" x="' + (pad.left - 8) + '" y="' + (y + 3.5) + '" text-anchor="end">' + fmt(v) + '</text>');
    }}
    labels.forEach((m, i) => {{
      parts.push('<text class="chart-axis-label" x="' + xAt(i) + '" y="' + (H - pad.bottom + 16) + '" text-anchor="middle">' + m + '</text>');
    }});

    function buildPath(data) {{
      let cmd = '', last = false;
      data.forEach((v, i) => {{
        if (v == null || isNaN(v)) {{ last = false; return; }}
        cmd += (last ? 'L' : 'M') + ' ' + xAt(i) + ' ' + yAt(v) + ' ';
        last = true;
      }});
      return cmd.trim();
    }}

    function drawSeries(data, lineClass, dotClass, dotR) {{
      if (!data || data.length === 0) return;
      const path = buildPath(data);
      if (path) parts.push('<path class="' + lineClass + '" d="' + path + '"/>');
      data.forEach((v, i) => {{
        if (v == null || isNaN(v)) return;
        parts.push('<circle class="' + dotClass + '" cx="' + xAt(i) + '" cy="' + yAt(v) + '" r="' + dotR + '"/>');
      }});
    }}

    if (available2025) drawSeries(dataA, 'chart-line-2025', 'chart-dot-2025', 3);
    drawSeries(dataB, 'chart-line-2026', 'chart-dot-2026', 3.5);

    svg.innerHTML = parts.join('');
  }}

  renderLineChart('chartPaid',    yoy.paid_2025,    yoy.paid_2026,    yoy.labels, yoy.available_2025);
</script>
</body>
</html>"""

def render_promo_page(emoji, title, plist, flag_non_mv=False):
    now_str = datetime.now(tz=timezone.utc).strftime("%B %d, %Y at %H:%M UTC")
    if not plist:
        body = '<div class="empty">No registrations yet</div>'
    else:
        rows = ""
        for p in plist:
            if flag_non_mv and not p["is_mv"]:
                badge = ' <span class="flag-ext">external</span>'
            elif flag_non_mv and p["is_mv"]:
                badge = ' <img src="https://www.mindvalley.com/favicon.ico" class="mv-icon" alt="MV">'
            else:
                badge = ""
            rows += f"<tr><td>{p['name']}{badge}</td><td>{p['week']}</td></tr>\n"
        body = f"""<table>
<thead><tr><th>Name</th><th>Weeks</th></tr></thead>
<tbody>{rows}</tbody>
</table>"""

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>{title} — MVU 2026</title>
<style>
:root{{--bg:#0b0a1a;--card:#14122a;--text:#e2e0f0;--text-dim:#7a7793;--gold:#d4a843;--purple:#7c3aed}}
*{{margin:0;padding:0;box-sizing:border-box}}
body{{background:var(--bg);color:var(--text);font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;min-height:100vh}}
.container{{max-width:900px;margin:0 auto;padding:32px 24px}}
h1{{font-size:1.6rem;margin-bottom:4px}}
h1 span.emoji{{font-size:1.4rem;margin-right:8px}}
.count{{display:inline-block;background:var(--purple);color:#fff;font-size:.8rem;padding:3px 10px;border-radius:10px;margin-left:8px;font-weight:700}}
.meta{{font-size:.8rem;color:var(--text-dim);margin-bottom:24px}}
table{{width:100%;border-collapse:collapse;font-size:.9rem}}
th{{text-align:left;padding:10px 14px;color:var(--text-dim);font-size:.75rem;text-transform:uppercase;letter-spacing:.05em;border-bottom:1px solid rgba(255,255,255,.1)}}
td{{padding:10px 14px;border-bottom:1px solid rgba(255,255,255,.04)}}
tr:hover td{{background:rgba(255,255,255,.03)}}
.flag-ext{{display:inline-block;background:#f87171;color:#fff;font-size:.65rem;padding:1px 6px;border-radius:4px;margin-left:6px;font-weight:600;vertical-align:middle}}
.mv-icon{{width:16px;height:16px;margin-left:6px;vertical-align:middle;border-radius:2px}}
.empty{{text-align:center;padding:48px;color:var(--text-dim);font-size:1rem;background:var(--card);border-radius:16px;border:1px solid rgba(255,255,255,.06)}}
</style>
</head>
<body>
<div class="container">
<h1><span class="emoji">{emoji}</span>{title}{f' <span class="count">{len(plist)}</span>' if plist else ''}</h1>
<div class="meta">Mindvalley U 2026 · Data snapshot: {now_str}</div>
{body}
</div>
</body>
</html>"""

# ── Entry point ───────────────────────────────────────────────────────────────
if __name__ == "__main__":
    print("🔐 Authenticating...")
    token = get_token()

    print("📥 Fetching MVU 2026 registrations...")
    regs = fetch_all(token, EVENT_ID)
    print(f"   Total records: {len(regs)}")

    print(f"📥 Fetching MVU 2025 registrations (event {EVENT_ID_2025}) for YoY...")
    try:
        regs_2025 = fetch_all(token, EVENT_ID_2025)
        print(f"   Total 2025 records: {len(regs_2025)}")
    except Exception as e:
        print(f"   ⚠️  Could not fetch 2025 data ({e}); YoY chart will show 2026 only.")
        regs_2025 = None

    print("🧮 Computing metrics...")
    hero, kids, teens, vip, fc, reg, cap, crew_list, vol_list, hex_list = compute(regs)
    yoy = compute_yoy(regs, regs_2025)

    print("✍️  Writing event-dashboards/mvu-2026/index.html...")
    html = render_html(hero, kids, teens, vip, fc, reg, cap, crew_list, vol_list, hex_list, yoy)
    import os
    os.makedirs("event-dashboards/mvu-2026", exist_ok=True)
    with open("event-dashboards/mvu-2026/index.html", "w", encoding="utf-8") as f:
        f.write(html)

    # Generate separate promo pages
    promo_pages = [
        ("Crew - Mindvalley Team", "🎫", "mycrewpass", crew_list, True),
        ("Volunteers", "🙋", "volunteers", vol_list, False),
        ("Hexagon", "🌟", "hexagon", hex_list, False),
    ]
    for name, emoji, slug, plist, flag_non_mv in promo_pages:
        path = f"event-dashboards/mvu-2026/{slug}.html"
        with open(path, "w", encoding="utf-8") as f:
            f.write(render_promo_page(emoji, name, plist, flag_non_mv))
        print(f"   {name}: {len(plist)} registrations -> {path}")

    print("✅ Done!")
    print(f"   Valid tickets: {hero['valid_total']}  (paid:{hero['paid_total']} comped:{hero['comped_total']} refunded:{hero['refund_total']})")
    print(f"   Kids total: {kids['total']}  (W1:{kids['w1']} W2:{kids['w2']} Unass:{kids['unassigned']})")
    print(f"   Teens total: {teens['total']} (W1:{teens['w1']} W2:{teens['w2']} Unass:{teens['unassigned']})")
    if yoy.get("available_2025"):
        print(f"   YoY paid: 2025 to date={yoy.get('paid_2025_to_date', 0)} → 2026 to date={yoy.get('paid_2026_to_date', 0)}")
