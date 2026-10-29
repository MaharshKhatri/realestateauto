"""Deal structure: getting an assignable contract in New Jersey, and choosing assignment vs. a double close.

The NJ REALTORS Standard Form of Real Estate Sales Contract (Form 118-Statewide) carries a "No Assignment" section
(Section 37 in the 4/17 through 07/2025.2 editions; it was Item 14 on the older NJAR form, which is what many
operators still call it). Contracts for the sale of real estate are assignable by statute (N.J.S.A. 2A:25-1) and NJ
courts read a bare anti-assignment covenant as limiting the right, not the power, to assign unless it says an
assignment is void (Garden State Buildings v. First Fidelity Bank, 305 N.J. Super. 510 (App. Div. 1997); Owen v. CNA
Ins., 167 N.J. 450 (2001)), but nobody should close on that argument. The clean path is a rider the seller's attorney
accepts during the three-business-day attorney review (N.J.A.C. 11:5-6.2(g); Conley v. Guerrero, 228 N.J. 339 (2017)):
`build_rider` writes that request.

The rider also asks for what New Jersey law now requires and what protects everyone: the Seller's Property Condition
Disclosure Statement under the Real Estate Consumer Protection Enhancement Act (P.L.2024, c.32, N.J.S.A. 45:15-16.87(e),
effective August 1, 2024: delivered before the buyer becomes obligated) with the flood-risk addendum (N.J.S.A.
56:8-19.2, P.L.2023, c.93); a 10-business-day inspection period with an underground oil tank sweep; access for the
buyer's inspectors, contractors, lenders, partners and assignees; and a plain disclosure to the seller that the buyer
is an investor who may resell or assign for profit and may take title in an entity it controls (Consumer Fraud Act:
no misrepresentation, ever).

`correa_acknowledgment` is the clause for the Assignment Agreement: the seller's duty to disclose known latent
material defects survives an "as-is" clause (Weintraub v. Krobatsch, 64 N.J. 445 (1974); Correa v. Maggiore, 196 N.J.
Super. 273 (App. Div. 1984)), and an assignor who knows of a defect is in no better position, so everything we know
goes to the assignee in writing.

`compare_structures` is the net check: an assignment records no deed from us (the seller pays the single Realty
Transfer Fee), while a same-day double close is two deeds, two sets of closing costs and the RTF (plus the graduated
fee over $1M, P.L.2025, c.69) on the full resale price with us as grantor, funded by a transactional lender; the
calculator says what each nets and when the double close is still the better call (the seller refuses the rider, or the
spread is large enough that a fee on the settlement statement would blow up the deal). Taking title in an entity
brings the bulk-sale notice (Form C-9600, N.J.S.A. 54:50-38: the purchaser files at least 10 business days before
closing when the seller is a business entity) and GIT/REP-3 (entity sellers owe no estimated tax at closing).
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import date
from typing import Any

from core.config import settings
from core.dates import nj_business_days_after
from db.models import CashBuyer, Lead
from engine import quant_pricing as qp
from engine.contracts import (
    ATTORNEY_REVIEW,
    Doc,
    buyer_entity,
    default_price,
    in_foreclosure,
    property_legal,
    seller_parties,
)
from engine.scripts import lead_facts, money

RIDER_TITLE = "Attorney Review Modification Request: Assignment, Disclosure and Inspection Rider"
ASSIGNMENT_SECTION = ("the \"No Assignment\" section of the NJ REALTORS Standard Form of Real Estate Sales Contract "
                      "(Section 37 in the current Form 118-Statewide; Item 14 on the older NJAR form) or the corresponding "
                      "paragraph of the contract the parties actually signed")
CPEA = ("the Seller's Property Condition Disclosure Statement required by the Real Estate Consumer Protection Enhancement "
        "Act (P.L.2024, c.32; N.J.S.A. 45:15-16.87(e), effective August 1, 2024), with the flood-risk disclosures required "
        "by N.J.S.A. 56:8-19.2")
CORREA_TITLE = "Acknowledgment of Disclosures and Independent Inspection"
VERIFY_LLC_FEES = ("New Jersey LLC formation fee ($125 public-records filing) and the $75 annual report: confirm the current "
                   "amounts on the Division of Revenue and Enterprise Services site before quoting them.")


def _dc() -> dict[str, Any]:
    return qp.costs().get("double_close") or {}


# -- the rider ------------------------------------------------------------------------------------------
def build_rider(lead: Lead, *, buyer: CashBuyer | None = None, today: date | None = None, price: float | None = None) -> Doc:
    """The attorney-review modification request that makes the seller's contract assignable, disclosed and inspectable."""
    today = today or date.today()
    cfg = settings().get("contracts", {}) or {}
    legal = property_legal(lead)
    seller, _, estate = seller_parties(lead)
    f = lead_facts(lead)
    price = price if price is not None else (lead.offer_amount or default_price(lead))
    inspection_days = int(cfg.get("inspection_business_days", 10))
    review_end = nj_business_days_after(today, 3)
    buyer_name = f"{buyer_entity()} and/or assigns or an affiliated investment entity"
    tenant = f.get("units") and f["units"] > 1
    clauses: list[tuple[str, str]] = [
        ("Purpose", f"Buyer's attorney submits this rider during the attorney-review period as a modification request. It "
                    f"becomes part of the Agreement of Sale only when both attorneys accept it in writing; until then the "
                    f"Agreement is under review and neither party is bound. Property: {legal['address']} (Block "
                    f"{legal['block']}, Lot {legal['lot']}{', Qual. ' + legal['qual'] if legal['qual'] else ''}, "
                    f"{legal['municipality']}, {legal['county']}). Seller: {seller}. Buyer: {buyer_name}."
                    + (f" Purchase price: {money(price)}." if price else "")),
        ("Assignment (modifies the No Assignment section)",
         f"{ASSIGNMENT_SECTION[0].upper() + ASSIGNMENT_SECTION[1:]} is deleted and replaced with: Buyer may assign this "
         "Agreement, in whole, to a single-purpose limited liability company or to an affiliated investment partner on "
         "written notice to Seller's attorney before closing. Seller's consent is not required for an assignment to an "
         "entity Buyer controls and is not to be unreasonably withheld, conditioned or delayed for any other assignee. "
         "Buyer remains liable for its obligations under this Agreement until closing unless the assignee assumes them in "
         "writing and the deposit is replaced, at which point Buyer is released. The purchase price and every other term "
         "owed to Seller are unchanged by any assignment, and no assignment fee is charged to Seller."),
        ("Seller's Property Condition Disclosure",
         f"Before the end of attorney review Seller delivers {CPEA}, completed and signed, together with any inspection, "
         "tank-sweep, environmental or engineering report in Seller's possession. Seller discloses every material defect "
         "known to Seller, including latent conditions not visible on a walkthrough; this obligation is independent of the "
         "as-is sale."),
        ("Inspection Contingency",
         f"Buyer has {inspection_days} business days after the end of attorney review (the \"Inspection Period\") to "
         "complete, at Buyer's expense, any inspections Buyer chooses: structural, mechanical, radon, wood-destroying "
         "insects, a sewer-line camera scope, a municipal certificate-of-occupancy / fire-certificate check and an "
         "underground oil tank sweep. If a sweep or any evidence indicates an underground storage tank, Seller, at "
         "Seller's option, removes or decommissions the tank before closing with NJDEP-compliant closure documentation "
         "and any required municipal permits, or the attorneys agree in writing to an escrow holdback at closing sized to "
         "the written removal / remediation estimate. Buyer may cancel this Agreement by written notice within the "
         "Inspection Period for any inspection result, in which case the deposit is returned in full."),
        ("Access", "During the Inspection Period and on reasonable notice before closing, Buyer and Buyer's inspectors, "
                   "contractors, appraisers, lenders, partners and prospective assignees may enter the Property on 24 "
                   "hours' notice during daylight hours" + (", subject to the tenants' rights under their leases and "
                                                            "N.J.S.A. 46:8 et seq." if tenant else "") + "."),
        ("Attorney Review", " ".join(ATTORNEY_REVIEW[:3]) + f" If the signed Agreement were delivered today, the review "
                                                               f"period would end on {review_end:%A, %B %d, %Y}. Nothing "
                                                               "about this Property is marketed to anyone until the review "
                                                               "period has ended without disapproval."),
        ("Disclosure to Seller", "Buyer is a real estate investor acting as a principal, not a licensed real estate broker "
                                 "or salesperson, and does not represent Seller. Buyer intends to resell the Property or "
                                 "assign this Agreement for a profit, and may take title in a limited liability company or "
                                 "other entity it controls. Seller is encouraged to review this rider with Seller's attorney; "
                                 "there is no obligation to accept it."),
    ]
    if tenant:
        clauses.insert(4, ("Leases and Deposits", "Within 5 business days after attorney review Seller delivers copies of "
                                                   "all leases, a current rent roll and tenant estoppel letters; security "
                                                   "deposits transfer to Buyer at closing with the interest statements "
                                                   "required by N.J.S.A. 46:8-19 to -21.1."))
    if in_foreclosure(lead):
        clauses.append(("Foreclosure", "The Property is the subject of a foreclosure action. Liens are paid from the sale "
                                       "proceeds at closing. Buyer does not offer to stop, delay or negotiate the foreclosure "
                                       "and is not Seller's advisor; free counseling is available from HUD-approved "
                                       "counselors (1-800-569-4287) and Legal Services of New Jersey (1-888-576-5529)."))
    if estate:
        clauses.append(("Estate", "Seller signs as the court-appointed executor or administrator; any New Jersey inheritance "
                                  "tax waiver needed to release the State's lien is delivered at closing."))
    blocks: list[tuple[str, Any]] = [
        ("h1", RIDER_TITLE),
        ("note", "DRAFT for the Buyer's attorney to submit during attorney review (N.J.A.C. 11:5-6.2(g)). Not legal advice; "
                 "the attorneys control the final wording."),
        ("p", f"Date: {today:%B %d, %Y}"),
        ("clauses", clauses),
        ("sig", [buyer_name, f"{seller} (acceptance by Seller's attorney during attorney review)"]),
    ]
    doc = Doc("rider", RIDER_TITLE, blocks, price=price)
    doc.warnings = ["Section numbers follow the current NJ REALTORS Form 118-Statewide; the parties' own contract controls "
                    "(older NJAR forms call the assignment clause Item 14). VERIFY against the live form before sending."]
    return doc


def correa_acknowledgment(lead: Lead) -> tuple[str, str]:
    """Clause for the Assignment Agreement: every known defect and every seller disclosure passes to the assignee."""
    legal = property_legal(lead)
    text = (f"Assignor delivers to Assignee, before Assignee signs, copies of every disclosure, inspection report, tank "
            f"sweep, title commitment and municipal notice Assignor has received for {legal['address']}, including {CPEA}. "
            "Assignee acknowledges receipt, has inspected the Property or knowingly waives inspection, and takes the "
            "Property from Seller as-is with no representation by Assignor beyond the facts disclosed in writing. Assignor "
            "affirms that it has disclosed every material defect actually known to it; the duty to disclose known latent "
            "material defects (Weintraub v. Krobatsch, 64 N.J. 445 (1974); Correa v. Maggiore, 196 N.J. Super. 273 "
            "(App. Div. 1984)) is not avoided by an as-is clause or by the fact that Assignor never held title.")
    return CORREA_TITLE, text


# -- the entity option ----------------------------------------------------------------------------------
def entity_option(lead: Lead | None = None, *, state_filing_fee: float = 125.0, annual_report_fee: float = 75.0
                  ) -> dict[str, Any]:
    """The disclosed single-purpose LLC route, with its conditions. Never a way to hide the fee from the seller."""
    name = lead and settings().operator.get("company_name")
    return {
        "title": "Single-purpose LLC (disclosed) instead of an assignment",
        "how": [
            "Form a New Jersey LLC for this deal; the LLC signs the Agreement of Sale as Buyer, and the rider tells the "
            "seller in writing that the Buyer is an investor who may take title in an entity it controls and may resell "
            "or assign for profit.",
            "At disposition either (a) the LLC assigns the contract to the end buyer, or (b) the end buyer purchases 100% "
            "of the LLC's membership interests and the LLC closes on the house in its own name.",
        ],
        "pros": [
            "A sale of membership interests records no deed: no Realty Transfer Fee or graduated fee on that transfer "
            "(N.J.S.A. 46:15-7 taxes the grantor of a recorded deed), and the Controlling Interest Transfer Tax does not "
            "apply to a Class 2 house (N.J.S.A. 54:15C-1 reaches Class 4A commercial property only).",
            "The seller's deed runs to the LLC named in the contract, so the No Assignment section is never in play.",
        ],
        "conditions": [
            "The end buyer's title insurer and lender must accept an interest purchase; most retail lenders will not, so "
            "this fits cash buyers and the buyer inherits the LLC's liabilities (no title-insurance reset on an interest sale).",
            "Bulk-sale notice: when the interests are sold by a business entity the purchaser files Form C-9600 at least 10 "
            "business days before closing (N.J.S.A. 54:50-38); an individual seller of a 1-2 family house is exempt.",
            "An LLC seller of the house itself files GIT/REP-3 (no estimated tax at closing); the LLC still needs an EIN, "
            "a bank account for the deposit and a registered agent.",
            "A New Jersey attorney structures the interest-purchase agreement and the operating agreement; this module only "
            "prices it.",
            "Consumer Fraud Act: the seller sees the same disclosure either way. The entity route is never used to keep "
            "the fee or the resale from the seller.",
        ],
        "costs": {"state_filing_fee": state_filing_fee, "annual_report_fee": annual_report_fee,
                  "registered_agent": "operator (free) or a service", "note": VERIFY_LLC_FEES},
        "when": ("Use it when the seller's attorney refuses the assignment modification, when the spread is above the "
                 f"double-close threshold ({money(_dc().get('prefer_double_close_over_spread', 30000))}) and a fee on the "
                 "settlement statement would stall the deal, or when the end buyer prefers to buy the entity."),
        "verify": [VERIFY_LLC_FEES],
        "operator_entity": name,
    }


# -- assignment vs double close --------------------------------------------------------------------------
@dataclass
class StructureComparison:
    contract_price: float
    buyer_price: float
    spread: float
    assignment: dict[str, Any]
    double_close: dict[str, Any]
    recommended: str
    reasons: list[str] = field(default_factory=list)
    summary: str = ""

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


def compare_structures(*, contract_price: float, buyer_price: float, arv: float | None = None,
                       seller_refuses_assignment: bool = False, in_llc: bool = False,
                       cfg: dict[str, Any] | None = None) -> StructureComparison:
    """Net check under both structures, rounded to dollars, with the recommendation and why."""
    dc = cfg if cfg is not None else _dc()
    spread = round(buyer_price - contract_price)
    recording = float(dc.get("assignment_recording_fee", 150))
    assignment = {
        "fee": spread, "costs": round(recording), "net": round(spread - recording),
        "notes": ["No deed from us: the assignment itself is not a Realty Transfer Fee event; the seller pays the RTF on the "
                  "single deed to the assignee.",
                  "The fee appears on the settlement statement; the assignee's lender and title company must accept an "
                  "assigned contract (cash buyers always do).",
                  "If the original seller is a business entity, the end buyer's attorney files the C-9600 bulk-sale notice; "
                  "an assignee steps into a notice already filed on notice to the Division."],
    }
    tf = max(float(dc.get("transactional_funding_pct", 0.01)) * contract_price, float(dc.get("transactional_min_fee", 2500))) \
        + float(dc.get("transactional_flat_fees", 750))
    leg_a, leg_b = float(dc.get("our_closing_costs_leg_a", 1800)), float(dc.get("our_closing_costs_leg_b", 900))
    rtf_b = qp.realty_transfer_fee(buyer_price) + qp.graduated_percent_fee(buyer_price)
    dc_costs = tf + leg_a + leg_b + rtf_b
    double = {
        "spread": spread, "transactional_funding": round(tf), "closing_costs_leg_a": round(leg_a),
        "closing_costs_leg_b": round(leg_b), "rtf_leg_b": round(rtf_b, 2), "costs": round(dc_costs),
        "net": round(spread - dc_costs), "out_of_pocket": 0.0,
        "condition": ("Zero out of pocket only when the transactional lender funds 100% of the A-to-B purchase against the "
                      "end buyer's funds already in escrow for the B-to-C closing the same day; our deposit on the seller "
                      "contract is still posted up front (JV partner's EMD where the vault has one)."),
        "notes": ["Two deeds: the seller is grantor on A-to-B (seller's RTF); we are grantor on B-to-C and pay the RTF"
                  + (" and the graduated fee over $1M" if buyer_price > 1_000_000 else "") + " on the full resale price.",
                  "The end buyer never sees the A-to-B price on their own settlement statement."],
    }
    if in_llc:
        double["notes"].append("Taking title in an LLC: the end buyer's attorney files Form C-9600 at least 10 business days "
                               "before the B-to-C closing (N.J.S.A. 54:50-38) and we file GIT/REP-3 as an entity seller.")
    reasons: list[str] = []
    threshold = float(dc.get("prefer_double_close_over_spread", 30000))
    if spread <= 0:
        recommended = "assignment"
        reasons.append("no spread between the contract price and the buyer's price: nothing to structure")
    elif seller_refuses_assignment:
        recommended = "double_close"
        reasons.append("the seller refused the assignment modification: only a double close delivers the house")
    elif spread > threshold:
        recommended = "double_close"
        reasons.append(f"spread {money(spread)} is above {money(threshold)}: a fee that size on the settlement statement "
                       "invites a renegotiation; the double close keeps the two prices apart")
    else:
        recommended = "assignment"
        reasons.append(f"spread {money(spread)} is within {money(threshold)}: the assignment nets {money(spread - recording)} "
                       f"against {money(spread - dc_costs)} after double-close costs")
    if double["net"] <= 0 and recommended == "double_close":
        reasons.append("warning: the double close costs more than the spread; renegotiate the price or the funding before "
                       "committing")
    summary = (f"Assignment: fee {money(spread)}, costs {money(recording)}, net {money(assignment['net'])}. Double close: "
               f"{money(tf)} transactional funding, {money(rtf_b)} transfer fee on the resale deed, {money(leg_a + leg_b)} "
               f"closing costs, net {money(double['net'])}. Recommended: {recommended.replace('_', ' ')} ({reasons[0]}).")
    return StructureComparison(contract_price=round(contract_price), buyer_price=round(buyer_price), spread=spread,
                               assignment=assignment, double_close=double, recommended=recommended, reasons=reasons,
                               summary=summary)


def structure_for_lead(lead: Lead, *, buyer_price: float | None = None, seller_refuses_assignment: bool = False) -> StructureComparison:
    """The comparison from the lead's own numbers (contract price if signed, else the target; buyer max as the resale)."""
    from engine.contracts import end_buyer_price
    v = lead.property.current_valuation
    contract = lead.offer_amount or default_price(lead) or 0.0
    buyer = buyer_price or end_buyer_price(lead) or 0.0
    return compare_structures(contract_price=float(contract), buyer_price=float(buyer), arv=v.arv_estimate if v else None,
                              seller_refuses_assignment=seller_refuses_assignment)
