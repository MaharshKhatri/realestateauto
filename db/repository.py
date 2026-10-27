"""Idempotent upsert helpers. Agents call these instead of touching the ORM directly so
re-running any agent updates records rather than duplicating them."""
from __future__ import annotations

import re
from datetime import date
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from db.models import (CashBuyer, Contact, DeedTransfer, DistressSignal, Lead, LeadStage, Listing, Property,
                       utcnow)


def normalize_block_lot(value: str | None) -> str:
    """'00335' -> '335', '78.00' -> '78', '1234.01' -> '1234.01', ' 12 A' -> '12A'."""
    s = re.sub(r"\s+", "", str(value or "")).upper()
    if not s:
        return ""
    m = re.match(r"^0*(\d+)(?:\.(\d+))?([A-Z]*)$", s)
    if not m:
        return s
    whole, frac, suffix = m.group(1) or "0", m.group(2), m.group(3)
    if frac and int(frac) != 0:
        return f"{whole}.{frac}{suffix}"
    return f"{whole}{suffix}"


def make_parcel_id(county: str, mun_code: str, block: str, lot: str, qual: str | None = None) -> str:
    pid = f"{county.upper()}-{mun_code}-{normalize_block_lot(block)}-{normalize_block_lot(lot)}"
    q = re.sub(r"\s+", "", (qual or "")).upper()
    return f"{pid}-{q}" if q else pid


def _merge_value(current: Any, new: Any, overwrite: bool) -> Any:
    if new is None or new == "":
        return current
    if current is None or current == "" or overwrite:
        return new
    return current


def upsert_property(session: Session, data: dict[str, Any], *, source: str,
                    overwrite: bool = True) -> tuple[Property, bool]:
    """Insert or update a property keyed on parcel_id. Returns (property, created)."""
    parcel_id = data["parcel_id"]
    prop = session.scalar(select(Property).where(Property.parcel_id == parcel_id))
    created = prop is None
    if created:
        prop = Property(parcel_id=parcel_id, county=data.get("county", ""), data_sources=[], raw_json={})
        session.add(prop)
    protected = {"id", "parcel_id", "data_sources", "raw_json", "created_at", "updated_at"}
    for key, value in data.items():
        if key in protected or not hasattr(Property, key):
            continue
        setattr(prop, key, _merge_value(getattr(prop, key), value, overwrite))
    if source not in (prop.data_sources or []):
        prop.data_sources = [*(prop.data_sources or []), source]
    if data.get("raw_json"):
        prop.raw_json = {**(prop.raw_json or {}), source: data["raw_json"]}
    session.flush()
    return prop, created


def upsert_signal(session: Session, prop: Property, *, signal_type: str, source: str, source_ref: str,
                  **fields: Any) -> tuple[DistressSignal, bool]:
    sig = session.scalar(select(DistressSignal).where(
        DistressSignal.signal_type == signal_type, DistressSignal.source == source,
        DistressSignal.source_ref == source_ref))
    created = sig is None
    if created:
        sig = DistressSignal(property=prop, signal_type=signal_type, source=source, source_ref=source_ref)
        session.add(sig)
    elif sig.property_id != prop.id:
        sig.property = prop
    for key, value in fields.items():
        if hasattr(DistressSignal, key) and value is not None:
            setattr(sig, key, value)
    sig.last_seen_at = utcnow()
    session.flush()
    return sig, created


def ensure_lead(session: Session, prop: Property, *, primary_source: str,
                contact_name: str | None = None) -> tuple[Lead, bool]:
    lead = session.scalar(select(Lead).where(Lead.property_id == prop.id))
    created = lead is None
    if created:
        lead = Lead(property=prop, primary_source=primary_source, status_stage=LeadStage.NEW,
                    contact_name=contact_name, score_breakdown_json={})
        session.add(lead)
    elif contact_name and not lead.contact_name:
        lead.contact_name = contact_name
    session.flush()
    return lead, created


def buyer_key(name: str) -> str:
    s = re.sub(r"[^A-Z0-9 ]", " ", (name or "").upper())
    s = re.sub(r"\b(L ?L ?C|INC|CORP|CORPORATION|CO|COMPANY|LTD|LP|LLP)\b", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def upsert_cash_buyer(session: Session, entity_name: str, **fields: Any) -> tuple[CashBuyer, bool]:
    key = buyer_key(entity_name)
    buyer = session.scalar(select(CashBuyer).where(CashBuyer.entity_key == key))
    created = buyer is None
    if created:
        buyer = CashBuyer(entity_key=key, entity_name=entity_name, target_counties=[],
                          target_municipalities=[], recent_purchases_json=[])
        session.add(buyer)
    for k, v in fields.items():
        if hasattr(CashBuyer, k) and v is not None:
            setattr(buyer, k, v)
    session.flush()
    return buyer, created


def upsert_deed(session: Session, *, parcel_id: str, deed_date: date | None, grantee: str | None,
                source: str, **fields: Any) -> tuple[DeedTransfer, bool]:
    deed = session.scalar(select(DeedTransfer).where(
        DeedTransfer.parcel_id == parcel_id, DeedTransfer.deed_date == deed_date,
        DeedTransfer.grantee == grantee))
    created = deed is None
    if created:
        deed = DeedTransfer(parcel_id=parcel_id, deed_date=deed_date, grantee=grantee, source=source)
        session.add(deed)
    for k, v in fields.items():
        if hasattr(DeedTransfer, k) and v is not None:
            setattr(deed, k, v)
    session.flush()
    return deed, created


def upsert_listing(session: Session, *, source: str, source_ref: str, **fields: Any) -> tuple[Listing, bool]:
    listing = session.scalar(select(Listing).where(Listing.source == source, Listing.source_ref == source_ref))
    created = listing is None
    if created:
        listing = Listing(source=source, source_ref=source_ref)
        session.add(listing)
    for k, v in fields.items():
        if hasattr(Listing, k) and v is not None:
            setattr(listing, k, v)
    listing.last_seen_at = utcnow()
    session.flush()
    return listing, created


def upsert_contact(session: Session, *, lead: Lead | None, contact_type: str, source: str, name: str | None = None,
                   phone: str | None = None, email: str | None = None, **fields: Any) -> tuple[Contact, bool]:
    """Contacts are keyed on (lead, type, name, phone, email); a later source can raise confidence."""
    email = (email or "").strip().lower() or None
    contact = session.scalar(select(Contact).where(
        Contact.lead_id == (lead.id if lead else None), Contact.contact_type == contact_type,
        Contact.name.is_(name) if name is None else Contact.name == name,
        Contact.phone.is_(phone) if phone is None else Contact.phone == phone,
        Contact.email.is_(email) if email is None else Contact.email == email))
    created = contact is None
    if created:
        contact = Contact(lead=lead, property_id=lead.property_id if lead else None, contact_type=contact_type,
                          name=name, phone=phone, email=email, source=source, raw_json={})
        session.add(contact)
    for k, v in fields.items():
        if not hasattr(Contact, k) or v is None:
            continue
        if k == "confidence_score":
            v = max(v, contact.confidence_score or 0)
        setattr(contact, k, v)
    session.flush()
    return contact, created
