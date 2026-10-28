"""Inbound funnel: sellers who ask for an offer and cash buyers who register, from the site/ pages.

Three ways a submission arrives:
  * local / tunnelled site → POST /api/inbound/seller | /api/inbound/buyer (web/app.py), handled here at once;
  * static site (GitHub Pages / Netlify) → a form service (Formspree or any JSON webhook) collects submissions;
    export them (JSON or CSV) and run `python manage.py inbound --ingest export.json`;
  * mailto fallback → the e-mail body is "key: value" lines; save it and `--ingest message.txt`.

A seller submission becomes a property (NJGIN parcel match when reachable), an INBOUND_LEAD signal, an OWNER
contact carrying the form's written consent (so a registry number can still be called about this inquiry; an
internal / Daniel's Law stop always wins), an underwriting pass, a seller-stated occupancy, and a drafted reply
(e-mail + SMS) with the follow-up due now — speed to lead. A buyer registration becomes a cash_buyers row with
their buy box and is matched against current deals.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from core.config import settings
from core.text import format_phone, normalize_phone, normalize_street, parse_one_line_address, postal_title
from db.models import CashBuyer, ContactType, Interaction, Occupancy, Property, SignalType
from db.repository import ensure_lead, upsert_contact, upsert_property, upsert_signal

SOURCE = "inbound_web"
COUNTIES = ("Middlesex", "Union", "Essex", "Hudson", "Passaic", "Bergen", "Camden", "Mercer", "Monmouth")
SITUATIONS = {
    "behind_on_payments": "behind on payments / foreclosure", "inherited": "inherited / estate",
    "tenant": "tired landlord / tenant issues", "repairs": "needs repairs", "vacant": "vacant",
    "relocating": "relocating", "divorce": "divorce", "code_violations": "code violations / oil tank",
    "other": "other",
}
OCCUPANCY = {"owner": Occupancy.OWNER_OCCUPIED, "tenant": Occupancy.TENANT_OCCUPIED, "vacant": Occupancy.VACANT}
CONSENT_TEXT = ("By checking this box I agree that {company} may call or text me at the number I entered about "
                "selling my property. Consent is not a condition of any purchase. Reply STOP to opt out.")
BUYER_CONSENT_TEXT = ("By checking this box I agree that {company} may e-mail, call or text me about off-market "
                      "properties that match my criteria. Reply STOP or unsubscribe at any time.")


class InboundError(ValueError):
    pass


def _clean(v: Any, limit: int = 300) -> str:
    return re.sub(r"\s+", " ", str(v or "")).strip()[:limit]


def _truthy(v: Any) -> bool:
    return str(v).strip().lower() in ("1", "true", "yes", "on", "y", "checked")


def _list(v: Any) -> list[str]:
    if isinstance(v, list):
        return [_clean(x, 40) for x in v if _clean(x, 40)]
    return [x.strip() for x in re.split(r"[,;|]", str(v or "")) if x.strip()]


def submission_key(kind: str, data: dict[str, Any]) -> str:
    raw = "|".join([kind, _clean(data.get("email")).lower(), normalize_phone(data.get("phone")) or "",
                    _clean(data.get("address")).upper(), _clean(data.get("submitted_at"))])
    return hashlib.sha1(raw.encode()).hexdigest()[:16]


# -- parsing exported submissions ----------------------------------------------------------------------
KEY_ALIASES = {"full_name": "name", "your_name": "name", "phone_number": "phone", "street": "address",
               "property_address": "address", "town": "city", "zipcode": "zip", "zip_code": "zip",
               "llc_name": "llc", "company": "llc", "_date": "submitted_at", "created_at": "submitted_at",
               "date": "submitted_at", "form": "form_type", "type": "form_type"}


def _normalize_keys(row: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in row.items():
        key = re.sub(r"[^a-z0-9]+", "_", str(k).strip().lower()).strip("_")
        key = KEY_ALIASES.get(key, key)
        if key in out and out[key] not in (None, ""):
            if isinstance(out[key], list):
                out[key].append(v)
                continue
            out[key] = [out[key], v] if key in ("situation", "counties", "asset_classes") else out[key]
            continue
        out[key] = v
    return out


def parse_export(text: str, filename: str = "") -> list[dict[str, Any]]:
    """Formspree / webhook JSON (list, {"submissions": [...]}, or one object), CSV, or 'key: value' text."""
    s = text.strip()
    if not s:
        return []
    if s[0] in "[{":
        data = json.loads(s)
        if isinstance(data, dict):
            data = data.get("submissions") or data.get("data") or [data]
        return [_normalize_keys(r.get("data", r) if isinstance(r, dict) else {}) for r in data]
    if filename.lower().endswith(".csv") or ("," in s.splitlines()[0] and ":" not in s.splitlines()[0]):
        return [_normalize_keys(r) for r in csv.DictReader(io.StringIO(s))]
    row: dict[str, Any] = {}
    for line in s.splitlines():
        if ":" in line:
            k, _, v = line.partition(":")
            row[k] = v.strip()
    return [_normalize_keys(row)] if row else []


def form_type(row: dict[str, Any]) -> str:
    t = _clean(row.get("form_type")).lower()
    if t in ("buyer", "buyers", "cash_buyer"):
        return "buyer"
    if t in ("seller", "sellers"):
        return "seller"
    return "buyer" if (row.get("counties") or row.get("asset_classes") or row.get("llc")) and not row.get("address") \
        else "seller"


# -- sellers ---------------------------------------------------------------------------------------------
@dataclass
class InboundResult:
    lead_id: int
    property_id: int
    created: bool
    matched_parcel: bool
    valued: bool
    verdict: str | None
    response_email: str
    response_sms: str
    notes: list[str] = field(default_factory=list)


def guess_county(session: Session, zip_code: str | None, city: str | None) -> str | None:
    """County from our own parcels: most common county for the ZIP, else for the town name."""
    if zip_code:
        rows = session.execute(select(Property.county, func.count()).where(Property.zip_code == zip_code[:5])
                               .group_by(Property.county)).all()
        if rows:
            return Counter(dict(rows)).most_common(1)[0][0]
    if city:
        like = f"{city.strip().upper()}%"
        rows = session.execute(select(Property.county, func.count()).where(Property.municipality.ilike(like))
                               .group_by(Property.county)).all()
        if rows:
            return Counter(dict(rows)).most_common(1)[0][0]
    return None


def validate_seller(data: dict[str, Any]) -> dict[str, Any]:
    d = {k: _clean(data.get(k)) for k in ("name", "phone", "email", "address", "city", "zip", "county", "timeline",
                                          "asking_price", "occupancy", "notes", "submitted_at", "source")}
    d["notes"] = _clean(data.get("notes"), 1500)
    d["situation"] = [x for x in _list(data.get("situation")) if x in SITUATIONS]
    d["consent"] = _truthy(data.get("consent"))
    d["phone"] = normalize_phone(d["phone"]) or ""
    if d["email"] and not re.fullmatch(r"[^@\s]+@[^@\s]+\.[A-Za-z]{2,}", d["email"]):
        raise InboundError("email address looks invalid")
    if not d["address"]:
        raise InboundError("property address is required")
    if not (d["phone"] or d["email"]):
        raise InboundError("a phone number or an e-mail is required so we can reply")
    if data.get("website"):          # honeypot field humans never see
        raise InboundError("rejected")
    return d


def response_drafts(name: str, address: str, phone_ok: bool) -> tuple[str, str]:
    """First reply to a website seller: a short human note, the disclosures under an "About Us" line."""
    from engine import human_copy as hc
    op = hc.operator()
    first = hc.first_name(name)
    st = hc.street(address)
    me = " ".join(x for x in (op["first"] or op["name"], "with " + op["company"] if op["company"] else "") if x)
    paragraphs = [
        f"Thanks for getting in touch about {st}. This is {me}. Who lives there right now, and what shape is it in? "
        "A few photos or a quick video call would help me give you a fair as-is number. When would you ideally "
        "like to sell?"]
    summary = [
        f"Who we are: {op['company'] or 'We'} is a real estate investment company. We're not agents and we don't "
        "represent you. If we agree on a price we may buy the house ourselves or assign our contract to another "
        "investor.",
        "Your attorney: you should have your own attorney review any agreement. NJ contracts include a "
        "3-business-day attorney review.",
        "If the home is in foreclosure: free help is available from a HUD-approved housing counselor "
        "(1-800-569-4287) and Legal Services of New Jersey (1-888-576-5529). We can't stop or postpone a sale.",
        *hc.footer_lines(op, "Reply STOP if you'd rather not hear from us."),
    ]
    email = hc.compose(f"about {st}", f"Hi {first},", paragraphs, summary, summary_title="--- About Us ---", op=op)
    sms = (f"Hi {first}, it's {me}. Thanks for your request about {st}. Is now a good time for a quick call? "
           "Reply STOP to opt out.") if phone_ok else ""
    return f"Subject: {email.subject}\n\n{email.text}", sms


def ingest_seller(session: Session, data: dict[str, Any], *, gis: Any = None, underwrite: bool = True,
                  now: datetime | None = None) -> InboundResult:
    from engine import crm
    from engine.scoring import active_weights, apply_score
    now = now or datetime.now()
    d = validate_seller(data)
    notes: list[str] = []
    # Only parse a full one-line address when the town is known; otherwise the parser would split the street name
    # into a "city" ("41 Sample Ave, NJ" -> street "41", city "Sample Ave").
    line = ", ".join(x for x in (d["address"], d["city"], f"NJ {d['zip']}".strip()) if x)
    parsed = parse_one_line_address(line if re.search(r"\d{5}", line) else f"{line} 00000") if d["city"] or \
        re.search(r",\s*[A-Za-z .'-]+,?\s*NJ", d["address"], re.I) else None
    street = (parsed.street if parsed else d["address"]).upper()
    city = postal_title(d["city"] or (parsed.city if parsed else ""))
    zip5 = (d["zip"] or (parsed.zip if parsed and parsed.zip != "00000" else ""))[:5]
    county = d["county"].title() if d["county"].title() in COUNTIES else guess_county(session, zip5, city)
    if county is None:
        raise InboundError("we could not tell which county the property is in — please choose it from the list")
    feature = None
    if gis is not None:
        try:
            mun = gis.resolve_municipality(county, city) if city else None
            feature = gis.find_by_address(county, street, mun[1] if mun else None)
        except Exception as exc:  # noqa: BLE001 - NJGIN down: keep the lead, match later
            notes.append(f"parcel lookup failed: {type(exc).__name__}")
    if feature is not None:
        from agents.scrapers.njgis_client import feature_to_property
        pdata = feature_to_property(feature)
    else:
        pdata = {"parcel_id": f"{county.upper()}-INBOUND-{re.sub(r'[^A-Z0-9]+', '-', normalize_street(street))}"
                              f"-{zip5 or 'NOZIP'}", "county": county, "street_address": street}
        notes.append("no NJGIN parcel match yet — check the address with the seller")
    pdata.update({"county": county, "city": city or pdata.get("city"), "zip_code": zip5 or pdata.get("zip_code")})
    prop, _ = upsert_property(session, pdata, source=SOURCE, overwrite=False)
    key = submission_key("seller", d)
    upsert_signal(session, prop, signal_type=SignalType.INBOUND_LEAD, source=SOURCE, source_ref=key,
                  severity_weight=1.0, active=True,
                  raw_metadata_json={"situation": [SITUATIONS[s] for s in d["situation"]], "timeline": d["timeline"],
                                     "asking_price": d["asking_price"], "occupancy": d["occupancy"],
                                     "notes": d["notes"], "submitted_at": d["submitted_at"] or now.isoformat(),
                                     "form_source": d["source"] or "site"})
    lead, created = ensure_lead(session, prop, primary_source=SOURCE, contact_name=d["name"] or None)
    company = settings().operator.get("company_name") or ""
    consent = {"text": CONSENT_TEXT.format(company=company), "at": d["submitted_at"] or now.isoformat(),
               "source": d["source"] or "site"} if d["consent"] and d["phone"] else None
    upsert_contact(session, lead=lead, contact_type=ContactType.OWNER, source=SOURCE, name=d["name"] or None,
                   phone=d["phone"] or None, email=d["email"] or None, confidence_score=1.0,
                   dnc_status="consented" if consent else None,
                   notes="Submitted the seller form on the website" + ("" if consent or not d["phone"] else
                                                                        " — no call/text consent: reply by e-mail"),
                   raw_json={"consent": consent} if consent else {})
    session.flush()
    session.refresh(lead)
    valued = False
    if underwrite and gis is not None:
        from engine.underwriter import Underwriter, _default_listing_provider
        try:
            valued = Underwriter(gis=gis, listing_provider=_default_listing_provider()).underwrite(session, prop) \
                is not None
        except Exception as exc:  # noqa: BLE001
            notes.append(f"underwriting failed: {type(exc).__name__}")
    if d["occupancy"] in OCCUPANCY:
        crm.confirm_occupancy(session, lead, OCCUPANCY[d["occupancy"]], "seller on the website form",
                              rescore_lead=False)
    version, weights = active_weights(session)
    apply_score(lead, version, weights)
    address = f"{postal_title(street)}, {city}".strip(", ")
    email, sms = response_drafts(d["name"], address, bool(consent))
    lead.next_followup_at = now
    session.add(Interaction(lead_id=lead.id, channel="web", outcome="INBOUND_RECEIVED",
                            notes=f"Website seller form: {', '.join(d['situation']) or 'no situation given'}"
                                  + (f"; timeline {d['timeline']}" if d["timeline"] else ""),
                            debrief_json={"response": {"email": email, "sms": sms}, "submission": key},
                            next_followup_at=now, occurred_at=now))
    session.flush()
    v = prop.current_valuation
    return InboundResult(lead_id=lead.id, property_id=prop.id, created=created, matched_parcel=feature is not None,
                         valued=valued, verdict=v.verdict if v else None, response_email=email, response_sms=sms,
                         notes=notes)


# -- buyers ----------------------------------------------------------------------------------------------
ASSET_CLASSES = {"1-4_family": "1-4 family", "fix_and_flip": "fix & flip", "buy_and_hold": "buy & hold",
                 "multifamily_5plus": "5+ units", "land": "land / new construction"}
REHAB = {"light": "LIGHT", "medium": "MEDIUM", "heavy": "HEAVY"}
POF = {"cash_ready": "cash ready", "hard_money": "hard money / private lender", "on_request": "on request"}


def _money_field(v: Any) -> float | None:
    s = str(v or "").strip().lower().replace("$", "").replace(",", "")
    if not s:
        return None
    mult = 1_000 if s.endswith("k") else 1_000_000 if s.endswith("m") else 1
    try:
        return float(s.rstrip("km")) * mult
    except ValueError:
        return None


def ingest_buyer(session: Session, data: dict[str, Any], now: datetime | None = None) -> tuple[CashBuyer, bool]:
    now = now or datetime.now()
    name, llc = _clean(data.get("name"), 120), _clean(data.get("llc"), 160)
    email, phone = _clean(data.get("email"), 120).lower(), normalize_phone(data.get("phone")) or None
    if data.get("website"):
        raise InboundError("rejected")
    if not name or not (email or phone):
        raise InboundError("name and an e-mail or phone are required")
    if email and not re.fullmatch(r"[^@\s]+@[^@\s]+\.[A-Za-z]{2,}", email):
        raise InboundError("email address looks invalid")
    if not _truthy(data.get("consent")):
        raise InboundError("please tick the box to receive deals — we only send them with your permission")
    counties = [c.title() for c in _list(data.get("counties")) if c.title() in COUNTIES]
    classes = [ASSET_CLASSES[c] for c in _list(data.get("asset_classes")) if c in ASSET_CLASSES]
    key = "web:" + (email or phone or "")
    b = session.scalar(select(CashBuyer).where(CashBuyer.entity_key == key))
    created = b is None
    if b is None:
        b = CashBuyer(entity_key=key, entity_name=llc or name, cash_purchases_count=0, flips_count=0, total_volume=0)
        session.add(b)
    b.entity_name, b.contact_name, b.email, b.phone = llc or name, name, email or b.email, phone or b.phone
    b.target_counties = counties or b.target_counties or []
    b.asset_classes = classes or b.asset_classes or []
    b.buyer_type = ("flipper" if "fix & flip" in classes else "portfolio_landlord" if "buy & hold" in classes
                    else b.buyer_type or "investor")
    b.max_rehab = REHAB.get(_clean(data.get("max_rehab")).lower(), b.max_rehab)
    b.min_arv = _money_field(data.get("min_price")) or b.min_arv
    b.max_arv = _money_field(data.get("max_price")) or b.max_arv
    b.proof_of_funds = POF.get(_clean(data.get("proof_of_funds")).lower(), b.proof_of_funds)
    b.registered_at = now
    b.source = SOURCE
    b.reliability_score = max(b.reliability_score or 0, 40.0 if b.proof_of_funds == "cash ready" else 25.0)
    session.flush()
    from engine.buyer_matcher import run_matching
    run_matching(session)
    return b, created


def ingest_rows(session: Session, rows: list[dict[str, Any]], *, gis: Any = None, underwrite: bool = True
                ) -> dict[str, Any]:
    out: dict[str, Any] = {"sellers": [], "buyers": [], "errors": []}
    for row in rows:
        try:
            if form_type(row) == "buyer":
                b, created = ingest_buyer(session, row)
                out["buyers"].append({"buyer_id": b.id, "name": b.contact_name, "created": created})
            else:
                r = ingest_seller(session, row, gis=gis, underwrite=underwrite)
                out["sellers"].append(r.__dict__)
        except InboundError as exc:
            out["errors"].append(str(exc))
    return out


def ingest_file(session: Session, path: Path, **kw: Any) -> dict[str, Any]:
    return ingest_rows(session, parse_export(path.read_text(encoding="utf-8"), path.name), **kw)


def site_config(endpoint: str | None = None, buyer_endpoint: str | None = None, mode: str | None = None
                ) -> dict[str, Any]:
    """What the static pages need (served at /site/config.js locally, written by `manage.py site --build`)."""
    op = settings().operator
    digits = normalize_phone(op.get("phone")) or ""
    return {"company": op.get("company_name") or "", "operator": op.get("name") or "",
            "phone": format_phone(digits) if digits else "", "email": op.get("email") or "",
            "mailing_address": op.get("mailing_address") or "",
            "mode": mode or ("endpoint" if endpoint else "mailto"),
            "seller_endpoint": endpoint or "", "buyer_endpoint": buyer_endpoint or endpoint or "",
            "consent_seller": CONSENT_TEXT.format(company=op.get("company_name") or "us"),
            "consent_buyer": BUYER_CONSENT_TEXT.format(company=op.get("company_name") or "us"),
            "counties": list(COUNTIES)}
