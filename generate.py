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
EVENT_ID      = os.environ.get("BIZZABO_EVENT_ID",   "754649")
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

    def recent(lst, since):
        return sum(1 for r in lst if (d := parse_date(r.get("registrationDate"))) and d >= since)

    # ── hero counts ──
    hero = {
        "valid_total":    len(valid),
        "valid_7d":       recent(valid, d7),
        "valid_24h":      recent(valid, d24),
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

    return hero, kids, teens, vip, fc, reg, cap

# ── HTML generation ───────────────────────────────────────────────────────────
def render_html(hero, kids, teens, vip, fc, reg, cap):
    now_str = datetime.now(tz=timezone.utc).strftime("%B %d, %Y at %H:%M UTC")

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
  .hero-card.refund::before{{background:linear-gradient(90deg,var(--red),#dc2626)}}
  .hero-card.unassigned::before{{background:linear-gradient(90deg,var(--orange),#ea580c)}}
  .hero-icon{{font-size:1.8rem;margin-bottom:8px}}
  .hero-label{{font-size:.85rem;color:var(--text-dim);font-weight:500;text-transform:uppercase;letter-spacing:.06em}}
  .hero-value{{font-size:2.8rem;font-weight:800;line-height:1.1;margin:6px 0}}
  .hero-card.valid .hero-value{{color:var(--green)}}
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

  <div class="section-label">Youth Program</div>
  <div class="cat-grid">
    {cat_card("🧒", "Kids (6-12)", kids)}
    {cat_card("🧑", "Teens (13-17)", teens)}
  </div>

  <div class="section-label">⚠️ Youth Program — Capacity Risk <span style="font-size:.75rem;font-weight:400;color:var(--text-dim);text-transform:none;letter-spacing:0;margin-left:8px">Cap. {CAPACITY} pax / category / week</span></div>
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
</script>
</body>
</html>"""

# ── Entry point ───────────────────────────────────────────────────────────────
if __name__ == "__main__":
    print("🔐 Authenticating...")
    token = get_token()

    print("📥 Fetching registrations...")
    regs = fetch_all(token)
    print(f"   Total records: {len(regs)}")

    print("🧮 Computing metrics...")
    hero, kids, teens, vip, fc, reg, cap = compute(regs)

    print("✍️  Writing index.html...")
    html = render_html(hero, kids, teens, vip, fc, reg, cap)
    with open("index.html", "w", encoding="utf-8") as f:
        f.write(html)

    print("✅ Done!")
    print(f"   Valid tickets: {hero['valid_total']}")
    print(f"   Kids total: {kids['total']}  (W1:{kids['w1']} W2:{kids['w2']} Unass:{kids['unassigned']})")
    print(f"   Teens total: {teens['total']} (W1:{teens['w1']} W2:{teens['w2']} Unass:{teens['unassigned']})")
