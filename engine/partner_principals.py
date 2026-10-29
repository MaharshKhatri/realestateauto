"""Who is behind a funding partner: the mailing-address family and the person most likely running it.

Repeat buyers in New Jersey usually buy through a string of single-purpose LLCs ("51 PERSHING LLC", "413 LIBERTY,
LLC") whose tax bills and deeds all go to one mailing address: the operator's home, office or mailbox. Grouping
cash buyers by that address turns twenty one-house LLCs into the one investor worth calling, and the address is
also the best route to the human: an individual who owns property, bought by deed, or is already a contact at the
same mailing address is the likely principal.

Evidence comes only from records we hold (imported county owner lists and deed exports, our parcels, deed
transfers, contacts and cash buyers) plus, optionally, the NJGIN parcel at the mailing address (its class tells a
house from an office). Owner names are blank in MOD-IV statewide, so most principals are still found the manual
way: the address-search and entity-lookup links the operator clicks (never fetched by code), then a paste into
Fast Skip-Trace Mode. A name here is a lead to verify, never a fact: every result carries its basis and confidence.
"""
from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import quote, quote_plus

from sqlalchemy import select
from sqlalchemy.orm import Session

from agents.scrapers.nj_tax_assessment_agent import address_mailing_key, mailing_key
from core.config import DATA_DIR
from core.text import (
    clean,
    is_entity,
    is_government,
    is_lender,
    mailing_city,
    mailing_state,
    muni_base,
    postal_title,
    street_key,
)
from db.models import CashBuyer, Contact, DeedTransfer, JVPartner, Property

PRIORITY_COUNTIES = ("Middlesex", "Union", "Essex", "Hudson", "Monmouth")
SHARED_OFFICE_NAMES = 4          # more people than this at one mailing address: a management office / accountant
NOT_A_NAME = re.compile(r"^(OWNER @|CURRENT OWNER|OCCUPANT|RESIDENT|UNKNOWN|ESTATE OF$)|CONDO\.?\s*ASSOC|ASSOCIATION", re.I)


def _person(name: str | None) -> bool:
    n = clean(name).upper()
    return bool(n) and not is_entity(n) and not is_lender(n) and not is_government(n) and not NOT_A_NAME.search(n) \
        and len(re.findall(r"[A-Z]{2,}", n)) >= 2


def _entity(name: str | None) -> bool:
    n = clean(name).upper()
    return bool(n) and is_entity(n) and not is_lender(n) and not is_government(n) and not NOT_A_NAME.search(n)


def _norm(name: str | None) -> str:
    return re.sub(r"[^A-Z0-9]", "", clean(name).upper().replace("&", "AND"))


def display_name(name: str | None) -> str:
    """'CHULANI,JITESH M & DEVNANI,SANGEETA' -> 'Jitesh M Chulani & Sangeeta Devnani'; entities keep their form."""
    n = clean(name)
    if not n or _entity(n):
        return postal_title(n)
    parts = []
    for chunk in re.split(r"\s*&\s*|\s+AND\s+", n, flags=re.I):
        if "," in chunk:
            last, _, first = chunk.partition(",")
            parts.append(f"{first.strip()} {last.strip()}".strip())
        else:
            parts.append(chunk.strip())
    return " & ".join(postal_title(x) for x in parts if x)


# -- the evidence index --------------------------------------------------------------------------------------
@dataclass
class NameIndex:
    """mailing key -> {name: sources} and (street key, town) -> {owner name: sources}."""
    by_mail: dict[str, dict[str, set[str]]] = field(default_factory=lambda: defaultdict(lambda: defaultdict(set)))
    by_location: dict[tuple[str, str], dict[str, set[str]]] = field(
        default_factory=lambda: defaultdict(lambda: defaultdict(set)))

    def add_mail(self, street: str | None, city_state: str | None, name: str | None, source: str) -> None:
        if street and name and (_person(name) or _entity(name)):
            self.by_mail[mailing_key(street, city_state)][clean(name).upper()].add(source)

    def add_location(self, street: str | None, town: str | None, name: str | None, source: str) -> None:
        if street and town and name and (_person(name) or _entity(name)):
            self.by_location[(street_key(street), muni_base(town))][clean(name).upper()].add(source)

    def at_mail(self, key: str | None) -> dict[str, set[str]]:
        return dict(self.by_mail.get(key or "", {}))

    def at_location(self, street: str | None, town: str | None) -> dict[str, set[str]]:
        if not street or not town:
            return {}
        return dict(self.by_location.get((street_key(street), muni_base(town)), {}))


def build_index(session: Session, imports: Path | None = None) -> NameIndex:
    from agents.scrapers.owner_records_agent import deed_rows, owner_rows
    idx = NameIndex()
    towns = {code: name for code, name in session.execute(
        select(Property.mun_code, Property.municipality).where(Property.mun_code.is_not(None)).distinct())}
    root = imports or DATA_DIR / "imports"
    for path in sorted((root / "owner_lists").glob("*.zip")) + sorted((root / "owner_lists").glob("*.csv")):
        for r in owner_rows(path):
            idx.add_mail(r["owner_street"], r["owner_city_state"], r["owner_name"], "county owner list")
            code = (r.get("parcel_id") or "").split("-")[1:2]
            if code and towns.get(code[0]):
                idx.add_location(r.get("location"), towns[code[0]], r["owner_name"], "county owner list")
    for path in sorted((root / "deeds").glob("*.zip")) + sorted((root / "deeds").glob("*.csv")):
        for d in deed_rows(path):
            idx.add_mail(d["grantee_street"], d["grantee_city_state"], d["grantee"], "deed export")
            if towns.get(d.get("district") or ""):
                idx.add_location(d.get("location"), towns[d["district"]], d["grantee"], "deed export")
    for p in session.scalars(select(Property).where(Property.owner_name.is_not(None))):
        if p.owner_mailing_address:
            idx.add_mail(p.owner_mailing_address, p.owner_city_state_zip, p.owner_name, f"parcel owner ({p.owner_name_source})")
        idx.add_location(p.street_address, p.municipality or p.city, p.owner_name, f"parcel owner ({p.owner_name_source})")
    for d in session.scalars(select(DeedTransfer).where(DeedTransfer.grantee_address.is_not(None))):
        street, _, cs = (d.grantee_address or "").partition(", ")
        idx.add_mail(street, cs, d.grantee, "recorded deed")
        idx.add_location(d.property_location, d.municipality, d.grantee, "recorded deed")
    for c in session.scalars(select(Contact).where(Contact.mailing_address.is_not(None))):
        street, _, cs = (c.mailing_address or "").partition(", ")
        idx.add_mail(street, cs, c.name, f"contact ({c.contact_type.lower()})")
    for b in session.scalars(select(CashBuyer)):
        key = family_key(b)
        if key:
            for n in (b.entity_name, b.contact_name):
                if n and (_person(n) or _entity(n)):
                    idx.by_mail[key][clean(n).upper()].add("cash buyer record")
    return idx


# -- the family behind a mailing address ----------------------------------------------------------------------
def family_key(b: CashBuyer | None) -> str | None:
    """Recomputed from the stored address: older rows keep the state in the key ('ELIZABETH NJ'), newer ones not."""
    if b is None:
        return None
    return address_mailing_key(b.mailing_address) or b.mailing_key


def family_buyers(session: Session, key: str | None) -> list[CashBuyer]:
    if not key:
        return []
    return [b for b in session.scalars(select(CashBuyer)) if family_key(b) == key]


def family_stats(buyers: list[CashBuyer], today: date | None = None,
                 towns_by_street: dict[str, str] | None = None) -> dict[str, Any]:
    """Purchases across every buyer record in the family. Deed-export purchases carry only the county, so their town
    comes from the recorded deed for the same street (towns_by_street)."""
    today = today or date.today()
    towns_by_street = towns_by_street or {}
    seen: set[tuple[str, str]] = set()
    purchases: list[dict[str, Any]] = []
    for b in buyers:
        for x in b.recent_purchases_json or []:
            if not isinstance(x, dict) or not x.get("address") or not x.get("price"):
                continue
            k = (re.sub(r"\W", "", str(x["address"]).upper()), str(x.get("deed_date") or ""))
            if k in seen:
                continue
            seen.add(k)
            if not x.get("municipality") and towns_by_street.get(street_key(x["address"])):
                x = {**x, "municipality": towns_by_street[street_key(x["address"])]}
            purchases.append(x)
    purchases.sort(key=lambda x: str(x.get("deed_date") or ""), reverse=True)
    cutoff = (today - timedelta(days=1095)).isoformat()
    recent = [x for x in purchases if str(x.get("deed_date") or "") >= cutoff]
    towns: dict[str, int] = defaultdict(int)
    for x in recent:
        towns[postal_title(muni_base(x.get("municipality"))) or f"{str(x.get('county') or '').title()} County"] += 1
    counties = sorted({str(x.get("county") or "").title() for x in recent if x.get("county")})
    last = purchases[0] if purchases else None
    return {"purchases": len(purchases), "purchases_36m": len(recent), "deployed": round(sum(float(x["price"]) for x in purchases)),
            "deployed_36m": round(sum(float(x["price"]) for x in recent)),
            "flips": sum(int(b.flips_count or 0) for b in buyers), "counties": counties,
            "top_towns": [t for t, _ in sorted(towns.items(), key=lambda kv: -kv[1])][:4],
            "last_purchase": ({"address": postal_title(last["address"]), "town": postal_title(muni_base(last.get("municipality")))
                               or f"{str(last.get('county') or '').title()} County", "price": last.get("price"),
                               "date": last.get("deed_date")} if last else None),
            "buyer_types": sorted({b.buyer_type for b in buyers if b.buyer_type}), "buyer_ids": [b.id for b in buyers],
            "entities": sorted({clean(b.entity_name) for b in buyers if b.entity_name})}


def partner_score(st: dict[str, Any], today: date | None = None) -> float:
    """Deal-desk rank: proven repeat volume first (36 months, MOD-IV sale data lags a year), then flips, dollars
    deployed, our priority counties and recency."""
    today = today or date.today()
    score = 3.0 * min(st["purchases_36m"], 25) + 6.0 * min(st["flips"], 5) + min(st["deployed_36m"] / 500_000, 12)
    score += 4.0 * len(set(st["counties"]) & set(PRIORITY_COUNTIES))
    lp = (st.get("last_purchase") or {}).get("date")
    if lp:
        try:
            months = (today - date.fromisoformat(str(lp)[:10])).days / 30.4
            score += max(0.0, 6 - months * 0.25)
        except ValueError:
            pass
    if len(st["entities"]) >= 3:
        score += 3.0                                   # a multi-LLC operator: a real business with capital
    return round(score, 1)


def mailing_kind(street: str | None, parcel: dict[str, Any] | None = None) -> str:
    s = clean(street).upper()
    if re.match(r"^P\.?\s*O\.?\s*BOX|^BOX\s+\d", s):
        return "PO box"
    if re.search(r"(#|\bSTE|\bSUITE|\bUNIT)\s*\d{2,}", s):
        return "mailbox / suite"
    if parcel:
        cls = str(parcel.get("PROP_CLASS") or "")
        desc = clean(parcel.get("BLDG_DESC"))
        if cls == "2":
            return "house (class 2" + (f", {desc}" if desc else "") + ")"
        if cls in ("4A", "4B"):
            return "commercial building (office or store)"
        if cls == "4C":
            return "apartment building"
        if cls:
            return f"class {cls} parcel"
    return "street address"


def lookup_parcel(gis: Any, street: str | None, city_state: str | None) -> dict[str, Any] | None:
    """The NJGIN parcel at an NJ mailing address (class / building only; owner names are blank in MOD-IV)."""
    if gis is None or not street or (mailing_state(city_state or "") or "NJ") != "NJ":
        return None
    key = street_key(street)
    m = re.match(r"^(\d+)[A-Z]?\s+(\S+)", key)
    city = muni_base(mailing_city(city_state or ""))
    if not m or not city:
        return None
    number, word = m.group(1), m.group(2).replace("'", "''")
    where = (f"MUN_NAME LIKE '{city.replace(chr(39), chr(39) * 2)}%' AND (PROP_LOC LIKE '{number} %{word}%' "
             f"OR PROP_LOC LIKE '{number}-%{word}%')")
    try:
        rows = list(gis.query(where, out_fields=["PROP_LOC", "PROP_CLASS", "BLDG_DESC", "MUN_NAME", "COUNTY",
                                                  "ST_ADDRESS", "CITY_STATE"], centroid=False, limit=5))
    except Exception:  # noqa: BLE001 - the class is a nice-to-have; the links still work without it
        return None
    return rows[0]["attributes"] if rows else None


# -- the principal -------------------------------------------------------------------------------------------
@dataclass
class Principal:
    name: str | None
    confidence: float
    basis: list[str]
    candidates: list[dict[str, Any]]
    family_entities: list[str]
    shared_office: bool = False

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


def unmask(p: JVPartner, idx: NameIndex, buyers: list[CashBuyer]) -> Principal:
    own = {_norm(p.entity_name)} | {_norm(b.entity_name) for b in buyers}
    key = p.family_key
    at_mail = idx.at_mail(key)
    street, _, cs = (p.mailing_address or "").partition(", ")
    at_loc = idx.at_location(street, mailing_city(cs) or cs)
    entities = sorted({postal_title(n) for n in at_mail if _entity(n)} | {postal_title(b.entity_name) for b in buyers
                                                                           if _entity(b.entity_name)})
    people: dict[str, set[str]] = defaultdict(set)
    for n, src in at_mail.items():
        if _person(n):
            people[n] |= {f"{s}: same mailing address" for s in src}
    for n, src in at_loc.items():
        if _person(n):
            people[n] |= {f"{s}: owns the parcel at the mailing address" for s in src}
    for b in buyers:
        if b.contact_name and _person(b.contact_name):
            people[clean(b.contact_name).upper()].add("linked on the cash-buyer record (owner resolver)")
    if _person(p.entity_name):                   # buys in their own name: they are the principal
        return Principal(display_name(p.entity_name), 0.95, ["buys in their own name on recorded deeds"],
                         [{"name": display_name(p.entity_name), "sources": ["recorded deeds"]}],
                         [e for e in entities if _norm(e) not in own][:20])
    cands = sorted(((n, s) for n, s in people.items() if _norm(n) not in own), key=lambda t: (-len(t[1]), t[0]))
    candidates = [{"name": display_name(n), "sources": sorted(s)} for n, s in cands[:6]]
    if len(cands) > SHARED_OFFICE_NAMES:
        return Principal(None, 0.0, [f"{len(cands)} different people get mail at this address: a shared office, "
                                     "accountant or mailbox, not one principal"], candidates[:3], entities[:20], True)
    if not cands:
        return Principal(None, 0.0, ["no individual on record at this mailing address yet: use the address search and "
                                     "entity lookups"], [], entities[:20])
    name, sources = cands[0]
    conf = 0.75 if len(sources) >= 2 else 0.6
    if len(cands) > 1:
        conf -= 0.15
    return Principal(display_name(name), round(conf, 2), sorted(sources), candidates, entities[:20])


# -- lookup links (the operator clicks them; the code never fetches these sites) -----------------------------------
def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", clean(s).lower()).strip("-")


def lookup_links(p: JVPartner) -> dict[str, str]:
    from agents.enrichment.skiptrace_pipeline import search_links
    from core.text import split_person_name
    street, _, cs = (p.mailing_address or "").partition(", ")
    if (p.mailing_kind or "").startswith("suppressed"):
        street = ""
    cs = re.sub(r"(,\s*|\s+)N\.?\s?J\.?(?=\s*\d{5}|\s*$)", ", NJ", cs.strip())     # 'ELIZABETH, N J' -> 'ELIZABETH, NJ'
    city, state = postal_title(mailing_city(cs)) or "", mailing_state(cs) or "NJ"
    links: dict[str, str] = {}
    person = p.principal_name or (display_name(p.entity_name) if _person(p.entity_name) else None)
    if person:
        first, last = split_person_name(person.split(" & ")[0])
        links.update({k: v for k, v in search_links(first, last, city, state).items() if k != "address_search"})
    if street:
        links["truepeoplesearch_address"] = (f"https://www.truepeoplesearch.com/resultaddress?streetaddress={quote(street)}"
                                             f"&citystatezip={quote(f'{city}, {state}')}")
        links["fastpeoplesearch_address"] = f"https://www.fastpeoplesearch.com/address/{_slug(street)}_{_slug(city)}-{state.lower()}"
    if _entity(p.entity_name):
        q = quote_plus(re.sub(r"[^\w\s&'-]", " ", p.entity_name).strip())
        links["opencorporates"] = f"https://opencorporates.com/companies/us_nj?q={q}"
        links["nj_business_search"] = "https://www.njportal.com/DOR/BusinessNameSearch/"
    return links


# -- refresh the vault ---------------------------------------------------------------------------------------
def refresh(session: Session, *, gis: Any = None, idx: NameIndex | None = None, today: date | None = None) -> list[JVPartner]:
    """Group partners by mailing family (merging untouched duplicates), attach the principal evidence and the
    family's purchase record, and re-rank by partner_score. Returns partners best first."""
    idx = idx or build_index(session)
    all_buyers = list(session.scalars(select(CashBuyer)))
    by_family: dict[str, list[CashBuyer]] = defaultdict(list)
    for b in all_buyers:
        k = family_key(b)
        if k:
            by_family[k].append(b)
    mun_names = {code: name for code, name in session.execute(
        select(Property.mun_code, Property.municipality).where(Property.mun_code.is_not(None)).distinct())}
    towns_by_street = {}
    for d in session.scalars(select(DeedTransfer)):
        town = d.municipality or mun_names.get(((d.parcel_id or "").split("-") + ["", ""])[1])
        if d.property_location and town:
            towns_by_street[street_key(d.property_location)] = town
    from engine.compliance import address_key
    from db.models import MailSuppression
    suppressed = {r.address_key for r in session.scalars(select(MailSuppression))}
    partners = list(session.scalars(select(JVPartner).order_by(JVPartner.id)))
    keep: dict[str, JVPartner] = {}
    for p in partners:
        p.mailing_address = p.mailing_address or (p.buyer.mailing_address if p.buyer else None)
        p.family_key = family_key(p.buyer) if p.buyer else (address_key(p.mailing_address) or p.family_key)
        if not p.family_key:
            continue
        first = keep.get(p.family_key)
        untouched = p.status == "TARGET" and not (p.last_contact_at or p.notes or p.pof_verified or p.phone or p.email)
        if first is not None and untouched:
            session.delete(p)                          # same operator seeded twice (two LLCs, one mailing address)
            continue
        keep.setdefault(p.family_key, p)
    session.flush()
    out = []
    for p in session.scalars(select(JVPartner)):
        buyers = by_family.get(p.family_key or "", []) or ([p.buyer] if p.buyer else [])
        st = family_stats(buyers, today, towns_by_street)
        pr = unmask(p, idx, buyers)
        street, _, cs = (p.mailing_address or "").partition(", ")
        if p.family_key in suppressed or address_key(p.mailing_address) in suppressed:
            p.mailing_kind = "suppressed (Daniel's Law or stop request): do not use or share this address"
        else:
            parcel = lookup_parcel(gis, street, cs) if gis is not None else None
            p.mailing_kind = mailing_kind(street, parcel) if (parcel or not p.mailing_kind) else p.mailing_kind
        p.family_stats = {**st, "principal_confidence": pr.confidence, "shared_office": pr.shared_office}
        p.family_entities = pr.family_entities
        if pr.name and not (p.principal_name and "pasted" in (p.principal_basis or "")):
            p.principal_name = pr.name
            p.principal_basis = "; ".join(pr.basis)
        elif not p.principal_name:
            p.principal_basis = "; ".join(pr.basis)
        p.principal_candidates = pr.candidates
        if st["purchases"]:
            p.velocity_score = partner_score(st, today)
        out.append(p)
    session.flush()
    out.sort(key=lambda p: -(p.velocity_score or 0))
    return out


def desk_row(p: JVPartner, rank: int | None = None) -> dict[str, Any]:
    st = p.family_stats or {}
    lp = st.get("last_purchase") or {}
    return {"rank": rank, "partner_id": p.id, "entity": postal_title(p.entity_name), "status": p.status,
            "principal": p.principal_name, "principal_basis": p.principal_basis,
            "principal_confidence": st.get("principal_confidence"), "candidates": p.principal_candidates or [],
            "family_entities": p.family_entities or [], "llc_count": len(p.family_entities or []),
            "purchases": st.get("purchases"), "purchases_36m": st.get("purchases_36m"), "deployed": st.get("deployed"),
            "flips": st.get("flips"), "top_towns": st.get("top_towns") or [], "counties": st.get("counties") or [],
            "icebreaker": lp, "mailing": p.mailing_address, "mailing_kind": p.mailing_kind, "score": p.velocity_score,
            "phone": p.phone, "phone_status": p.phone_status, "email": p.email, "links": lookup_links(p)}


def desk_rows(session: Session, top: int = 10, *, open_only: bool = True) -> list[dict[str, Any]]:
    rows = [p for p in session.scalars(select(JVPartner)) if not open_only or p.status in ("TARGET", "CONTACTED")]
    rows.sort(key=lambda p: -(p.velocity_score or 0))
    return [desk_row(p, i) for i, p in enumerate(rows[:top], 1)]
