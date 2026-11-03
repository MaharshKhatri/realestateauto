"""Listing-agent contacts for stale / distressed MLS leads.

A 90+ day "as-is" listing has a professional on the other end whose name, brokerage, phones and
email are published on the listing. No skip-tracing is needed: the listing agent presents our offer.
Calls to an agent about their own advertised listing are business-to-business; numbers are still
checked against any loaded DNC data and flagged.

Sources: STALE_LISTING signal metadata (captured by stale_listing_distress_agent), optionally
refreshed with the full advertiser payload from homeharvest (all phones incl. office, brokerage).

Run: python -m agents.enrichment.listing_agent_enricher [--refresh]
"""
from __future__ import annotations

import argparse
import logging
from collections import defaultdict
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from agents.base import AgentContext, BaseAgent
from core.text import clean, is_fax_label, normalize_phone
from db.models import ContactType, DistressSignal, SignalType
from db.repository import upsert_contact
from engine.compliance import classify_number

log = logging.getLogger(__name__)
SOURCE = "mls_listing"


def phone_kind(label: str | None) -> str:
    """Realtor's phone type ("Mobile", "Office", "Fax") as stored on a contact ("wireless", "office", "fax")."""
    kind = (label or "").strip().lower()
    return {"mobile": "wireless"}.get(kind, kind or "unknown")


def advertiser_contacts(raw: dict[str, Any]) -> list[dict[str, Any]]:
    """Flatten homeharvest/Realtor 'advertisers' into contact dicts (seller-side agents and offices).

    Fax lines are dropped: a fax number is never a callable phone (an agent with only a fax and an e-mail becomes an
    e-mail contact)."""
    out: list[dict[str, Any]] = []
    for adv in raw.get("advertisers") or []:
        if not adv:
            continue
        broker = clean((adv.get("broker") or {}).get("name")) or None
        office = adv.get("office") or {}
        name = clean(adv.get("name")) or None
        phones = [(normalize_phone(p.get("number")), p.get("type")) for p in (adv.get("phones") or []) if p]
        phones = [(n, t) for n, t in phones if n and not is_fax_label(t)]
        if not phones and not adv.get("email"):
            continue
        for number, kind in phones or [(None, None)]:
            out.append({"name": name, "company": broker or clean(office.get("name")) or None,
                        "phone": number, "phone_type": phone_kind(kind) if number else None,
                        "email": (adv.get("email") or "").strip().lower() or None,
                        "role": adv.get("type")})
        for p in office.get("phones") or []:
            number = normalize_phone((p or {}).get("number"))
            if number and not is_fax_label((p or {}).get("type")):
                out.append({"name": clean(office.get("name")) or broker, "company": broker, "phone": number,
                            "phone_type": "office", "email": (office.get("email") or "").lower() or None,
                            "role": "office"})
    return out


def scrub_fax_contacts(session: Session) -> int:
    """Contacts saved before fax lines were filtered: a number labelled fax (on the contact, or as the advertiser's fax
    on its listing) leaves `Contact.phone`, so no call list, script or packet can offer it. The number is kept in
    raw_json["fax"] for reference; a contact left with nothing to reach and no history is removed."""
    from db.models import Contact, Interaction
    fax_by_lead: dict[int, set[str]] = defaultdict(set)
    for sig in session.scalars(select(DistressSignal).where(
            DistressSignal.signal_type.in_((SignalType.STALE_LISTING, SignalType.ACTIVE_LISTING)))):
        lead = sig.property.lead if sig.property else None
        fax = (sig.raw_metadata_json or {}).get("listing_agent_fax") or []
        if lead is not None and fax:
            fax_by_lead[lead.id].update(fax)
    stmt = select(Contact).where(Contact.phone.is_not(None), or_(
        Contact.phone_type.ilike("%fax%"),
        Contact.lead_id.in_(list(fax_by_lead)) & (Contact.contact_type == ContactType.LISTING_AGENT)))
    n = 0
    for c in session.scalars(stmt).all():
        if not (is_fax_label(c.phone_type) or c.phone in fax_by_lead.get(c.lead_id or 0, set())):
            continue
        used = session.scalar(select(Interaction.id).where(Interaction.contact_id == c.id).limit(1)) is not None
        twin = c.email and session.scalar(select(Contact.id).where(
            Contact.id != c.id, Contact.lead_id == c.lead_id, Contact.contact_type == c.contact_type,
            Contact.email == c.email, Contact.phone.is_(None)).limit(1)) is not None
        if not used and (twin or not c.email):
            session.delete(c)
        else:
            c.raw_json = {**(c.raw_json or {}), "fax": c.phone}
            c.phone, c.phone_type, c.dnc_status = None, None, None
        n += 1
    session.flush()
    return n


def meta_contact(meta: dict[str, Any]) -> dict[str, Any]:
    """The listing agent as the stale-listing harvest stored it on the signal (first non-fax phone, e-mail)."""
    phone = normalize_phone(meta.get("listing_agent_phone"))
    kind = meta.get("listing_agent_phone_type")
    if phone and (is_fax_label(kind) or phone in set(meta.get("listing_agent_fax") or [])):
        phone, kind = None, None
    return {"name": meta.get("listing_agent"), "company": meta.get("listing_broker"), "phone": phone,
            "phone_type": phone_kind(kind) if phone else None, "email": meta.get("listing_agent_email"),
            "role": "seller"}


class ListingAgentEnricher(BaseAgent):
    name = "listing_agents"
    description = "Attach listing-agent contacts (name, brokerage, phones, email) to stale MLS leads."

    def run(self, ctx: AgentContext) -> None:
        scrubbed = scrub_fax_contacts(ctx.session)
        if scrubbed:
            ctx.bump("fax_numbers_removed", scrubbed)
        sigs = ctx.session.scalars(select(DistressSignal).where(
            DistressSignal.signal_type == SignalType.STALE_LISTING, DistressSignal.active.is_(True))).all()
        refreshed: dict[str, list[dict]] = {}
        if self.options.get("refresh"):
            refreshed = self._refresh(ctx, sigs)
        for sig in sigs:
            lead = sig.property.lead
            if lead is None:
                continue
            meta = sig.raw_metadata_json or {}
            contacts = refreshed.get(sig.source_ref) or [meta_contact(meta)]
            ctx.records_processed += 1
            for c in contacts:
                if not (c.get("phone") or c.get("email")):
                    continue
                _, created = upsert_contact(
                    ctx.session, lead=lead, contact_type=ContactType.LISTING_AGENT, source=SOURCE,
                    name=c.get("name"), phone=c.get("phone"), email=c.get("email"), company=c.get("company"),
                    phone_type=c.get("phone_type"), confidence_score=0.9,
                    dnc_status=classify_number(ctx.session, c.get("phone"), b2b=True),
                    notes=f"Listing agent for {sig.property.street_address} ({meta.get('days_on_market')} DOM, "
                          f"list ${meta.get('list_price') or 0:,.0f})",
                    raw_json={"listing_url": meta.get("url"), "role": c.get("role")})
                ctx.bump("contacts_created" if created else "contacts_updated")
            if lead.contact_name is None and contacts and contacts[0].get("name"):
                lead.contact_name = f"{contacts[0]['name']} (listing agent)"
        ctx.checkpoint()

    def _refresh(self, ctx: AgentContext, sigs: list[DistressSignal]) -> dict[str, list[dict]]:
        """Re-read active listings per ZIP (cached 24h) to capture every advertiser phone."""
        from engine import homeharvest_provider as hh
        from engine.listing_comps import _DiskCache

        cache = _DiskCache(24.0)
        by_zip: dict[str, set[str]] = defaultdict(set)
        for s in sigs:
            z = s.property.zip_code
            if z:
                by_zip[z].add(s.source_ref)
        out: dict[str, list[dict]] = {}
        for zip_code, refs in sorted(by_zip.items()):
            key = f"active_raw|{zip_code}"
            rows = cache.get(key)
            if rows is None:
                try:
                    rows = [{"property_id": r.get("property_id"), "url": r.get("url"),
                             "advertisers": (r.get("raw") or {}).get("advertisers")}
                            for r in hh.active_listings(zip_code)]
                except hh.ProviderBlocked as exc:
                    ctx.error(f"listing provider blocked: {exc}; using stored listing metadata")
                    break
                except Exception as exc:  # noqa: BLE001
                    ctx.error(f"{zip_code}: {type(exc).__name__}: {exc}")
                    continue
                cache.set(key, rows)
            for r in rows:
                ref = str(r.get("property_id") or r.get("url"))
                if ref in refs:
                    contacts = advertiser_contacts(r)
                    if contacts:
                        out[ref] = contacts
            ctx.bump("zips_refreshed")
        return out


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--refresh", action="store_true", help="re-read advertiser payloads from homeharvest")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    run = ListingAgentEnricher(refresh=args.refresh).execute()
    print(f"{run.agent_name}: {run.status} processed={run.records_processed} {run.metrics_json}")


if __name__ == "__main__":
    main()
