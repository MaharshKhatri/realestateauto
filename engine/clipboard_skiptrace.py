"""Fast Skip-Trace Mode: the queue of owners worth finding, the manual lookup links, and a one-click parser for the
page the operator pastes back.

The people-search sites (TruePeopleSearch, FastPeopleSearch, CyberBackgroundChecks) forbid automated access, so the
code never fetches them. The operator clicks the link, selects the result page, copies it, and clicks "Paste raw page".
This module reads the paste (phones with their type and "last reported" date, e-mails, age, deceased markers, current
and previous addresses, relatives with ages), scrubs every number against the FTC data and the internal / Daniel's Law
lists (engine/compliance.py: those always win), ranks the best mobile, saves the contacts and flips READY_TO_CALL when
the lead now has an offer path and someone to call.

Queue order follows who converts from a desk: exempt estates (an heir with a house they never lived in), vacant
sheriff sales, lis pendens filings, absentee / portfolio owners, then everything else, by priority score. Listed
properties stay out of the queue: the listing agent is the route there and needs no skip trace.
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from agents.enrichment import skiptrace_pipeline as sp
from core.text import clean, format_phone
from db.models import ContactType, Lead, LeadStage, Property, SignalType, Valuation
from db.repository import upsert_contact
from engine import compliance
from engine.scoring import ready_to_call, valuation_suspect
from engine.scripts import lead_facts

SKIP_VERDICTS = {"PASS", "FRFPA_BLOCKED", "EQUITY_RESCUE_REFERRAL"}
TERMINAL = (LeadStage.DEAD, LeadStage.DO_NOT_CONTACT, LeadStage.CLOSED, LeadStage.UNDER_CONTRACT, LeadStage.ASSIGNED)
TAG_ORDER = ("EXEMPT_ESTATE", "VACANT_SHERIFF", "LIS_PENDENS", "ABSENTEE_PORTFOLIO", "OTHER")
TAG_LABELS = {"EXEMPT_ESTATE": "exempt estate", "VACANT_SHERIFF": "vacant sheriff sale", "LIS_PENDENS": "lis pendens",
              "ABSENTEE_PORTFOLIO": "absentee / portfolio owner", "OTHER": "other distress"}
KIND_ORDER = {"wireless": 0, "unknown": 1, "landline": 2, "voip": 3}

PHONE_RE = re.compile(r"(?<!\d)\(?(\d{3})\)?[-.\s]?(\d{3})[-.\s]?(\d{4})(?!\d)")
KIND_RE = re.compile(r"\b(wireless|mobile|cell(?:ular)?|land\s?line|voip)\b", re.I)
REPORTED_RE = re.compile(r"(?:last\s+)?reported\s*:?\s*\(?([A-Za-z]{3,9}\.?\s+\d{4}|\d{4}-\d{2}(?:-\d{2})?|\d{1,2}/\d{4}|\d{4})\)?",
                         re.I)
PRIMARY_RE = re.compile(r"\bprimary\b", re.I)
AGE_RE = re.compile(r"\bAge\s*:?\s*(\d{2,3})\b|\b(\d{2,3})\s+years?\s+old\b", re.I)
DECEASED_RE = re.compile(r"\b(deceased|died|passed away)\b", re.I)
EMAIL_RE = sp.EMAIL_RE
HEADER_RE = re.compile(r"^\s*(current (home )?address|previous addresses|past addresses|address history|phone numbers?|"
                       r"email addresses?|emails?|possible relatives|relatives|possible associates|associates|neighbors|"
                       r"aka|also known as|background report|reverse phone lookup|lives in|born|social profiles|"
                       r"education|employment|court records|criminal records|property records|work history)\s*:?\s*$", re.I)
SITE_RE = re.compile(r"truepeoplesearch|fastpeoplesearch|cyberbackgroundchecks|home\s+name\s+phone\s+address|"
                     r"search results|people search|reverse phone|terms of use|privacy policy|member support", re.I)
NAME_RE = re.compile(r"^[A-Z][A-Za-z'\-]+(?:\s+[A-Z][A-Za-z'\-\.]*){1,3}$")
MONTHS = {m: i for i, m in enumerate(("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"), 1)}


# -- the queue ----------------------------------------------------------------------------------------
def has_callable_phone(lead: Lead) -> bool:
    return any(c.phone and c.dnc_status != "dnc" and (c.confidence_score or 0) > 0 for c in lead.contacts)


def listed_with_agent(lead: Lead) -> bool:
    kinds = {s.signal_type for s in lead.property.active_signals}
    if not kinds & {SignalType.STALE_LISTING, SignalType.ACTIVE_LISTING}:
        return False
    return any(c.contact_type == ContactType.LISTING_AGENT and (c.phone or c.email) and c.dnc_status != "dnc"
               for c in lead.contacts)


def reason_tags(lead: Lead) -> list[str]:
    p = lead.property
    kinds = {s.signal_type for s in p.active_signals}
    estate = bool(p.is_estate_owned) or SignalType.ESTATE_OWNER in kinds
    exempt = str(lead.frfpa_track or "").startswith("EXEMPT") or not (kinds & {SignalType.SHERIFF_SALE, SignalType.LIS_PENDENS})
    tags: list[str] = []
    if estate and exempt:
        tags.append("EXEMPT_ESTATE")
    if SignalType.SHERIFF_SALE in kinds and SignalType.VACANT in kinds:
        tags.append("VACANT_SHERIFF")
    if SignalType.LIS_PENDENS in kinds:
        tags.append("LIS_PENDENS")
    portfolio = getattr(SignalType, "PORTFOLIO_OWNER", None)
    if p.is_absentee or (portfolio is not None and portfolio in kinds):
        tags.append("ABSENTEE_PORTFOLIO")
    return tags or ["OTHER"]


def _why(lead: Lead, tags: list[str], f: dict[str, Any]) -> str:
    bits = []
    if "EXEMPT_ESTATE" in tags:
        bits.append("estate-owned and not owner-occupied: an heir or executor decides, FRFPA does not apply")
    if "VACANT_SHERIFF" in tags:
        bits.append("vacant with a sheriff sale" + (f" in {f['days_to_sale']} days" if f.get("days_to_sale") is not None else ""))
    if "LIS_PENDENS" in tags:
        bits.append(f"foreclosure complaint filed ({f.get('county')} County clerk)")
    if "ABSENTEE_PORTFOLIO" in tags:
        bits.append("owner lives elsewhere" + (f" ({f['owner_state']})" if f.get("owner_state") and f["owner_state"] != "NJ" else ""))
    if f.get("equity_at_risk"):
        bits.append(f"about {sp_money(f['equity_at_risk'])} of equity")
    return "; ".join(bits) or "underwritten distress lead with no phone on file"


def sp_money(v: float | None) -> str:
    return "—" if v is None else f"${v:,.0f}"


def queue(session: Session, limit: int = 25, *, now: datetime | None = None) -> list[dict[str, Any]]:
    """Open, underwritten, unlisted leads with nobody to call yet, best skip-trace candidates first."""
    stmt = (select(Lead).join(Property, Lead.property_id == Property.id)
            .join(Valuation, (Valuation.property_id == Property.id) & Valuation.is_current.is_(True))
            .where(Lead.status_stage.notin_(list(TERMINAL))).order_by(Lead.priority_rank_score.desc()))
    rows: list[tuple[int, float, dict[str, Any]]] = []
    for lead in session.scalars(stmt).unique():
        v = lead.property.current_valuation
        if v is None or v.verdict in SKIP_VERDICTS or valuation_suspect(v):
            continue
        if has_callable_phone(lead) or listed_with_agent(lead):
            continue
        target = sp.skiptrace_target(lead)
        if target is None:
            continue
        tags = reason_tags(lead)
        f = lead_facts(lead)
        p = lead.property
        a = v.assumptions_json or {}
        rank = min(TAG_ORDER.index(t) for t in tags)
        rows.append((rank, -(lead.priority_rank_score or 0), {
            "lead_id": lead.id, "address": f["address"], "town": f.get("town"), "county": f.get("county"),
            "target": {"role": target.role, "name": target.name, "city": target.city, "state": target.state},
            "mailing": ", ".join(x for x in (p.owner_mailing_address, p.owner_city_state_zip) if x) or None,
            "tags": tags, "reasons": [TAG_LABELS[t] for t in tags], "why": _why(lead, tags, f),
            "links": sp.lookup_links(lead, target),
            "numbers": {"arv": v.arv_estimate, "mao_target": v.mao_conservative, "mao_walkaway": v.mao_aggressive,
                        "fee": v.target_assignment_fee, "verdict": v.verdict, "equity_at_risk": f.get("equity_at_risk"),
                        "buyer_max": (a.get("quant") or {}).get("buyer_max")},
            "sale_date": f.get("sale_date"), "days_to_sale": f.get("days_to_sale"), "frfpa_track": lead.frfpa_track,
            "stage": lead.status_stage, "priority": lead.priority_rank_score,
        }))
        if len(rows) >= limit * 6:
            break
    rows.sort(key=lambda t: (t[0], t[1]))
    return [r[2] for r in rows[:limit]]


# -- parsing the paste ---------------------------------------------------------------------------------
@dataclass
class PhoneHit:
    digits: str
    display: str
    kind: str                         # wireless / landline / voip / unknown
    last_reported: str | None
    reported_key: int                 # yyyymm, 0 when unknown
    primary: bool
    line: str

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ParsedPage:
    name: str | None = None
    age: int | None = None
    deceased: bool = False
    current_address: str | None = None
    previous_addresses: list[str] = field(default_factory=list)
    phones: list[PhoneHit] = field(default_factory=list)
    emails: list[str] = field(default_factory=list)
    relatives: list[dict[str, Any]] = field(default_factory=list)
    site: str | None = None
    notes: list[str] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        d = asdict(self)
        d["phones"] = [h.to_json() for h in self.phones]
        return d


def _reported_key(text: str | None) -> int:
    if not text:
        return 0
    t = text.strip().strip("()").replace(".", "")
    m = re.match(r"([A-Za-z]{3,9})\s+(\d{4})$", t)
    if m:
        return int(m.group(2)) * 100 + MONTHS.get(m.group(1)[:3].lower(), 0)
    m = re.match(r"(\d{4})-(\d{2})", t)
    if m:
        return int(m.group(1)) * 100 + int(m.group(2))
    m = re.match(r"(\d{1,2})/(\d{4})$", t)
    if m:
        return int(m.group(2)) * 100 + int(m.group(1))
    m = re.match(r"(\d{4})$", t)
    return int(m.group(1)) * 100 if m else 0


def _kind(word: str | None) -> str:
    w = (word or "").lower().replace(" ", "")
    if w in ("wireless", "mobile", "cell", "cellular"):
        return "wireless"
    if w == "landline":
        return "landline"
    if w == "voip":
        return "voip"
    return "unknown"


def _section(lines: list[str], names: tuple[str, ...]) -> list[str]:
    """Lines under the first header whose name is in `names`, up to the next header."""
    out: list[str] = []
    grab = False
    for line in lines:
        s = line.strip()
        if HEADER_RE.match(s) or re.match(r"^(current (home )?address|previous addresses|past addresses)\s*:?", s, re.I):
            if grab:
                break
            grab = re.sub(r"\s*:\s*$", "", s).strip().lower() in names
            continue
        if grab and s:
            out.append(s)
    return out


def parse_page(text: str) -> ParsedPage:
    page = ParsedPage()
    raw = (text or "").replace("\r", "")
    lines = raw.split("\n")
    low = raw.lower()
    page.site = next((s for s in ("truepeoplesearch", "fastpeoplesearch", "cyberbackgroundchecks") if s in low), None)
    # name: the first person-looking line that is not a site chrome line or a header
    for line in lines[:12]:
        s = line.strip()
        if not s or SITE_RE.search(s) or HEADER_RE.match(s) or PHONE_RE.search(s):
            continue
        if NAME_RE.match(s) and len(s.split()) <= 4:
            page.name = s
            break
    m = AGE_RE.search(raw)
    page.age = int(m.group(1) or m.group(2)) if m else None
    page.deceased = bool(DECEASED_RE.search("\n".join(lines[:15])))
    # addresses
    cur = _section(lines, ("current address", "current home address"))
    cur = [x for x in cur if not re.match(r"^\(", x)]
    if cur:
        page.current_address = ", ".join(cur[:2])
    else:
        m = re.search(r"lives in\s*:?\s*(.+)", raw, re.I)
        page.current_address = clean(m.group(1)) if m else None
    prev = [x for x in _section(lines, ("previous addresses", "past addresses", "address history")) if not re.match(r"^\(", x)]
    joined: list[str] = []
    i = 0
    while i < len(prev):
        line = prev[i]
        if i + 1 < len(prev) and re.search(r"\b[A-Z]{2}\s+\d{5}", prev[i + 1]) and not re.search(r"\b[A-Z]{2}\s+\d{5}", line):
            joined.append(f"{line}, {prev[i + 1]}")
            i += 2
        else:
            joined.append(line)
            i += 1
    page.previous_addresses = joined[:5]
    # phones: the number's own line plus the next three lines carry type / reported / primary
    seen: set[str] = set()
    for idx, line in enumerate(lines):
        for m in PHONE_RE.finditer(line):
            digits = "".join(m.groups())
            if digits in seen or digits.startswith(("0", "1")) or digits[:3] in ("800", "888", "877", "866", "855", "844", "833"):
                continue
            block = [line[m.end():]]                   # the number's own tail, then the lines up to the next number
            for nxt in lines[idx + 1: idx + 5]:
                if PHONE_RE.search(nxt):
                    break
                block.append(nxt.strip())
            window = " ".join(block)
            kind_m = KIND_RE.search(window)
            rep_m = REPORTED_RE.search(window)
            seen.add(digits)
            page.phones.append(PhoneHit(digits=digits, display=format_phone(digits), kind=_kind(kind_m.group(1) if kind_m else None),
                                        last_reported=rep_m.group(1).strip() if rep_m else None,
                                        reported_key=_reported_key(rep_m.group(1) if rep_m else None),
                                        primary=bool(PRIMARY_RE.search(window)), line=line.strip()))
    page.emails = sorted({e.lower() for e in EMAIL_RE.findall(raw) if not e.lower().endswith((".png", ".jpg"))})
    # relatives: "Name, 41" / "Name" + "Age 41" pairs under the relatives header
    rel_lines = _section(lines, ("possible relatives", "relatives"))
    rel: list[dict[str, Any]] = []
    for line in rel_lines:
        m = re.match(r"^(.+?),\s*(\d{2,3})$", line)
        if m and NAME_RE.match(m.group(1).strip()):
            rel.append({"name": m.group(1).strip(), "age": int(m.group(2))})
            continue
        m = re.match(r"^Age\s*:?\s*(\d{2,3})$", line, re.I)
        if m and rel and rel[-1].get("age") is None:
            rel[-1]["age"] = int(m.group(1))
            continue
        if NAME_RE.match(line) and not HEADER_RE.match(line):
            rel.append({"name": line, "age": None})
    page.relatives = rel[:10]
    if not page.phones:
        page.notes.append("no phone numbers on this page")
    if page.deceased:
        page.notes.append("the page marks this person as deceased: work the heirs / executor")
    return page


def rank(parsed: ParsedPage) -> list[PhoneHit]:
    """Wireless first, then most recently reported, then the primary flag; one entry per number."""
    seen: set[str] = set()
    out: list[PhoneHit] = []
    for h in sorted(parsed.phones, key=lambda h: (KIND_ORDER.get(h.kind, 1), -h.reported_key, not h.primary)):
        if h.digits in seen or len(h.digits) != 10:
            continue
        seen.add(h.digits)
        out.append(h)
    return out


# -- scrub and save ---------------------------------------------------------------------------------------
@dataclass
class ScrubbedHit:
    hit: PhoneHit
    status: str
    callable_now: bool
    reason: str

    def to_json(self) -> dict[str, Any]:
        return {**self.hit.to_json(), "status": self.status, "callable_now": self.callable_now, "reason": self.reason}


def scrub(session: Session, hits: list[PhoneHit], *, now: datetime | None = None) -> list[ScrubbedHit]:
    out: list[ScrubbedHit] = []
    hours = compliance.within_calling_hours(now)
    for h in hits:
        status = compliance.classify_number(session, h.digits)
        if status == "dnc":
            out.append(ScrubbedHit(h, status, False, "on a do-not-call list (FTC, internal stop request or Daniel's Law): letter only"))
        elif status == "not_checked":
            out.append(ScrubbedHit(h, status, False, "FTC data not loaded or stale for this area code: unscrubbed, load fresh data first"))
        elif not hours:
            out.append(ScrubbedHit(h, status, False, "clear, but outside 8am-9pm Eastern right now"))
        else:
            out.append(ScrubbedHit(h, status, True, "clear to call"))
    return out


@dataclass
class SaveResult:
    contacts_created: int = 0
    contacts_updated: int = 0
    best_phone: str | None = None
    best_status: str | None = None
    best_callable_now: bool = False
    ready_to_call: bool = False
    via: str | None = None
    deceased_flagged: bool = False
    relatives_saved: int = 0
    notes: list[str] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


def _confidence(h: PhoneHit, status: str, today: date) -> float:
    if status == "dnc":
        return 0.0
    recent = h.reported_key and h.reported_key >= (today.year - 2) * 100 + today.month
    base = {"wireless": 0.85 if recent else 0.7, "unknown": 0.55, "landline": 0.5, "voip": 0.4}[h.kind]
    return round(base + (0.05 if h.primary else 0.0), 2)


def save(session: Session, lead: Lead, parsed: ParsedPage, *, hits: list[ScrubbedHit] | None = None,
         source: str = "clipboard_paste", now: datetime | None = None) -> SaveResult:
    """Persist the paste as contacts (DNC numbers stay visible but blocked), then recompute READY_TO_CALL."""
    today = (now or datetime.now()).date()
    hits = hits if hits is not None else scrub(session, rank(parsed), now=now)
    target = sp.skiptrace_target(lead)
    role = target.role if target else "OWNER"
    ctype = sp._target_type(role)
    name = (target.name if target else None) or parsed.name
    res = SaveResult()
    stamp = f"{source} {today:%Y-%m-%d}"
    for i, sh in enumerate(hits):
        h = sh.hit
        note = ", ".join(x for x in (f"{h.kind}", f"last reported {h.last_reported}" if h.last_reported else None,
                                     "primary" if h.primary else None, f"age {parsed.age}" if parsed.age else None,
                                     "DECEASED on page" if parsed.deceased else None, stamp) if x)
        _, created = upsert_contact(session, lead=lead, contact_type=ctype, source=source, name=name, phone=h.digits,
                                    phone_type=h.kind, dnc_status=sh.status, confidence_score=_confidence(h, sh.status, today),
                                    notes=note)
        res.contacts_created += int(bool(created))
        res.contacts_updated += int(not created)
        if res.best_phone is None and sh.status != "dnc":
            res.best_phone, res.best_status, res.best_callable_now = h.display, sh.status, sh.callable_now
    for e in parsed.emails[:3]:
        _, created = upsert_contact(session, lead=lead, contact_type=ctype, source=source, name=name, email=e,
                                    confidence_score=0.6, notes=stamp)
        res.contacts_created += int(bool(created))
        res.contacts_updated += int(not created)
    estate = parsed.deceased or bool(lead.property.is_estate_owned) or any(
        s.signal_type == SignalType.ESTATE_OWNER for s in lead.property.active_signals)
    if estate:
        for r in parsed.relatives[:6]:
            _, created = upsert_contact(session, lead=lead, contact_type=ContactType.RELATIVE, source=source, name=r["name"],
                                        confidence_score=0.4,
                                        notes=f"relative of {name or 'the owner'}" + (f", age {r['age']}" if r.get("age") else "")
                                              + (" (owner reported deceased)" if parsed.deceased else ""))
            res.relatives_saved += 1
            res.contacts_created += int(bool(created))
    if parsed.deceased:
        res.deceased_flagged = True
        line = f"{today:%Y-%m-%d} skip trace: people-search page marks {name or 'the owner'} deceased; work the heirs / executor"
        lead.notes = "\n".join(x for x in (lead.notes, line) if x)
        res.notes.append("owner reported deceased: relatives saved as RELATIVE contacts; confirm the executor with the Surrogate")
    if (res.contacts_created or res.contacts_updated) and lead.status_stage == LeadStage.NEW:
        lead.status_stage = LeadStage.SKIP_TRACE
    session.flush()
    session.refresh(lead)
    res.ready_to_call, res.via = ready_to_call(lead)
    lead.ready_to_call = res.ready_to_call
    if not hits:
        res.notes.append("no phone saved: try the other two sites or the address search link")
    elif res.best_phone is None:
        res.notes.append("every number found is on a do-not-call list: send a letter (mail-merge) instead")
    session.flush()
    return res


def trace(session: Session, lead_id: int, text: str, *, now: datetime | None = None) -> dict[str, Any]:
    """Parse -> rank -> scrub -> save for one lead, plus the next lead in the queue."""
    lead = session.get(Lead, lead_id)
    if lead is None:
        raise ValueError(f"lead {lead_id} not found")
    parsed = parse_page(text)
    hits = scrub(session, rank(parsed), now=now)
    saved = save(session, lead, parsed, hits=hits, now=now)
    nxt = next((row for row in queue(session, limit=5, now=now) if row["lead_id"] != lead_id), None)
    return {"lead_id": lead.id, "parsed": parsed.to_json(), "hits": [h.to_json() for h in hits], "saved": saved.to_json(),
            "stage": lead.status_stage, "ready_to_call": lead.ready_to_call, "next": nxt}


# -- funding partners (Phase 0) ------------------------------------------------------------------------------
def partner_queue(session: Session, limit: int = 10) -> list[dict[str, Any]]:
    """The top open JV-partner targets with no phone yet (engine/partner_principals.py ranks and unmasks them)."""
    from db.models import JVPartner
    from engine import partner_principals as pp
    rows = [p for p in session.scalars(select(JVPartner)) if p.status in ("TARGET", "CONTACTED") and not p.phone]
    rows.sort(key=lambda p: -(p.velocity_score or 0))
    out = []
    for i, p in enumerate(rows[:limit], 1):
        r = pp.desk_row(p, i)
        r["kind"] = "partner"
        r["look_up"] = r["principal"] or ("the residents at the mailing address" if (p.mailing_kind or "").startswith("house")
                                          else "the LLC's registered agent / members")
        out.append(r)
    return out


def trace_partner(session: Session, partner_id: int, text: str, *, now: datetime | None = None) -> dict[str, Any]:
    """Paste the people-search page for a partner's principal: best callable mobile + e-mail onto the partner.

    Proposing a joint venture to an investor is still a sales call to whatever number the page shows, so every
    number is classified like any other (FTC registry, internal, Daniel's Law); a DNC number is never saved as the
    partner's phone.
    """
    from db.models import JVPartner
    p = session.get(JVPartner, partner_id)
    if p is None:
        raise ValueError(f"partner {partner_id} not found")
    parsed = parse_page(text)
    hits = scrub(session, rank(parsed), now=now)
    best = next((h for h in hits if h.status != "dnc"), None)
    today = (now or datetime.now()).date()
    if best is not None and not p.phone:
        p.phone, p.phone_status = best.hit.digits, best.status
    if parsed.emails and not p.email:
        p.email = parsed.emails[0]
    if parsed.name and not p.principal_name:
        p.principal_name = parsed.name
        p.principal_basis = f"people-search page you pasted on {today:%Y-%m-%d}"
    lines = [f"{today:%Y-%m-%d} skip trace ({parsed.site or 'pasted page'}): {parsed.name or 'name not on page'}"
             + (f", age {parsed.age}" if parsed.age else "")]
    lines += [f"  {h.hit.display} {h.hit.kind}{' last reported ' + h.hit.last_reported if h.hit.last_reported else ''}"
              f" [{h.status}]" for h in hits[:5]]
    p.notes = "\n".join(x for x in (p.notes, *lines) if x)
    session.flush()
    nxt = next((r for r in partner_queue(session, limit=5) if r["partner_id"] != partner_id), None)
    notes = []
    if not hits:
        notes.append("no phone on this page: try the address search or the other two sites")
    elif best is None:
        notes.append("every number on the page is on a do-not-call list: e-mail or write to this partner instead")
    elif best.status == "not_checked":
        notes.append("saved, but unscrubbed: load fresh FTC DNC data (manage.py dnc --load) before you call")
    return {"partner_id": p.id, "parsed": parsed.to_json(), "hits": [h.to_json() for h in hits],
            "saved": {"phone": format_phone(p.phone) if p.phone else None, "phone_status": p.phone_status,
                      "email": p.email, "principal": p.principal_name, "notes": notes}, "next": nxt}
