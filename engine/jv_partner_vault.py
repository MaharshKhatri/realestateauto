"""JV partner & proof-of-funds vault: the funding partners an operator with no capital needs before any MLS offer.

A listing agent wants proof of funds and a deposit with an offer. The operator has neither, so the deal is done
with a local cash buyer / rehabber as a joint-venture partner: the partner's LLC and proof of funds back the offer,
the partner's deposit goes into escrow, the partner (or their contractor) walks the house, and the net profit is
split under the JV agreement (engine/contracts.py build_jv). Both are principals in the deal; nobody is paid a
referral fee for finding a buyer or seller (N.J.S.A. 45:15-3).

  * `seed()` fills the vault with the highest-velocity named repeat buyers on record (county deed clusters,
    registered web buyers), ranked by recent purchases, flips and activity in our counties.
  * `outreach()` writes the plain first touch to a partner (e-mail, text, 3-sentence call) from public deed facts
    and the operator's own one-sentence intro (settings operator.intro; it must be true).
  * `attach_pof()` stores the partner's proof-of-funds document under data/partners/ (git-ignored; it is their
    bank information) and marks the partner verified. `pof_share_ok` records that the partner agreed we may show
    it to listing agents; without it the offer only says proof of funds is available on request.
  * `best_partner()` picks the partner whose buy box covers a deal, so offers and replies can honestly say the
    deal is being acquired with a verified local capital partner.
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from core.config import DATA_DIR, settings
from core.text import format_phone, is_entity, muni_base, postal_title
from db.models import CashBuyer, JVPartner, Lead
from engine import human_copy as hc
from engine.scripts import buyer_display_name, lead_facts

PARTNERS_DIR = DATA_DIR / "partners"
STATUSES = ("TARGET", "CONTACTED", "ACTIVE_PARTNER", "DECLINED")
PRIORITY_COUNTIES = ("Middlesex", "Union", "Essex", "Hudson", "Monmouth")
POF_SUFFIXES = (".pdf", ".png", ".jpg", ".jpeg")
MAX_POF_BYTES = 15 * 1024 * 1024


def _cfg() -> dict[str, Any]:
    return settings().get("autopilot", {}) or {}


def partners_needed() -> int:
    return int(_cfg().get("partners_needed", 3))


# -- seeding -----------------------------------------------------------------------------------------
def _recent(b: CashBuyer, days: int = 730, today: date | None = None) -> list[dict[str, Any]]:
    cutoff = ((today or date.today()) - timedelta(days=days)).isoformat()
    return [x for x in (b.recent_purchases_json or []) if isinstance(x, dict) and str(x.get("deed_date") or "") >= cutoff]


def velocity(b: CashBuyer, today: date | None = None) -> float:
    """Purchases in the last 24 months (weighted to our priority counties), flips, and recency."""
    recent = _recent(b, today=today)
    counties = {str(x.get("county") or "").title() for x in recent}
    score = 3.0 * len(recent) + 6.0 * (b.flips_count or 0) + 0.1 * (b.reliability_score or 0)
    score += 4.0 * len(counties & set(PRIORITY_COUNTIES))
    if b.last_purchase_date:
        months = ((today or date.today()) - b.last_purchase_date).days / 30.4
        score += max(0.0, 6 - months * 0.25)
    if b.registered_at:
        score += 5.0                                   # asked us for deals on the website
    return round(score, 1)


def _buy_box(b: CashBuyer) -> dict[str, Any]:
    recent = _recent(b, days=1095)
    prices = sorted(float(x["price"]) for x in recent if x.get("price"))
    towns = sorted({str(x.get("municipality") or "") for x in recent if x.get("municipality")})
    counties = sorted({str(x.get("county") or "").title() for x in recent if x.get("county")} |
                      {str(c).title() for c in (b.target_counties or [])})
    return {"min_price": (b.min_arv or (prices[0] if prices else None)),
            "max_price": (b.max_arv or (prices[-1] * 1.15 if prices else None)),
            "counties": counties, "towns": towns[:12], "max_rehab": b.max_rehab}


def seed(session: Session, top: int = 30, today: date | None = None) -> list[JVPartner]:
    """Fill the vault with the top named repeat buyers (idempotent: keyed on the cash_buyers row)."""
    ranked: list[tuple[float, CashBuyer]] = []
    for b in session.scalars(select(CashBuyer)):
        if not buyer_display_name(b):
            continue                                   # unnamed mailing clusters: nobody to write to
        if (b.cash_purchases_count or 0) < 2 and not b.registered_at and not (b.flips_count or 0):
            continue
        ranked.append((velocity(b, today), b))
    ranked.sort(key=lambda t: (-t[0], -(t[1].cash_purchases_count or 0)))
    from engine.partner_principals import family_key
    existing = {p.buyer_id: p for p in session.scalars(select(JVPartner).where(JVPartner.buyer_id.is_not(None)))}
    families = {family_key(p.buyer) for p in existing.values() if p.buyer is not None}
    out: list[JVPartner] = []
    for score, b in ranked:
        if len(out) >= top:
            break
        box = _buy_box(b)
        p = existing.get(b.id)
        fam = family_key(b)
        if p is None and fam and fam in families:
            continue                                   # another LLC of an operator already in the vault
        families.add(fam)
        if p is None:
            p = JVPartner(buyer_id=b.id, entity_name=b.entity_name, status="TARGET")
            session.add(p)
        p.contact_name = p.contact_name or (b.contact_name if b.contact_name and not is_entity(b.contact_name.upper())
                                            else None)
        p.phone, p.email = p.phone or b.phone, p.email or b.email
        p.target_counties = p.target_counties or box["counties"]
        p.target_towns = p.target_towns or box["towns"]
        p.min_price, p.max_price = p.min_price or box["min_price"], p.max_price or box["max_price"]
        p.max_rehab = p.max_rehab or box["max_rehab"]
        p.velocity_score = score
        out.append(p)
    session.flush()
    return out


# -- queries -----------------------------------------------------------------------------------------
def partners(session: Session, status: str | None = None) -> list[JVPartner]:
    stmt = select(JVPartner)
    if status:
        stmt = stmt.where(JVPartner.status == status)
    rows = list(session.scalars(stmt))
    order = {s: i for i, s in enumerate(("ACTIVE_PARTNER", "CONTACTED", "TARGET", "DECLINED"))}
    rows.sort(key=lambda p: (order.get(p.status, 9), not p.pof_verified, -(p.velocity_score or 0)))
    return rows


def active_count(session: Session) -> int:
    return sum(1 for p in partners(session, "ACTIVE_PARTNER") if p.pof_verified)


def phase0_open(session: Session) -> bool:
    return active_count(session) < partners_needed()


def best_partner(session: Session, lead: Lead, *, need_pof: bool = False, price: float | None = None
                 ) -> JVPartner | None:
    """The active partner whose buy box covers this deal (county, price band, rehab), best proof first."""
    from engine.contracts import end_buyer_price
    county = (lead.property.county or "").title()
    town = muni_base(lead.property.municipality)
    price = price or end_buyer_price(lead)
    v = lead.property.current_valuation
    tier = {"LIGHT": 1, "MEDIUM": 2, "HEAVY": 3}.get((v.rehab_tier if v else None) or "", 2)
    cands = []
    for p in partners(session, "ACTIVE_PARTNER"):
        if need_pof and not p.pof_verified:
            continue
        counties = {str(c).title() for c in (p.target_counties or [])}
        if counties and county not in counties:
            continue
        if price and p.max_price and price > p.max_price * 1.05:
            continue
        if price and p.min_price and price < p.min_price * 0.6:
            continue
        if p.max_rehab and {"LIGHT": 1, "MEDIUM": 2, "HEAVY": 3}.get(p.max_rehab, 3) < tier:
            continue
        towns = {muni_base(t) for t in (p.target_towns or [])}
        cands.append(((town in towns), p.pof_verified, p.provides_emd, p.will_walk_properties,
                      p.velocity_score or 0, p))
    if not cands:
        return None
    cands.sort(key=lambda t: t[:5], reverse=True)
    return cands[0][5]


def pof_language(session: Session, lead: Lead, price: float | None = None) -> str | None:
    """One true sentence for offers and replies, only when a verified partner backs this deal."""
    p = best_partner(session, lead, need_pof=True, price=price)
    if p is None:
        return None
    shown = "attached" if p.pof_share_ok and p.pof_document_path else "available on request"
    return f"Acquiring with our verified local capital partner (proof of funds {shown})."


# -- outreach ----------------------------------------------------------------------------------------
@dataclass
class PartnerCard:
    partner_id: int
    name: str
    first: str
    entity: str
    phone: str | None
    email: str | None
    status: str
    recent: dict[str, Any] | None
    subject: str
    body: str
    sms: str
    phone_script: list[str]
    mailto: str | None
    reasons: list[str] = field(default_factory=list)
    style: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _recent_purchase(p: JVPartner) -> dict[str, Any] | None:
    if p.buyer is None:
        return None
    recent = [x for x in (p.buyer.recent_purchases_json or []) if isinstance(x, dict) and x.get("address")]
    recent.sort(key=lambda x: str(x.get("deed_date") or ""), reverse=True)
    if not recent:
        return None
    x = recent[0]
    # Deed exports carry the county; MOD-IV purchases carry the municipality. Say what we actually know.
    town = postal_title(muni_base(x.get("municipality"))) if x.get("municipality") else ""
    place = town or (f"{str(x.get('county')).title()} County" if x.get("county") else "New Jersey")
    return {"address": postal_title(x.get("address")), "town": place, "price": x.get("price"), "date": x.get("deed_date"),
            "count_24m": len(_recent(p.buyer))}


def outreach(session: Session, p: JVPartner) -> PartnerCard:
    """Plain first touch: the public deed fact, who we are (one true sentence), the ask."""
    op = hc.operator()
    intro = re.sub(r"\s+", " ", str(settings().operator.get("intro") or "")).strip().rstrip(".")
    recent = _recent_purchase(p)
    person = p.contact_name or p.principal_name
    first = hc.first_name(person.split(" & ")[0]) if person and not is_entity(person.upper()) else "there"
    entity = p.entity_name if is_entity(p.entity_name.upper()) else "you"
    saw = (f"Saw from the county deed records that {entity} bought {recent['address']} in {recent['town']}."
           if recent else f"Saw from the county deed records that {entity} has been buying houses in "
           f"{', '.join(c for c in (p.target_counties or [])[:2]) or 'New Jersey'}.")
    me = (intro + ". " if intro else "")
    paragraphs = [
        f"{saw} {me}Since I only source and underwrite off-market and distressed deals from my desk, I'm looking "
        "for two active local rehabbers where I bring you the deals that fit your exact buy box and we lock them "
        "up under your LLC and proof of funds as a joint venture. What towns and price points are you buying "
        "right now?"]
    summary = ["How it works: you pick the deals, your LLC signs and your deposit goes into escrow, you or your "
               "contractor walk the house, and we split the net profit under a one-page JV agreement (both of us "
               "as principals; no referral fees).",
               "I'm a real estate investor, not a licensed agent, and every deal gets a 3-business-day attorney "
               "review.", *hc.footer_lines(op, "Reply STOP and I won't e-mail again.")]
    subject = hc.pick([f"deals in {recent['town']}" if recent else "off-market deals", "quick question for a rehabber",
                       f"{recent['town']} buy box" if recent else "your buy box"], p.id)
    email = hc.compose(subject, f"Hi {first},", paragraphs, summary, summary_title="--- Details ---", op=op)
    who = " ".join(x for x in (op["first"] or op["name"], "with " + op["company"] if op["company"] else "") if x)
    sms = (f"Hi {first}, it's {who}. Saw {entity} bought {recent['address']} in {recent['town']}. " if recent else
           f"Hi {first}, it's {who}. ") + ("I find off-market NJ fixers from my desk and I'm looking for a couple "
                                           "of local rehabbers to JV with. What's your buy box? Reply STOP to opt out.")
    script = [f"Hi {first}, this is {who}. {saw}",
              (me + "I only source and underwrite off-market and distressed deals, so I'm looking for two local "
                    "rehabbers to lock them up with under your LLC and proof of funds, as a joint venture.").strip(),
              "What towns and price points are you buying right now, and could you send me a proof of funds letter "
              "so I can write offers in your name?"]
    reasons = []
    fam = p.family_stats or {}
    if fam.get("purchases_36m"):
        llcs = len(p.family_entities or [])
        reasons.append(f"{fam['purchases_36m']} recorded purchases in 36 months, {hc.money(fam.get('deployed'))} deployed"
                       + (f", {llcs} LLCs at one mailing address" if llcs > 1 else ""))
    if recent:
        reasons.append(f"latest {recent['address']}, "
                       f"{recent['town']} ({hc.money(recent['price'])}, {recent['date']})")
    if p.buyer and p.buyer.flips_count:
        reasons.append(f"{p.buyer.flips_count} confirmed flip(s)")
    if p.target_counties:
        reasons.append("buys in " + ", ".join(str(c) for c in p.target_counties[:4]))
    return PartnerCard(partner_id=p.id, name=p.contact_name or p.entity_name, first=first, entity=p.entity_name,
                       phone=format_phone(p.phone) if p.phone else None, email=p.email, status=p.status, recent=recent,
                       subject=email.subject, body=email.text, sms=hc.plain(sms), phone_script=[hc.plain(s) for s in script],
                       mailto=email.mailto(p.email), reasons=reasons, style=hc.lint(email.text))


def phase0_cards(session: Session, limit: int = 5, today: date | None = None) -> list[dict[str, Any]]:
    """Partners to reach today, until `partners_needed` are active with proof of funds."""
    if not phase0_open(session):
        return []
    cutoff = datetime.combine((today or date.today()) - timedelta(days=7), datetime.min.time())
    rows = [p for p in partners(session) if p.status in ("TARGET", "CONTACTED")
            and (p.last_contact_at is None or p.last_contact_at < cutoff)]
    return [outreach(session, p).to_dict() for p in rows[:limit]]


# -- updates -----------------------------------------------------------------------------------------
def log_touch(session: Session, p: JVPartner, channel: str, notes: str = "", when: datetime | None = None) -> JVPartner:
    when = when or datetime.now()
    p.last_contact_at = when
    if p.status == "TARGET":
        p.status = "CONTACTED"
    line = f"{when:%Y-%m-%d} {channel}: {notes}".strip()
    p.notes = "\n".join(x for x in (p.notes, line) if x)
    session.flush()
    return p


def update(session: Session, p: JVPartner, **fields: Any) -> JVPartner:
    allowed = {"contact_name", "entity_name", "phone", "email", "target_counties", "target_towns", "min_price",
               "max_price", "max_rehab", "pof_verified", "pof_share_ok", "provides_emd", "will_walk_properties",
               "default_split_pct", "status", "notes"}
    for k, v in fields.items():
        if k not in allowed or v is None:
            continue
        if k == "status" and v not in STATUSES:
            raise ValueError(f"status must be one of {', '.join(STATUSES)}")
        if k == "phone":
            v = "".join(ch for ch in str(v) if ch.isdigit())[-10:] or None
        setattr(p, k, v)
    if fields.get("pof_verified") and not p.pof_verified_at:
        p.pof_verified_at = datetime.now()
    session.flush()
    return p


def attach_pof(session: Session, p: JVPartner, filename: str, data: bytes, root: Path | None = None) -> Path:
    """Store the partner's proof-of-funds document (never committed) and mark the partner verified."""
    suffix = Path(filename).suffix.lower()
    if suffix not in POF_SUFFIXES:
        raise ValueError("upload the proof of funds as a PDF or an image (png / jpg)")
    if not data or len(data) > MAX_POF_BYTES:
        raise ValueError("the file is empty or larger than 15 MB")
    folder = (root or PARTNERS_DIR) / str(p.id)
    folder.mkdir(parents=True, exist_ok=True)
    dest = folder / f"pof_{date.today():%Y-%m-%d}{suffix}"
    dest.write_bytes(data)
    try:
        dest.chmod(0o600)
    except OSError:
        pass
    p.pof_document_path = str(dest)
    p.pof_verified = True
    p.pof_verified_at = datetime.now()
    if p.status in ("TARGET", "CONTACTED"):
        p.status = "ACTIVE_PARTNER"
    session.flush()
    return dest


def partner_dict(p: JVPartner) -> dict[str, Any]:
    return {"id": p.id, "buyer_id": p.buyer_id, "contact_name": p.contact_name, "entity_name": p.entity_name,
            "name": p.contact_name or p.entity_name, "phone": p.phone,
            "phone_display": format_phone(p.phone) if p.phone else None, "email": p.email,
            "target_counties": p.target_counties or [], "target_towns": p.target_towns or [],
            "min_price": p.min_price, "max_price": p.max_price, "max_rehab": p.max_rehab,
            "pof_verified": bool(p.pof_verified), "pof_verified_at": p.pof_verified_at.isoformat() if p.pof_verified_at
            else None, "pof_document": Path(p.pof_document_path).name if p.pof_document_path else None,
            "pof_share_ok": bool(p.pof_share_ok), "provides_emd": bool(p.provides_emd),
            "will_walk_properties": bool(p.will_walk_properties), "default_split_pct": p.default_split_pct,
            "status": p.status, "velocity_score": p.velocity_score,
            "last_contact_at": p.last_contact_at.isoformat() if p.last_contact_at else None, "notes": p.notes,
            "recent": _recent_purchase(p), "principal_name": p.principal_name, "principal_basis": p.principal_basis,
            "mailing_address": p.mailing_address, "mailing_kind": p.mailing_kind, "phone_status": p.phone_status,
            "family_entities": p.family_entities or [], "family_stats": p.family_stats or {}}


def deal_partner_summary(session: Session, lead: Lead) -> dict[str, Any]:
    """What the dashboard shows on a deal: who backs it, and who to call if nobody does yet."""
    p = best_partner(session, lead)
    matched = [{"buyer_id": m.buyer.id, "name": buyer_display_name(m.buyer) or m.buyer.entity_name,
                "phone": format_phone(m.buyer.phone) if m.buyer.phone else None, "email": m.buyer.email}
               for m in lead.buyer_matches[:3]]
    f = lead_facts(lead)
    return {"partner": partner_dict(p) if p else None, "pof_sentence": pof_language(session, lead),
            "active_partners": active_count(session), "partners_needed": partners_needed(),
            "call_now": matched if p is None else [],
            "note": (f"No verified partner covers {f.get('town')}: call the matched buyers above to co-sign this offer, "
                     "or add a partner in the Vault." if p is None else "")}
