"""ORM models — the single source of truth for the database schema.

Natural keys (used for idempotent upserts):
  properties.parcel_id                 COUNTY-MUNCODE-BLOCK-LOT[-QUAL]   e.g. ESSEX-0702-335-78
  distress_signals (signal_type, source, source_ref)
  cash_buyers.entity_key               normalized buyer name
  listings (source, source_ref)
  deed_transfers (parcel_id, deed_date, grantee)
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from enum import StrEnum
from typing import Any

from sqlalchemy import (JSON, Boolean, Date, DateTime, Float, ForeignKey, Index, Integer, String,
                        Text, UniqueConstraint)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Base(DeclarativeBase):
    type_annotation_map = {dict[str, Any]: JSON, list[Any]: JSON}


class SignalType(StrEnum):
    SHERIFF_SALE = "SHERIFF_SALE"
    LIS_PENDENS = "LIS_PENDENS"
    TAX_DELINQUENT = "TAX_DELINQUENT"
    TAX_SALE_CERTIFICATE = "TAX_SALE_CERTIFICATE"
    CODE_VIOLATION = "CODE_VIOLATION"
    PROBATE = "PROBATE"
    ESTATE_OWNER = "ESTATE_OWNER"
    LONG_TERM_ABSENTEE = "LONG_TERM_ABSENTEE"
    OUT_OF_STATE_OWNER = "OUT_OF_STATE_OWNER"
    VACANT = "VACANT"
    FAILED_LISTING = "FAILED_LISTING"
    STALE_LISTING = "STALE_LISTING"
    PORTFOLIO_OWNER = "PORTFOLIO_OWNER"        # same owner/mailing holds several distressed or absentee leads
    ACTIVE_LISTING = "ACTIVE_LISTING"          # listed for sale now (any DOM) — found by cross-matching the MLS feed
    INBOUND_LEAD = "INBOUND_LEAD"              # the owner contacted us through the website form (engine/inbound.py)


class LeadStage(StrEnum):
    NEW = "NEW"
    SKIP_TRACE = "SKIP_TRACE"
    CONTACT_ATTEMPTED = "CONTACT_ATTEMPTED"
    CONTACTED = "CONTACTED"
    INTERESTED = "INTERESTED"
    APPOINTMENT = "APPOINTMENT"
    OFFER_SENT_TO_AGENT = "OFFER_SENT_TO_AGENT"   # LOI e-mailed/texted to the listing agent, awaiting presentation
    OFFER_MADE = "OFFER_MADE"
    NEGOTIATING = "NEGOTIATING"
    UNDER_CONTRACT = "UNDER_CONTRACT"
    ASSIGNED = "ASSIGNED"
    CLOSED = "CLOSED"
    DEAD = "DEAD"
    DO_NOT_CONTACT = "DO_NOT_CONTACT"


class Occupancy(StrEnum):
    """Who lives in the property — drives FRFPA applicability (N.J.S.A. 46:10B-54)."""
    UNKNOWN = "UNKNOWN"
    OWNER_OCCUPIED = "OWNER_OCCUPIED"
    HEIR_OCCUPIED = "HEIR_OCCUPIED"          # an heir/devisee (now an owner) lives there
    TENANT_OCCUPIED = "TENANT_OCCUPIED"
    VACANT = "VACANT"


class ContactType(StrEnum):
    OWNER = "OWNER"
    HEIR_EXECUTOR = "HEIR_EXECUTOR"
    LISTING_AGENT = "LISTING_AGENT"
    REGISTERED_AGENT = "REGISTERED_AGENT"
    ENTITY_PRINCIPAL = "ENTITY_PRINCIPAL"     # individual sharing an entity owner's mailing address
    RELATIVE = "RELATIVE"                     # co-defendant / co-owner / relative named on records
    BUYER = "BUYER"


class CallOutcome(StrEnum):
    NO_ANSWER = "NO_ANSWER"
    LEFT_VM = "LEFT_VM"
    WRONG_NUMBER = "WRONG_NUMBER"
    SPOKE_WARM_FOLLOWUP = "SPOKE_WARM_FOLLOWUP"
    SPOKE_NEEDS_OFFER = "SPOKE_NEEDS_OFFER"
    SPOKE_NOT_INTERESTED = "SPOKE_NOT_INTERESTED"
    AGENT_WILL_PRESENT_LOI = "AGENT_WILL_PRESENT_LOI"
    OFFER_SENT_TO_AGENT = "OFFER_SENT_TO_AGENT"    # written LOI e-mailed / texted to the listing agent
    OFFER_ACCEPTED = "OFFER_ACCEPTED"
    DEAD = "DEAD"
    DNC = "DNC"


class RehabTier(StrEnum):
    LIGHT = "LIGHT"
    MEDIUM = "MEDIUM"
    HEAVY = "HEAVY"


class Property(Base):
    __tablename__ = "properties"

    id: Mapped[int] = mapped_column(primary_key=True)
    parcel_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    pams_pin: Mapped[str | None] = mapped_column(String(40), index=True)
    county: Mapped[str] = mapped_column(String(32), index=True)
    municipality: Mapped[str | None] = mapped_column(String(64), index=True)
    mun_code: Mapped[str | None] = mapped_column(String(4), index=True)
    block: Mapped[str | None] = mapped_column(String(16))
    lot: Mapped[str | None] = mapped_column(String(16))
    qual: Mapped[str | None] = mapped_column(String(16))
    street_address: Mapped[str | None] = mapped_column(String(160), index=True)
    city: Mapped[str | None] = mapped_column(String(64))
    zip_code: Mapped[str | None] = mapped_column(String(10), index=True)
    property_class: Mapped[str | None] = mapped_column(String(4), index=True)
    building_desc: Mapped[str | None] = mapped_column(String(40))
    dwellings: Mapped[int | None] = mapped_column(Integer)

    owner_name: Mapped[str | None] = mapped_column(String(200))
    owner_name_source: Mapped[str | None] = mapped_column(String(40))
    owner_mailing_address: Mapped[str | None] = mapped_column(String(200))
    owner_city_state_zip: Mapped[str | None] = mapped_column(String(120))
    owner_state: Mapped[str | None] = mapped_column(String(2))
    is_absentee: Mapped[bool | None] = mapped_column(Boolean, index=True)
    is_out_of_state: Mapped[bool | None] = mapped_column(Boolean, index=True)
    is_corporate_owned: Mapped[bool | None] = mapped_column(Boolean)
    is_estate_owned: Mapped[bool | None] = mapped_column(Boolean)

    year_built: Mapped[int | None] = mapped_column(Integer)
    sqft: Mapped[int | None] = mapped_column(Integer)
    sqft_source: Mapped[str | None] = mapped_column(String(40))
    lot_size: Mapped[float | None] = mapped_column(Float)          # acres
    beds: Mapped[float | None] = mapped_column(Float)
    baths: Mapped[float | None] = mapped_column(Float)
    assessed_land: Mapped[int | None] = mapped_column(Integer)
    assessed_improvement: Mapped[int | None] = mapped_column(Integer)
    assessed_total: Mapped[int | None] = mapped_column(Integer)
    annual_taxes: Mapped[float | None] = mapped_column(Float)
    last_sale_date: Mapped[date | None] = mapped_column(Date)
    last_sale_price: Mapped[int | None] = mapped_column(Integer)
    estimated_value: Mapped[float | None] = mapped_column(Float)   # latest as-is/market estimate
    estimated_equity: Mapped[float | None] = mapped_column(Float)
    lat: Mapped[float | None] = mapped_column(Float)
    lon: Mapped[float | None] = mapped_column(Float)

    data_sources: Mapped[list[Any]] = mapped_column(JSON, default=list)
    raw_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    signals: Mapped[list[DistressSignal]] = relationship(back_populates="property", cascade="all, delete-orphan")
    valuations: Mapped[list[Valuation]] = relationship(back_populates="property", cascade="all, delete-orphan")
    lead: Mapped[Lead | None] = relationship(back_populates="property", uselist=False, cascade="all, delete-orphan")
    listings: Mapped[list[Listing]] = relationship(back_populates="property")

    __table_args__ = (Index("ix_properties_muni_block_lot", "mun_code", "block", "lot"),)

    @property
    def active_signals(self) -> list[DistressSignal]:
        return [s for s in self.signals if s.active]

    @property
    def current_valuation(self) -> Valuation | None:
        current = [v for v in self.valuations if v.is_current]
        return max(current, key=lambda v: v.calculated_at) if current else None

    @property
    def display_address(self) -> str:
        parts = [self.street_address or "", self.city or self.municipality or "", "NJ", self.zip_code or ""]
        return " ".join(p for p in parts if p).strip()


class DistressSignal(Base):
    __tablename__ = "distress_signals"

    id: Mapped[int] = mapped_column(primary_key=True)
    property_id: Mapped[int] = mapped_column(ForeignKey("properties.id", ondelete="CASCADE"), index=True)
    signal_type: Mapped[str] = mapped_column(String(32), index=True)
    severity_weight: Mapped[float] = mapped_column(Float, default=0.5)  # 0..1
    filing_date: Mapped[date | None] = mapped_column(Date)
    scheduled_auction_date: Mapped[date | None] = mapped_column(Date, index=True)
    amount_owed: Mapped[float | None] = mapped_column(Float)
    plaintiff_or_lienholder: Mapped[str | None] = mapped_column(String(300))
    defendant: Mapped[str | None] = mapped_column(String(300))
    case_number: Mapped[str | None] = mapped_column(String(64))
    status: Mapped[str | None] = mapped_column(String(64))
    source: Mapped[str] = mapped_column(String(48))
    source_ref: Mapped[str] = mapped_column(String(128))
    source_url: Mapped[str | None] = mapped_column(String(500))
    raw_metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    property: Mapped[Property] = relationship(back_populates="signals")

    __table_args__ = (UniqueConstraint("signal_type", "source", "source_ref", name="uq_signal_natural_key"),)


class Valuation(Base):
    __tablename__ = "valuations"

    id: Mapped[int] = mapped_column(primary_key=True)
    property_id: Mapped[int] = mapped_column(ForeignKey("properties.id", ondelete="CASCADE"), index=True)
    arv_estimate: Mapped[float | None] = mapped_column(Float)
    as_is_value: Mapped[float | None] = mapped_column(Float)
    est_repair_cost: Mapped[float | None] = mapped_column(Float)
    rehab_tier: Mapped[str | None] = mapped_column(String(8))
    target_assignment_fee: Mapped[float | None] = mapped_column(Float)
    mao_conservative: Mapped[float | None] = mapped_column(Float)
    mao_aggressive: Mapped[float | None] = mapped_column(Float)
    anchor_offer: Mapped[float | None] = mapped_column(Float)
    confidence_score: Mapped[float | None] = mapped_column(Float)       # 0..100
    comps_json: Mapped[list[Any]] = mapped_column(JSON, default=list)
    comp_count: Mapped[int] = mapped_column(Integer, default=0)
    comp_source: Mapped[str | None] = mapped_column(String(40))
    sqft_used: Mapped[int | None] = mapped_column(Integer)
    effective_tax_rate: Mapped[float | None] = mapped_column(Float)
    tax_drag_adjustment: Mapped[float | None] = mapped_column(Float)
    debt_estimate: Mapped[float | None] = mapped_column(Float)          # upset amount / liens known
    equity_estimate: Mapped[float | None] = mapped_column(Float)
    deal_spread: Mapped[float | None] = mapped_column(Float)            # MAO - debt (room to make a deal)
    verdict: Mapped[str | None] = mapped_column(String(24))
    assumptions_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    is_current: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    calculated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    property: Mapped[Property] = relationship(back_populates="valuations")


class Lead(Base):
    __tablename__ = "leads"

    id: Mapped[int] = mapped_column(primary_key=True)
    property_id: Mapped[int] = mapped_column(ForeignKey("properties.id", ondelete="CASCADE"), unique=True)
    motivation_score: Mapped[float] = mapped_column(Float, default=0)
    deal_viability_score: Mapped[float] = mapped_column(Float, default=0)
    priority_rank_score: Mapped[float] = mapped_column(Float, default=0, index=True)
    score_breakdown_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    scoring_version: Mapped[int | None] = mapped_column(Integer)
    status_stage: Mapped[str] = mapped_column(String(24), default=LeadStage.NEW, index=True)
    primary_source: Mapped[str | None] = mapped_column(String(48))
    contact_name: Mapped[str | None] = mapped_column(String(200))
    next_followup_at: Mapped[datetime | None] = mapped_column(DateTime)
    ai_strategy_brief: Mapped[str | None] = mapped_column(Text)
    notes: Mapped[str | None] = mapped_column(Text)
    outcome: Mapped[str | None] = mapped_column(String(48))   # feeds self-improvement (closed/dead reason)
    # Occupancy drives FRFPA. Underwriting may *presume* it from data; only an operator-confirmed value
    # (with its source, e.g. "seller said on 10/27 call") unlocks a below-82% contract.
    occupancy_status: Mapped[str] = mapped_column(String(20), default=Occupancy.UNKNOWN)
    occupancy_source: Mapped[str | None] = mapped_column(String(200))
    occupancy_confirmed_at: Mapped[datetime | None] = mapped_column(DateTime)
    frfpa_track: Mapped[str | None] = mapped_column(String(40), index=True)
    # Underwritten deal with a number we can dial (or a listing agent we can e-mail) right now.
    ready_to_call: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    contact_attempts: Mapped[int] = mapped_column(Integer, default=0)
    last_contact_at: Mapped[datetime | None] = mapped_column(DateTime)
    offer_amount: Mapped[float | None] = mapped_column(Float)
    last_counter_amount: Mapped[float | None] = mapped_column(Float)  # seller's / agent's latest counter (negotiator)
    contract_date: Mapped[date | None] = mapped_column(Date)          # fully signed contract delivered to both
    attorney_review_ends: Mapped[date | None] = mapped_column(Date)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    property: Mapped[Property] = relationship(back_populates="lead")
    contacts: Mapped[list[Contact]] = relationship(back_populates="lead", cascade="all, delete-orphan")
    interactions: Mapped[list[Interaction]] = relationship(back_populates="lead", cascade="all, delete-orphan",
                                                           order_by="Interaction.occurred_at.desc()")
    buyer_matches: Mapped[list[BuyerMatch]] = relationship(back_populates="lead", cascade="all, delete-orphan",
                                                           order_by="BuyerMatch.rank")


class CashBuyer(Base):
    __tablename__ = "cash_buyers"

    id: Mapped[int] = mapped_column(primary_key=True)
    entity_key: Mapped[str] = mapped_column(String(200), unique=True, index=True)
    entity_name: Mapped[str] = mapped_column(String(200))
    contact_name: Mapped[str | None] = mapped_column(String(200))
    phone: Mapped[str | None] = mapped_column(String(32))
    email: Mapped[str | None] = mapped_column(String(120))
    mailing_address: Mapped[str | None] = mapped_column(String(250))
    mailing_key: Mapped[str | None] = mapped_column(String(250), index=True)  # normalized street|unit|city
    buyer_type: Mapped[str | None] = mapped_column(String(24))      # flipper / landlord / builder / unknown
    target_counties: Mapped[list[Any]] = mapped_column(JSON, default=list)
    target_municipalities: Mapped[list[Any]] = mapped_column(JSON, default=list)
    min_arv: Mapped[float | None] = mapped_column(Float)
    max_arv: Mapped[float | None] = mapped_column(Float)
    max_rehab: Mapped[str | None] = mapped_column(String(8))         # LIGHT / MEDIUM / HEAVY
    cash_purchases_count: Mapped[int] = mapped_column(Integer, default=0)
    flips_count: Mapped[int] = mapped_column(Integer, default=0)
    total_volume: Mapped[float] = mapped_column(Float, default=0)
    avg_purchase_price: Mapped[float | None] = mapped_column(Float)
    first_purchase_date: Mapped[date | None] = mapped_column(Date)
    last_purchase_date: Mapped[date | None] = mapped_column(Date)
    recent_purchases_json: Mapped[list[Any]] = mapped_column(JSON, default=list)
    reliability_score: Mapped[float] = mapped_column(Float, default=0)
    source: Mapped[str | None] = mapped_column(String(48))
    asset_classes: Mapped[list[Any]] = mapped_column(JSON, default=list)   # self-reported (buyer registration form)
    proof_of_funds: Mapped[str | None] = mapped_column(String(32))
    registered_at: Mapped[datetime | None] = mapped_column(DateTime)       # opted in to deal e-mails / texts
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class DeedTransfer(Base):
    """A recorded sale (public record). Feeds cash-buyer detection and comps."""
    __tablename__ = "deed_transfers"

    id: Mapped[int] = mapped_column(primary_key=True)
    parcel_id: Mapped[str] = mapped_column(String(64), index=True)
    county: Mapped[str | None] = mapped_column(String(32))
    municipality: Mapped[str | None] = mapped_column(String(64))
    property_location: Mapped[str | None] = mapped_column(String(160))
    grantor: Mapped[str | None] = mapped_column(String(250))
    grantee: Mapped[str | None] = mapped_column(String(250))
    grantee_address: Mapped[str | None] = mapped_column(String(250))
    sale_price: Mapped[int | None] = mapped_column(Integer)
    deed_date: Mapped[date | None] = mapped_column(Date, index=True)
    nu_code: Mapped[str | None] = mapped_column(String(8))
    property_class: Mapped[str | None] = mapped_column(String(4))
    source: Mapped[str] = mapped_column(String(48))
    raw_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    __table_args__ = (UniqueConstraint("parcel_id", "deed_date", "grantee", name="uq_deed_natural_key"),)


class Listing(Base):
    """MLS-style listing data (active, sold, off-market) from listing providers."""
    __tablename__ = "listings"

    id: Mapped[int] = mapped_column(primary_key=True)
    property_id: Mapped[int | None] = mapped_column(ForeignKey("properties.id", ondelete="SET NULL"), index=True)
    source: Mapped[str] = mapped_column(String(32))
    source_ref: Mapped[str] = mapped_column(String(200))
    url: Mapped[str | None] = mapped_column(String(500))
    status: Mapped[str | None] = mapped_column(String(24), index=True)
    address: Mapped[str | None] = mapped_column(String(200))
    city: Mapped[str | None] = mapped_column(String(64))
    zip_code: Mapped[str | None] = mapped_column(String(10), index=True)
    property_type: Mapped[str | None] = mapped_column(String(40))
    beds: Mapped[float | None] = mapped_column(Float)
    baths: Mapped[float | None] = mapped_column(Float)
    sqft: Mapped[int | None] = mapped_column(Integer)
    year_built: Mapped[int | None] = mapped_column(Integer)
    list_price: Mapped[float | None] = mapped_column(Float)
    sold_price: Mapped[float | None] = mapped_column(Float)
    list_date: Mapped[date | None] = mapped_column(Date)
    sold_date: Mapped[date | None] = mapped_column(Date)
    days_on_market: Mapped[int | None] = mapped_column(Integer)
    price_reduced: Mapped[bool | None] = mapped_column(Boolean)
    distress_keywords: Mapped[list[Any]] = mapped_column(JSON, default=list)
    description: Mapped[str | None] = mapped_column(Text)
    lat: Mapped[float | None] = mapped_column(Float)
    lon: Mapped[float | None] = mapped_column(Float)
    raw_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    property: Mapped[Property | None] = relationship(back_populates="listings")

    __table_args__ = (UniqueConstraint("source", "source_ref", name="uq_listing_natural_key"),)


class AgentRun(Base):
    __tablename__ = "agent_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    agent_name: Mapped[str] = mapped_column(String(48), index=True)
    status: Mapped[str] = mapped_column(String(16), default="running")   # running/success/partial/failed
    started_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime)
    records_processed: Mapped[int] = mapped_column(Integer, default=0)
    new_leads_found: Mapped[int] = mapped_column(Integer, default=0)
    records_updated: Mapped[int] = mapped_column(Integer, default=0)
    error_count: Mapped[int] = mapped_column(Integer, default=0)
    errors: Mapped[list[Any]] = mapped_column(JSON, default=list)
    metrics_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    self_improvement_notes: Mapped[str | None] = mapped_column(Text)


class ScoringWeights(Base):
    """Versioned scoring weights. The self-improvement agent writes new versions."""
    __tablename__ = "scoring_weights"

    id: Mapped[int] = mapped_column(primary_key=True)
    version: Mapped[int] = mapped_column(Integer, unique=True)
    weights_json: Mapped[dict[str, Any]] = mapped_column(JSON)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    reason: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class SwarmState(Base):
    """Small key/value store for swarm decisions that persist between runs
    (e.g. auto-expansion municipalities chosen by the self-improvement agent)."""
    __tablename__ = "swarm_state"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)



class Contact(Base):
    """A person/entity we can reach about a lead: owner, heir/executor, listing agent, registered agent."""
    __tablename__ = "contacts"

    id: Mapped[int] = mapped_column(primary_key=True)
    lead_id: Mapped[int | None] = mapped_column(ForeignKey("leads.id", ondelete="CASCADE"), index=True)
    property_id: Mapped[int | None] = mapped_column(ForeignKey("properties.id", ondelete="CASCADE"), index=True)
    contact_type: Mapped[str] = mapped_column(String(20), index=True)
    name: Mapped[str | None] = mapped_column(String(200))
    phone: Mapped[str | None] = mapped_column(String(20), index=True)       # E.164-ish: 10 digits
    phone_type: Mapped[str | None] = mapped_column(String(20))              # wireless / landline / office / unknown
    dnc_status: Mapped[str | None] = mapped_column(String(20))              # clear / dnc / not_checked / exempt_b2b
    email: Mapped[str | None] = mapped_column(String(120))
    mailing_address: Mapped[str | None] = mapped_column(String(250))
    company: Mapped[str | None] = mapped_column(String(200))
    source: Mapped[str] = mapped_column(String(60))
    confidence_score: Mapped[float] = mapped_column(Float, default=0.5)
    notes: Mapped[str | None] = mapped_column(Text)
    raw_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    lead: Mapped[Lead | None] = relationship(back_populates="contacts")

    __table_args__ = (UniqueConstraint("lead_id", "contact_type", "name", "phone", "email",
                                       name="uq_contact_natural_key"),)


class Interaction(Base):
    """Call/text/email/visit log entry. Outcomes feed follow-up scheduling and the learner."""
    __tablename__ = "interactions"

    id: Mapped[int] = mapped_column(primary_key=True)
    lead_id: Mapped[int] = mapped_column(ForeignKey("leads.id", ondelete="CASCADE"), index=True)
    contact_id: Mapped[int | None] = mapped_column(ForeignKey("contacts.id", ondelete="SET NULL"))
    channel: Mapped[str] = mapped_column(String(16), default="call")
    outcome: Mapped[str] = mapped_column(String(32))
    notes: Mapped[str | None] = mapped_column(Text)
    debrief_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    next_followup_at: Mapped[datetime | None] = mapped_column(DateTime)
    occurred_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)

    lead: Mapped[Lead] = relationship(back_populates="interactions")


class DncEntry(Base):
    """Numbers we must not cold call: FTC registry download, internal requests, Daniel's Law, wrong numbers."""
    __tablename__ = "dnc_entries"

    phone: Mapped[str] = mapped_column(String(10), primary_key=True)
    source: Mapped[str] = mapped_column(String(24), primary_key=True)   # ftc | internal | daniels_law | litigator
    list_date: Mapped[date | None] = mapped_column(Date)                # FTC data must be <= 31 days old
    added_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    notes: Mapped[str | None] = mapped_column(String(200))


class MailSuppression(Base):
    """Addresses we must not mail or share: Daniel's Law nondisclosure requests, stop-mail requests, DNC leads."""
    __tablename__ = "mail_suppressions"

    address_key: Mapped[str] = mapped_column(String(250), primary_key=True)   # nj_tax_assessment_agent.mailing_key
    source: Mapped[str] = mapped_column(String(24), primary_key=True)         # daniels_law | stop_mail | internal
    address: Mapped[str | None] = mapped_column(String(250))
    added_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    notes: Mapped[str | None] = mapped_column(String(200))


class BuyerBid(Base):
    """A cash buyer's walkthrough and bid on one of our contracts (remote disposition)."""
    __tablename__ = "buyer_bids"

    id: Mapped[int] = mapped_column(primary_key=True)
    lead_id: Mapped[int] = mapped_column(ForeignKey("leads.id", ondelete="CASCADE"), index=True)
    buyer_id: Mapped[int] = mapped_column(ForeignKey("cash_buyers.id", ondelete="CASCADE"), index=True)
    walkthrough_date: Mapped[datetime | None] = mapped_column(DateTime)
    bid_amount: Mapped[float | None] = mapped_column(Float)
    emd_amount: Mapped[float | None] = mapped_column(Float)
    status: Mapped[str] = mapped_column(String(16), default="invited")   # invited|scheduled|walked|bid|accepted|declined|withdrawn
    notes: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    buyer: Mapped[CashBuyer] = relationship()

    __table_args__ = (UniqueConstraint("lead_id", "buyer_id", name="uq_buyer_bid_lead_buyer"),)


class ClosingMilestone(Base):
    """One step of a deal's contract-to-close checklist (engine/closing_coordinator.py)."""
    __tablename__ = "closing_milestones"

    id: Mapped[int] = mapped_column(primary_key=True)
    lead_id: Mapped[int] = mapped_column(ForeignKey("leads.id", ondelete="CASCADE"), index=True)
    milestone: Mapped[str] = mapped_column(String(48))
    sequence: Mapped[int] = mapped_column(Integer)
    due_date: Mapped[date | None] = mapped_column(Date, index=True)
    status: Mapped[str] = mapped_column(String(12), default="pending")       # pending | done | waived | cancelled
    completed_at: Mapped[datetime | None] = mapped_column(DateTime)
    notes: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    __table_args__ = (UniqueConstraint("lead_id", "milestone", name="uq_closing_milestone"),)


class LedgerEntry(Base):
    """Money in and out of the business (engine/finance.py): assignment fees, marketing, legal, title, tools."""
    __tablename__ = "financial_ledger"

    id: Mapped[int] = mapped_column(primary_key=True)
    lead_id: Mapped[int | None] = mapped_column(ForeignKey("leads.id", ondelete="SET NULL"), index=True)
    entry_date: Mapped[date] = mapped_column(Date, index=True)
    kind: Mapped[str] = mapped_column(String(8))              # income | expense
    category: Mapped[str] = mapped_column(String(32))
    amount: Mapped[float] = mapped_column(Float)
    description: Mapped[str | None] = mapped_column(String(300))
    source_ref: Mapped[str | None] = mapped_column(String(80), unique=True)   # idempotency key for automatic entries
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class BuyerMatch(Base):
    __tablename__ = "buyer_matches"

    id: Mapped[int] = mapped_column(primary_key=True)
    lead_id: Mapped[int] = mapped_column(ForeignKey("leads.id", ondelete="CASCADE"), index=True)
    buyer_id: Mapped[int] = mapped_column(ForeignKey("cash_buyers.id", ondelete="CASCADE"), index=True)
    rank: Mapped[int] = mapped_column(Integer)
    score: Mapped[float] = mapped_column(Float)
    reasons: Mapped[list[Any]] = mapped_column(JSON, default=list)
    matched_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    lead: Mapped[Lead] = relationship(back_populates="buyer_matches")
    buyer: Mapped[CashBuyer] = relationship()


class GeneratedDocument(Base):
    __tablename__ = "generated_documents"

    id: Mapped[int] = mapped_column(primary_key=True)
    lead_id: Mapped[int] = mapped_column(ForeignKey("leads.id", ondelete="CASCADE"), index=True)
    doc_type: Mapped[str] = mapped_column(String(32))
    path: Mapped[str] = mapped_column(String(400))
    price: Mapped[float | None] = mapped_column(Float)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class ClerkFiling(Base):
    """A county-clerk index entry (lis pendens / foreclosure complaint / discharge), matched or not.

    Clerk indexes carry party names, the recorded date and the instrument number, but rarely the street address,
    so a filing is kept here until it can be tied to a parcel (block/lot or address in an import, or a unique
    owner-name match in the same town); the match turns it into a LIS_PENDENS signal on that property.
    """
    __tablename__ = "clerk_filings"

    id: Mapped[int] = mapped_column(primary_key=True)
    county: Mapped[str] = mapped_column(String(32), index=True)
    source: Mapped[str] = mapped_column(String(32))                 # ucpa | press | import
    instrument: Mapped[str] = mapped_column(String(48))
    doc_type: Mapped[str] = mapped_column(String(80))
    recorded_date: Mapped[date | None] = mapped_column(Date, index=True)
    defendants: Mapped[list[Any]] = mapped_column(JSON, default=list)
    plaintiffs: Mapped[list[Any]] = mapped_column(JSON, default=list)
    town: Mapped[str | None] = mapped_column(String(64))
    block: Mapped[str | None] = mapped_column(String(32))
    lot: Mapped[str | None] = mapped_column(String(32))
    qual: Mapped[str | None] = mapped_column(String(16))
    address: Mapped[str | None] = mapped_column(String(200))
    docket: Mapped[str | None] = mapped_column(String(48))
    url: Mapped[str | None] = mapped_column(String(500))
    is_discharge: Mapped[bool] = mapped_column(Boolean, default=False)
    estate: Mapped[bool] = mapped_column(Boolean, default=False)
    tax_lien: Mapped[bool] = mapped_column(Boolean, default=False)
    property_id: Mapped[int | None] = mapped_column(ForeignKey("properties.id", ondelete="SET NULL"), index=True)
    match_method: Mapped[str | None] = mapped_column(String(48))
    first_seen_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    __table_args__ = (UniqueConstraint("county", "instrument", name="uq_clerk_filing"),)


class OpraRequest(Base):
    """An OPRA request the operator sent to a town (engine/opra_generator.py) and its response deadline."""
    __tablename__ = "opra_requests"

    id: Mapped[int] = mapped_column(primary_key=True)
    county: Mapped[str] = mapped_column(String(32))
    town: Mapped[str] = mapped_column(String(64))
    year: Mapped[int] = mapped_column(Integer)
    channel: Mapped[str] = mapped_column(String(16))               # email | portal | mail | phone
    status: Mapped[str] = mapped_column(String(16), default="sent")  # sent | received | denied
    sent_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    due_date: Mapped[date | None] = mapped_column(Date)
    received_at: Mapped[datetime | None] = mapped_column(DateTime)
    notes: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (UniqueConstraint("county", "town", "year", name="uq_opra_request"),)


class JVPartner(Base):
    """A local cash buyer / rehabber the operator has (or wants) as a funding partner: their LLC signs, their proof
    of funds and deposit back the offer, they walk the house, and the profit is split under a JV agreement
    (both are principals; nobody is paid a referral fee, N.J.S.A. 45:15-3). engine/jv_partner_vault.py."""
    __tablename__ = "jv_partners"

    id: Mapped[int] = mapped_column(primary_key=True)
    buyer_id: Mapped[int | None] = mapped_column(ForeignKey("cash_buyers.id", ondelete="SET NULL"), index=True)
    contact_name: Mapped[str | None] = mapped_column(String(200))
    entity_name: Mapped[str] = mapped_column(String(200))
    phone: Mapped[str | None] = mapped_column(String(32))
    email: Mapped[str | None] = mapped_column(String(120))
    target_counties: Mapped[list[Any]] = mapped_column(JSON, default=list)
    target_towns: Mapped[list[Any]] = mapped_column(JSON, default=list)
    min_price: Mapped[float | None] = mapped_column(Float)
    max_price: Mapped[float | None] = mapped_column(Float)
    max_rehab: Mapped[str | None] = mapped_column(String(8))             # LIGHT / MEDIUM / HEAVY
    pof_verified: Mapped[bool] = mapped_column(Boolean, default=False)
    pof_verified_at: Mapped[datetime | None] = mapped_column(DateTime)
    pof_document_path: Mapped[str | None] = mapped_column(String(400))   # data/partners/<id>/... (git-ignored)
    pof_share_ok: Mapped[bool] = mapped_column(Boolean, default=False)   # partner agreed we may show it to agents
    provides_emd: Mapped[bool] = mapped_column(Boolean, default=False)
    will_walk_properties: Mapped[bool] = mapped_column(Boolean, default=False)
    default_split_pct: Mapped[float] = mapped_column(Float, default=50.0)  # partner's share of net profit
    status: Mapped[str] = mapped_column(String(16), default="TARGET", index=True)  # TARGET / CONTACTED / ACTIVE_PARTNER / DECLINED
    velocity_score: Mapped[float] = mapped_column(Float, default=0)      # why they were seeded (purchases, flips, recency)
    last_contact_at: Mapped[datetime | None] = mapped_column(DateTime)
    notes: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    buyer: Mapped[CashBuyer | None] = relationship()

