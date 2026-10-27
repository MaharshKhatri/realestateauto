"""Foreclosure Rescue Fraud Prevention Act classifier (N.J.S.A. 46:10B-53 to -68).

FRFPA's "distressed property" is 1–4 family residential property, **at least one unit occupied by the
owner as the owner's principal residence**, that is in foreclosure or whose owner is 90+ days
delinquent on a loan it secures. If it applies, the owner must receive >= 82% of fair market value,
the contract needs statutory notices + a 10-business-day cancellation right, etc. (CLAUDE.md).

So the question for every foreclosure lead is *occupancy*:

  confidence  track                         rule
  ----------  ----------------------------  -------------------------------------------------------
  confirmed   operator-confirmed occupancy   the operator recorded who lives there and how they know
  record      EXEMPT_ENTITY                 owner/defendant is an LLC/corp (an entity has no principal
                                            residence). Trusts are NOT included: a living trust's
                                            settlor can live in the home.
  presumed    EXEMPT_VACANT                 sheriff listing says VACANT / vacant signal
  presumed    EXEMPT_ESTATE                 estate/heirs AND (vacant OR owner mails elsewhere)
  presumed    EXEMPT_NON_OWNER_OCCUPIED     owner's tax-bill mailing address is elsewhere
  verify      ESTATE_VERIFY_OCCUPANCY       estate/heirs with unknown occupancy — an heir who lives
                                            there is an owner-occupant, so FRFPA may apply
  covered     OWNER_OCCUPIED                presumed owner-occupied (mailing = property address)

Presumed exemptions unlock the standard 70% MAO for *screening*, but a below-82% contract may only
be generated after the operator confirms occupancy on the lead (engine/contracts.py enforces it).

For covered leads the underwriter then picks a monetization track:
  WORKABLE_82_PCT_FLOOR   an offer at 82% of FMV still leaves a buyer spread (light rehab, high equity)
  EQUITY_RESCUE_REFERRAL  82% doesn't work for an investor but (0.90 x FMV - debt) > $75K: the owner
                          is about to lose real equity — refer to a listing agent (no fee unless the
                          operator is licensed: N.J.S.A. 45:15-3)
  FRFPA_BLOCKED           neither works
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from core.text import ESTATE_PATTERNS, clean, is_entity

TRUST_RE = re.compile(r"\b(TRUST|TRUSTEE|TRUSTEES|TR|REVOCABLE|LIVING TRUST)\b")
ESTATE_DEFENDANT_RE = re.compile(
    r"\b(HEIRS|DEVISEES|ESTATE OF|EXECUTOR|EXECUTRIX|ADMINISTRAT(?:OR|RIX)|DECEASED|PERSONAL REPRESENTATIVE)\b", re.I)

EXEMPT_TRACKS = {"EXEMPT_ENTITY", "EXEMPT_VACANT", "EXEMPT_ESTATE", "EXEMPT_NON_OWNER_OCCUPIED",
                 "EXEMPT_CONFIRMED", "NOT_IN_FORECLOSURE"}
COVERED_TRACKS = {"OWNER_OCCUPIED", "ESTATE_VERIFY_OCCUPANCY", "CONFIRMED_OWNER_OCCUPIED"}


@dataclass
class OccupancyFacts:
    in_foreclosure: bool
    one_to_four: bool
    owner_names: list[str]                    # owner of record and/or first defendant
    defendant: str | None = None              # full sheriff-sale defendant line
    vacant_signal: bool = False
    estate_signal: bool = False
    is_absentee: bool | None = None           # mailing address elsewhere (servicer addresses already excluded)
    confirmed_occupancy: str | None = None    # Occupancy value recorded by the operator
    confirmed_source: str | None = None


@dataclass
class FrfpaDecision:
    track: str
    applies: bool                             # price/contract as FRFPA-covered
    confidence: str                           # record | presumed | verify | covered | confirmed | n/a
    basis: str
    verify_before_contract: list[str] = field(default_factory=list)

    @property
    def exempt(self) -> bool:
        return not self.applies


def entity_owner(names: list[str]) -> str | None:
    for n in names:
        n_up = clean(n).upper()
        if n_up and is_entity(n_up) and not TRUST_RE.search(n_up):
            return n
    return None


def is_estate(facts: OccupancyFacts) -> bool:
    if facts.estate_signal:
        return True
    texts = [*(facts.owner_names or []), facts.defendant or ""]
    return any(ESTATE_DEFENDANT_RE.search(t or "") or ESTATE_PATTERNS.search(clean(t).upper()) for t in texts)


def classify(facts: OccupancyFacts) -> FrfpaDecision:
    if not facts.in_foreclosure:
        return FrfpaDecision("NOT_IN_FORECLOSURE", False, "n/a",
                             "No foreclosure signal: FRFPA's distressed-property test is not met on the record.")
    if not facts.one_to_four:
        return FrfpaDecision("NOT_IN_FORECLOSURE", False, "record",
                             "Not 1–4 family residential: outside FRFPA's distressed-property definition.")

    if facts.confirmed_occupancy:
        occ = facts.confirmed_occupancy
        src = facts.confirmed_source or "operator"
        if occ in ("OWNER_OCCUPIED", "HEIR_OCCUPIED"):
            return FrfpaDecision("CONFIRMED_OWNER_OCCUPIED", True, "confirmed",
                                 f"Operator confirmed {occ.replace('_', ' ').lower()} ({src}): FRFPA applies.")
        if occ in ("TENANT_OCCUPIED", "VACANT"):
            return FrfpaDecision("EXEMPT_CONFIRMED", False, "confirmed",
                                 f"Operator confirmed {occ.replace('_', ' ').lower()} ({src}): no owner lives there.")

    ent = entity_owner(facts.owner_names)
    if ent:
        return FrfpaDecision("EXEMPT_ENTITY", False, "record",
                             f"Owner is an entity ({ent}); an entity cannot occupy the home as a principal residence.")
    if facts.vacant_signal:
        return FrfpaDecision("EXEMPT_VACANT", False, "presumed",
                             "Sheriff listing / records show the property VACANT.",
                             ["Confirm vacancy (drive-by or seller statement) and record it on the lead"])
    if is_estate(facts):
        if facts.is_absentee:
            return FrfpaDecision("EXEMPT_ESTATE", False, "presumed",
                                 "Estate/heirs and the tax bill goes elsewhere: likely no heir lives there.",
                                 ["Ask whether any heir or family member lives in the house; record the answer"])
        return FrfpaDecision("ESTATE_VERIFY_OCCUPANCY", True, "verify",
                             "Estate/heirs with unknown occupancy. An heir living there is an owner-occupant, so "
                             "FRFPA may apply — priced at the 82% floor until occupancy is confirmed.",
                             ["Ask who lives in the house now; if nobody (or only tenants), record it to unlock 70% MAO"])
    if facts.is_absentee:
        return FrfpaDecision("EXEMPT_NON_OWNER_OCCUPIED", False, "presumed",
                             "Owner's tax-bill mailing address is elsewhere: presumed not owner-occupied.",
                             ["Confirm the owner does not live there (tenant-occupied or vacant) and record it"])
    return FrfpaDecision("OWNER_OCCUPIED", True, "covered",
                         "Presumed owner-occupied principal residence in foreclosure: FRFPA applies "
                         "(>= 82% of FMV, statutory contract, 10-business-day cancellation).")


# -- monetization tracks for covered leads --------------------------------------------------------------
@dataclass
class CoveredTrack:
    track: str
    compliant_offer: float | None
    buyer_price: float | None
    fee: float | None
    equity_at_risk: float | None
    notes: list[str]


def covered_track(*, arv: float, fmv: float, repairs: float, debt: float | None, aggressive_pct: float = 0.75,
                  min_fee: float = 10000, equity_threshold: float = 75000) -> CoveredTrack:
    """Decide how a FRFPA-covered deal can still be worked."""
    floor = round(0.82 * fmv, -2)
    buyer_price = arv * aggressive_pct - repairs            # what a light-rehab / wholetail buyer can pay
    owed = debt or 0.0
    notes: list[str] = []
    if floor >= owed and buyer_price - floor >= min_fee:
        fee = round(min(buyer_price - floor, 35000), -2)
        notes.append(f"Compliant offer ${floor:,.0f} (82% of FMV ${fmv:,.0f}); a {aggressive_pct:.0%}-of-ARV buyer at "
                     f"${buyer_price:,.0f} leaves a ${fee:,.0f} fee.")
        return CoveredTrack("WORKABLE_82_PCT_FLOOR", floor, round(buyer_price, -2), fee, None, notes)
    equity_at_risk = round(fmv * 0.90 - owed, -2)
    if equity_at_risk > equity_threshold:
        notes.append(f"Owner stands to lose ~${equity_at_risk:,.0f} of equity (90% of FMV minus debt) at sheriff sale. "
                     "Best outcome is a market sale through a listing agent before the auction.")
        notes.append("Unlicensed, you cannot accept a referral fee for this (N.J.S.A. 45:15-3); refer for goodwill, "
                     "or get licensed to earn referral fees through a broker.")
        return CoveredTrack("EQUITY_RESCUE_REFERRAL", None, None, None, equity_at_risk, notes)
    notes.append(f"An 82%-of-FMV offer (${floor:,.0f}) leaves no buyer spread and equity at risk is small.")
    return CoveredTrack("FRFPA_BLOCKED", None, None, None, equity_at_risk, notes)
