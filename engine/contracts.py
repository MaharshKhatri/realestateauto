"""Deal documents: LOI, NJ purchase-agreement draft, assignment, JV agreement and the 1-page deal sheet.

Every document is built once as structured blocks and rendered three ways into deals/<property_id>/:
Markdown (edit), HTML (print/e-mail) and PDF (reportlab). Each generation is recorded in
generated_documents. deals/ is git-ignored: the documents name the seller.

These are drafts for a New Jersey real estate attorney to review — not legal advice. The generator
enforces the compliance gates from CLAUDE.md before it writes anything:

  * FRFPA (N.J.S.A. 46:10B-53 to -68): a covered owner-occupied foreclosure gets no generated purchase
    contract at all (the statute dictates its form: 14-pt bold notices, 10-business-day cancellation,
    notarized and recorded — the attorney drafts it). Its LOI must be at or above 82% of fair market value.
  * A *presumed* exemption (vacant / absentee / estate) only unlocks a below-82% offer once the operator has
    confirmed occupancy on the lead (cockpit or `manage.py occupancy`).
  * No investor offer on EQUITY_RESCUE_REFERRAL / FRFPA_BLOCKED verdicts.
  * Attorney review (3 NJ business days) is written into every purchase and assignment draft.
  * Deal sheets carry an INTERNAL watermark until the seller contract is signed and attorney review has
    ended; assignment and JV drafts need a recorded seller contract.
"""
from __future__ import annotations

import html
import os
import re
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from core.config import ROOT, settings
from core.dates import nj_business_days_after
from db.models import CashBuyer, ContactType, GeneratedDocument, Lead, SignalType
from engine.scripts import buyer_display_name, lead_facts, marketable, money, speaking_name

DEALS_DIR = Path(os.environ.get("WHOLESALING_DEALS_DIR") or ROOT / "deals")
DOC_TYPES = ("loi", "psa", "assignment", "jv", "deal_sheet")
NO_OFFER_VERDICTS = {"FRFPA_BLOCKED", "EQUITY_RESCUE_REFERRAL"}
DEAL_VERDICTS = {"STRONG", "WORKABLE", "WORKABLE_82_PCT_FLOOR", "TIGHT"}
LEAD_PAINT_YEAR = 1978
INTERNAL_MARK = "INTERNAL - NOT FOR DISTRIBUTION"
DRAFT_MARK = "DRAFT - ATTORNEY REVIEW"


class ContractBlocked(Exception):
    def __init__(self, doc_type: str, reasons: list[str]):
        self.doc_type, self.reasons = doc_type, reasons
        super().__init__(f"{doc_type}: " + "; ".join(reasons))


@dataclass
class Gate:
    allowed: bool
    reasons: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    floor: float | None = None
    track: str | None = None


@dataclass
class Doc:
    doc_type: str
    title: str
    blocks: list[tuple[str, Any]]
    price: float | None = None
    watermark: str | None = DRAFT_MARK
    warnings: list[str] = field(default_factory=list)


# -- facts ------------------------------------------------------------------------------------------
def _cfg() -> dict[str, Any]:
    return settings().get("contracts", {})


def buyer_entity() -> str:
    return f"{settings().operator.get('company_name')} and/or assigns"


def seller_parties(lead: Lead) -> tuple[str, list[str], bool]:
    """(seller line, signature names, is_estate). Blank lines are fill-ins the attorney completes from the deed."""
    p = lead.property
    execs = [c for c in lead.contacts if c.contact_type == ContactType.HEIR_EXECUTOR and c.name]
    if execs:
        ex = execs[0]
        m = re.search(r"deceased:\s*([^;]+)", ex.notes or "")
        decedent = m.group(1).strip() if m else "________________"
        role = "Administrator" if "administrat" in (ex.notes or "").lower() else "Executor"
        return (f"{ex.name}, as {role} of the Estate of {decedent}", [f"{ex.name}, {role}"], True)
    owners = [c.name for c in lead.contacts if c.contact_type == ContactType.OWNER and c.name]
    names = owners or ([speaking_name(p.owner_name)] if p.owner_name else [])
    estate_signal = bool(p.is_estate_owned) or any(s.signal_type in (SignalType.ESTATE_OWNER, SignalType.PROBATE)
                                                    for s in p.active_signals)
    if estate_signal and names and not owners:
        return (f"The Estate of {names[0]}, by its court-appointed personal representative ________________",
                ["Personal representative of the Estate"], True)
    if not names:
        return ("________________________ (every owner on the deed)", ["Seller", "Seller"], bool(p.is_estate_owned))
    return (" and ".join(dict.fromkeys(names)), list(dict.fromkeys(names)), bool(p.is_estate_owned))


def property_legal(lead: Lead) -> dict[str, str]:
    p = lead.property
    return {"address": f"{(p.street_address or '').title()}, {(p.city or '').title() or (p.municipality or '').title()}, "
                       f"NJ {p.zip_code or ''}".strip(),
            "municipality": (p.municipality or "").title(), "county": f"{p.county} County",
            "block": p.block or "____", "lot": p.lot or "____", "qual": p.qual or ""}


def default_price(lead: Lead) -> float | None:
    v = lead.property.current_valuation
    if v is None:
        return None
    a = v.assumptions_json or {}
    if v.verdict == "WORKABLE_82_PCT_FLOOR":
        return a.get("compliant_offer")
    if a.get("frfpa_applies") and a.get("frfpa_floor"):
        return max(v.anchor_offer or 0, a["frfpa_floor"])
    return v.anchor_offer


def end_buyer_price(lead: Lead) -> float | None:
    v = lead.property.current_valuation
    if v is None:
        return None
    a = v.assumptions_json or {}
    return a.get("track_buyer_price") if v.verdict == "WORKABLE_82_PCT_FLOOR" else v.mao_aggressive


def in_foreclosure(lead: Lead) -> bool:
    return any(s.signal_type in (SignalType.SHERIFF_SALE, SignalType.LIS_PENDENS) for s in lead.property.active_signals)


# -- compliance gate ---------------------------------------------------------------------------------
def contract_gate(lead: Lead, doc_type: str, price: float | None, explicit_price: bool = False) -> Gate:
    from engine.underwriter import frfpa_decision

    v = lead.property.current_valuation
    gate = Gate(True)
    if doc_type in ("loi", "psa"):
        if v is None:
            return Gate(False, ["Underwrite the lead first (manage.py underwrite --lead <id>)."])
        decision = frfpa_decision(lead.property)
        gate.track = decision.track
        fmv = v.as_is_value
        pct = float(settings().get("compliance", {}).get("frfpa_min_pct_fmv", 0.82))
        gate.floor = round(fmv * pct, -2) if fmv else None
        if v.verdict in NO_OFFER_VERDICTS:
            gate.allowed = False
            gate.reasons.append(f"Verdict {v.verdict}: no investor offer. Refer the owner to a listing agent / HUD "
                                "counselor (free referral only while unlicensed — N.J.S.A. 45:15-3).")
        if v.verdict == "PASS" and not explicit_price:
            gate.allowed = False
            gate.reasons.append("Verdict PASS: the numbers don't support an offer. Pass an explicit --price to override.")
        if price is None:
            gate.allowed = False
            gate.reasons.append("No offer price available.")
        if decision.applies:
            if price is not None and gate.floor and price < gate.floor:
                gate.allowed = False
                gate.reasons.append(f"{money(price)} is below the FRFPA floor of {money(gate.floor)} (82% of "
                                    f"{money(fmv)} fair market value). {decision.basis}")
            if doc_type == "psa":
                gate.allowed = False
                gate.reasons.append("FRFPA-covered (owner-occupied foreclosure): the purchase contract must be drafted "
                                    "by a NJ attorney with the statutory form — 14-pt bold notices, notarized, "
                                    "10-business-day cancellation, recorded within 10 business days. Send the LOI and "
                                    "the deal facts to the attorney instead.")
            gate.warnings.append("FRFPA applies: owner must receive at least 82% of FMV; closing at an attorney/title office.")
        elif decision.confidence == "presumed" and in_foreclosure(lead) and price is not None and gate.floor \
                and price < gate.floor and not lead.occupancy_confirmed_at:
            gate.allowed = False
            gate.reasons.append(f"Exemption is only presumed ({decision.basis}). Confirm who lives there on the call and "
                                f"record it on the lead before an offer below {money(gate.floor)} (82% of FMV).")
        for item in decision.verify_before_contract:
            gate.warnings.append(f"Verify: {item}")
    if doc_type == "deal_sheet" and (v is None or v.verdict not in DEAL_VERDICTS):
        return Gate(False, [f"Verdict {v.verdict if v else 'none'}: no deal to show buyers."])
    if doc_type in ("assignment", "jv"):
        if not lead.contract_date:
            gate.allowed = False
            gate.reasons.append("Record the signed seller contract first (contract date + price on the lead).")
        elif not marketable(lead):
            gate.warnings.append(f"Seller attorney review ends {lead.attorney_review_ends or 'unknown'}: do not sign or "
                                 "market until it ends with no disapproval.")
    return gate


# -- document builders ------------------------------------------------------------------------------
ATTORNEY_REVIEW = [
    "**Study by Attorney.** The Buyer or the Seller may choose to have an attorney study this Agreement. If an attorney "
    "is consulted, the attorney must complete his or her review of the Agreement within a three-day period. This "
    "Agreement will be legally binding at the end of this three-day period unless an attorney for the Buyer or the "
    "Seller reviews and disapproves of the Agreement.",
    "**Counting the Time.** You count the three days from the date of delivery of the fully signed Agreement to the "
    "Buyer and the Seller. You do not count Saturdays, Sundays or legal holidays. The Buyer and the Seller may agree in "
    "writing to extend the three-day period for attorney review.",
    "**Notice of Disapproval.** If an attorney for the Buyer or the Seller reviews and disapproves of this Agreement, the "
    "attorney must notify the other party within the three-day period, by e-mail, fax, personal delivery or overnight "
    "mail with proof of delivery. Otherwise this Agreement will be legally binding as written.",
    "No deposit is due, no inspection or title charges are incurred and the Buyer will not market or assign this "
    "Agreement until the attorney-review period has ended without disapproval.",
]


def _facts_table(f: dict[str, Any]) -> list[list[str]]:
    return [["Address", f.get("address") or ""], ["Units", str(f.get("units") or "—")],
            ["Year built", str(f.get("year_built") or "—")], ["Living area", f"{f['sqft']:,} sq ft" if f.get("sqft") else "—"],
            ["Annual taxes", money(f.get("annual_taxes"))]]


def build_loi(lead: Lead, price: float, today: date | None = None) -> Doc:
    today = today or date.today()
    cfg = _cfg()
    f = lead_facts(lead)
    legal = property_legal(lead)
    seller, _, estate = seller_parties(lead)
    agent = next((c for c in lead.contacts if c.contact_type == ContactType.LISTING_AGENT and c.name), None)
    valid = today + timedelta(days=int(cfg.get("loi_valid_days", 5)))
    review_end = nj_business_days_after(today, 3)
    blocks: list[tuple[str, Any]] = [
        ("h1", "Letter of Intent to Purchase Real Estate"),
        ("p", f"Date: {today:%B %d, %Y}"),
        ("p", f"To: {seller}" + (f", c/o {agent.name}{' — ' + agent.company if agent.company else ''} (listing agent)"
                                 if agent else "")),
        ("p", f"From: {buyer_entity()} (\"Buyer\")"),
        ("p", f"Re: {legal['address']} — Block {legal['block']}, Lot {legal['lot']}"
              f"{', Qual. ' + legal['qual'] if legal['qual'] else ''}, {legal['municipality']}, {legal['county']}"),
        ("p", "This letter states the main terms on which Buyer proposes to purchase the property. It is **not a binding "
              "contract**: neither party is bound until a written purchase agreement is signed by everyone on title and "
              "the three-business-day attorney-review period has ended without disapproval."),
        ("table", [["Term", "Proposal"],
                   ["Purchase price", money(price)],
                   ["Payment", "All cash at closing; no mortgage or appraisal contingency"],
                   ["Condition", "As-is, where-is; Seller makes no repairs; Buyer may remove left-behind items"],
                   ["Deposit", f"{money(cfg.get('earnest_money', 1000))}, held in escrow by the closing title company/"
                               "attorney, paid after attorney review ends"],
                   ["Inspection", f"{cfg.get('inspection_business_days', 10)} business days after attorney review; "
                                  "Buyer may cancel during that time and the deposit is returned"],
                   ["Closing", f"On or before {cfg.get('closing_days', 30)} days after attorney review, at a New Jersey "
                               "title company or attorney's office; Seller's liens paid from the proceeds"],
                   ["Possession", "Vacant and broom-clean at closing" if f.get("units") in (None, 1)
                    else "Vacant, or subject to existing leases with rent roll and security deposits transferred"],
                   ["Title", "Marketable and insurable title by bargain-and-sale deed with covenants"],
                   ["Closing costs", "Each party pays its customary costs; Seller pays the NJ Realty Transfer Fee"]]),
        ("h2", "Disclosures"),
        ("ul", ["Buyer is a real estate investor acting as a principal — **not a licensed real estate broker or agent** — "
                "and does not represent the Seller.",
                f"Buyer may assign its purchase contract to another investor (\"{buyer_entity()}\"). The price and terms to "
                "the Seller do not change, and any assignment fee is paid by the assignee, not the Seller.",
                "The Seller is encouraged to have a New Jersey attorney review the purchase agreement before it becomes "
                "binding. There is no obligation and no pressure to accept.",
                f"If delivered and signed today, attorney review would end on {review_end:%A, %B %d}."]),
    ]
    if estate:
        blocks.append(("p", "Estate sale: the purchase agreement is signed by the court-appointed executor/administrator "
                            "(Letters from the County Surrogate). Any NJ inheritance-tax waiver needed to release the "
                            "State's lien is delivered at closing; Buyer will work to the estate attorney's timeline."))
    if in_foreclosure(lead):
        blocks.append(("p", "The property is the subject of a foreclosure action. The mortgage and any other liens are paid "
                            "from the sale proceeds at closing. Buyer is not offering to stop, delay or negotiate the "
                            "foreclosure and is not acting as the Seller's advisor; free counseling is available from "
                            "HUD-approved counselors (1-800-569-4287) and Legal Services of New Jersey (1-888-576-5529)."))
    from engine.underwriter import frfpa_decision
    if frfpa_decision(lead.property).applies:
        blocks.append(("p", "**If this is your home (your principal residence):** New Jersey's Foreclosure Rescue Fraud "
                            "Prevention Act applies. The purchase contract will be prepared by an attorney in the form the "
                            "Act requires, you will have 10 business days after signing to cancel it, the price will be at "
                            "least 82% of the property's fair market value, and closing takes place at an attorney's or "
                            "title company's office."))
    blocks += [("p", f"This proposal expires at 5:00 p.m. on {valid:%B %d, %Y} unless accepted in writing."),
               ("sig", [buyer_entity(), "Seller (acknowledging receipt; not binding)"])]
    return Doc("loi", "Letter of Intent", blocks, price=price)


def build_psa(lead: Lead, price: float, today: date | None = None) -> Doc:
    today = today or date.today()
    cfg = _cfg()
    f = lead_facts(lead)
    legal = property_legal(lead)
    seller, signers, estate = seller_parties(lead)
    occ = lead.occupancy_status or "UNKNOWN"
    tenants = occ == "TENANT_OCCUPIED" or (f.get("units") or 1) > 1
    em = cfg.get("earnest_money", 1000)
    clauses: list[tuple[str, str]] = [
        ("Parties", f"**Seller:** {seller}. **Buyer:** {buyer_entity()}."),
        ("Property", f"{legal['address']}, known as Block {legal['block']}, Lot {legal['lot']}"
                     f"{', Qualifier ' + legal['qual'] if legal['qual'] else ''} on the tax map of {legal['municipality']}, "
                     f"{legal['county']}, New Jersey, with all buildings, fixtures and improvements (the \"Property\")."),
        ("Purchase Price", f"**{money(price)}**, payable: (a) deposit of {money(em)} within 3 business days after the "
                           "attorney-review period ends, held in escrow by the closing title company or Buyer's attorney; "
                           "(b) the balance at closing by wire transfer or certified funds, subject to adjustments."),
        ("Attorney Review", " ".join(ATTORNEY_REVIEW)),
        ("Investor Disclosure and Assignment",
         "Seller acknowledges that Buyer is a real estate investor acting as a principal, is not a licensed real estate "
         "broker or salesperson, and does not represent Seller. Buyer may assign this Agreement, in whole or in part, "
         "without Seller's consent, and may be paid by the assignee for doing so. An assignment does not change the price "
         "or terms to Seller, and Buyer remains liable until closing. Seller has had the opportunity to consult an "
         "attorney of Seller's choosing and is not relying on any statement by Buyer that is not in this Agreement."),
        ("Inspection", f"For {cfg.get('inspection_business_days', 10)} business days after attorney review ends, Buyer and its "
                       "contractors, lenders and assignees may inspect the Property on reasonable notice. Buyer may cancel "
                       "this Agreement by written notice within that period for any reason, and the deposit is returned."),
        ("Condition", "Buyer is purchasing the Property AS-IS, WHERE-IS, with no repairs by Seller. Seller will disclose in "
                      "writing any known material defects, including underground storage tanks, water intrusion, open "
                      "permits and code violations, and will not remove fixtures."),
        ("Title", "Seller will convey marketable title, insurable at regular rates by a reputable title insurer licensed in "
                  "New Jersey, by a Bargain and Sale Deed with Covenants as to Grantor's Acts, free of all mortgages, "
                  "judgments, liens, tax sale certificates and municipal charges, which are paid from the proceeds at "
                  "closing. If Seller cannot deliver such title, Buyer may accept title as it is or cancel and receive the "
                  "deposit back."),
        ("Possession", ("Seller delivers possession at closing subject only to the existing tenancies listed on a rent "
                        "roll Seller certifies; Seller assigns the leases and transfers tenant security deposits with "
                        "interest under the Rent Security Deposit Act (N.J.S.A. 46:8-19 et seq.), with notice to tenants. "
                        "Buyer acknowledges the NJ Anti-Eviction Act (N.J.S.A. 2A:18-61.1).") if tenants else
                       ("Seller delivers vacant possession, broom-clean, with all keys at closing. Personal property left "
                        "after closing is deemed abandoned to Buyer.")),
        ("Closing", f"On or before {cfg.get('closing_days', 30)} days after attorney review ends, at the offices of the title "
                    f"company or attorney Buyer designates{(' (' + cfg['title_company'] + ')') if cfg.get('title_company') else ''}"
                    ". Taxes, water/sewer and rents are adjusted as of the closing date."),
        ("Transfer Fees and Taxes", "Seller pays the New Jersey Realty Transfer Fee (N.J.S.A. 46:15-7 et seq.), including any "
                                    "additional fee on consideration over $1,000,000 as currently allocated by statute. A "
                                    "non-resident Seller's estimated NJ Gross Income Tax payment (GIT/REP forms) is "
                                    "collected at closing unless an exemption applies."),
        ("Certificates", "Any municipal certificate of occupancy/resale inspection and the smoke detector, carbon monoxide "
                         "alarm and portable fire extinguisher certificate (N.J.A.C. 5:70-2.3) required for the transfer are "
                         "obtained by Buyer at Buyer's cost where the municipality permits a buyer to assume them; otherwise "
                         "by Seller."),
        ("Flood Disclosure", "Before this Agreement becomes binding Seller will deliver the flood-risk disclosure required "
                             "of New Jersey sellers (P.L.2023, c.93): whether the Property is in a FEMA special flood hazard "
                             "or moderate-risk area and any known flood damage, insurance claims or federal flood "
                             "assistance."),
        ("Risk of Loss", "Risk of loss by fire or casualty stays with Seller until closing. If the Property is materially "
                         "damaged, Buyer may cancel and receive the deposit back or close and receive the insurance "
                         "proceeds."),
        ("Default", "If Buyer defaults after attorney review and inspection, Seller's sole remedy is to keep the deposit as "
                    "liquidated damages. If Seller defaults, Buyer may seek specific performance or the return of the "
                    "deposit and its title and inspection costs."),
        ("Occupancy Representation", {
            "VACANT": "Seller represents that the Property is vacant and is not Seller's principal residence.",
            "TENANT_OCCUPIED": "Seller represents that the Property is occupied only by tenants and is not Seller's "
                               "principal residence.",
        }.get(occ, "Seller represents that the Property is not the principal residence of Seller or any heir or family "
                   "member of Seller. (If it is, this form must not be used — see the attorney's note below.)")),
        ("Entire Agreement; Notices", "This Agreement is the entire agreement and may be changed only in a writing signed "
                                      "by both parties. Notices go to the addresses below by e-mail with confirmation, "
                                      "overnight courier or personal delivery. New Jersey law governs."),
    ]
    if f.get("year_built") is None or f["year_built"] < LEAD_PAINT_YEAR:
        clauses.insert(7, ("Lead-Based Paint", "The Property was or may have been built before 1978. Seller will deliver the "
                                               "federal Lead Warning Statement and Disclosure (42 U.S.C. 4852d; 40 CFR 745), "
                                               "any known reports, and the EPA pamphlet \"Protect Your Family From Lead in "
                                               "Your Home\". Buyer has the 10-day opportunity to inspect for lead-based "
                                               "paint, which Buyer may waive in writing."))
    if estate:
        clauses.insert(2, ("Estate", "Seller signs as the personal representative appointed by the County Surrogate and "
                                     "will deliver the Letters, any required consent of heirs, and any New Jersey "
                                     "inheritance-tax waiver (Form 0-1 / L-9) needed to release the State's lien."))
    if in_foreclosure(lead):
        clauses.insert(4, ("Foreclosure", f"The Property is the subject of a foreclosure action"
                                          f"{' (Docket ' + f['docket'] + ')' if f.get('docket') else ''}. The mortgage and "
                                          "all related charges are paid in full from the proceeds at closing. Buyer is not "
                                          "providing any service to stop, delay or settle the foreclosure, is not "
                                          "negotiating with any lender for Seller, and is not Seller's advisor."))
    blocks: list[tuple[str, Any]] = [
        ("h1", "Agreement of Sale — Residential Real Estate (New Jersey)"),
        ("note", "DRAFT for review by a New Jersey real estate attorney before use. Do not use this form for an "
                 "owner-occupied home in foreclosure (FRFPA) — that contract must be attorney-drafted."),
        ("p", f"Prepared {today:%B %d, %Y}."),
        ("clauses", clauses),
        ("sig", [*signers, buyer_entity()]),
        ("p", "Date the fully signed Agreement was delivered to both parties (day 0 of attorney review): ____________"),
    ]
    return Doc("psa", "Agreement of Sale (Draft)", blocks, price=price)


def build_assignment(lead: Lead, buyer: CashBuyer | None, assignee_price: float | None,
                     today: date | None = None) -> Doc:
    today = today or date.today()
    cfg = _cfg()
    legal = property_legal(lead)
    seller, _, _ = seller_parties(lead)
    contract_price = lead.offer_amount or default_price(lead) or 0
    total = assignee_price or end_buyer_price(lead) or contract_price
    fee = max(0.0, total - contract_price)
    assignee = (buyer_display_name(buyer) or buyer.entity_name) if buyer else "________________________"
    blocks: list[tuple[str, Any]] = [
        ("h1", "Assignment of Agreement of Sale"),
        ("note", "DRAFT for attorney review. Sign only after the seller contract's attorney-review period has ended "
                 "without disapproval."),
        ("clauses", [
            ("Parties", f"**Assignor:** {settings().operator.get('company_name')}. **Assignee:** {assignee}."),
            ("Underlying Agreement", f"Agreement of Sale dated {lead.contract_date or '__________'} between {seller} "
                                     f"(Seller) and Assignor (Buyer) for {legal['address']} (Block {legal['block']}, "
                                     f"Lot {legal['lot']}, {legal['municipality']}) at a price of {money(contract_price)}."),
            ("Assignment", "Assignor assigns all of its rights under the Underlying Agreement to Assignee, and Assignee "
                           "assumes and agrees to perform all of Buyer's obligations, including paying the purchase "
                           "price and closing on time."),
            ("Assignment Fee", f"Assignee pays Assignor an assignment fee of **{money(fee)}** at closing, shown on the "
                               "closing disclosure / settlement statement. Assignee's total cost is "
                               f"{money(contract_price + fee)} plus Assignee's closing costs."),
            ("Deposit", f"Assignee deposits {money(cfg.get('assignee_deposit', 5000))} with the closing title company within "
                        "1 business day after attorney review ends. It is credited to Assignee at closing and is "
                        "non-refundable except for Seller default or Assignor's inability to assign."),
            ("As-Is; Own Diligence", "Assignee relies on its own inspection, valuation and title review. Assignor makes no "
                                     "representation about value, after-repair value, rents or condition; any figures "
                                     "provided are estimates."),
            ("Disclosure", "Assignor is a real estate investor acting as a principal, not a licensed real estate broker, and "
                           "is assigning its contractual interest — not brokering the Property."),
            ("Attorney Review", " ".join(ATTORNEY_REVIEW[:3])),
        ]),
        ("sig", [settings().operator.get("company_name"), assignee]),
    ]
    return Doc("assignment", "Assignment of Contract", blocks, price=total)


def build_jv(lead: Lead, partner: str | None = None, split_pct: float | None = None) -> Doc:
    split = float(split_pct if split_pct is not None else _cfg().get("jv_split_pct", 50))
    legal = property_legal(lead)
    partner = partner or "________________________"
    blocks: list[tuple[str, Any]] = [
        ("h1", "Joint Venture Agreement — Single Property"),
        ("note", "DRAFT for attorney review. Both parties are principals in the contract interest; neither is paid for "
                 "referring a buyer or seller (unlicensed referral fees are prohibited, N.J.S.A. 45:15-3)."),
        ("clauses", [
            ("Parties", f"{settings().operator.get('company_name')} (\"Contract Holder\") and {partner} (\"Partner\")."),
            ("Purpose", f"To jointly market and assign, or purchase and resell, the Contract Holder's equitable interest "
                        f"under its Agreement of Sale for {legal['address']} (Block {legal['block']}, Lot {legal['lot']}), "
                        "and for no other purpose."),
            ("Contributions", "Contract Holder contributes the Agreement of Sale and seller relationship; Partner contributes "
                              "marketing to its buyers and transaction coordination. Each pays its own expenses unless "
                              "agreed in writing."),
            ("Profit Split", f"Net profit (assignment fee or resale profit less agreed shared costs) is split "
                             f"{100 - split:.0f}% to Contract Holder and {split:.0f}% to Partner, paid directly by the "
                             "closing title company on the settlement statement."),
            ("Marketing Rules", "Neither party markets the Property before the seller contract's attorney review has ended. "
                                "All marketing describes an assignment of contract by investors acting as principals, is "
                                "truthful, and never states the parties own the Property."),
            ("Non-Circumvention", "Neither party will contact the other's seller or buyer to close this Property without "
                                  "the other, for 12 months."),
            ("Term", "Ends at closing and distribution, or if the Agreement of Sale is cancelled."),
            ("No Partnership Beyond This Deal", "This is a single-purpose venture; neither party may bind the other except "
                                                "as stated here. New Jersey law governs."),
        ]),
        ("sig", [settings().operator.get("company_name"), partner]),
    ]
    return Doc("jv", "JV Agreement", blocks)


def build_deal_sheet(lead: Lead, today: date | None = None) -> Doc:
    today = today or date.today()
    f = lead_facts(lead)
    v = lead.property.current_valuation
    a = (v.assumptions_json or {}) if v else {}
    price = end_buyer_price(lead)
    arv, repairs = f.get("arv"), f.get("repairs")
    profit = (arv - price - repairs - 0.10 * arv) if arv and price and repairs is not None else None
    comps = sorted((v.comps_json or []) if v else [], key=lambda c: c.get("distance_mi") or 9)[:5]
    blocks: list[tuple[str, Any]] = [
        ("h1", f"{(lead.property.street_address or '').title()}, {f.get('town')} — Off-Market Assignment"),
        ("table", [["", ""], ["Price (assignment)", money(price)], ["ARV (est.)", money(arv)],
                   [f"Repairs (est., {(f.get('rehab_tier') or '').lower()})", money(repairs)],
                   ["Est. buyer profit after 10% costs", money(profit)],
                   *_facts_table(f)[1:]]),
        ("h2", "Comparable sales"),
        ("table", [["Address", "Sold", "Date", "Sq ft", "Dist."]] +
         [[c.get("address", "")[:42], money(c.get("sold_price")), str(c.get("sold_date") or ""),
           f"{c['sqft']:,}" if c.get("sqft") else "—", f"{c.get('distance_mi') or 0:.2f} mi"] for c in comps]),
        ("ul", (a.get("comp_notes") or [])[:3] + (a.get("rehab_reasons") and
                [f"Rehab drivers: {', '.join(a['rehab_reasons'])}"] or [])),
        ("h2", "Terms"),
        ("ul", ["Assignment of contract; cash or hard money; close at a New Jersey title company.",
                f"Non-refundable deposit {money(_cfg().get('assignee_deposit', 5000))} on signing the assignment.",
                "Buyer does its own due diligence; all figures are estimates.",
                "We are investors assigning a contract interest, not licensed real estate agents."]),
    ]
    mark = None if marketable(lead) else INTERNAL_MARK
    if mark:
        blocks.insert(1, ("note", "INTERNAL: do not send to buyers until the seller contract is signed and attorney "
                                  "review has ended with no disapproval."))
    return Doc("deal_sheet", "Deal Sheet", blocks, price=price, watermark=mark)


# -- renderers --------------------------------------------------------------------------------------
def _inline_html(text: str) -> str:
    return re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", html.escape(text))


def to_markdown(doc: Doc) -> str:
    out: list[str] = []
    if doc.watermark:
        out += [f"> **{doc.watermark}**", ""]
    for kind, val in doc.blocks:
        if kind == "h1":
            out += [f"# {val}", ""]
        elif kind == "h2":
            out += [f"## {val}", ""]
        elif kind in ("p", "note"):
            out += [(f"> {val}" if kind == "note" else val), ""]
        elif kind == "ul":
            out += [f"- {x}" for x in val] + [""]
        elif kind == "table":
            head, *rows = val
            out += ["| " + " | ".join(head) + " |", "|" + "---|" * len(head)]
            out += ["| " + " | ".join(r) + " |" for r in rows] + [""]
        elif kind == "clauses":
            out += [f"{i}. **{t}.** {body}" for i, (t, body) in enumerate(val, 1)] + [""]
        elif kind == "sig":
            for name in val:
                out += ["", "______________________________  Date: __________", name, ""]
    return "\n".join(out).rstrip() + "\n"


HTML_CSS = """
body{font-family:Georgia,'Times New Roman',serif;max-width:760px;margin:32px auto;padding:0 20px;color:#111;
background:#fff;line-height:1.45;font-size:15px}
h1{font-size:22px;margin:0 0 12px}h2{font-size:17px;margin:22px 0 8px}
table{border-collapse:collapse;width:100%;margin:10px 0}td,th{border:1px solid #bbb;padding:5px 8px;text-align:left;
vertical-align:top}th{background:#f2f2f2}.note{border-left:4px solid #b45309;background:#fff7ed;padding:8px 12px}
.mark{color:#b91c1c;font-weight:bold;letter-spacing:.08em;text-align:center;border:2px solid #b91c1c;padding:4px}
ol li{margin-bottom:8px}.sig{margin-top:34px}.sig div{border-top:1px solid #111;width:340px;padding-top:4px;margin-top:38px}
@media print{body{margin:0}.mark{position:fixed;top:0;left:0;right:0}}
"""


def to_html(doc: Doc) -> str:
    parts = [f"<!doctype html><html><head><meta charset='utf-8'><title>{html.escape(doc.title)}</title>"
             f"<style>{HTML_CSS}</style></head><body>"]
    if doc.watermark:
        parts.append(f"<div class='mark'>{html.escape(doc.watermark)}</div>")
    for kind, val in doc.blocks:
        if kind in ("h1", "h2"):
            parts.append(f"<{kind}>{html.escape(val)}</{kind}>")
        elif kind == "p":
            parts.append(f"<p>{_inline_html(val)}</p>")
        elif kind == "note":
            parts.append(f"<p class='note'>{_inline_html(val)}</p>")
        elif kind == "ul":
            parts.append("<ul>" + "".join(f"<li>{_inline_html(x)}</li>" for x in val) + "</ul>")
        elif kind == "table":
            head, *rows = val
            th = "".join(f"<th>{html.escape(h)}</th>" for h in head) if any(head) else ""
            parts.append("<table>" + (f"<tr>{th}</tr>" if th else "") +
                         "".join("<tr>" + "".join(f"<td>{_inline_html(c)}</td>" for c in r) + "</tr>" for r in rows)
                         + "</table>")
        elif kind == "clauses":
            parts.append("<ol>" + "".join(f"<li><b>{html.escape(t)}.</b> {_inline_html(b)}</li>" for t, b in val) + "</ol>")
        elif kind == "sig":
            parts.append("<div class='sig'>" + "".join(f"<div>{html.escape(n)} &nbsp;&nbsp; Date: ________</div>"
                                                        for n in val) + "</div>")
    parts.append("</body></html>")
    return "".join(parts)


def _pdf_safe(text: str) -> str:
    """Helvetica is WinAnsi-encoded; map the few characters outside it."""
    return text.replace("≥", ">=").replace("≤", "<=").replace("→", "->").replace("✓", "x")


def to_pdf(doc: Doc, path: Path) -> None:
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import LETTER
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import inch
    from reportlab.platypus import ListFlowable, ListItem, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    ss = getSampleStyleSheet()
    body = ParagraphStyle("body", parent=ss["BodyText"], fontName="Times-Roman", fontSize=10.5, leading=14)
    note = ParagraphStyle("note", parent=body, backColor=colors.HexColor("#fff7ed"), borderPadding=5,
                          borderColor=colors.HexColor("#b45309"), borderWidth=0.6, spaceBefore=4, spaceAfter=8)
    h1 = ParagraphStyle("h1", parent=ss["Heading1"], fontName="Times-Bold", fontSize=16, leading=20)
    h2 = ParagraphStyle("h2", parent=ss["Heading2"], fontName="Times-Bold", fontSize=13, leading=16)
    cell = ParagraphStyle("cell", parent=body, fontSize=9.5, leading=12)

    def para(text: str, style: ParagraphStyle = body) -> Paragraph:
        return Paragraph(_pdf_safe(_inline_html(text)), style)

    story: list[Any] = []
    for kind, val in doc.blocks:
        if kind == "h1":
            story.append(para(val, h1))
        elif kind == "h2":
            story.append(para(val, h2))
        elif kind == "p":
            story += [para(val), Spacer(1, 4)]
        elif kind == "note":
            story.append(para(val, note))
        elif kind == "ul":
            story.append(ListFlowable([ListItem(para(x), leftIndent=12) for x in val], bulletType="bullet",
                                      start="•", leftIndent=12))
        elif kind == "table":
            head, *rows = val
            data = ([[para(h, cell) for h in head]] if any(head) else []) + [[para(c, cell) for c in r] for r in rows]
            if not data:
                continue
            t = Table(data, hAlign="LEFT", colWidths=None)
            style = [("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#999999")), ("VALIGN", (0, 0), (-1, -1), "TOP")]
            if any(head):
                style.append(("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#eeeeee")))
            t.setStyle(TableStyle(style))
            story += [t, Spacer(1, 6)]
        elif kind == "clauses":
            story.append(ListFlowable([ListItem(para(f"**{t}.** {b}"), leftIndent=14) for t, b in val],
                                      bulletType="1", leftIndent=14))
        elif kind == "sig":
            for name in val:
                story += [Spacer(1, 26), para("______________________________ &nbsp;&nbsp; Date: __________"),
                          para(name)]

    def decorate(canvas, _doc) -> None:
        canvas.saveState()
        canvas.setFont("Helvetica", 8)
        canvas.setFillColor(colors.HexColor("#666666"))
        canvas.drawString(0.75 * inch, 0.5 * inch, f"{doc.title} · page {canvas.getPageNumber()}")
        if doc.watermark:
            canvas.setFont("Helvetica-Bold", 38)
            canvas.setFillColor(colors.Color(0.8, 0.1, 0.1, alpha=0.12))
            canvas.translate(4.25 * inch, 5.5 * inch)
            canvas.rotate(35)
            canvas.drawCentredString(0, 0, doc.watermark)
        canvas.restoreState()

    SimpleDocTemplate(str(path), pagesize=LETTER, title=doc.title, leftMargin=0.8 * inch, rightMargin=0.8 * inch,
                      topMargin=0.7 * inch, bottomMargin=0.8 * inch).build(story, onFirstPage=decorate,
                                                                           onLaterPages=decorate)


# -- orchestration ----------------------------------------------------------------------------------
@dataclass
class Generated:
    doc_type: str
    paths: list[Path]
    price: float | None
    warnings: list[str]


def write_doc(session: Session, lead: Lead, doc: Doc, out_root: Path | None = None) -> Generated:
    folder = (out_root or DEALS_DIR) / str(lead.property_id)
    folder.mkdir(parents=True, exist_ok=True)
    md, htm, pdf = (folder / f"{doc.doc_type}.{ext}" for ext in ("md", "html", "pdf"))
    md.write_text(to_markdown(doc), encoding="utf-8")
    htm.write_text(to_html(doc), encoding="utf-8")
    to_pdf(doc, pdf)
    session.add(GeneratedDocument(lead_id=lead.id, doc_type=doc.doc_type, path=str(pdf.relative_to(ROOT))
                                  if pdf.is_relative_to(ROOT) else str(pdf), price=doc.price))
    session.flush()
    return Generated(doc.doc_type, [md, htm, pdf], doc.price, doc.warnings)


def generate(session: Session, lead: Lead, doc_type: str, *, price: float | None = None,
             buyer: CashBuyer | None = None, partner: str | None = None, split_pct: float | None = None,
             out_root: Path | None = None, today: date | None = None) -> Generated:
    if doc_type not in DOC_TYPES:
        raise ValueError(f"unknown document type {doc_type!r}; choose from {', '.join(DOC_TYPES)}")
    explicit = price is not None
    if doc_type in ("loi", "psa"):
        price = price if explicit else default_price(lead)
    gate = contract_gate(lead, doc_type, price, explicit_price=explicit)
    if not gate.allowed:
        raise ContractBlocked(doc_type, gate.reasons)
    if doc_type == "loi":
        doc = build_loi(lead, float(price), today)
    elif doc_type == "psa":
        doc = build_psa(lead, float(price), today)
    elif doc_type == "assignment":
        doc = build_assignment(lead, buyer, price, today)
    elif doc_type == "jv":
        doc = build_jv(lead, partner, split_pct)
    else:
        doc = build_deal_sheet(lead, today)
    doc.warnings = gate.warnings
    return write_doc(session, lead, doc, out_root)


def generate_package(session: Session, lead: Lead, out_root: Path | None = None) -> tuple[list[Generated], dict[str, list[str]]]:
    """Everything the lead's stage allows: LOI + PSA draft + deal sheet now; assignment/JV once under contract."""
    done: list[Generated] = []
    blocked: dict[str, list[str]] = {}
    for doc_type in DOC_TYPES:
        try:
            done.append(generate(session, lead, doc_type, out_root=out_root))
        except ContractBlocked as exc:
            blocked[doc_type] = exc.reasons
    return done, blocked
