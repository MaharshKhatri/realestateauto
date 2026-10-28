"""Free tax-delinquent / tax-sale lists from the towns, by Open Public Records Act request (N.J.S.A. 47:1A-1 et seq.,
as amended by P.L.2024, c.16 — in force Sept 3, 2024).

What the law requires of the request (sources in config/opra_towns.yaml → rules):
  * a letter or e-mail is a valid request when it carries everything on the town's adopted form: name, address,
    e-mail, phone, the records sought, and the certifications (47:1A-5(f)); towns may route requests to a portal;
  * one agency per request (47:1A-5(g));
  * the commercial-purpose certification is mandatory and must be true. Buying houses is a commercial use, so every
    request says "I WILL use the requested records for a commercial purpose" (intentionally failing to certify it:
    $1,000 / $2,500 / $5,000 civil penalty, 47:1A-11(b)). The custodian then has up to 14 business days;
  * electronic records are free apart from actual supply costs (47:1A-5(b)); a special service charge must be
    quoted first, so the request asks for the amount before any fee over $5;
  * the custodian need not create a record, so the request names existing tax-collector reports (tax sale list,
    municipal-held certificate list, Tax Search Export) — the GRC has ordered these disclosed (Hall v. Camden,
    GRC 2013-305; McBride v. Camden, GRC 2014-54).

The operator reviews and sends each request (their own e-mail or the town's portal); `mark_sent` records it with
its due date, and uploading the town's spreadsheet (engine/tax_list_import.py) marks it received.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from functools import lru_cache
from typing import Any
from urllib.parse import quote

import yaml
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from core.config import CONFIG_DIR
from core.dates import nj_business_days_after
from core.text import muni_base, postal_title
from db.models import Lead, LeadStage, OpraRequest, Property

DIRECTORY = CONFIG_DIR / "opra_towns.yaml"


@lru_cache(maxsize=1)
def directory() -> dict[str, Any]:
    return yaml.safe_load(DIRECTORY.read_text()) or {}


def rules() -> dict[str, Any]:
    return directory().get("rules", {})


def entry_for(county: str, town: str) -> dict[str, Any] | None:
    base = muni_base(town)
    # Directory towns are written as the base name ("JERSEY CITY", "NEWARK"): compare as written first.
    return next((t for t in directory().get("towns", []) if t["county"].lower() == (county or "").lower()
                 and (t["town"].upper() == base or muni_base(t["town"]) == base)), None)


def top_towns(session: Session, n: int = 20) -> list[tuple[str, str, int]]:
    """(county, MUN_NAME, open leads) — where a town's tax list adds the most."""
    rows = session.execute(
        select(Property.county, Property.municipality, func.count(Lead.id))
        .join(Lead, Lead.property_id == Property.id)
        .where(Property.municipality.is_not(None),
               Lead.status_stage.notin_((LeadStage.DEAD, LeadStage.DO_NOT_CONTACT, LeadStage.CLOSED)))
        .group_by(Property.county, Property.municipality)
        .order_by(func.count(Lead.id).desc()).limit(n)).all()
    return [(c, m, int(k)) for c, m, k in rows]


@dataclass
class OpraDraft:
    county: str
    town: str
    leads: int
    channel: str
    to: str | None
    portal: str | None
    form: str | None
    clerk: str | None
    subject: str
    body: str
    mailto: str | None
    notes: list[str] = field(default_factory=list)
    source: str | None = None
    status: str | None = None
    sent_at: str | None = None
    due_date: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def town_label(county: str, town: str) -> str:
    """'NEWARK CITY' → 'City of Newark'; 'EDISON TWP' → 'Township of Edison'."""
    up = (town or "").upper()
    kind = next((k for s, k in ((" CITY", "City"), (" TWP", "Township"), (" BORO", "Borough"), (" TOWN", "Town"),
                                (" VILLAGE", "Village")) if up.endswith(s)), None)
    base = postal_title(muni_base(town).lower())
    return f"{kind} of {base}" if kind else base


def request_text(county: str, town: str, op: dict[str, str], year: int | None = None) -> tuple[str, str]:
    """(subject, body). Formal on purpose: it is a statutory request, and every certification must be true."""
    year = year or date.today().year
    agency = town_label(county, town)
    subject = f"OPRA request: {agency} tax sale list and tax lien reports (commercial purpose)"
    lines = [
        "Dear Records Custodian,", "",
        f"Please accept this e-mail as my request for government records under the Open Public Records Act "
        f"(N.J.S.A. 47:1A-1 et seq.), addressed to the {agency} ({county} County) only.", "",
        "Records requested, as electronic copies (Excel or CSV if your system can export them, otherwise PDF), from "
        "the Tax Collector's office:",
        f"1. The most recent tax sale list: the properties included in or advertised for the {year} (or latest) "
        "municipal tax sale, by block, lot and qualifier, with the amounts.",
        "2. The current list of outstanding tax sale certificates held by the municipality (for example the Edmunds "
        "\"Condensed Foreclosure Listing\" or your system's equivalent report).",
        "3. The current delinquency listing or Tax Search Export file from the tax collection system (properties "
        "with unpaid property taxes, water or sewer charges).", "",
        "I am asking only for existing reports, not for a new record to be created. If any of these is already "
        "posted on your website, a link is fine.", "",
        "Certifications (N.J.S.A. 47:1A-5(f)):",
        "- I WILL use the requested records for a commercial purpose (real estate investment).",
        "- I am not seeking these records in connection with a legal proceeding.",
        "- I have not been convicted of any indictable offense under the laws of New Jersey, any other state, or the "
        "United States.", "",
        "I understand a commercial request may take up to 14 business days. Please e-mail the records to "
        f"{op.get('email') or '[your e-mail]'}. If any fee will be more than $5, please tell me the amount before "
        "doing the work.", "",
        f"Requester: {op.get('name') or '[your name]'}" + (f", {op['company']}" if op.get("company") else ""),
        f"Address: {op.get('postal') or '[your mailing address]'}",
        f"Phone: {op.get('phone') or '[your phone]'}",
        f"E-mail: {op.get('email') or '[your e-mail]'}", "",
        "Thank you,", op.get("name") or "[your name]",
    ]
    return subject, "\n".join(lines)


def draft(session: Session, county: str, town: str, leads: int = 0, year: int | None = None) -> OpraDraft:
    from engine.human_copy import operator
    op = operator()
    e = entry_for(county, town) or {}
    subject, body = request_text(county, town, op, year)
    channel = e.get("channel") or "verify"
    notes = [e["notes"]] if e.get("notes") else []
    if not e:
        notes.append("Not in config/opra_towns.yaml yet: find the clerk's OPRA page on the town website (or use the "
                     f"GRC form, {rules().get('grc_form', '')}) and add it to the directory.")
    if e.get("form_required"):
        notes.append("This town wants its own form: fill it in with the same details and attach it.")
    if channel == "portal":
        notes.append("Paste the text below into the portal's description box; tick 'commercial purpose' where asked.")
    to = e.get("email")
    row = session.scalar(select(OpraRequest).where(OpraRequest.county == county, OpraRequest.town == town,
                                                   OpraRequest.year == (year or date.today().year)))
    return OpraDraft(
        county=county, town=town, leads=leads, channel=channel, to=to, portal=e.get("portal"), form=e.get("form"),
        clerk=e.get("clerk"), subject=subject, body=body,
        mailto=f"mailto:{to}?subject={quote(subject)}&body={quote(body)}" if to else None, notes=notes,
        source=e.get("source"), status=row.status if row else None,
        sent_at=row.sent_at.isoformat() if row else None, due_date=row.due_date.isoformat() if row and row.due_date
        else None)


def drafts(session: Session, n: int = 20, extra_counties: list[str] | None = None) -> list[OpraDraft]:
    towns = top_towns(session, n)
    have = {(c, (entry_for(c, m) or {}).get("town") or muni_base(m)) for c, m, _ in towns}
    out = [draft(session, c, m, k) for c, m, k in towns]
    for e in directory().get("towns", []):                       # expansion towns we may not have leads in yet
        if e["county"] in (extra_counties or []) and (e["county"], e["town"]) not in have:
            out.append(draft(session, e["county"], e["town"], 0))
    return out


def mark_sent(session: Session, county: str, town: str, channel: str, when: datetime | None = None,
              year: int | None = None) -> OpraRequest:
    when = when or datetime.now()
    year = year or when.year
    days = int(rules().get("commercial_business_days", 14))
    row = session.scalar(select(OpraRequest).where(OpraRequest.county == county, OpraRequest.town == town,
                                                   OpraRequest.year == year))
    if row is None:
        row = OpraRequest(county=county, town=town, year=year, channel=channel)
        session.add(row)
    row.channel, row.status, row.sent_at = channel, "sent", when
    row.due_date = nj_business_days_after(when.date(), days)
    session.flush()
    return row


def mark_received(session: Session, county: str, town: str, notes: str = "", when: datetime | None = None) -> None:
    base = muni_base(town)
    for row in session.scalars(select(OpraRequest).where(OpraRequest.county == county)):
        if muni_base(row.town) == base and row.status != "received":
            row.status, row.received_at = "received", when or datetime.now()
            row.notes = ((row.notes or "") + " " + notes).strip() or None
    session.flush()


def overdue(session: Session, today: date | None = None) -> list[OpraRequest]:
    today = today or date.today()
    return list(session.scalars(select(OpraRequest).where(OpraRequest.status == "sent",
                                                          OpraRequest.due_date < today)))
