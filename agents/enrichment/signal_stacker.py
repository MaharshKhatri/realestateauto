"""Multi-signal distress stacking: the same property (or owner) showing up on several distress lists.

Stacked leads convert best, and some stacks are a virtual-operator shortcut:

  1. **Listed + distressed** — a foreclosure, absentee or tax-delinquent lead that is *also* for sale on the
     MLS right now. Cross-matching our active-listing feed (the `listings` table, every DOM) by house number,
     street and ZIP (or <= 40 m with the same house number) gives an ACTIVE_LISTING signal, the asking price,
     and the listing agent's phone/e-mail — no skip trace, no driving.
  2. **Portfolio distress** — one owner (same mailing address or same owner name) behind 2+ distressed or
     absentee leads: one conversation can cover several houses. Each gets a PORTFOLIO_OWNER signal.
  3. Tax-sale items already land on the existing parcel (agents/scrapers/tax_sale_agent.py matches by
     municipality + block + lot + qualifier, local properties first), so TAX signals stack automatically.

Scoring (engine/scoring.py) turns the number of distinct signal families into the stack bonus and the
`[2x STACKED]` badge. Run: python -m agents.enrichment.signal_stacker [--no-agents]
"""
from __future__ import annotations

import argparse
import logging
import re
from collections import Counter, defaultdict
from dataclasses import dataclass

from sqlalchemy import select

from agents.base import AgentContext, BaseAgent
from agents.scrapers.nj_tax_assessment_agent import address_mailing_key
from core.geo import haversine_miles
from core.text import clean, is_entity, is_government, is_lender, is_servicer_mailing, normalize_street, street_key
from db.models import DistressSignal, Lead, Listing, Property, SignalType
from db.repository import upsert_signal

log = logging.getLogger(__name__)
SOURCE = "signal_stacker"
DISTRESS = {SignalType.SHERIFF_SALE, SignalType.LIS_PENDENS, SignalType.TAX_DELINQUENT, SignalType.TAX_SALE_CERTIFICATE,
            SignalType.LONG_TERM_ABSENTEE, SignalType.OUT_OF_STATE_OWNER, SignalType.ESTATE_OWNER, SignalType.PROBATE,
            SignalType.VACANT, SignalType.CODE_VIOLATION}
NEAR_MILES = 0.025          # ~40 m
MAX_PORTFOLIO = 40          # bigger "owners" are property managers / servicers, not one decision-maker


def zip5(z: str | None) -> str:
    m = re.match(r"\d{5}", (z or "").strip())
    return m.group(0) if m else ""


def house_number(street: str | None) -> str:
    m = re.match(r"\s*(\d+)", street or "")
    return m.group(1) if m else ""


def listing_key(street: str | None, zip_code: str | None) -> tuple[str, str]:
    return street_key(street), zip5(zip_code)


@dataclass
class ListingIndex:
    by_key: dict[tuple[str, str], list[Listing]]
    by_number: dict[str, list[Listing]]

    @classmethod
    def build(cls, listings: list[Listing]) -> "ListingIndex":
        by_key: dict[tuple[str, str], list[Listing]] = defaultdict(list)
        by_number: dict[str, list[Listing]] = defaultdict(list)
        for lst in listings:
            if lst.address:
                by_key[listing_key(lst.address, lst.zip_code)].append(lst)
                if lst.lat and lst.lon:
                    by_number[house_number(lst.address)].append(lst)
        return cls(by_key, by_number)

    def match(self, prop: Property) -> Listing | None:
        if prop.street_address:
            hits = self.by_key.get(listing_key(prop.street_address, prop.zip_code))
            if hits:
                return max(hits, key=lambda x: x.last_seen_at)
        num = house_number(prop.street_address)
        if num and prop.lat and prop.lon:
            near = [x for x in self.by_number.get(num, [])
                    if haversine_miles(prop.lat, prop.lon, x.lat, x.lon) <= NEAR_MILES]
            if near:
                return min(near, key=lambda x: haversine_miles(prop.lat, prop.lon, x.lat, x.lon))
        return None


# Municipal buildings that receive tax bills for city-owned parcels, and care-of / management addresses.
CIVIC_MAILING = re.compile(r"\b(920 (BROAD|MAYOR (KENNETH )?(A )?GIBSON)|280 GROVE|155 MARKET ST|520 MARKET ST|"
                           r"50 WINFIELD SCOTT|1 CITY HALL|CITY HALL|MUNICIPAL (BLDG|BUILDING))\b")
CARE_OF = re.compile(r"(%|\bC/O\b|\bC/0\b|\bCARE OF\b|\bATTN\b|\bMGMT\b|\bMANAGEMENT\b)")


def owner_keys(prop: Property) -> list[str]:
    """Grouping keys for one owner: the absentee mailing address, and an *entity* owner's name.

    Personal names alone are not grouped (two different people share common names); care-of /
    management and city-hall mailings are skipped (many unrelated owners, or the city itself).
    """
    keys: list[str] = []
    street, csz = prop.owner_mailing_address, prop.owner_city_state_zip
    if street and (CIVIC_MAILING.search(street.upper()) or CARE_OF.search(street.upper())):
        street = None
    if street and not is_servicer_mailing(street, csz):
        mk = address_mailing_key(f"{street}, {csz or ''}")
        own = address_mailing_key(f"{prop.street_address or ''}, {prop.city or ''}")
        if mk and mk != own and mk.split("|")[0] != (own or "").split("|")[0]:
            keys.append(f"mail:{mk}")
    name = clean(prop.owner_name).upper()
    if name and is_entity(name) and not is_government(name) and not is_lender(name):
        keys.append("name:" + re.sub(r"[^A-Z0-9 ]", "", name))
    return keys


def portfolio_groups(props: list[Property]) -> list[list[Property]]:
    """Union-find over shared owner keys; groups of 2..MAX_PORTFOLIO properties."""
    parent: dict[int, int] = {p.id: p.id for p in props}

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    first_by_key: dict[str, int] = {}
    for p in props:
        for k in owner_keys(p):
            if k in first_by_key:
                parent[find(p.id)] = find(first_by_key[k])
            else:
                first_by_key[k] = p.id
    groups: dict[int, list[Property]] = defaultdict(list)
    for p in props:
        groups[find(p.id)].append(p)
    return [g for g in groups.values() if 2 <= len(g) <= MAX_PORTFOLIO]


class SignalStackerAgent(BaseAgent):
    name = "signal_stacker"
    description = "Stack distress: listed foreclosure/absentee/tax leads (agent contacts), portfolio owners."

    def run(self, ctx: AgentContext) -> None:
        s = ctx.session
        leads = s.scalars(select(Lead).where(Lead.status_stage.notin_(["DEAD", "DO_NOT_CONTACT", "CLOSED"]))).all()
        props = [lead.property for lead in leads]
        self._listing_cross_match(ctx, props)
        ctx.checkpoint()
        self._portfolios(ctx, props)
        ctx.checkpoint()
        self._summary(ctx, props)

    # 1. listed + distressed -------------------------------------------------------------------------
    def _listing_cross_match(self, ctx: AgentContext, props: list[Property]) -> None:
        s = ctx.session
        index = ListingIndex.build(s.scalars(select(Listing).where(Listing.address.is_not(None))).all())
        new_sigs: list[DistressSignal] = []
        seen_refs: set[str] = set()
        matches: dict[str, list[tuple[Property, Listing]]] = defaultdict(list)
        for p in props:
            types = {g.signal_type for g in p.active_signals}
            if not types & DISTRESS or SignalType.STALE_LISTING in types:
                continue           # stale listings already carry their listing + agent
            lst = index.match(p)
            if lst is not None:
                matches[str(lst.source_ref)].append((p, lst))
        chosen: list[tuple[Property, Listing]] = []
        for pairs in matches.values():
            if len(pairs) == 1:
                chosen.append(pairs[0])
                continue
            # Several parcels (condo units, a split lot) hit one listing: keep only an exact unit match.
            exact = [(p, l) for p, l in pairs if normalize_street(p.street_address) == normalize_street(l.address)]
            if len(exact) == 1:
                chosen.append(exact[0])
            else:
                ctx.bump("ambiguous_listing_matches")
        for p, lst in chosen:
            types = {g.signal_type for g in p.active_signals}
            ctx.records_processed += 1
            sig, created = upsert_signal(
                s, p, signal_type=SignalType.ACTIVE_LISTING, source=SOURCE, source_ref=str(lst.source_ref),
                severity_weight=0.5, status=lst.status, source_url=lst.url, active=True,
                raw_metadata_json={"url": lst.url, "list_price": lst.list_price, "days_on_market": lst.days_on_market,
                                   "list_date": lst.list_date.isoformat() if lst.list_date else None,
                                   "status": lst.status, "listing_address": lst.address, "sqft": lst.sqft,
                                   "beds": lst.beds, "baths": lst.baths, "keywords": lst.distress_keywords or [],
                                   "stacked_with": sorted(t for t in types if t in DISTRESS)})
            seen_refs.add(str(lst.source_ref))
            ctx.bump("listed_distress_created" if created else "listed_distress_seen")
            if SignalType.SHERIFF_SALE in types:
                ctx.bump("listed_foreclosures")
            new_sigs.append(sig)
        # A listing that came off the market no longer stacks.
        for old in s.scalars(select(DistressSignal).where(DistressSignal.source == SOURCE,
                                                          DistressSignal.signal_type == SignalType.ACTIVE_LISTING,
                                                          DistressSignal.active.is_(True))):
            if old.source_ref not in seen_refs:
                old.active = False
                ctx.bump("listings_deactivated")
        if new_sigs and not self.options.get("no_agents"):
            self._attach_agents(ctx, new_sigs)
        self._prune_agent_contacts(ctx)

    def _prune_agent_contacts(self, ctx: AgentContext) -> None:
        """Drop stacker-added agent contacts whose listing no longer matches the lead (and was never used)."""
        from db.models import Contact, Interaction
        for c in ctx.session.scalars(select(Contact).where(Contact.contact_type == "LISTING_AGENT",
                                                           Contact.notes.like("Listing agent — property is listed AND%"))):
            url = (c.raw_json or {}).get("listing_url")
            lead = c.lead
            live = lead is not None and any(g.source_url == url for g in lead.property.active_signals
                                            if g.signal_type in (SignalType.ACTIVE_LISTING, SignalType.STALE_LISTING))
            used = ctx.session.scalar(select(Interaction.id).where(Interaction.contact_id == c.id).limit(1))
            if not live and used is None:
                ctx.session.delete(c)
                ctx.bump("agent_contacts_pruned")

    def _attach_agents(self, ctx: AgentContext, sigs: list[DistressSignal]) -> None:
        """Listing-agent contacts for the newly stacked listings (homeharvest advertisers, cached 24 h per ZIP)."""
        from agents.enrichment.listing_agent_enricher import SOURCE as AGENT_SOURCE
        from agents.enrichment.listing_agent_enricher import ListingAgentEnricher
        from core.text import normalize_phone
        from db.models import ContactType
        from db.repository import upsert_contact
        from engine.compliance import classify_number

        found = ListingAgentEnricher()._refresh(ctx, sigs)
        for sig in sigs:
            lead = sig.property.lead
            for c in found.get(sig.source_ref, []):
                if not (c.get("phone") or c.get("email")):
                    continue
                _, created = upsert_contact(
                    ctx.session, lead=lead, contact_type=ContactType.LISTING_AGENT, source=AGENT_SOURCE,
                    name=c.get("name"), phone=normalize_phone(c.get("phone")), email=c.get("email"),
                    company=c.get("company"), phone_type=c.get("phone_type"), confidence_score=0.9,
                    dnc_status=classify_number(ctx.session, c.get("phone"), b2b=True),
                    notes=f"Listing agent — property is listed AND {', '.join((sig.raw_metadata_json or {}).get('stacked_with') or [])}",
                    raw_json={"listing_url": sig.source_url, "role": c.get("role")})
                if created:
                    ctx.bump("agent_contacts_added")

    # 2. portfolio owners -----------------------------------------------------------------------------
    def _portfolios(self, ctx: AgentContext, props: list[Property]) -> None:
        s = ctx.session
        candidates = [p for p in props if {g.signal_type for g in p.active_signals} & (DISTRESS | {SignalType.STALE_LISTING})]
        groups = portfolio_groups(candidates)
        keep: set[str] = set()
        for g in groups:
            ids = sorted(p.id for p in g)
            owner = next((p.owner_name for p in g if p.owner_name), None)
            mailing = next((f"{p.owner_mailing_address}, {p.owner_city_state_zip or ''}" for p in g
                            if p.owner_mailing_address), None)
            for p in g:
                ref = f"portfolio|{p.id}"
                keep.add(ref)
                upsert_signal(s, p, signal_type=SignalType.PORTFOLIO_OWNER, source=SOURCE, source_ref=ref,
                              severity_weight=min(0.4 + 0.1 * len(g), 1.0), active=True,
                              raw_metadata_json={"properties": len(g), "property_ids": ids, "owner": owner,
                                                 "mailing": mailing,
                                                 "addresses": [q.street_address for q in g if q.id != p.id][:10]})
            ctx.bump("portfolio_groups")
            ctx.bump("portfolio_properties", len(g))
        for old in s.scalars(select(DistressSignal).where(DistressSignal.source == SOURCE,
                                                          DistressSignal.signal_type == SignalType.PORTFOLIO_OWNER,
                                                          DistressSignal.active.is_(True))):
            if old.source_ref not in keep:
                old.active = False

    # 3. summary ---------------------------------------------------------------------------------------
    def _summary(self, ctx: AgentContext, props: list[Property]) -> None:
        from engine.scoring import signal_families
        stacks = Counter()
        for p in props:
            n = len(signal_families(p))
            if n >= 2:
                stacks[f"{min(n, 4)}x"] += 1
        ctx.metrics["stacked"] = dict(stacks)
        ctx.note("Stacked leads: " + ", ".join(f"{k} {v}" for k, v in sorted(stacks.items())) +
                 f"; listed distress {ctx.metrics.get('listed_distress_created', 0) + ctx.metrics.get('listed_distress_seen', 0)}"
                 f"; portfolio groups {ctx.metrics.get('portfolio_groups', 0)}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--no-agents", action="store_true", help="skip the listing-agent lookup for new matches")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    run = SignalStackerAgent(no_agents=args.no_agents).execute()
    print(f"{run.agent_name}: {run.status} processed={run.records_processed} {run.metrics_json}")
    for n in (run.self_improvement_notes or "").splitlines():
        print("  ", n)


if __name__ == "__main__":
    main()
