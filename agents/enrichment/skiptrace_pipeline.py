"""Zero-cost skip-trace workflow: export a queue with lookup links, import what the operator finds.

People-search sites block bots and their terms limit automated use, so we never scrape them. Instead:

  python manage.py skiptrace --export-top 50
      → data/exports/skiptrace_<date>.csv + .html: the top uncontacted leads, the *right person* to look
        up (an estate's executor/heirs rather than the deceased owner), clean first/last name, property
        and mailing addresses, and one-click links that open each site's own search page.
  python manage.py skiptrace --import <filled.csv>         (phone_1.. / email_1 / relatives columns)
  python manage.py skiptrace --import <page.txt> --lead 42 (raw text copied from a results page)
      → phones (wireless/landline), emails, age and relatives parsed into contacts; every number is
        checked against loaded DNC data (engine/compliance.py) before it shows as callable.

Paid batch skip tracing (~$0.10–0.20/record) is the scalable upgrade; its CSV output imports the same way.
"""
from __future__ import annotations

import csv
import html
import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from urllib.parse import quote

from sqlalchemy import select
from sqlalchemy.orm import Session

from core.config import DATA_DIR
from core.text import PHONE_RE, clean, format_phone, is_entity, mailing_city, mailing_state, normalize_phone, \
    split_person_name
from db.models import Contact, ContactType, Lead, LeadStage
from db.repository import upsert_contact
from engine.compliance import classify_number

EXPORT_DIR = DATA_DIR / "exports"
TERMINAL = {LeadStage.DEAD, LeadStage.DO_NOT_CONTACT, LeadStage.CLOSED}
EXPORT_COLUMNS = ["lead_id", "priority", "target_role", "first_name", "last_name", "full_name", "property_address",
                  "property_city", "property_zip", "mailing_address", "mailing_city", "mailing_state", "mailing_zip",
                  "why", "truepeoplesearch", "fastpeoplesearch", "cyberbackgroundchecks", "address_search",
                  "phone_1", "phone_1_type", "phone_2", "phone_2_type", "phone_3", "phone_3_type", "email_1",
                  "relatives", "notes"]


@dataclass
class Target:
    role: str             # OWNER | HEIR_EXECUTOR | RELATIVE | ENTITY
    name: str
    city: str
    state: str


def search_links(first: str, last: str, city: str, state: str, street: str | None = None,
                 street_city: str | None = None) -> dict[str, str]:
    full = f"{first} {last}".strip()
    city_st = f"{city}, {state}".strip(", ")
    slug = lambda s: re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")  # noqa: E731
    links = {
        "truepeoplesearch": f"https://www.truepeoplesearch.com/results?name={quote(full)}&citystatezip={quote(city_st)}",
        "fastpeoplesearch": f"https://www.fastpeoplesearch.com/name/{slug(full)}_{slug(city)}-{slug(state)}",
        "cyberbackgroundchecks": f"https://www.cyberbackgroundchecks.com/people/{slug(first)}-{slug(last)}/{slug(state)}/{slug(city)}",
    }
    if street:
        links["address_search"] = (f"https://www.truepeoplesearch.com/resultaddress?streetaddress={quote(street)}"
                                   f"&citystatezip={quote(street_city or city_st)}")
    return links


def callable_phone(lead: Lead) -> Contact | None:
    phones = [c for c in lead.contacts if c.phone and c.dnc_status != "dnc"]
    phones.sort(key=lambda c: (c.contact_type != "HEIR_EXECUTOR", c.contact_type != "OWNER", -(c.confidence_score or 0)))
    return phones[0] if phones else None


def skiptrace_target(lead: Lead) -> Target | None:
    """Who to look up: executor/heir > owner > co-defendant; entities get the entity name."""
    p = lead.property
    by_type: dict[str, list[Contact]] = {}
    for c in lead.contacts:
        by_type.setdefault(c.contact_type, []).append(c)
    city = mailing_city(p.owner_city_state_zip or "") or (p.city or "").upper()
    state = mailing_state(p.owner_city_state_zip or "") or "NJ"
    for role in (ContactType.HEIR_EXECUTOR, ContactType.OWNER, ContactType.RELATIVE, ContactType.ENTITY_PRINCIPAL):
        for c in by_type.get(role, []):
            if c.name and not is_entity(c.name):
                return Target(role, c.name, city or "", state)
    if p.owner_name:
        return Target("ENTITY" if is_entity(p.owner_name) else "OWNER", p.owner_name, city or "", state)
    return None


def lookup_links(lead: Lead, target: Target | None = None) -> dict[str, str]:
    """Manual people-search / entity links for the lead's best skip-trace target (the operator clicks them)."""
    target = target or skiptrace_target(lead)
    if target is None:
        return {}
    p = lead.property
    if target.role == "ENTITY":
        return {"truepeoplesearch": "", "fastpeoplesearch": "", "cyberbackgroundchecks": "",
                "address_search": f"https://opencorporates.com/companies/us_nj?q={quote(target.name)}"}
    first, last = split_person_name(target.name)
    mail_city = mailing_city(p.owner_city_state_zip or "") or p.city or p.municipality or ""
    mail_state = mailing_state(p.owner_city_state_zip or "") or "NJ"
    return search_links(first, last, target.city or (p.city or ""), target.state or "NJ",
                        street=p.owner_mailing_address or p.street_address, street_city=f"{mail_city}, {mail_state}")


def export_queue(session: Session, top: int = 50, out_dir: Path = EXPORT_DIR) -> tuple[Path, Path, int]:
    leads = session.scalars(select(Lead).where(Lead.status_stage.notin_(list(TERMINAL)))
                            .order_by(Lead.priority_rank_score.desc())).all()
    rows: list[dict] = []
    for lead in leads:
        if len(rows) >= top:
            break
        if lead.primary_source == "stale_listing" or callable_phone(lead):
            continue        # listing agents need no skip trace; already have a number
        target = skiptrace_target(lead)
        if target is None:
            continue
        p = lead.property
        first, last = ("", target.name) if target.role == "ENTITY" else split_person_name(target.name)
        mail_zip = (re.search(r"(\d{5})", p.owner_city_state_zip or "") or [None, None])[1]
        row = {
            "lead_id": lead.id, "priority": round(lead.priority_rank_score), "target_role": target.role,
            "first_name": first, "last_name": last, "full_name": target.name,
            "property_address": p.street_address, "property_city": p.city or p.municipality,
            "property_zip": p.zip_code, "mailing_address": p.owner_mailing_address,
            "mailing_city": mailing_city(p.owner_city_state_zip or ""), "mailing_state": mailing_state(p.owner_city_state_zip or ""),
            "mailing_zip": mail_zip,
            "why": ((lead.score_breakdown_json or {}).get("reasons") or [lead.primary_source])[0],
        }
        row.update(lookup_links(lead, target))
        rows.append(row)
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = date.today().isoformat()
    csv_path, html_path = out_dir / f"skiptrace_{stamp}.csv", out_dir / f"skiptrace_{stamp}.html"
    with csv_path.open("w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=EXPORT_COLUMNS)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in EXPORT_COLUMNS})
    html_path.write_text(render_html(rows))
    return csv_path, html_path, len(rows)


def render_html(rows: list[dict]) -> str:
    def link(url: str, label: str) -> str:
        return f'<a href="{html.escape(url)}" target="_blank" rel="noopener">{label}</a>' if url else ""
    body = "".join(
        f"<tr><td>{r['lead_id']}</td><td>{r['priority']}</td><td><b>{html.escape(r['full_name'])}</b><br>"
        f"<small>{r['target_role']}</small></td><td>{html.escape(str(r['property_address']))}, {html.escape(str(r['property_city']))}"
        f"</td><td>{html.escape(str(r['mailing_address'] or ''))}<br><small>{html.escape(str(r['mailing_city'] or ''))} "
        f"{r['mailing_state'] or ''}</small></td><td><small>{html.escape(str(r['why']))}</small></td><td>"
        f"{link(r.get('truepeoplesearch',''), 'TruePeopleSearch')}<br>{link(r.get('fastpeoplesearch',''), 'FastPeopleSearch')}<br>"
        f"{link(r.get('cyberbackgroundchecks',''), 'CyberBackgroundChecks')}<br>{link(r.get('address_search',''), 'Address / entity')}"
        "</td></tr>" for r in rows)
    return ("<!doctype html><meta charset=utf-8><title>Skip-trace queue</title>"
            "<style>body{font:14px system-ui;margin:20px;background:#0f1115;color:#e6e6e6}a{color:#7cc4ff}"
            "table{border-collapse:collapse;width:100%}td,th{border-bottom:1px solid #2a2f3a;padding:6px;vertical-align:top}"
            "small{color:#9aa4b2}</style><h1>Skip-trace queue</h1><p>Look each person up, then paste phones/emails "
            "into the CSV (phone_1, phone_1_type, …) or the lead's page in the cockpit. Numbers are DNC-checked on import."
            "</p><table><tr><th>Lead</th><th>Pri</th><th>Look up</th><th>Property</th><th>Mailing address</th><th>Why</th>"
            f"<th>Search</th></tr>{body}</table>")


# -- import ------------------------------------------------------------------------------------------
PHONE_TYPE_RE = re.compile(r"\b(wireless|mobile|cell|landline|land line|voip)\b", re.I)
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
AGE_RE = re.compile(r"\bAge\s*:?\s*(\d{2,3})\b", re.I)
RELATIVES_RE = re.compile(r"(?:Possible\s+)?Relatives?\s*:?\s*(.+?)(?:\n\s*\n|Associates|Associated|Previous|Email|$)",
                          re.I | re.S)


def parse_people_text(text: str) -> dict:
    """Pull phones (with type), emails, age and relatives out of text copied from a people-search page."""
    phones: list[tuple[str, str]] = []
    for m in PHONE_RE.finditer(text):
        digits = "".join(m.groups())
        if digits in [p for p, _ in phones]:
            continue
        window = text[m.end(): m.end() + 40] + " " + text[max(0, m.start() - 25): m.start()]
        t = PHONE_TYPE_RE.search(window)
        kind = (t.group(1).lower() if t else "unknown").replace("mobile", "wireless").replace("cell", "wireless")
        phones.append((digits, "landline" if "land" in kind else kind))
    emails = sorted({e.lower() for e in EMAIL_RE.findall(text)})
    age = AGE_RE.search(text)
    relatives: list[str] = []
    m = RELATIVES_RE.search(text)
    if m:
        for part in re.split(r"[,\n•·|]+", m.group(1)):
            name = clean(part)
            if 2 <= len(name.split()) <= 4 and re.match(r"^[A-Za-z .'\-]+$", name):
                relatives.append(name.title())
    return {"phones": phones, "emails": emails, "age": int(age.group(1)) if age else None, "relatives": relatives[:10]}


def _target_type(role: str | None) -> str:
    return {"HEIR_EXECUTOR": ContactType.HEIR_EXECUTOR, "RELATIVE": ContactType.RELATIVE,
            "ENTITY": ContactType.REGISTERED_AGENT}.get(role or "", ContactType.OWNER)


def import_found(session: Session, lead: Lead, *, name: str | None, role: str | None, phones: list[tuple[str, str]],
                 emails: list[str], relatives: list[str] | None = None, age: int | None = None,
                 source: str = "manual_skiptrace") -> int:
    n = 0
    ctype = _target_type(role)
    for i, (digits, kind) in enumerate(phones):
        upsert_contact(session, lead=lead, contact_type=ctype, source=source, name=name, phone=digits,
                       phone_type=kind or "unknown", dnc_status=classify_number(session, digits),
                       confidence_score=max(0.75 - 0.1 * i, 0.4),
                       notes=f"age {age}" if age else None)
        n += 1
    for e in emails:
        upsert_contact(session, lead=lead, contact_type=ctype, source=source, name=name, email=e, confidence_score=0.6)
        n += 1
    for r in relatives or []:
        upsert_contact(session, lead=lead, contact_type=ContactType.RELATIVE, source=source, name=r,
                       confidence_score=0.4, notes=f"relative of {name}")
        n += 1
    if n and lead.status_stage == LeadStage.NEW:
        lead.status_stage = LeadStage.SKIP_TRACE
    return n


def import_csv(session: Session, path: Path) -> tuple[int, int]:
    leads_touched, contacts = 0, 0
    with path.open(newline="", encoding="utf-8-sig") as fh:
        for row in csv.DictReader(fh):
            lead = session.get(Lead, int(row.get("lead_id") or 0)) if (row.get("lead_id") or "").isdigit() else None
            if lead is None:
                continue
            phones = []
            for i in (1, 2, 3):
                digits = normalize_phone(row.get(f"phone_{i}"))
                if digits:
                    phones.append((digits, (row.get(f"phone_{i}_type") or "unknown").strip().lower()))
            emails = [e for e in [(row.get("email_1") or "").strip().lower()] if EMAIL_RE.fullmatch(e or "")]
            relatives = [clean(r).title() for r in (row.get("relatives") or "").split(";") if clean(r)]
            n = import_found(session, lead, name=row.get("full_name") or None, role=row.get("target_role"),
                             phones=phones, emails=emails, relatives=relatives)
            if n:
                leads_touched += 1
                contacts += n
                if row.get("notes"):
                    lead.notes = "\n".join(x for x in (lead.notes, f"skip trace: {row['notes']}") if x)
    session.flush()
    return leads_touched, contacts


def import_text(session: Session, lead: Lead, text: str, *, name: str | None = None, role: str | None = None) -> dict:
    parsed = parse_people_text(text)
    target = skiptrace_target(lead)
    parsed["contacts_added"] = import_found(
        session, lead, name=name or (target.name if target else None), role=role or (target.role if target else None),
        phones=parsed["phones"], emails=parsed["emails"], relatives=parsed["relatives"], age=parsed["age"])
    return parsed


def describe(contact: Contact) -> str:
    return f"{contact.contact_type} {contact.name or ''} {format_phone(contact.phone)} {contact.phone_type or ''} [{contact.dnc_status}]"
