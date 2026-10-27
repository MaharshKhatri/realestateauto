"""Public owner & entity resolver — names (and mailing addresses) for the people behind a lead.

Free sources only (see docs/nj_data_sources.md §5 and docs/recon/public_owner_layers.md):
1. Foreclosure defendant lines (CivilView): every individual party — the borrower/owner, co-defendants
   (spouses, co-owners), and for estates the **executor / administrator / known heirs**, who are the
   people with authority to sell. Deceased parties are noted, never called.
2. Mailing-address cross-reference against the county owner lists the operator imported
   (data/imports/owner_lists): an unnamed absentee lead whose tax bill goes to an address where the
   owner list shows a named owner gets that name (the landlord who lives there); an entity owner or an
   "Owner @ address" cash buyer that shares a mailing address with an individual gets that individual
   as its likely principal. Deed exports add grantee names the same way.
3. Entity lookup links for LLC/corp owners (NJ Division of Revenue business name search, OpenCorporates)
   for the operator to click — those sites are not crawled.

Run: python -m agents.enrichment.public_owner_resolver
"""
from __future__ import annotations

import argparse
import logging
import re
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote_plus

from sqlalchemy import select

from agents.base import AgentContext, BaseAgent
from agents.scrapers.nj_tax_assessment_agent import mailing_key
from agents.scrapers.owner_records_agent import deed_rows, owner_rows
from core.config import DATA_DIR
from core.text import clean, is_entity, is_government, is_lender, strip_et_al
from db.models import CashBuyer, Contact, ContactType, Lead, Property, SignalType
from db.repository import upsert_contact

log = logging.getLogger(__name__)

ROLE_WORDS = re.compile(
    r"\b(HEIRS?|DEVISEES?|DEVISES|BENEFICIARIES|SUCCESSORS?|ASSIGNS|PERSONAL|PERSONNAL|REPRESENTATIVES?|RIGHT|TITLE|"
    r"TITTLE|INTEREST|INDIVIDUALLY|DECEASED|DIVORCED|MARRIED|SINGLE|WIFE|HUSBAND|SPOUSE|UNKNOWN|ET\.?\s*ALS?\.?|"
    r"GUARDIAN|TENANTS?|OCCUPANTS?|JOHN DOE|JANE DOE|MR\.? OR MRS\.?|DOMESTIC PARTNER|CIVIL UNION|PARTNER|"
    r"HIS|HER|THEIR|HERS|THEIRS|ANY|OF|AND|THE|OR|IN|ASSIGNEE)\b", re.I)
EXECUTOR_RE = re.compile(
    r"^(?P<name>[^,]+?),?\s+(?:INDIVIDUALLY\s*,?\s*)?(?:AND\s+)?AS\s+(?:THE\s+)?(?:KNOWN\s+)?"
    r"(?P<role>ADMINISTRAT\w+|EXECUT\w+|HEIR|PERSONAL REPRESENTATIVE|TRUSTEE)\b(?:.*?\bOF\s+(?:THE\s+)?"
    r"(?:ESTATE\s+OF\s+)?(?P<dec>[A-Z][A-Z .'\-]+))?", re.I)
ESTATE_OF_RE = re.compile(r"^(?:THE\s+)?ESTATE\s+OF\s+(?P<dec>[^,;]+)", re.I)
AKA_RE = re.compile(r"\b[AF]/K/A\b[^,;]*", re.I)
NAME_RE = re.compile(r"^[A-Z][A-Z.'\- ]+$")
NOT_A_PERSON = re.compile(
    r"\b(GUARDIAN|FOR|CONSULTANTS?|SERVICES?|ASSOCIATES|CENTER|HOSPITAL|MEDICAL|COLLECTIONS?|ACCEPTANCE|"
    r"CAPITAL|CREDIT|FUNDING|PORTFOLIO|RECOVERY|FINANCE|FINANCIAL|INSURANCE|PATHOLOGY|RADIOLOGY|"
    r"EMERGENCY|PHYSICIANS|UNIVERSITY|SCHOOL|CONDOMINIUM|HOMEOWNERS|TENANTS?|OCCUPANTS?)\b")


@dataclass
class Party:
    name: str
    role: str            # OWNER | HEIR_EXECUTOR | RELATIVE | DECEASED
    evidence: str


def _looks_like_person(text: str) -> bool:
    t = clean(text).upper().strip(" ,.")
    if not t or not NAME_RE.match(t):
        return False
    words = [w for w in t.split() if w]
    if not 2 <= len(words) <= 5:
        return False
    if ROLE_WORDS.sub("", t).strip() != t.strip() and len(ROLE_WORDS.sub("", t).split()) < 2:
        return False
    return not (is_entity(t) or is_government(t) or is_lender(t) or NOT_A_PERSON.search(t))


def parse_defendants(line: str | None) -> list[Party]:
    """Parties named in a NJ foreclosure defendant line, with roles."""
    parties: list[Party] = []
    if not line:
        return parties
    text = AKA_RE.sub("", clean(line).upper())
    for seg in [s.strip(" ,") for s in text.split(";") if s.strip(" ,")]:
        seg = strip_et_al(seg)
        m = ESTATE_OF_RE.match(seg)
        if m:
            dec = clean(m.group("dec").split(",")[0])
            if _looks_like_person(dec):
                parties.append(Party(dec.title(), "DECEASED", "named as 'Estate of' in the foreclosure"))
            continue
        m = EXECUTOR_RE.match(seg)
        if m and _looks_like_person(m.group("name")):
            parties.append(Party(clean(m.group("name")).title(), "HEIR_EXECUTOR",
                                 f"defendant {m.group('role').lower()} of the estate"))
            if m.group("dec") and _looks_like_person(m.group("dec").split(",")[0]):
                parties.append(Party(clean(m.group("dec").split(",")[0]).title(), "DECEASED", "estate decedent"))
            continue
        current: Party | None = None
        for piece in [p.strip() for p in seg.split(",") if p.strip()]:
            if re.search(r"\b(HEIRS|DEVISEES|DEVISES|DECEASED)\b", piece) and current:
                current.role, current.evidence = "DECEASED", "listed with 'heirs, devisees' (deceased owner)"
                continue
            if _looks_like_person(piece):
                current = Party(clean(piece).title(), "OWNER" if not parties else "RELATIVE",
                                "foreclosure defendant")
                parties.append(current)
    # De-duplicate by name, keeping the most specific role.
    rank = {"HEIR_EXECUTOR": 0, "DECEASED": 1, "OWNER": 2, "RELATIVE": 3}
    best: dict[str, Party] = {}
    for p in parties:
        if p.name not in best or rank[p.role] < rank[best[p.name].role]:
            best[p.name] = p
    out = list(best.values())
    estate = any(p.role in ("DECEASED", "HEIR_EXECUTOR") for p in out)
    for p in out:
        if estate and p.role == "OWNER":
            # With a decedent on title, other living defendants are usually heirs, not the owner of record.
            p.role, p.evidence = "RELATIVE", "co-defendant in an estate foreclosure (possible heir)"
    if out and not estate and not any(p.role == "OWNER" for p in out):
        out[0].role = "OWNER"
    return out


def entity_lookup_links(name: str) -> dict[str, str]:
    q = quote_plus(re.sub(r"[^\w\s&'-]", " ", name).strip())
    return {"nj_business_search": "https://www.njportal.com/DOR/BusinessNameSearch/",
            "opencorporates": f"https://opencorporates.com/companies/us_nj?q={q}"}


class MailingIndex:
    """mailing key -> owner names seen at that mailing address, from imported county exports."""

    def __init__(self) -> None:
        self.names: dict[str, set[str]] = defaultdict(set)

    def add(self, street: str | None, city_state: str | None, name: str | None) -> None:
        if street and name and not is_government(name) and not is_lender(name):
            self.names[mailing_key(street, city_state)].add(clean(name))

    def lookup(self, street: str | None, city_state: str | None) -> list[str]:
        return sorted(self.names.get(mailing_key(street, city_state), set())) if street else []

    @classmethod
    def from_imports(cls, root: Path) -> "MailingIndex":
        idx = cls()
        for path in sorted((root / "owner_lists").glob("*.zip")) + sorted((root / "owner_lists").glob("*.csv")):
            for r in owner_rows(path):
                idx.add(r["owner_street"], r["owner_city_state"], r["owner_name"])
        for path in sorted((root / "deeds").glob("*.zip")) + sorted((root / "deeds").glob("*.csv")):
            for d in deed_rows(path):
                idx.add(d["grantee_street"], d["grantee_city_state"], d["grantee"])
        return idx


class PublicOwnerResolver(BaseAgent):
    name = "owner_resolver"
    description = "Owner/heir/co-defendant contacts from foreclosure filings; names via mailing cross-reference."

    def run(self, ctx: AgentContext) -> None:
        s = ctx.session
        self._defendant_parties(ctx)
        ctx.checkpoint()
        idx = MailingIndex.from_imports(Path(self.options.get("imports") or DATA_DIR / "imports"))
        ctx.metrics["mailing_index_addresses"] = len(idx.names)
        if idx.names:
            self._cross_reference_leads(ctx, idx)
            self._cross_reference_buyers(ctx, idx)
        self._entity_links(ctx)
        ctx.checkpoint()
        total = s.scalar(select(Lead.id).limit(1)) and len(s.scalars(select(Lead.id)).all())
        named = len(s.scalars(select(Property.id).join(Lead).where(Property.owner_name.is_not(None))).all())
        ctx.metrics["owner_name_coverage"] = round(named / total, 3) if total else 0.0
        ctx.note(f"Owner names known on {named:,} of {total:,} leads ({named / total:.0%})" if total else "no leads")

    def _defendant_parties(self, ctx: AgentContext) -> None:
        leads = ctx.session.scalars(select(Lead)).all()
        for lead in leads:
            prop = lead.property
            sheriff = next((s for s in prop.active_signals if s.signal_type == SignalType.SHERIFF_SALE and s.defendant), None)
            if not sheriff:
                continue
            parties = parse_defendants(sheriff.defendant)
            deceased = [p.name for p in parties if p.role == "DECEASED"]
            mail = None
            if prop.owner_mailing_address:
                mail = f"{prop.owner_mailing_address}, {prop.owner_city_state_zip or ''}".strip(", ")
            for p in parties:
                if p.role == "DECEASED":
                    continue
                ctype = {"OWNER": ContactType.OWNER, "HEIR_EXECUTOR": ContactType.HEIR_EXECUTOR}.get(
                    p.role, ContactType.RELATIVE)
                _, created = upsert_contact(
                    ctx.session, lead=lead, contact_type=ctype, source="foreclosure_defendant", name=p.name,
                    mailing_address=mail if ctype == ContactType.OWNER else None,
                    confidence_score=0.85 if ctype != ContactType.RELATIVE else 0.6,
                    notes=p.evidence + (f"; deceased: {', '.join(deceased)}" if deceased else ""),
                    raw_json={"defendant_line": sheriff.defendant[:300], "case": sheriff.case_number})
                ctx.bump("defendant_contacts_created" if created else "defendant_contacts_seen")
            if deceased:
                ctx.bump("estates_with_named_decedent")

    def _cross_reference_leads(self, ctx: AgentContext, idx: MailingIndex) -> None:
        # This agent owns its principal links: rebuild them each run so rule fixes take effect.
        for c in ctx.session.scalars(select(Contact).where(Contact.source == "mailing_cross_reference",
                                                           Contact.contact_type == ContactType.ENTITY_PRINCIPAL)):
            ctx.session.delete(c)
        ctx.session.flush()
        props = ctx.session.scalars(select(Property).join(Lead).where(Property.owner_mailing_address.is_not(None))).all()
        for p in props:
            names = idx.lookup(p.owner_mailing_address, p.owner_city_state_zip or "")
            if not names:
                continue
            people = [n for n in names if not is_entity(n)]
            entities = [n for n in names if is_entity(n)]
            if p.owner_name is None and len(names) <= 3:
                guess = people[0] if people else entities[0]
                p.owner_name, p.owner_name_source = guess, "mailing_cross_reference"
                upsert_contact(ctx.session, lead=p.lead, contact_type=ContactType.OWNER, source="mailing_cross_reference",
                               name=guess, mailing_address=f"{p.owner_mailing_address}, {p.owner_city_state_zip or ''}",
                               confidence_score=0.6 if len(names) == 1 else 0.45,
                               notes=f"Named owner at the same tax-bill mailing address ({len(names)} name(s) there)")
                ctx.bump("owners_named_by_mailing")
            elif p.owner_name and is_entity(p.owner_name) and people:
                for person in people[:2]:
                    upsert_contact(ctx.session, lead=p.lead, contact_type=ContactType.ENTITY_PRINCIPAL,
                                   source="mailing_cross_reference", name=person,
                                   mailing_address=f"{p.owner_mailing_address}, {p.owner_city_state_zip or ''}",
                                   confidence_score=0.5,
                                   notes=f"Individual owner sharing {p.owner_name}'s mailing address — likely principal")
                ctx.bump("entity_principals_linked")

    def _cross_reference_buyers(self, ctx: AgentContext, idx: MailingIndex) -> None:
        for b in ctx.session.scalars(select(CashBuyer)):
            street, _, city_state = (b.mailing_address or "").partition(", ")
            names = idx.lookup(street, city_state) if street else []
            if not names:
                continue
            people = [n for n in names if not is_entity(n)]
            entities = [n for n in names if is_entity(n)]
            if b.entity_name.startswith("Owner @") and (entities or people):
                b.entity_name = (entities or people)[0]
                ctx.bump("buyers_named")
            if people and not b.contact_name:
                b.contact_name = people[0]
                ctx.bump("buyer_principals_linked")

    def _entity_links(self, ctx: AgentContext) -> None:
        for p in ctx.session.scalars(select(Property).join(Lead).where(Property.is_corporate_owned.is_(True))):
            if not p.owner_name or is_lender(p.owner_name) or is_government(p.owner_name):
                continue
            upsert_contact(ctx.session, lead=p.lead, contact_type=ContactType.REGISTERED_AGENT,
                           source="entity_lookup_links", name=p.owner_name,
                           mailing_address=(f"{p.owner_mailing_address}, {p.owner_city_state_zip or ''}"
                                            if p.owner_mailing_address else None),
                           confidence_score=0.3, raw_json=entity_lookup_links(p.owner_name),
                           notes="Look up the registered agent / principals: NJ business name search, OpenCorporates")
            ctx.bump("entity_lookups_prepared")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--imports", help="folder with owner_lists/ and deeds/ (default data/imports)")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    run = PublicOwnerResolver(imports=args.imports).execute()
    print(f"{run.agent_name}: {run.status} {run.metrics_json}")
    for n in (run.self_improvement_notes or "").splitlines():
        print("  ", n)


if __name__ == "__main__":
    main()
