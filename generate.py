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
    """A valid ticket is 'paid' if Bizzabo actually charged money for it
    (top-level `charge` field > 0). This correctly classifies as 'comped'
    any regular ticket type (Adult/VIP/Teen/...) that was given out via a
    100% promo code — those have a non-zero `price` (the face value of the
    ticket type) but `charge == 0` because no money changed hands."""
    try:
        return float(r.get("charge") or 0) > 0
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

# ── Attendee-name helper with buyer fallback ─────────────────────────────────
def get_attendee_name(r):
    """Return the attendee's name. If the ticket has no attendee assigned yet
    (empty firstName/lastName in properties), fall back to the buyer's name
    from billingAddress and suffix with ' (Unassigned)' so it's clear the
    ticket still needs to be claimed by an actual attendee."""
    props = r.get("properties") or {}
    first = (props.get("firstName") or "").strip()
    last  = (props.get("lastName")  or "").strip()
    name = f"{first} {last}".strip()
    if name:
        return name
    billing = r.get("billingAddress") or {}
    b_first = (billing.get("firstName") or "").strip()
    b_last  = (billing.get("lastName")  or "").strip()
    buyer = f"{b_first} {b_last}".strip()
    return f"{buyer} (Unassigned)" if buyer else "(Unassigned)"

# ── Purchaser helper (Bizzabo "Order Placed By" fields) ──────────────────────
def get_purchaser(r):
    """Return (name, email) for the person who placed the order — Bizzabo's
    'Order Placed By (Name)' / 'Order Placed By (Email)' columns. Sourced
    from `billingAddress` which is the buyer info at order level.
    Returns ('', '') if not present."""
    bill = r.get("billingAddress") or {}
    first = (bill.get("firstName") or "").strip()
    last  = (bill.get("lastName")  or "").strip()
    name  = f"{first} {last}".strip()
    email = (bill.get("email") or "").strip()
    return name, email

# ── Week assignment helper ────────────────────────────────────────────────────
def get_week_full(reg):
    """Returns the raw 'when_are_you_joining' form value verbatim, e.g.
    'Week 1: July, 20 - 26' or 'Both weeks' — preserving the dates the user
    selected. Returns '' if not set."""
    props = reg.get("properties", {})
    if isinstance(props, dict):
        val = (props.get("when_are_you_joining") or "").strip()
        if val:
            return val
        for k, v in props.items():
            if "when" in k.lower() and "joining" in k.lower():
                return str(v).strip()
    elif isinstance(props, list):
        for prop in props:
            if not isinstance(prop, dict):
                continue
            sys_id = (prop.get("systemFieldId") or "").upper()
            label  = (prop.get("label") or "").upper()
            if "WHEN_ARE_YOU_JOINING" in sys_id or "WHEN_ARE_YOU_JOINING" in label:
                return (prop.get("value") or "").strip()
    return ""

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

# ── Date-of-birth + age-at-event helpers (Kids & Teens page) ─────────────────
EVENT_START_DATE = datetime(2026, 7, 20)  # MVU 2026 Day 1 — used for age-at-event

def get_dob(reg):
    """Return the attendee's date of birth as a datetime (date portion only),
    or None if missing/unparseable. Robust against both dict and list shapes
    of `properties`, and several date formats the Bizzabo form might emit."""
    props = reg.get("properties", {})
    raw = None
    if isinstance(props, dict):
        for k in ("date_of_birth", "dob", "birthdate", "dateOfBirth", "date-of-birth"):
            v = props.get(k)
            if v:
                raw = v; break
        if not raw:
            for k, v in props.items():
                if v and ("birth" in k.lower() or k.lower() == "dob"):
                    raw = v; break
    elif isinstance(props, list):
        for p in props:
            if not isinstance(p, dict): continue
            sid   = (p.get("systemFieldId") or "").lower()
            label = (p.get("label") or "").lower()
            if "birth" in sid or "birth" in label or sid == "dob" or label == "dob":
                v = p.get("value")
                if v: raw = v; break
    if not raw: return None
    s = str(raw).strip()
    if "T" in s: s = s.split("T")[0]
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%m/%d/%Y", "%Y/%m/%d", "%d-%m-%Y", "%d.%m.%Y"):
        try: return datetime.strptime(s, fmt)
        except ValueError: continue
    return None

def age_at_event(reg):
    """Age the attendee will have on EVENT_START_DATE (20 Jul 2026), or None
    if DOB is missing/unparseable."""
    dob = get_dob(reg)
    if not dob: return None
    age = EVENT_START_DATE.year - dob.year
    if (EVENT_START_DATE.month, EVENT_START_DATE.day) < (dob.month, dob.day):
        age -= 1
    return age

# ── Ticket-tier classifier ────────────────────────────────────────────────────
def classify_tier(ticket_name):
    """Maps a Bizzabo ticketName to a pricing tier label.
    Examples seen: 'Adult | 2 Weeks - Super Early Bird', 'VIP | 1 Week - Early Bird',
    'Teen (13-17 y.o) | 2 Weeks - Early Bird', 'Comped Ticket | 1 Week'."""
    s = (ticket_name or "").lower()
    if "comped" in s:
        return "Comped"
    if "super early bird" in s:
        return "Super Early Bird"
    if "early bird" in s:
        return "Early Bird"
    return "Standard"

# ── Special Guests: Airtable access system ───────────────────────────────────
# The Airtable table Álvaro agreed with his boss is the CANONICAL language for
# special-guest access types. Bizzabo is the implementation layer (ticket types
# + promo codes) and this mapping is the translator between the two. Cards on
# the page are the 5 wristband groups; the Category column shows the Airtable
# access name; the raw Bizzabo ticket stays visible as a secondary column.
SPECIAL_GUESTS_GROUPS = [
    {
        "id": "speaker",
        "name": "Speakers",
        "emoji": "🎤",
        "benefits": ["Event Access", "Fast Track Registration", "VIP Party",
                     "First Row Seating", "Hexagon Events", "Speaker Lounge",
                     "Speaker Dinner"],
    },
    {
        "id": "hexagon",
        "name": "Hexagon",
        "emoji": "🔷",
        "benefits": ["Event Access", "Fast Track Registration", "VIP Party",
                     "First Row Seating", "Hexagon Events", "Speaker Lounge"],
    },
    {
        "id": "friends",
        "name": "Non-Hex Friends",
        "emoji": "💜",
        "benefits": ["Event Access", "Fast Track Registration", "VIP Party",
                     "First Row Seating", "Hexagon Events", "Speaker Lounge"],
    },
    {
        "id": "vip",
        "name": "VIP",
        "emoji": "⭐",
        "benefits": ["Event Access", "Fast Track Registration", "VIP Party"],
    },
    {
        "id": "firstclass",
        "name": "First Class",
        "emoji": "💎",
        "benefits": ["Event Access", "Fast Track Registration", "VIP Party",
                     "First Row Seating", "First Class Experiences"],
    },
]

# The 10 Airtable access types. Matching order: promo code first (precise),
# then ticket-type fallback for manual Bizzabo activations without promo.
# Speakers: ticket type only — every speaker has an individual promo code
# (e.g. cynthiathurlowaccess) so promos can't be enumerated.
# First Class Comp: promo only — a ticket-key fallback on "first class"
# would swallow every regular First Class buyer from the sales tiers.
# "bizzabo": what Álvaro must set in Bizzabo to make a guest match this access
# (shown in the review panel as the action translation).
SPECIAL_GUESTS_ACCESS = [
    {"label": "Main Stage Speakers",                      "group": "speaker",
     "promos": [],                        "ticket_keys": ["speaker"],
     "bizzabo": "ticket 'Speaker' (each speaker has their own promo code)"},
    {"label": "Hexagon | $500 Ticket",                    "group": "hexagon",
     "promos": ["hex"],                   "ticket_keys": ["hexagon"], "paid": True,
     "bizzabo": "ticket 'Hexagon | 2 Weeks' · promo hex"},
    {"label": "Hexagon | Comped Ticket",                  "group": "hexagon",
     "promos": ["hexcomped"],             "ticket_keys": ["hexagon"], "paid": False,
     "bizzabo": "ticket '[Comped] Hexagon | 2 Weeks' · promo hexcomped"},
    {"label": "Friends of Vishen | $500 Ticket + Hex Parties",   "group": "friends",
     "promos": ["friendsofvishen"],       "ticket_keys": ["friends of vishen"], "paid": True,
     "bizzabo": "ticket 'Friends of Vishen | 2 Weeks' · promo friendsofvishen"},
    {"label": "Friends of Vishen | Comped Ticket + Hex Parties", "group": "friends",
     "promos": ["friendsofvishencomped"], "ticket_keys": ["friends of vishen"], "paid": False,
     "bizzabo": "ticket '[Comped] Friends of Vishen 2 Weeks' · promo friendsofvishencomped"},
    {"label": "VIP Guest | $500 Ticket",                  "group": "vip",
     "promos": ["specialguest"],          "ticket_keys": ["special guest", "vip guest"], "paid": True,
     "bizzabo": "ticket 'Special Guest | 2 Weeks' · promo specialguest"},
    {"label": "VIP Guest | Comped Ticket",                "group": "vip",
     "promos": ["specialguestcomped"],    "ticket_keys": ["special guest", "vip guest"], "paid": False,
     "bizzabo": "ticket '[Comped] Special Guest | 2 Weeks' · promo specialguestcomped"},
    {"label": "VIP Guest",                                "group": "vip",
     "promos": ["vipguest"],              "ticket_keys": [],
     "bizzabo": "promo vipguest"},
    {"label": "VIP Media",                                "group": "vip",
     "promos": ["vipmedia"],              "ticket_keys": ["vip media"],
     "bizzabo": "promo vipmedia"},
    {"label": "First Class Comp",                         "group": "firstclass",
     "promos": ["firstclassguest"],       "ticket_keys": [],
     "bizzabo": "promo firstclassguest"},
]

# Benefit → tag colour class
BENEFIT_TIER = {
    "Event Access":              "basic",
    "Fast Track Registration":   "basic",
    "VIP Party":                 "mid",
    "First Row Seating":         "premium",
    "Hexagon Events":            "premium",
    "Speaker Lounge":            "premium",
    "Speaker Dinner":            "premium",
    "First Class Experiences":   "premium",
}

# ── Main compute ─────────────────────────────────────────────────────────────
def compute(regs):
    now   = datetime.now(tz=timezone.utc)
    d7    = now - timedelta(days=7)
    d24   = now - timedelta(hours=24)

    valid     = [r for r in regs if r.get("validity","").lower() == "valid"]
    # 'Refunded' = ticket is no longer valid because it was refunded.
    # This excludes partial refunds where the customer kept a (downgraded)
    # valid ticket — those are still attendees.
    refunded  = [r for r in regs
                 if (r.get("paymentStatus") or "").lower() == "refunded"
                 and r.get("validity","").lower() == "invalid"]
    unassigned_tickets = [r for r in valid if (r.get("formSubmissionStatus") or "").lower() == "unassigned"]
    paid      = [r for r in valid if is_paid(r)]
    comped    = [r for r in valid if not is_paid(r)]

    def recent(lst, since, date_field="registrationDate"):
        return sum(1 for r in lst if (d := parse_date(r.get(date_field))) and d >= since)

    # ── hero counts ──
    # valid/paid use registrationDate (when ticket was bought).
    # refund_7d/24h use `modified` instead — Bizzabo doesn't expose a refund
    # timestamp, but for active events the record is touched at refund time,
    # so `modified` is a reliable proxy (validated against Activity Stream).
    hero = {
        "valid_total":    len(valid),
        "valid_7d":       recent(valid, d7),
        "valid_24h":      recent(valid, d24),
        "paid_total":     len(paid),
        "paid_7d":        recent(paid, d7),
        "paid_24h":       recent(paid, d24),
        "comped_total":   len(comped),
        "refund_total":   len(refunded),
        "refund_7d":      recent(refunded, d7,  "modified"),
        "refund_24h":     recent(refunded, d24, "modified"),
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

    def stats_from(records):
        """Same week-bucket counters as cat_stats but takes a pre-filtered list."""
        w1 = w2 = unass = 0
        for r in records:
            w = get_week(r)
            if w in ("Week 1", "Both Weeks"):  w1   += 1
            if w in ("Week 2", "Both Weeks"):  w2   += 1
            if not w:                          unass += 1
        return {"total": len(records), "w1": w1, "w2": w2, "unassigned": unass}

    def cat_for_breakdown(name):
        """Precedence-based bucket for the Ticket Breakdown section.
        Returns 'vip' / 'fc' / 'reg' / None. None = excluded (sales-focused
        section, so comped/hexagon/friends/crew and the kid/teen tickets
        which already have their own section are filtered out)."""
        if not name: return None
        t = name.lower()
        if 'comped' in t:               return None
        if 'hexagon' in t:               return None
        if 'friends of vishen' in t:     return None
        if 'crew' in t:                  return None
        if 'kid' in t or 'teen' in t:    return None
        if 'first class' in t:           return 'fc'
        if 'vip' in t:                   return 'vip'
        if 'adult' in t or 'standard' in t: return 'reg'
        return None

    def is_three_day(name):
        return bool(name) and '3 day' in name.lower()

    kids  = cat_stats(valid, "kid")
    teens = cat_stats(valid, "teen")
    vip      = stats_from([r for r in valid if cat_for_breakdown(r.get("ticketName")) == 'vip'])
    fc       = stats_from([r for r in valid if cat_for_breakdown(r.get("ticketName")) == 'fc'])
    reg      = stats_from([r for r in valid if cat_for_breakdown(r.get("ticketName")) == 'reg'])
    # 3 Days card: cross-cutting view — all 3-day tickets across VIP / First Class / Regular.
    # Same records also appear in their tier card (double-count by design).
    # Structured as a per-tier breakdown so the card can show Standard / VIP /
    # First Class rows each with their own Week 1 / Week 2 / Unassigned sub-bucket.
    threeday_recs = [r for r in valid
                     if is_three_day(r.get("ticketName"))
                     and cat_for_breakdown(r.get("ticketName")) is not None]
    threeday = {
        "total":   len(threeday_recs),
        "by_tier": {
            "Standard":    stats_from([r for r in threeday_recs if cat_for_breakdown(r.get("ticketName")) == 'reg']),
            "VIP":         stats_from([r for r in threeday_recs if cat_for_breakdown(r.get("ticketName")) == 'vip']),
            "First Class": stats_from([r for r in threeday_recs if cat_for_breakdown(r.get("ticketName")) == 'fc']),
        },
    }

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

    # ── refunds breakdown by tier (paid refunds only; comped excluded) ──
    TIERS = ("Super Early Bird", "Early Bird", "Standard")
    refunds_by_tier = {t: {"count": 0, "amount_cents": 0} for t in TIERS}
    _refund_tier_dist = {}  # diagnostic
    for r in refunded:
        tname = r.get("ticketName") or "<no ticket name>"
        _refund_tier_dist[tname] = _refund_tier_dist.get(tname, 0) + 1
        tier = classify_tier(tname)
        if tier in refunds_by_tier:
            refunds_by_tier[tier]["count"] += 1
            try:
                refunds_by_tier[tier]["amount_cents"] += int(r.get("price") or 0)
            except (TypeError, ValueError):
                pass
    # Diagnostic — show the full distribution of refunded ticketNames so we can
    # verify the classifier didn't bucket something incorrectly into Standard
    print("   Refunded ticket names by classified tier:")
    for name, n in sorted(_refund_tier_dist.items(), key=lambda x: -x[1]):
        print(f"     {n:4d}  [{classify_tier(name):16s}]  {name!r}")
    for tier in TIERS:
        d = refunds_by_tier[tier]
        print(f"   Refunds {tier}: {d['count']} tickets, ${d['amount_cents']/100:,.2f}")

    # ── promo code lists ──
    def promo_list(promo_codes, lst=valid):
        """Accepts a single code or a list of codes (case-insensitive)."""
        if isinstance(promo_codes, str):
            promo_codes = [promo_codes]
        codes_lower = {c.lower() for c in promo_codes}
        results = []
        for r in lst:
            if (r.get("promoCode") or "").lower() not in codes_lower:
                continue
            props = r.get("properties") or {}
            email = props.get("email", "")
            name = get_attendee_name(r)
            week = get_week(r)
            week_label = week if week else "Unassigned"
            week_full = get_week_full(r) or "Unassigned"
            is_mv = "@mindvalley" in email.lower()
            results.append({"name": name, "email": email, "week": week_label,
                            "weeks_full": week_full, "is_mv": is_mv,
                            "ticket": r.get("ticketName", "")})
        return results

    crew_list  = promo_list("MyCrewPass")
    vol_list   = promo_list(["Volunteer2Weeks", "Volunteer1Week"])

    # ── Special Guests: map each reg to an Airtable access type ──
    # 1) promo code (precise) → canonical Airtable label.
    # 2) ticket-type substring fallback for manual Bizzabo activations w/o
    #    promo, disambiguating paid vs comped variants via charge (is_paid).
    # 3) In the group but no clean access match → flagged 'unmapped' so the
    #    page surfaces where Bizzabo drifted from the Airtable agreement.
    promo_to_access = {}     # lower_promo -> access dict
    for acc in SPECIAL_GUESTS_ACCESS:
        for p in acc["promos"]:
            promo_to_access[p.lower()] = acc

    def _access_by_ticket(tt_lower, paid):
        """Fallback: find the access whose ticket_keys match, preferring the
        variant whose paid/comped expectation agrees with the actual charge."""
        candidates = [a for a in SPECIAL_GUESTS_ACCESS
                      if any(k in tt_lower for k in a.get("ticket_keys", []))]
        if not candidates:
            return None
        for a in candidates:
            if "paid" not in a or a["paid"] == paid:
                return a
        return candidates[0]

    sg_data = {grp["id"]: [] for grp in SPECIAL_GUESTS_GROUPS}
    for r in valid:
        p  = (r.get("promoCode")  or "").strip().lower()
        tt = (r.get("ticketName") or "").strip()
        tt_lower = tt.lower()
        unmapped = False
        acc = promo_to_access.get(p)
        if acc is None:
            acc = _access_by_ticket(tt_lower, is_paid(r))
        if acc is None:
            continue
        # Drift check: matched by promo but the ticket type contradicts the
        # expected paid/comped variant → keep the canonical label, flag it.
        if "paid" in acc and acc["ticket_keys"]:
            if not any(k in tt_lower for k in acc["ticket_keys"]):
                unmapped = True
        props = r.get("properties") or {}
        email = props.get("email", "")
        name = get_attendee_name(r)
        week = get_week(r)
        is_mv = "@mindvalley" in email.lower()
        sg_data[acc["group"]].append({
            "rid": str(r.get("id") or ""),
            "name": name, "email": email, "sub": acc["label"],
            "unmapped": unmapped,
            "week": week if week else "Unassigned",
            "weeks_full": get_week_full(r) or "Unassigned",
            "is_mv": is_mv, "ticket": tt,
        })

    return hero, kids, teens, vip, fc, reg, threeday, cap, crew_list, vol_list, sg_data, refunds_by_tier

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
def render_html(hero, kids, teens, vip, fc, reg, threeday, cap, crew_list, vol_list, hex_list, yoy=None, refunds_by_tier=None):
    now_str = datetime.now(tz=timezone.utc).strftime("%B %d, %Y at %H:%M UTC")

    # ── Refunds-by-tier section (paid refunds only; comped excluded) ──
    _TIER_ORDER = ("Super Early Bird", "Early Bird", "Standard")
    _TIER_ICONS = {"Super Early Bird": "🥇", "Early Bird": "🥈", "Standard": "🎫"}
    refunds_by_tier = refunds_by_tier or {t: {"count": 0, "amount_cents": 0} for t in _TIER_ORDER}
    _refund_total_count = sum(refunds_by_tier[t]["count"] for t in _TIER_ORDER)
    _refund_total_cents = sum(refunds_by_tier[t]["amount_cents"] for t in _TIER_ORDER)
    _comped_refunds = max(0, hero.get("refund_total", 0) - _refund_total_count)
    _refund_tier_cards = "".join(f"""
    <div class="cat-card">
      <div class="cat-icon">{_TIER_ICONS[t]}</div>
      <div class="cat-label">{t}</div>
      <div class="cat-value" data-target="{refunds_by_tier[t]['count']}">0</div>
      <div class="tier-lost">${refunds_by_tier[t]['amount_cents']/100:,.0f} refunded</div>
    </div>""" for t in _TIER_ORDER)
    refunds_tier_section = f"""
  <div class="section-label">Refunds Breakdown by Tier <span style="font-size:.75rem;font-weight:400;color:var(--text-dim);text-transform:none;letter-spacing:0;margin-left:8px">{_refund_total_count} paid refunds · ${_refund_total_cents/100:,.0f} lost · {_comped_refunds} comped excluded</span></div>
  <div class="cat-grid">{_refund_tier_cards}
  </div>
"""
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

    def threeday_card(td):
        """Custom 3 Days card: rows by tier (Standard / VIP / First Class),
        each with W1 / W2 / Unassigned counts."""
        rows = ""
        for tier_name in ("Standard", "VIP", "First Class"):
            s = td["by_tier"].get(tier_name, {"total":0,"w1":0,"w2":0,"unassigned":0})
            rows += f'''
        <tr>
          <td class="td-tier">{tier_name}<span class="td-tier-total" data-target="{s['total']}">0</span></td>
          <td data-target="{s['w1']}">0</td>
          <td data-target="{s['w2']}">0</td>
          <td data-target="{s['unassigned']}">0</td>
        </tr>'''
        return f"""
    <div class="cat-card">
      <div class="cat-icon">⏱️</div>
      <div class="cat-label">3 Days</div>
      <div class="cat-value" data-target="{td['total']}">0</div>
      <table class="td-mini">
        <thead><tr><th></th><th>W1</th><th>W2</th><th>None</th></tr></thead>
        <tbody>{rows}
        </tbody>
      </table>
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
  .td-mini{{width:100%;border-collapse:collapse;margin-top:6px}}
  .td-mini th{{color:var(--text-dim);font-weight:600;text-transform:uppercase;letter-spacing:.05em;padding:5px 4px;font-size:.62rem;text-align:right;border-bottom:1px solid rgba(255,255,255,.06)}}
  .td-mini th:first-child{{text-align:left}}
  .td-mini td{{padding:6px 4px;font-size:.85rem;font-weight:700;color:var(--text);text-align:right;border-bottom:1px solid rgba(255,255,255,.03);font-variant-numeric:tabular-nums}}
  .td-mini tbody tr:last-child td{{border-bottom:none}}
  .td-mini td.td-tier{{text-align:left;color:var(--text-dim);font-weight:500;font-size:.78rem;white-space:nowrap}}
  .td-mini .td-tier-total{{color:var(--gold);font-weight:700;margin-left:6px;font-size:.85rem}}
  .tier-lost{{text-align:center;font-size:.95rem;font-weight:700;color:var(--red);margin-top:8px;padding-top:8px;border-top:1px solid rgba(255,255,255,.05)}}
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
    {threeday_card(threeday)}
  </div>
{refunds_tier_section}

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

def render_ticket_types_page(regs):
    """Standalone page grouping valid registrations by wristband colour for
    the operations team. Each colour section shows the subtotal of
    wristbands needed plus a breakdown by ticket type with count and % of
    overall valid total. Sort order within each colour: tier (Super Early
    Bird → Early Bird → Regular → other) then duration (2 Weeks → 1 Week
    → 3 Days). Inside Purple, three sub-groups in order: Adult/Standard,
    Volunteers, Teens."""
    from collections import Counter as _Counter
    now_str = datetime.now(tz=timezone.utc).strftime("%B %d, %Y at %H:%M UTC")

    valid = [r for r in regs if r.get("validity","").lower() == "valid"]
    total = len(valid)

    VOLUNTEER_CODES = {"volunteer1week", "volunteer2weeks"}
    CREW_CODES      = {"mycrewpass"}

    def classify(r):
        name  = (r.get("ticketName") or "").strip()
        promo = (r.get("promoCode")  or "").strip().lower()
        n = name.lower()
        # Crew Access ticket name is unambiguous → it wins over the promo
        # code, so it never splits into "Crew Access" + "Crew Access (Crew)"
        # depending on whether the MyCrewPass promo was kept.
        if "crew access" in n:               return "Black", name
        if promo in VOLUNTEER_CODES:
            return "Purple", f"{name} (Volunteers)"
        if promo in CREW_CODES:
            return "Black", f"{name} (Crew)"
        if "hexagon" in n:                   return "Blue", name
        if "friends of vishen" in n:         return "Blue", name
        if "speaker" in n:                   return "Blue", name
        if "special guest" in n:             return "Brown", name
        if "crew access" in n:               return "Black", name
        if "vip" in n:                       return "Brown", name
        if "first class" in n:               return "Silver", name
        if "teen" in n:                      return "Purple", name
        if "kid" in n:                       return "Elastic band", name
        if "adult" in n or "standard" in n:  return "Purple", name
        return "Pending to categorize", name

    BANDS_ORDER = ("Purple", "Brown", "Silver", "Blue", "Black", "Elastic band", "Pending to categorize")
    bands = {c: _Counter() for c in BANDS_ORDER}
    for r in valid:
        color, label = classify(r)
        bands[color][label] += 1

    # Blue is always shown (even with 0). Pending only if non-empty.
    sections = [c for c in BANDS_ORDER if c != "Pending to categorize" or bands[c]]

    def sort_key(label, color):
        n = label.lower()
        sub = 0
        if color == "Purple":
            if "(volunteers)" in n: sub = 1
            elif "teen" in n:       sub = 2
        tier = 0 if "super early bird" in n else 1 if "early bird" in n else 2 if "regular" in n else 3
        dur  = 0 if "2 weeks" in n else 1 if "1 week" in n else 2 if "3 days" in n else 3
        return (sub, tier, dur, label)

    BAND_HEX = {
        "Purple":                "#a78bfa",
        "Brown":                 "#a0522d",
        "Silver":                "#d4d4d8",
        "Blue":                  "#60a5fa",
        "Black":                 "#9ca3af",   # neutral gray, pure black invisible on dark bg
        "Elastic band":          "#c4a373",   # natural rubber tan
        "Pending to categorize": "#fb923c",
    }
    # Units ordered from supplier per colour. Drives the capacity % bar in
    # each section header. None for Pending (no capacity defined).
    BAND_CAPACITY = {
        "Purple":       1500,
        "Brown":         500,
        "Silver":         25,
        "Blue":          200,
        "Black":          70,
        "Elastic band":  140,
    }

    sections_html = ""
    for color in sections:
        items = sorted(bands[color].items(), key=lambda x: sort_key(x[0], color))
        subtotal = sum(n for _, n in items)
        capacity = BAND_CAPACITY.get(color)
        rows_html = ""
        for label, n in items:
            pct = (n / total * 100) if total else 0
            rows_html += f'<tr><td class="name">{label}</td><td class="num">{n}</td><td class="pct">{pct:.1f}%</td></tr>'
        if not rows_html:
            rows_html = '<tr><td colspan="3" class="empty-row">No tickets in this category yet</td></tr>'

        if capacity is not None:
            cap_pct = (subtotal / capacity * 100) if capacity else 0
            fill_w  = min(cap_pct, 100)
            over    = cap_pct > 100
            stat_html = (
                f'<span class="band-stat"><span class="band-stat-num">{subtotal}</span> of '
                f'<span class="band-stat-cap">{capacity:,}</span> ordered · '
                f'<span class="band-stat-pct{" over" if over else ""}">{cap_pct:.1f}%{" — OVER" if over else ""}</span></span>'
            )
            progress_html = (
                f'<div class="band-progress{" over" if over else ""}">'
                f'<div class="band-progress-fill" style="width:{fill_w:.1f}%"></div></div>'
            )
        else:
            stat_html = (
                f'<span class="band-stat"><span class="band-stat-num">{subtotal}</span> '
                f'wristband{"s" if subtotal != 1 else ""} needed</span>'
            )
            progress_html = ""

        sections_html += f'''
<div class="band-section" style="--band:{BAND_HEX[color]}">
  <div class="band-header">
    <div class="band-header-top">
      <span class="band-name">{color}</span>
      {stat_html}
    </div>
    {progress_html}
  </div>
  <table class="band-table">
    <thead><tr><th>Ticket Type</th><th class="num">Count</th><th class="pct">% of total</th></tr></thead>
    <tbody>{rows_html}</tbody>
  </table>
</div>'''

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>Wristbands & Ticket Types — Mindvalley U 2026</title>
<style>
*,*::before,*::after{{box-sizing:border-box;margin:0;padding:0}}
:root{{--bg:#0b0a1a;--card:#14122a;--card-border:#2a2650;--gold:#d4a843;--purple:#7c3aed;--purple-light:#a78bfa;--text:#e8e4f0;--text-dim:#9a93b0;--green:#34d399}}
body{{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;background:var(--bg);color:var(--text);min-height:100vh;padding:32px 20px}}
.container{{max-width:820px;margin:0 auto}}
header{{text-align:center;margin-bottom:28px}}
h1{{font-size:1.9rem;font-weight:800;background:linear-gradient(135deg,var(--gold),var(--purple-light));-webkit-background-clip:text;-webkit-text-fill-color:transparent;letter-spacing:-.02em}}
header p{{color:var(--text-dim);margin-top:6px;font-size:.9rem}}
.timestamp{{display:inline-block;margin-top:10px;padding:4px 14px;border-radius:20px;background:rgba(124,58,237,.15);border:1px solid rgba(124,58,237,.3);font-size:.8rem;color:var(--purple-light)}}
.summary{{display:flex;justify-content:center;margin-bottom:28px}}
.summary-card{{background:var(--card);border:1px solid var(--card-border);border-radius:14px;padding:14px 26px;text-align:center;min-width:260px}}
.summary-val{{font-size:1.8rem;font-weight:800;color:var(--green);line-height:1.1}}
.summary-label{{font-size:.7rem;color:var(--text-dim);text-transform:uppercase;letter-spacing:.06em;margin-top:4px}}
.band-section{{margin-bottom:24px}}
.band-header{{padding:12px 18px 14px;background:var(--card);border:1px solid var(--card-border);border-left:5px solid var(--band);border-radius:14px 14px 0 0;border-bottom:none}}
.band-header-top{{display:flex;justify-content:space-between;align-items:baseline;gap:12px;margin-bottom:10px;flex-wrap:wrap}}
.band-name{{font-size:1.05rem;font-weight:800;color:var(--band);text-transform:uppercase;letter-spacing:.08em}}
.band-stat{{font-size:.8rem;color:var(--text-dim);font-weight:500;font-variant-numeric:tabular-nums;text-align:right}}
.band-stat-num{{font-size:1.3rem;font-weight:800;color:var(--band);margin-right:2px}}
.band-stat-cap{{color:var(--text);font-weight:700}}
.band-stat-pct{{color:var(--band);font-weight:700;margin-left:4px}}
.band-stat-pct.over{{color:#f87171}}
.band-progress{{height:6px;background:rgba(255,255,255,.06);border-radius:3px;overflow:hidden}}
.band-progress-fill{{height:100%;background:var(--band);border-radius:3px;transition:width .3s ease}}
.band-progress.over .band-progress-fill{{background:#f87171}}
.band-table{{width:100%;border-collapse:collapse;font-size:.88rem;background:var(--card);border:1px solid var(--card-border);border-top:none;border-radius:0 0 14px 14px;overflow:hidden}}
.band-table th{{text-align:left;padding:10px 18px;color:var(--text-dim);font-size:.7rem;text-transform:uppercase;letter-spacing:.06em;border-bottom:1px solid rgba(255,255,255,.06);background:rgba(255,255,255,.015);font-weight:600}}
.band-table th.num,.band-table th.pct{{text-align:right}}
.band-table td{{padding:10px 18px;border-bottom:1px solid rgba(255,255,255,.04)}}
.band-table td.name{{font-size:.86rem}}
.band-table td.num{{text-align:right;font-weight:700;color:var(--band);font-variant-numeric:tabular-nums}}
.band-table td.pct{{text-align:right;color:var(--text-dim);font-size:.8rem;font-variant-numeric:tabular-nums}}
.band-table tbody tr:last-child td{{border-bottom:none}}
.band-table tbody tr:hover td{{background:rgba(255,255,255,.02)}}
.empty-row{{text-align:center;color:var(--text-dim);font-size:.82rem;padding:18px}}
.footnote{{text-align:center;font-size:.72rem;color:var(--text-dim);margin-top:24px;line-height:1.5}}
</style>
</head>
<body>
<div class="container">
<header>
  <h1>Wristbands &amp; Ticket Types</h1>
  <p>Mindvalley U 2026 — Tallinn, Estonia</p>
  <div class="timestamp">Data snapshot: {now_str}</div>
</header>

<div class="summary">
  <div class="summary-card">
    <div class="summary-val">{total}</div>
    <div class="summary-label">Valid registrations total</div>
  </div>
</div>
{sections_html}

<div class="footnote">Only registrations with validity = valid · Volunteers &amp; Crew classified by promo code · Hexagon, Friends of Vishen &amp; Speaker → Blue · Special Guests → Brown · Crew Access → Black · ticket type labels kept verbatim (Bizzabo-side variants stay as separate rows)</div>
</div>
</body>
</html>"""

def render_refunds_analysis_page(regs_2026, regs_2025=None):
    """Standalone HTML page: deep-dive on paid refunds for MVU 2026.
    Lives at /event-dashboards/mvu-2026/refunds-analysis.html, not linked
    from the main dashboard. Bot regenerates with each run."""
    now_str = datetime.now(tz=timezone.utc).strftime("%B %d, %Y at %H:%M UTC")

    def _paid_refunds(regs):
        """Records where Bizzabo flags the payment as refunded. Includes both
        fully-refunded (validity=invalid, charge=0) and partial refunds where
        the customer kept a downgraded ticket. Explicit Comped Ticket
        products (price=0) excluded so they don't pad the count."""
        return [r for r in (regs or [])
                if (r.get("paymentStatus") or "").lower() == "refunded"
                and "comped" not in (r.get("ticketName") or "").lower()]

    def _refund_amount_cents(r):
        """How much money was actually given back for this refund.
        For full refunds (charge=0): price - 0 = price.
        For partial refunds:        price - residual charge.
        Edge case charge>price (tax/fees inflate): 0 (no $ counted)."""
        try:
            p = int(r.get("price") or 0)
            c = int(r.get("charge") or 0)
            return max(0, p - c)
        except (TypeError, ValueError):
            return 0

    refunds_2026 = _paid_refunds(regs_2026)
    refunds_2025 = _paid_refunds(regs_2025)

    total = len(refunds_2026)
    total_cents = sum(_refund_amount_cents(r) for r in refunds_2026)
    avg_cents = (total_cents // total) if total else 0

    # Gross Revenue excluding tax, including refunds: sum of `price` (base, no tax)
    # for every record that paid money at some point — i.e. currently has charge>0,
    # or had paymentStatus=refunded. Excludes Comped Ticket products (price=0).
    def _contributes_to_gross(r):
        if "comped" in (r.get("ticketName") or "").lower():
            return False
        try:
            price = int(r.get("price") or 0)
            charge = int(r.get("charge") or 0)
        except (TypeError, ValueError):
            return False
        if price <= 0:
            return False
        return charge > 0 or (r.get("paymentStatus") or "").lower() == "refunded"

    gross_cents = sum(int(r.get("price") or 0) for r in (regs_2026 or []) if _contributes_to_gross(r))
    refund_rate_pct = (total_cents / gross_cents * 100) if gross_cents else 0

    # ── Tier breakdown ──
    TIER_ORDER = ("Super Early Bird", "Early Bird", "Standard")
    TIER_ICONS = {"Super Early Bird": "🥇", "Early Bird": "🥈", "Standard": "🎫"}
    tier_counts = {t: {"count": 0, "cents": 0} for t in TIER_ORDER}
    for r in refunds_2026:
        t = classify_tier(r.get("ticketName"))
        if t in tier_counts:
            tier_counts[t]["count"] += 1
            tier_counts[t]["cents"] += _refund_amount_cents(r)

    # ── Category breakdown (Adult and Standard ticket names both → "Standard") ──
    def classify_cat(name):
        s = (name or "").lower()
        if "vip" in s:                       return "VIP"
        if "first class" in s:               return "First Class"
        if "teen" in s:                      return "Teen"
        if "kid" in s:                       return "Kid"
        if "adult" in s or "standard" in s:  return "Standard"
        return "Other"

    CAT_ORDER = ("VIP", "Standard", "First Class", "Teen", "Kid")
    cat_counts = {c: 0 for c in CAT_ORDER}
    for r in refunds_2026:
        c = classify_cat(r.get("ticketName"))
        if c in cat_counts:
            cat_counts[c] += 1

    # ── Week selection ──
    WEEK_ORDER = ("Week 1", "Week 2", "Both Weeks", "Unassigned")
    week_counts = {w: 0 for w in WEEK_ORDER}
    for r in refunds_2026:
        w = get_week(r)
        key = w if w in ("Week 1", "Week 2", "Both Weeks") else "Unassigned"
        week_counts[key] += 1

    # ── 2025 vs 2026 (totals) ──
    total_2025 = len(refunds_2025)

    # ── Tier cards ──
    TIER_CLASSES = {"Super Early Bird": "t-seb", "Early Bird": "t-eb", "Standard": "t-std"}
    tier_cards_html = ""
    for t in TIER_ORDER:
        d = tier_counts[t]
        pct = (d["count"] / total * 100) if total else 0
        tier_cards_html += f"""
    <div class="tier-card {TIER_CLASSES[t]}">
      <div class="tier-icon">{TIER_ICONS[t]}</div>
      <div class="tier-label">{t}</div>
      <div class="tier-count">{d['count']}</div>
      <div class="tier-pct">{pct:.0f}% of refunds</div>
      <div class="tier-money">${d['cents']/100:,.0f}</div>
    </div>"""

    def _bar_rows(items, scale_max):
        rows = ""
        for label, n in items:
            pct_of_total = (n / total * 100) if total else 0
            w = (n / scale_max * 100) if scale_max else 0
            rows += f"""
      <div class="bar-row">
        <div class="bar-label">{label}</div>
        <div class="bar-track"><div class="bar-fill" style="width:{w:.1f}%">{n} ({pct_of_total:.0f}%)</div></div>
      </div>"""
        return rows

    cat_max = max(cat_counts.values()) if cat_counts else 1
    cat_bars = _bar_rows([(c, cat_counts[c]) for c in CAT_ORDER], cat_max)

    week_max = max(week_counts.values()) if week_counts else 1
    week_bars = _bar_rows([(w, week_counts[w]) for w in WEEK_ORDER], week_max)

    yoy_max = max(total_2025, total) or 1
    yoy_2025_w = (total_2025 / yoy_max * 100)
    yoy_2026_w = (total / yoy_max * 100)
    yoy_bars = f"""
      <div class="bar-row">
        <div class="bar-label">2025 (full season)</div>
        <div class="bar-track"><div class="bar-fill yoy-2025" style="width:{yoy_2025_w:.1f}%">{total_2025}</div></div>
      </div>
      <div class="bar-row">
        <div class="bar-label">2026 (to date)</div>
        <div class="bar-track"><div class="bar-fill" style="width:{yoy_2026_w:.1f}%">{total}</div></div>
      </div>"""

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>Refunds Analysis — Mindvalley U 2026</title>
<style>
*,*::before,*::after{{box-sizing:border-box;margin:0;padding:0}}
:root{{--bg:#0b0a1a;--card:#14122a;--card-border:#2a2650;--gold:#d4a843;--gold-dim:#a07e30;--purple:#7c3aed;--purple-light:#a78bfa;--text:#e8e4f0;--text-dim:#9a93b0;--green:#34d399;--green-dim:#059669;--teal:#5eead4;--teal-dim:#14b8a6;--orange:#fb923c}}
body{{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;background:var(--bg);color:var(--text);min-height:100vh;padding:32px 20px}}
.container{{max-width:900px;margin:0 auto}}
header{{text-align:center;margin-bottom:36px}}
h1{{font-size:1.9rem;font-weight:800;background:linear-gradient(135deg,var(--gold),var(--purple-light));-webkit-background-clip:text;-webkit-text-fill-color:transparent;letter-spacing:-.02em}}
header p{{color:var(--text-dim);margin-top:6px;font-size:.9rem}}
.timestamp{{display:inline-block;margin-top:10px;padding:4px 14px;border-radius:20px;background:rgba(124,58,237,.15);border:1px solid rgba(124,58,237,.3);font-size:.8rem;color:var(--purple-light)}}
.section{{margin-bottom:32px}}
.section-label{{font-size:1rem;font-weight:700;color:var(--gold);margin-bottom:14px;text-transform:uppercase;letter-spacing:.08em;display:flex;align-items:center;gap:8px}}
.section-label::after{{content:'';flex:1;height:1px;background:linear-gradient(90deg,var(--gold-dim),transparent)}}
.kpi-grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:14px}}
.kpi-card{{background:var(--card);border:1px solid var(--card-border);border-radius:14px;padding:18px;text-align:center}}
.kpi-val{{font-size:2rem;font-weight:800;line-height:1.1}}
.kpi-label{{font-size:.7rem;color:var(--text-dim);text-transform:uppercase;letter-spacing:.06em;margin-top:6px}}
.kpi-card.k-count .kpi-val{{color:var(--purple-light)}}
.kpi-card.k-money .kpi-val{{color:var(--gold)}}
.kpi-card.k-avg   .kpi-val{{color:var(--green)}}
.kpi-card.k-rate  .kpi-val{{color:var(--orange)}}
.tier-grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:14px}}
.tier-card{{background:var(--card);border:1px solid var(--card-border);border-radius:14px;padding:20px;text-align:center;position:relative;overflow:hidden}}
.tier-card::before{{content:'';position:absolute;top:0;left:0;right:0;height:3px}}
.tier-icon{{font-size:1.6rem;margin-bottom:4px}}
.tier-label{{font-size:.78rem;color:var(--text-dim);text-transform:uppercase;letter-spacing:.05em}}
.tier-count{{font-size:2.2rem;font-weight:800;line-height:1.1;margin:4px 0}}
.tier-pct{{font-size:.78rem;color:var(--text-dim)}}
.tier-money{{font-size:1.05rem;font-weight:700;margin-top:10px;padding-top:8px;border-top:1px solid rgba(255,255,255,.06)}}
.tier-card.t-seb::before{{background:linear-gradient(90deg,var(--gold),var(--gold-dim))}}
.tier-card.t-seb .tier-count, .tier-card.t-seb .tier-money{{color:var(--gold)}}
.tier-card.t-eb::before{{background:linear-gradient(90deg,var(--teal),var(--teal-dim))}}
.tier-card.t-eb .tier-count, .tier-card.t-eb .tier-money{{color:var(--teal)}}
.tier-card.t-std::before{{background:linear-gradient(90deg,var(--purple-light),var(--purple))}}
.tier-card.t-std .tier-count, .tier-card.t-std .tier-money{{color:var(--purple-light)}}
.bar-row{{display:flex;align-items:center;gap:12px;margin-bottom:8px}}
.bar-label{{width:150px;font-size:.85rem;color:var(--text);flex-shrink:0}}
.bar-track{{flex:1;height:28px;background:rgba(255,255,255,.04);border-radius:6px;overflow:hidden}}
.bar-fill{{height:100%;display:flex;align-items:center;justify-content:flex-end;padding-right:10px;color:#fff;font-size:.78rem;font-weight:700;white-space:nowrap;border-radius:6px;min-width:fit-content}}
.bar-cat  .bar-fill{{background:linear-gradient(90deg,var(--gold),var(--gold-dim))}}
.bar-week .bar-fill{{background:linear-gradient(90deg,var(--teal),var(--teal-dim))}}
.bar-yoy  .bar-fill{{background:linear-gradient(90deg,var(--gold),var(--gold-dim))}}
.bar-yoy  .bar-fill.yoy-2025{{background:linear-gradient(90deg,var(--purple-light),var(--purple));opacity:.95}}
.footnote{{text-align:center;font-size:.72rem;color:var(--text-dim);margin-top:40px;padding-top:20px;border-top:1px solid rgba(255,255,255,.04)}}
@media(max-width:600px){{.bar-label{{width:100px;font-size:.75rem}}h1{{font-size:1.4rem}}.kpi-val{{font-size:1.6rem}}}}
</style>
</head>
<body>
<div class="container">
<header>
  <h1>Refunds Analysis</h1>
  <p>Mindvalley U 2026 — Tallinn, Estonia</p>
  <div class="timestamp">Data snapshot: {now_str}</div>
</header>

<div class="section">
  <div class="section-label">Headline</div>
  <div class="kpi-grid">
    <div class="kpi-card k-count"><div class="kpi-val">{total}</div><div class="kpi-label">Paid refunds</div></div>
    <div class="kpi-card k-money"><div class="kpi-val">${total_cents/100:,.0f}</div><div class="kpi-label">Total $ lost</div></div>
    <div class="kpi-card k-avg"><div class="kpi-val">${avg_cents/100:,.0f}</div><div class="kpi-label">Avg per refund</div></div>
    <div class="kpi-card k-rate"><div class="kpi-val">{refund_rate_pct:.1f}%</div><div class="kpi-label">Refund rate</div></div>
  </div>
</div>

<div class="section">
  <div class="section-label">By Pricing Tier</div>
  <div class="tier-grid">{tier_cards_html}
  </div>
</div>

<div class="section bar-cat">
  <div class="section-label">By Ticket Category</div>
  {cat_bars}
</div>

<div class="section bar-week">
  <div class="section-label">By Week Selection</div>
  {week_bars}
</div>

<div class="section bar-yoy">
  <div class="section-label">Year-over-Year</div>
  {yoy_bars}
</div>

<div class="footnote">
Comped tickets excluded throughout. Adult and Standard ticket types are merged into 'Standard'. Tier and category classification derived from ticketName.
</div>
</div>
</body>
</html>"""

def render_promo_page(emoji, title, plist, flag_non_mv=False):
    now_str = datetime.now(tz=timezone.utc).strftime("%B %d, %Y at %H:%M UTC")
    if not plist:
        body = '<div class="empty">No registrations yet</div>'
    else:
        # ── Overview stats ──
        total = len(plist)
        w1    = sum(1 for p in plist if p.get("week") == "Week 1")
        w2    = sum(1 for p in plist if p.get("week") == "Week 2")
        both  = sum(1 for p in plist if p.get("week") == "Both Weeks")
        unass = total - w1 - w2 - both
        stats_html = f"""<div class="stats-grid">
  <div class="stat-card total"><div class="stat-val">{total}</div><div class="stat-label">Total</div></div>
  <div class="stat-card w1"><div class="stat-val">{w1}</div><div class="stat-label">Week 1</div></div>
  <div class="stat-card w2"><div class="stat-val">{w2}</div><div class="stat-label">Week 2</div></div>
  <div class="stat-card both"><div class="stat-val">{both}</div><div class="stat-label">Both Weeks</div></div>
  <div class="stat-card unass"><div class="stat-val">{unass}</div><div class="stat-label">Unassigned</div></div>
</div>"""
        rows = ""
        for p in plist:
            if flag_non_mv and not p["is_mv"]:
                badge = ' <span class="flag-ext">external</span>'
            elif flag_non_mv and p["is_mv"]:
                badge = ' <img src="https://www.mindvalley.com/favicon.ico" class="mv-icon" alt="MV">'
            else:
                badge = ""
            rows += f"<tr><td>{p['name']}{badge}</td><td>{p['ticket']}</td><td>{p['weeks_full']}</td></tr>\n"
        body = stats_html + f"""<table>
<thead><tr><th>Name</th><th>Ticket Type</th><th>When</th></tr></thead>
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
.stats-grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(130px,1fr));gap:10px;margin-bottom:24px}}
.stat-card{{background:var(--card);border:1px solid rgba(255,255,255,.06);border-radius:12px;padding:14px 12px;text-align:center}}
.stat-val{{font-size:1.8rem;font-weight:800;line-height:1.1}}
.stat-label{{font-size:.68rem;color:var(--text-dim);text-transform:uppercase;letter-spacing:.05em;margin-top:4px}}
.stat-card.total .stat-val{{color:#a78bfa}}
.stat-card.w1 .stat-val{{color:#34d399}}
.stat-card.w2 .stat-val{{color:#fbbf24}}
.stat-card.both .stat-val{{color:var(--gold)}}
.stat-card.unass .stat-val{{color:#7a7793}}
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

def render_special_guests_page(sg_data, review=False):
    """Special Guests page. One card per wristband group (Speakers / Hexagon /
    Non-Hex Friends / VIP / First Class) with the group's benefits as tags and
    an embedded attendee table. The Access column speaks Airtable (the access
    system agreed with the boss); the raw Bizzabo ticket stays as a secondary
    column. Rows whose Bizzabo data doesn't cleanly match any Airtable access
    are flagged amber ('check Bizzabo').

    With review=True, the Access column becomes a dropdown of the 10 Airtable
    access types so the boss can propose reassignments. Selections differ from
    current → ⚠️ + entry in a floating pending-changes panel that renders the
    Bizzabo translation for Álvaro and encodes everything in a shareable URL
    (?ov=rid:accessIndex,...). No write-back — Bizzabo changes stay manual."""
    now_str = datetime.now(tz=timezone.utc).strftime("%B %d, %Y at %H:%M UTC")

    access_labels = [a["label"] for a in SPECIAL_GUESTS_ACCESS]

    cards_html = ""
    for grp in SPECIAL_GUESTS_GROUPS:
        attendees = sg_data.get(grp["id"], [])

        # Benefit tags
        tags = "".join(
            f'<span class="sg-benefit b-{BENEFIT_TIER.get(b,"basic")}">{b}</span>'
            for b in grp["benefits"]
        )

        # Embedded table (or empty state) for this group's attendees
        if attendees:
            rows_html = ""
            for a in attendees:
                if review:
                    opts = "".join(
                        f'<option value="{i}"{" selected" if lbl == a["sub"] else ""}>{lbl}</option>'
                        for i, lbl in enumerate(access_labels)
                    )
                    cur_idx = access_labels.index(a["sub"]) if a["sub"] in access_labels else -1
                    access_cell = (
                        f'<td class="sg-tsub"><span class="sg-ov-flag" hidden>⚠️</span>'
                        f'<select class="sg-select" data-rid="{a["rid"]}" data-name="{a["name"]}" '
                        f'data-cur="{cur_idx}">{opts}</select></td>'
                    )
                else:
                    flag = ' <span class="sg-unmapped" title="Bizzabo data does not cleanly match this Airtable access — check Bizzabo">⚠️</span>' if a.get("unmapped") else ""
                    access_cell = f"<td class='sg-tsub'>{a['sub']}{flag}</td>"
                rows_html += (
                    f"<tr><td>{a['name']}</td>{access_cell}"
                    f"<td class='sg-bizzabo'>{a['ticket']}</td><td>{a['weeks_full']}</td></tr>"
                )
            inner = f"""<table class="sg-table">
<thead><tr><th>Name</th><th>Access</th><th>Bizzabo Ticket</th><th>When</th></tr></thead>
<tbody>{rows_html}</tbody>
</table>"""
        else:
            inner = '<div class="sg-empty-inline">No registrations yet</div>'

        cards_html += f"""
<div class="sg-card">
  <div class="sg-head">
    <span class="sg-emoji">{grp['emoji']}</span>
    <h2 class="sg-name">{grp['name']}</h2>
    <span class="sg-count">{len(attendees)}</span>
  </div>
  <div class="sg-benefits">{tags}</div>
  {inner}
</div>"""

    # ── Review-mode extras (preview page only) ──
    review_badge = ""
    review_css = ""
    review_js = ""
    if review:
        review_badge = '<div class="sg-review-badge">🧪 REVIEW MODE — preview · selections are proposals only, nothing changes in Bizzabo</div>'
        review_css = """
.sg-review-badge{margin-top:12px;display:inline-block;padding:6px 16px;border-radius:20px;background:rgba(251,191,36,.12);border:1px solid rgba(251,191,36,.4);font-size:.8rem;color:#fbbf24;font-weight:600}
.sg-select{background:#1c1938;color:var(--purple-light);border:1px solid var(--card-border);border-radius:8px;padding:5px 8px;font-size:.8rem;max-width:280px;cursor:pointer}
.sg-select.changed{border-color:#fbbf24;color:#fbbf24;background:rgba(251,191,36,.08)}
.sg-ov-flag{margin-right:6px}
#sg-panel{position:fixed;bottom:20px;right:20px;width:380px;max-height:60vh;overflow-y:auto;background:#1c1938;border:1px solid #fbbf24;border-radius:14px;padding:16px 18px;z-index:50;box-shadow:0 12px 48px rgba(0,0,0,.5)}
#sg-panel h3{font-size:.9rem;color:#fbbf24;margin-bottom:10px}
#sg-panel .pc-item{font-size:.78rem;padding:8px 0;border-bottom:1px solid rgba(255,255,255,.06)}
#sg-panel .pc-item:last-of-type{border-bottom:none}
#sg-panel .pc-name{font-weight:700;color:var(--text)}
#sg-panel .pc-change{color:var(--text-dim);margin-top:2px}
#sg-panel .pc-change b{color:#fbbf24;font-weight:600}
#sg-panel .pc-bizzabo{color:var(--green);margin-top:2px;font-size:.72rem}
#sg-panel .pc-actions{display:flex;gap:8px;margin-top:12px}
#sg-panel button{flex:1;padding:8px 10px;border-radius:8px;border:none;font-size:.78rem;font-weight:700;cursor:pointer}
#sg-panel .pc-copy{background:#fbbf24;color:#1c1938}
#sg-panel .pc-copy:hover{background:#fcd34d}
#sg-panel .pc-clear{background:rgba(255,255,255,.08);color:var(--text-dim)}
#sg-panel .pc-clear:hover{background:rgba(255,255,255,.14)}
"""
        access_json  = json.dumps([a["label"]   for a in SPECIAL_GUESTS_ACCESS])
        bizzabo_json = json.dumps([a["bizzabo"] for a in SPECIAL_GUESTS_ACCESS])
        # Plain (non-f) string so JS braces don't need escaping.
        review_js = ("<script>\nconst SG_ACCESS=" + access_json +
                     ";\nconst SG_BIZZABO=" + bizzabo_json + ";\n" + """
const overrides = {};   // rid -> {name, curIdx, newIdx}

function syncURL() {
  const parts = Object.entries(overrides).map(([rid, o]) => rid + ':' + o.newIdx);
  const url = new URL(location.href);
  if (parts.length) url.searchParams.set('ov', parts.join(','));
  else url.searchParams.delete('ov');
  history.replaceState(null, '', url);
}

function renderPanel() {
  let panel = document.getElementById('sg-panel');
  const n = Object.keys(overrides).length;
  if (!n) { if (panel) panel.remove(); return; }
  if (!panel) {
    panel = document.createElement('div');
    panel.id = 'sg-panel';
    document.body.appendChild(panel);
  }
  let html = '<h3>⚠️ ' + n + ' pending change' + (n > 1 ? 's' : '') + '</h3>';
  for (const [rid, o] of Object.entries(overrides)) {
    html += '<div class="pc-item">'
          + '<div class="pc-name">' + o.name + '</div>'
          + '<div class="pc-change">' + (SG_ACCESS[o.curIdx] || '(unmapped)') + ' → <b>' + SG_ACCESS[o.newIdx] + '</b></div>'
          + '<div class="pc-bizzabo">Bizzabo: ' + SG_BIZZABO[o.newIdx] + '</div>'
          + '</div>';
  }
  html += '<div class="pc-actions">'
        + '<button class="pc-copy" onclick="copyLink(this)">Copy shareable link</button>'
        + '<button class="pc-clear" onclick="clearAll()">Clear all</button>'
        + '</div>';
  panel.innerHTML = html;
}

function copyLink(btn) {
  navigator.clipboard.writeText(location.href).then(() => {
    btn.textContent = 'Copied!';
    setTimeout(() => { btn.textContent = 'Copy shareable link'; }, 1600);
  });
}

function clearAll() {
  document.querySelectorAll('.sg-select').forEach(sel => {
    sel.value = sel.dataset.cur;
    applyChange(sel, false);
  });
  renderPanel(); syncURL();
}

function applyChange(sel, updatePanel = true) {
  const rid = sel.dataset.rid;
  const cur = parseInt(sel.dataset.cur, 10);
  const val = parseInt(sel.value, 10);
  const flag = sel.parentElement.querySelector('.sg-ov-flag');
  if (val !== cur) {
    overrides[rid] = { name: sel.dataset.name, curIdx: cur, newIdx: val };
    sel.classList.add('changed');
    if (flag) flag.hidden = false;
  } else {
    delete overrides[rid];
    sel.classList.remove('changed');
    if (flag) flag.hidden = true;
  }
  if (updatePanel) { renderPanel(); syncURL(); }
}

document.querySelectorAll('.sg-select').forEach(sel => {
  sel.addEventListener('change', () => applyChange(sel));
});

// Restore overrides from a shared URL
const ovParam = new URLSearchParams(location.search).get('ov');
if (ovParam) {
  const byRid = {};
  document.querySelectorAll('.sg-select').forEach(sel => { byRid[sel.dataset.rid] = sel; });
  ovParam.split(',').forEach(pair => {
    const [rid, idx] = pair.split(':');
    const sel = byRid[rid];
    if (sel && idx !== undefined && SG_ACCESS[parseInt(idx, 10)] !== undefined) {
      sel.value = idx;
      applyChange(sel, false);
    }
  });
  renderPanel(); syncURL();
}
</script>""")

    title_suffix = " · Review Preview" if review else ""

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>Special Guests — MVU 2026{title_suffix}</title>
<style>
*,*::before,*::after{{box-sizing:border-box;margin:0;padding:0}}
:root{{--bg:#0b0a1a;--card:#14122a;--card-border:#2a2650;--gold:#d4a843;--purple:#7c3aed;--purple-light:#a78bfa;--text:#e8e4f0;--text-dim:#9a93b0;--green:#34d399}}
body{{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;background:var(--bg);color:var(--text);min-height:100vh;padding:32px 20px}}
.container{{max-width:1100px;margin:0 auto}}
header{{text-align:center;margin-bottom:30px}}
h1{{font-size:1.9rem;font-weight:800;background:linear-gradient(135deg,var(--gold),var(--purple-light));-webkit-background-clip:text;-webkit-text-fill-color:transparent;letter-spacing:-.02em}}
header p{{color:var(--text-dim);margin-top:6px;font-size:.9rem}}
.timestamp{{display:inline-block;margin-top:10px;padding:4px 14px;border-radius:20px;background:rgba(124,58,237,.15);border:1px solid rgba(124,58,237,.3);font-size:.8rem;color:var(--purple-light)}}
.sg-card{{background:var(--card);border:1px solid var(--card-border);border-radius:16px;padding:22px 24px;margin-bottom:20px;position:relative;overflow:hidden}}
.sg-card::before{{content:'';position:absolute;top:0;left:0;right:0;height:3px;background:linear-gradient(90deg,var(--purple-light),var(--gold));border-radius:16px 16px 0 0}}
.sg-head{{display:flex;align-items:center;gap:12px;margin-bottom:14px}}
.sg-emoji{{font-size:1.7rem;line-height:1}}
.sg-name{{font-size:1.35rem;font-weight:800;color:var(--text);flex:1;letter-spacing:-.01em}}
.sg-count{{font-size:1.9rem;font-weight:800;color:var(--gold);line-height:1;font-variant-numeric:tabular-nums}}
.sg-benefits{{display:flex;flex-wrap:wrap;gap:6px;margin-bottom:18px}}
.sg-benefit{{font-size:.68rem;font-weight:600;padding:4px 10px;border-radius:11px;white-space:nowrap;letter-spacing:.02em}}
.sg-benefit.b-basic{{background:rgba(212,168,67,.15);color:#e0bc62;border:1px solid rgba(212,168,67,.3)}}
.sg-benefit.b-mid{{background:rgba(96,165,250,.15);color:#7dadeb;border:1px solid rgba(96,165,250,.3)}}
.sg-benefit.b-premium{{background:rgba(236,72,153,.15);color:#f069b6;border:1px solid rgba(236,72,153,.3)}}
.sg-table{{width:100%;border-collapse:collapse;font-size:.88rem}}
.sg-table th{{text-align:left;padding:9px 12px;color:var(--text-dim);font-size:.68rem;text-transform:uppercase;letter-spacing:.06em;border-top:1px solid rgba(255,255,255,.06);border-bottom:1px solid rgba(255,255,255,.06);background:rgba(255,255,255,.015);font-weight:600}}
.sg-table td{{padding:9px 12px;border-bottom:1px solid rgba(255,255,255,.03)}}
.sg-table tbody tr:last-child td{{border-bottom:none}}
.sg-table tbody tr:hover td{{background:rgba(255,255,255,.02)}}
.sg-table td.sg-tsub{{color:var(--purple-light);font-size:.82rem;font-weight:500}}
.sg-table td.sg-bizzabo{{color:var(--text-dim);font-size:.78rem}}
.sg-unmapped{{cursor:help}}
.sg-empty-inline{{text-align:center;padding:18px;color:var(--text-dim);font-size:.85rem;font-style:italic;border-top:1px solid rgba(255,255,255,.06)}}
{review_css}</style>
</head>
<body>
<div class="container">
<header>
  <h1>Special Guests</h1>
  <p>Mindvalley U 2026 — Tallinn, Estonia</p>
  <div class="timestamp">Data snapshot: {now_str}</div>
  {review_badge}
</header>
{cards_html}
</div>
{review_js}
</body>
</html>"""

def render_kids_teens_page(regs, kids, teens, cap):
    """Kids & Teens roster page. Two rows of two side-by-side tables:
      Week 1: [Kids | Teens]
      Week 2: [Kids | Teens]
    Each table is sorted ascending by age (no-DOB rows at the end).
    'Both Weeks' attendees appear in BOTH W1 and W2.
    Ages outside the expected program range get a ⚠️ flag
    (Kids: 6–12 · Teens: 13–17). Comped tickets included; refunded excluded."""
    now_str = datetime.now(tz=timezone.utc).strftime("%B %d, %Y at %H:%M UTC")

    # ── Source set: same 'valid' definition as the rest of the dashboard ──
    valid = [r for r in regs if r.get("validity","").lower() == "valid"]

    # ── Classify each kid/teen registration ──
    def classify(r):
        t = (r.get("ticketName") or "").lower()
        if "kid"  in t: return "kids"
        if "teen" in t: return "teens"
        return None

    BANDS = {"kids": (6, 12), "teens": (13, 17)}

    import unicodedata as _ud
    def _norm(s):
        s = _ud.normalize('NFKD', s or '').encode('ASCII','ignore').decode()
        return ' '.join(s.lower().split())

    def row(r, program):
        age = age_at_event(r)
        lo, hi = BANDS[program]
        out_of_range = (age is not None) and (age < lo or age > hi)
        p_name, p_email = get_purchaser(r)
        # Data-quality flag: on a Kids & Teens page, a kid's attendee data
        # should NEVER match the purchaser (kids can't self-register). If
        # name AND email both match, the parent likely typed their own info
        # into the attendee fields by mistake — worth verifying with CS.
        props = r.get("properties") or {}
        att_name  = f"{(props.get('firstName') or '').strip()} {(props.get('lastName') or '').strip()}".strip()
        att_email = (props.get("email") or "").strip()
        same_as_attendee = bool(
            p_email and p_name
            and _norm(p_email) == _norm(att_email)
            and _norm(p_name)  == _norm(att_name)
        )
        return {
            "name":              get_attendee_name(r),
            "ticket":            r.get("ticketName") or "",
            "age":               age,
            "out_of_range":      out_of_range,
            "week":              get_week(r),
            "purchaser_name":    p_name,
            "purchaser_email":   p_email,
            "same_as_attendee":  same_as_attendee,
        }

    buckets = {
        "kids":  {"w1": [], "w2": [], "unass": []},
        "teens": {"w1": [], "w2": [], "unass": []},
    }
    for r in valid:
        prog = classify(r)
        if not prog: continue
        rec  = row(r, prog)
        w    = rec["week"]
        if w == "Week 1":     buckets[prog]["w1"].append(rec)
        elif w == "Week 2":   buckets[prog]["w2"].append(rec)
        elif w == "Both Weeks":
            buckets[prog]["w1"].append(rec)
            buckets[prog]["w2"].append(rec)
        else:
            buckets[prog]["unass"].append(rec)

    def sort_key(rec):
        # No-DOB rows go last: ages-known sorted ascending, then None bucket.
        return (rec["age"] is None, rec["age"] if rec["age"] is not None else 0, rec["name"].lower())

    for prog in buckets:
        for k in buckets[prog]:
            buckets[prog][k].sort(key=sort_key)

    # ── Table builder ──
    def render_table(prog, week_key, emoji, label):
        rows = buckets[prog][week_key]
        if not rows:
            return f"""
<div class="kt-card">
  <div class="kt-head">
    <span class="kt-emoji">{emoji}</span>
    <h2 class="kt-name">{label}</h2>
    <span class="kt-count">0</span>
  </div>
  <div class="kt-empty">No registrations yet</div>
</div>"""
        body = ""
        for rec in rows:
            age_html = (
                f'{rec["age"]} <span class="kt-warn" title="Outside the expected {BANDS[prog][0]}–{BANDS[prog][1]} range">⚠️</span>'
                if rec["out_of_range"] else
                (str(rec["age"]) if rec["age"] is not None else '<span class="kt-nodob">—</span>')
            )
            # Purchaser sub-line ("Order Placed By" in Bizzabo). Always shown
            # when present. When purchaser data matches the attendee (name AND
            # email), the line switches to amber with a warning label — on a
            # Kids & Teens page this almost certainly means the parent typed
            # their own info into the attendee fields by mistake.
            purchaser_html = ""
            if rec["purchaser_name"] or rec["purchaser_email"]:
                bits = []
                if rec["purchaser_name"]:  bits.append(rec["purchaser_name"])
                if rec["purchaser_email"]: bits.append(f'<a href="mailto:{rec["purchaser_email"]}">{rec["purchaser_email"]}</a>')
                info = " · ".join(bits)
                if rec["same_as_attendee"]:
                    purchaser_html = (
                        f'<div class="kt-purchaser kt-purchaser-warn" '
                        f'title="Attendee data matches purchaser — parent likely put their own info in the attendee fields by mistake. Verify with CS.">'
                        f'↳ ⚠️ Same as attendee — verify: {info}</div>'
                    )
                else:
                    purchaser_html = f'<div class="kt-purchaser">↳ Purchased by {info}</div>'
            body += f"<tr><td>{rec['name']}{purchaser_html}</td><td class='kt-ticket'>{rec['ticket']}</td><td class='kt-age'>{age_html}</td></tr>"
        return f"""
<div class="kt-card">
  <div class="kt-head">
    <span class="kt-emoji">{emoji}</span>
    <h2 class="kt-name">{label}</h2>
    <span class="kt-count">{len(rows)}</span>
  </div>
  <table class="kt-table">
    <thead><tr><th>Name</th><th>Ticket Type</th><th>Age</th></tr></thead>
    <tbody>{body}</tbody>
  </table>
</div>"""

    # ── Capacity Risk cards (Kids/Teens × W1/W2) — mirrors main dashboard ──
    def cap_card_html(emoji, name, week_label, confirmed, unassigned_count, cap_tuple):
        level, status, subtitle = cap_tuple
        worst = confirmed + unassigned_count
        overflow = worst - CAPACITY
        if overflow > 0:
            overflow_html = f'<strong style="color:#f87171">+{overflow}</strong>'
        else:
            color = "34d399" if level == "green" else "fbbf24"
            overflow_html = f'<strong style="color:#{color}">{CAPACITY - worst} spots</strong>'
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

    cap_grid = f"""
<div class="cap-grid">
  {cap_card_html("🧒", "Kids",  "Week 1", kids["w1"],  kids["unassigned"],  cap["kids_w1"])}
  {cap_card_html("🧒", "Kids",  "Week 2", kids["w2"],  kids["unassigned"],  cap["kids_w2"])}
  {cap_card_html("🧑", "Teens", "Week 1", teens["w1"], teens["unassigned"], cap["teens_w1"])}
  {cap_card_html("🧑", "Teens", "Week 2", teens["w2"], teens["unassigned"], cap["teens_w2"])}
</div>"""

    w1_grid = f"""
<div class="kt-week-grid">
  {render_table("kids",  "w1", "🧒", "Kids")}
  {render_table("teens", "w1", "🧑", "Teens")}
</div>"""

    w2_grid = f"""
<div class="kt-week-grid">
  {render_table("kids",  "w2", "🧒", "Kids")}
  {render_table("teens", "w2", "🧑", "Teens")}
</div>"""

    # Unassigned-week row only shown if anyone has no week selected
    has_unass = bool(buckets["kids"]["unass"]) or bool(buckets["teens"]["unass"])
    unass_section = ""
    if has_unass:
        unass_section = f"""
<div class="kt-section-label">No Week Selected</div>
<div class="kt-week-grid">
  {render_table("kids",  "unass", "🧒", "Kids")}
  {render_table("teens", "unass", "🧑", "Teens")}
</div>"""

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>Kids &amp; Teens — MVU 2026</title>
<style>
*,*::before,*::after{{box-sizing:border-box;margin:0;padding:0}}
:root{{--bg:#0b0a1a;--card:#14122a;--card-border:#2a2650;--gold:#d4a843;--purple:#7c3aed;--purple-light:#a78bfa;--text:#e8e4f0;--text-dim:#9a93b0;--green:#34d399;--red:#f87171;--orange:#fb923c}}
body{{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;background:var(--bg);color:var(--text);min-height:100vh;padding:32px 20px}}
.container{{max-width:1200px;margin:0 auto}}
header{{text-align:center;margin-bottom:30px}}
h1{{font-size:1.9rem;font-weight:800;background:linear-gradient(135deg,var(--gold),var(--purple-light));-webkit-background-clip:text;-webkit-text-fill-color:transparent;letter-spacing:-.02em}}
header p{{color:var(--text-dim);margin-top:6px;font-size:.9rem}}
.timestamp{{display:inline-block;margin-top:10px;padding:4px 14px;border-radius:20px;background:rgba(124,58,237,.15);border:1px solid rgba(124,58,237,.3);font-size:.8rem;color:var(--purple-light)}}

/* program stats — Kids & Teens with W1 / W2 / No Week breakdown */
.cat-grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:16px;margin-bottom:28px}}
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

.kt-section-label{{font-size:1.1rem;font-weight:700;color:var(--gold);margin:8px 0 14px;text-transform:uppercase;letter-spacing:.08em;display:flex;align-items:center;gap:8px}}
.kt-section-label::after{{content:'';flex:1;height:1px;background:linear-gradient(90deg,rgba(212,168,67,.5),transparent)}}

/* capacity-risk cards (mirrors main dashboard) */
.cap-grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:16px;margin-bottom:28px}}
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
.cap-bar-marker::after{{content:'{CAPACITY}';position:absolute;top:-16px;left:50%;transform:translateX(-50%);font-size:.65rem;color:var(--gold);font-weight:700;white-space:nowrap}}
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

.kt-week-grid{{display:grid;grid-template-columns:1fr 1fr;gap:18px;margin-bottom:28px}}
@media (max-width:760px){{.kt-week-grid{{grid-template-columns:1fr}}}}

.kt-card{{background:var(--card);border:1px solid var(--card-border);border-radius:16px;padding:20px 22px;position:relative;overflow:hidden}}
.kt-card::before{{content:'';position:absolute;top:0;left:0;right:0;height:3px;background:linear-gradient(90deg,var(--purple-light),var(--gold));border-radius:16px 16px 0 0}}
.kt-head{{display:flex;align-items:center;gap:10px;margin-bottom:14px}}
.kt-emoji{{font-size:1.5rem;line-height:1}}
.kt-name{{font-size:1.2rem;font-weight:800;color:var(--text);flex:1;letter-spacing:-.01em}}
.kt-count{{font-size:1.5rem;font-weight:800;color:var(--gold);line-height:1;font-variant-numeric:tabular-nums}}

.kt-table{{width:100%;border-collapse:collapse;font-size:.86rem}}
.kt-table th{{text-align:left;padding:8px 10px;color:var(--text-dim);font-size:.66rem;text-transform:uppercase;letter-spacing:.06em;border-top:1px solid rgba(255,255,255,.06);border-bottom:1px solid rgba(255,255,255,.06);background:rgba(255,255,255,.015);font-weight:600}}
.kt-table th.kt-age, .kt-table td.kt-age{{text-align:right;width:62px;white-space:nowrap}}
.kt-table td{{padding:8px 10px;border-bottom:1px solid rgba(255,255,255,.03)}}
.kt-table tbody tr:last-child td{{border-bottom:none}}
.kt-table tbody tr:hover td{{background:rgba(255,255,255,.02)}}
.kt-table td.kt-ticket{{color:var(--purple-light);font-size:.8rem}}
.kt-warn{{cursor:help;margin-left:2px}}
.kt-nodob{{color:var(--text-dim)}}
.kt-purchaser{{font-size:.72rem;color:var(--text-dim);margin-top:2px;padding-left:8px;line-height:1.35}}
.kt-purchaser a{{color:var(--purple-light);text-decoration:none}}
.kt-purchaser a:hover{{text-decoration:underline}}
.kt-purchaser-warn{{color:#fbbf24;font-weight:600;cursor:help}}
.kt-purchaser-warn a{{color:#fbbf24;text-decoration:underline}}
.kt-purchaser-warn a:hover{{color:#fcd34d}}
.kt-empty{{text-align:center;padding:24px;color:var(--text-dim);font-size:.86rem;font-style:italic;border-top:1px solid rgba(255,255,255,.06)}}
</style>
</head>
<body>
<div class="container">
<header>
  <h1>Kids &amp; Teens</h1>
  <p>Mindvalley U 2026 — Tallinn, Estonia</p>
  <div class="timestamp">Data snapshot: {now_str}</div>
</header>

<div class="cat-grid">
  <div class="cat-card">
    <div class="cat-icon">🧒</div>
    <div class="cat-label">Kids (6-12)</div>
    <div class="cat-value">{kids['total']}</div>
    <div class="cat-breakdown">
      <div class="item"><div class="item-val">{kids['w1']}</div><div class="item-label">Week 1</div></div>
      <div class="item"><div class="item-val">{kids['w2']}</div><div class="item-label">Week 2</div></div>
      <div class="item"><div class="item-val">{kids['unassigned']}</div><div class="item-label">No Week Selected</div></div>
    </div>
  </div>
  <div class="cat-card">
    <div class="cat-icon">🧑</div>
    <div class="cat-label">Teens (13-17)</div>
    <div class="cat-value">{teens['total']}</div>
    <div class="cat-breakdown">
      <div class="item"><div class="item-val">{teens['w1']}</div><div class="item-label">Week 1</div></div>
      <div class="item"><div class="item-val">{teens['w2']}</div><div class="item-label">Week 2</div></div>
      <div class="item"><div class="item-val">{teens['unassigned']}</div><div class="item-label">No Week Selected</div></div>
    </div>
  </div>
</div>

<div class="kt-section-label">⚠️ Capacity Risk <span style="font-size:.75rem;font-weight:400;color:var(--text-dim);text-transform:none;letter-spacing:0;margin-left:8px">Cap. {CAPACITY} pax / category / week</span></div>
{cap_grid}

<div class="kt-section-label">Week 1 · July 20 – 26</div>
{w1_grid}

<div class="kt-section-label">Week 2 · July 27 – August 2</div>
{w2_grid}
{unass_section}

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
    hero, kids, teens, vip, fc, reg, threeday, cap, crew_list, vol_list, sg_data, refunds_by_tier = compute(regs)
    yoy = compute_yoy(regs, regs_2025)

    print("✍️  Writing event-dashboards/mvu-2026/index.html...")
    html = render_html(hero, kids, teens, vip, fc, reg, threeday, cap, crew_list, vol_list, sg_data, yoy, refunds_by_tier)
    import os
    os.makedirs("event-dashboards/mvu-2026", exist_ok=True)
    with open("event-dashboards/mvu-2026/index.html", "w", encoding="utf-8") as f:
        f.write(html)

    # Generate separate promo pages
    promo_pages = [
        ("Crew - Mindvalley Team", "🎫", "mycrewpass", crew_list, True),
        ("Volunteers", "🙋", "volunteers", vol_list, False),
    ]
    for name, emoji, slug, plist, flag_non_mv in promo_pages:
        path = f"event-dashboards/mvu-2026/{slug}.html"
        with open(path, "w", encoding="utf-8") as f:
            f.write(render_promo_page(emoji, name, plist, flag_non_mv))
        print(f"   {name}: {len(plist)} registrations -> {path}")

    # Special Guests page (replaces the old hexagon.html). 8 sub-categories
    # identified by promo code, each with their benefits as small tags.
    sg_path = "event-dashboards/mvu-2026/special-guests.html"
    with open(sg_path, "w", encoding="utf-8") as f:
        f.write(render_special_guests_page(sg_data))
    sg_total = sum(len(v) for v in sg_data.values())
    print(f"   Special Guests: {sg_total} registrations -> {sg_path}")

    # Review-mode preview (NOT linked from the sidebar — direct URL only).
    # Same data, but the Access column is a dropdown so the boss can propose
    # reassignments; proposals live in the URL, nothing writes to Bizzabo.
    sg_preview_path = "event-dashboards/mvu-2026/special-guests-preview.html"
    with open(sg_preview_path, "w", encoding="utf-8") as f:
        f.write(render_special_guests_page(sg_data, review=True))
    print(f"   Special Guests (review preview) -> {sg_preview_path}")

    # Kids & Teens page — one card per program × week, ages ascending,
    # 'Both Weeks' attendees appear in both W1 and W2 lists.
    kt_path = "event-dashboards/mvu-2026/kids-teens.html"
    with open(kt_path, "w", encoding="utf-8") as f:
        f.write(render_kids_teens_page(regs, kids, teens, cap))
    print(f"   Kids & Teens     -> {kt_path}")

    # Generate the standalone refunds analysis page (not linked from main dashboard)
    refunds_analysis_path = "event-dashboards/mvu-2026/refunds-analysis.html"
    with open(refunds_analysis_path, "w", encoding="utf-8") as f:
        f.write(render_refunds_analysis_page(regs, regs_2025))
    print(f"   Refunds analysis -> {refunds_analysis_path}")

    # Generate the standalone ticket-types page (not linked from main dashboard)
    ticket_types_path = "event-dashboards/mvu-2026/ticket-types.html"
    with open(ticket_types_path, "w", encoding="utf-8") as f:
        f.write(render_ticket_types_page(regs))
    print(f"   Ticket types     -> {ticket_types_path}")

    print("✅ Done!")
    print(f"   Valid tickets: {hero['valid_total']}  (paid:{hero['paid_total']} comped:{hero['comped_total']} refunded:{hero['refund_total']})")
    print(f"   Kids total: {kids['total']}  (W1:{kids['w1']} W2:{kids['w2']} Unass:{kids['unassigned']})")
    print(f"   Teens total: {teens['total']} (W1:{teens['w1']} W2:{teens['w2']} Unass:{teens['unassigned']})")
    if yoy.get("available_2025"):
        print(f"   YoY paid: 2025 to date={yoy.get('paid_2025_to_date', 0)} → 2026 to date={yoy.get('paid_2026_to_date', 0)}")
