"""Live agent-reply negotiator: paste what the listing agent (or seller) wrote back, get the math and the next line.

The operator pastes the reply into the deal card. We read it (amounts, intent, the things that change the price:
an oil tank, tenants, a certificate of occupancy, a bidding war, a proof-of-funds request), run the seller's number
against the lead's buyer-implied pricing (engine/quant_pricing.py: buyer_max, mao_walkaway, mao_target) and answer
with a traffic light and a three-sentence reply a person would send:

  GREEN   accept and lock it up (their number is at or under our walk-away: the fee is buyer_max minus their price)
  YELLOW  counter at buyer_max minus a $12,000 fee when that is within 8% of their number, and say why with the
          repair / tax / carry numbers
  RED     hold at the walk-away and come back in 14 days
  INFO    no price to give: they asked for proof of funds, the house went under contract, the agent opted out,
          or a gate (FRFPA floor, PASS / referral verdicts) says no number yet

Every reply that states a price carries the Offer Summary block (unrepresented buyer, investor / may-assign
disclosure, postal address, opt-out) and passes engine.human_copy.lint. Nothing here promises to stop or postpone a
foreclosure, implies paying the agent, or claims we are cash buyers when we assign. record() writes one
"AGENT_REPLIED" interaction, the counter on the lead, the stage (NEGOTIATING once a number is on the table) and the
follow-up date the verdict calls for; analyze() never writes.
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, time, timedelta
from typing import Any

from sqlalchemy.orm import Session

from core.config import settings
from db.models import Interaction, Lead, LeadStage
from engine import agent_outreach as ao
from engine import human_copy as hc
from engine import quant_pricing as qp
from engine.scripts import lead_facts

REPLY_OUTCOME = ao.REPLY_OUTCOME
NO_PRICE_VERDICTS = {"PASS", "FRFPA_BLOCKED", "EQUITY_RESCUE_REFERRAL", "SHORT_SALE_ONLY"}
INTENTS = ("COUNTER", "ACCEPT", "REJECT", "AVAILABLE", "UNDER_CONTRACT", "POF_REQUEST", "HIGHEST_AND_BEST", "OIL_TANK",
           "TENANTS", "CO", "SHORT_SALE", "ESTATE", "OPT_OUT", "QUESTION")

# -- amounts ------------------------------------------------------------------------------------------
_PHONE = re.compile(r"\(?\b\d{3}\)?[-.\s]\d{3}[-.\s]\d{4}\b")
_MONEY = re.compile(r"(?<![\w.])(\$)?\s?(\d{1,3}(?:,\d{3})+|\d+(?:\.\d+)?)\s*(k\b|m\b|mm\b|thousand\b|million\b)?", re.I)
_BAND = re.compile(r"\b(?:(low|lower|mid|middle|high|upper)[\s-]*)?(\d)(\d{2})\s?'?s\b", re.I)
_UNIT_AFTER = re.compile(r"^\s*(sq\.?\s?ft|square|sqft|/\s?mo\b|a month|per month|monthly|%|percent|days?\b|business day|"
                         r"bedroom|bath|\bbr\b|\bba\b|years?\b|acres?\b|units?\b|families|family)", re.I)
_LABEL_BEFORE = re.compile(r"(tax(es)?|rent|hoa|deposit|emd|earnest|commission|fee|mortgage|balance|owed|payoff|"
                           r"lien|arrears|repairs?|rehab|estimate)\W{0,14}$", re.I)
_ASK_WORDS = re.compile(r"(wants?|needs?|is at|would take|will take|would accept|'d take|take less than|counter(ed|offer)?|"
                        r"came back|come back|looking for|looking at|won't go|will not go|not go below|lowest|bottom line|"
                        r"minimum|firm at|has to be|needs to be|no less than|at least|asking|is firm|their number|"
                        r"the number is|can do|could do|would do|would sell|will sell|sell for|let it go for)", re.I)
_NEG = re.compile(r"\b(not|no|isn't|aren't|haven't|hasn't|never|nope|without|nor)\b", re.I)

PHRASES: dict[str, list[str]] = {
    "ACCEPT": [r"accept(s|ed)? (your|the|that) (offer|number|price)", r"we have a deal", r"let'?s do it", r"deal\b.*\bworks",
               r"send (over )?(the|a|your) (contract|paperwork|agreement|psa)", r"seller (has )?agreed", r"they'?ll take (it|that)",
               r"he'?ll take (it|that)", r"she'?ll take (it|that)", r"that works for (us|them|the seller|me)", r"works for the seller",
               r"you got a deal", r"we'?re good at", r"go ahead and (send|write) (it|the offer)", r"accepted\b(?! an)"],
    "REJECT": [r"not interested", r"no thanks", r"too low", r"won'?t work", r"\bpass\b", r"insult", r"not even close",
               r"no way", r"declined", r"rejected", r"lowball", r"way off", r"not going to work", r"isn'?t going to work",
               r"nowhere near"],
    "AVAILABLE": [r"still available", r"(it'?s|it is|yes,? it'?s|yes it is|yes,? still) available", r"\bavailable\b",
                  r"still (on the market|active|listed|for sale)", r"no offers (yet|so far)", r"haven'?t (had|gotten|received) any offers",
                  r"not (yet )?under contract", r"no accepted offer", r"what'?s your (offer|number|price)", r"how much",
                  r"what number", r"send (me )?(your|the|an) (offer|numbers?|details)", r"make an offer", r"submit (an|your) offer",
                  r"open to offers", r"bring (me |us )?an offer"],
    "UNDER_CONTRACT": [r"under contract", r"in attorney review", r"attorney review (now|already|period)", r"accepted an offer",
                       r"has an accepted offer", r"\bpending\b", r"already sold", r"\bsold\b", r"no longer available",
                       r"went under contract", r"off the market", r"withdrawn", r"\bclosed\b", r"in contract", r"contract signed"],
    "POF_REQUEST": [r"proof of funds", r"\bpof\b", r"bank statement", r"verif(y|ication of) (your )?funds", r"show (me |us )?(the )?funds",
                    r"funds letter", r"pre-?qual", r"pre-?approval", r"where('s| is) the money coming from", r"source of funds"],
    "HIGHEST_AND_BEST": [r"highest and best", r"best and final", r"multiple offers", r"other offers?", r"bidding war",
                         r"several offers", r"competing offer", r"another offer", r"a few offers", r"offers? on the table",
                         r"offer deadline"],
    "OIL_TANK": [r"oil tank", r"\bust\b", r"underground (storage )?tank", r"tank sweep", r"oil heat", r"heating oil",
                 r"tank (was|is|got) (removed|decommissioned|abandoned|filled)", r"buried tank"],
    "TENANTS": [r"\btenants?\b", r"tenant[- ]occupied", r"\brenters?\b", r"\blease\b", r"month[- ]to[- ]month", r"section 8",
                r"occupied by", r"\brented\b", r"rent roll", r"\bsubsidy\b"],
    "CO": [r"certificate of occupancy", r"\bc\.?\s?of\s?o\.?\b", r"\bcco\b", r"fire cert", r"smoke cert", r"smoke detector cert",
           r"(?-i:\bCO\b)(?! ?-? ?owner)", r"co inspection", r"continued certificate"],
    "SHORT_SALE": [r"short sale", r"bank approval", r"lender approval", r"\bunderwater\b", r"owes? more than", r"third[- ]party approval"],
    "ESTATE": [r"\bestate\b", r"\bprobate\b", r"surrogate", r"executor", r"executrix", r"administrat(or|rix)", r"passed away",
               r"\bdeceased\b", r"\bheirs?\b", r"letters testamentary"],
    "OPT_OUT": [r"remove me", r"^\s*stop\b", r"please stop", r"\bno more\b", r"unsubscribe", r"do not contact", r"don'?t contact",
                r"take me off", r"don'?t e-?mail", r"stop e-?mailing", r"stop contacting", r"not interested in (any|other|more)"],
    "QUESTION": [r"\?", r"who are you", r"are you (an |a )?(agent|realtor|broker|investor|wholesaler)", r"how (do|does) (this|that) work",
                 r"what('s| is) your (company|name)", r"tell me more"],
}
_COMPILED = {k: [re.compile(p, re.I) for p in v] for k, v in PHRASES.items()}
_DEADLINE = re.compile(r"\bby\s+((?:this\s+|next\s+)?(?:mon|tues?|wed(?:nes)?|thu(?:rs)?|fri|sat(?:ur)?|sun)(?:day)?"
                       r"(?:\s+(?:at\s+)?\d{1,2}(?::\d{2})?\s*(?:am|pm|noon)?)?|tomorrow(?:\s+(?:at\s+)?\d{1,2}(?::\d{2})?\s*(?:am|pm)?)?|"
                       r"end of (?:the )?day|eod|noon|\d{1,2}(?::\d{2})?\s*(?:am|pm))\b", re.I)


@dataclass
class ParsedReply:
    amounts: list[float] = field(default_factory=list)
    counter_amount: float | None = None
    intents: set[str] = field(default_factory=set)
    deadline: str | None = None
    notes: list[str] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        return {"amounts": self.amounts, "counter_amount": self.counter_amount, "intents": sorted(self.intents),
                "deadline": self.deadline, "notes": self.notes}


def _negated(text: str, start: int) -> bool:
    return bool(_NEG.search(text[max(0, start - 14):start]))


def _amount_value(dollar: str | None, number: str, suffix: str | None, ask_before: bool = False) -> float | None:
    raw = number.replace(",", "")
    try:
        n = float(raw)
    except ValueError:
        return None
    sfx = (suffix or "").lower()
    if sfx == "k" or sfx == "thousand":
        return n * 1000
    if sfx in ("m", "mm", "million"):
        return n * 1_000_000
    if "," not in number and "." not in number and 100 <= n < 1000 and (dollar or ask_before):
        return n * 1000           # "$355" / "seller wants 310" in a text means thousands
    return n


def parse_reply(text: str) -> ParsedReply:
    """Amounts, the seller's counter and what the message is doing (intents), from plain agent-speak."""
    out = ParsedReply()
    raw = text or ""
    scrubbed = _PHONE.sub(" ", raw)
    scored: list[tuple[float, float, int]] = []          # (value, phrase-score, position)
    band_spans = [(b.start(), b.end()) for b in _BAND.finditer(scrubbed)]
    for m in _MONEY.finditer(scrubbed):
        if any(a <= m.start() < z for a, z in band_spans):
            continue                                   # "mid 300s" is read as a band below, not as 300
        dollar, number, suffix = m.group(1), m.group(2), m.group(3)
        window_before = re.split(r"[.;!?\n]", scrubbed[max(0, m.start() - 45):m.start()])[-1]   # same sentence only
        window_after = re.split(r"[.;!?\n]", scrubbed[m.end():m.end() + 25], maxsplit=1)[0]
        ask_before = bool(_ASK_WORDS.search(window_before))
        value = _amount_value(dollar, number, suffix, ask_before)
        if value is None:
            continue
        if not suffix and not dollar and "," not in number and 1900 <= value <= 2100 and "." not in number:
            continue                                   # a year
        if _UNIT_AFTER.search(window_after) or _LABEL_BEFORE.search(window_before):
            continue                                   # 1,400 sqft / 30 days / taxes are $9,800 / rent $2,100 a month
        if value < 20000 and not suffix:
            continue
        if value > 50_000_000:
            continue
        score = 1.0 if ask_before else 0.0
        if re.search(r"(your|my|our) (offer|number)", window_before, re.I):
            score -= 0.5                               # they are quoting us, not countering
        scored.append((round(value, 2), score, m.start()))
    for m in _BAND.finditer(scrubbed):
        mod, h, rest = (m.group(1) or "").lower(), m.group(2), m.group(3)
        if rest.endswith("0") and len(rest) == 2:          # "300s", "mid 300s": hundreds of thousands
            base = int(h) * 100000 + int(rest) * 1000
            value = base + {"low": 25000, "lower": 25000, "high": 75000, "upper": 75000}.get(mod, 50000)
            if rest != "00":                               # "350s": that decade
                value = base + 5000
            scored.append((float(value), 0.8, m.start()))
            out.notes.append(f"range phrase '{m.group(0).strip()}' read as {hc.money(value)}")
    seen: set[float] = set()
    for value, _, _ in sorted(scored, key=lambda t: t[2]):
        if value not in seen:
            seen.add(value)
            out.amounts.append(value)
    if scored:
        best = max(scored, key=lambda t: (t[1], t[0]))
        out.counter_amount = best[0]
    low = scrubbed.lower()
    for intent, pats in _COMPILED.items():
        for p in pats:
            m = p.search(scrubbed)
            if not m:
                continue
            if intent in ("AVAILABLE", "UNDER_CONTRACT", "ACCEPT") and _negated(scrubbed, m.start()):
                if intent == "UNDER_CONTRACT":
                    out.intents.add("AVAILABLE")           # "not under contract" / "no accepted offer"
                    out.notes.append(f"'{m.group(0)}' is negated: reading it as still available")
                continue
            out.intents.add(intent)
            out.notes.append(f"{intent}: '{m.group(0).strip()}'")
            break
    if out.counter_amount and ("ACCEPT" not in out.intents or _ASK_WORDS.search(low)):
        out.intents.add("COUNTER")
    if "UNDER_CONTRACT" in out.intents and "AVAILABLE" in out.intents and not re.search(r"not (yet )?under contract|no accepted",
                                                                                        low):
        out.intents.discard("AVAILABLE")                   # "no longer available" style replies
    if "QUESTION" in out.intents and len(out.intents) > 1 and "?" in raw and not re.search(
            r"who are you|are you (an |a )?(agent|realtor|broker|investor|wholesaler)|how (do|does) (this|that) work", low):
        out.intents.discard("QUESTION")
    if "OPT_OUT" in out.intents and re.search(r"not interested in (any|other|more)", low):
        out.intents.discard("REJECT")
    dl = _DEADLINE.search(scrubbed)
    out.deadline = dl.group(0) if dl else None
    return out


# -- the deal's numbers ----------------------------------------------------------------------------------------
def deal_numbers(lead: Lead) -> dict[str, Any] | None:
    v = lead.property.current_valuation
    if v is None or not v.arv_estimate:
        return None
    a = v.assumptions_json or {}
    q = a.get("quant") or {}
    buyer = q.get("buyer") or {}
    buyer_max = q.get("buyer_max") or a.get("buyer_purchase_price") or ((v.mao_aggressive or 0) + (v.target_assignment_fee or 0))
    return {
        "arv": v.arv_estimate, "repairs": v.est_repair_cost or 0.0, "annual_taxes": lead.property.annual_taxes,
        "buyer_max": float(buyer_max), "mao_walkaway": float(q.get("mao_walkaway") or v.mao_aggressive or 0),
        "mao_target": float(q.get("mao_target") or v.mao_conservative or 0),
        "opening_offer": float(q.get("opening_offer") or v.anchor_offer or 0), "fee_target": q.get("fee_target") or v.target_assignment_fee,
        "verdict": v.verdict, "frfpa_floor": a.get("frfpa_floor") if a.get("frfpa_applies") else None,
        "compliant_offer": a.get("compliant_offer"), "quant": bool(q),
        "rehab_total": (buyer.get("rehab") or v.est_repair_cost or 0) + (buyer.get("rehab_contingency") or 0),
        "taxes_held": buyer.get("taxes_held"), "carry": buyer.get("carry"), "capital_cost": buyer.get("capital_cost"),
        "holding_months": buyer.get("holding_months") or int(qp.costs()["end_buyer"]["holding_months"]),
        "last_offer": lead.offer_amount,
    }


def _ncfg() -> dict[str, Any]:
    return qp.costs().get("negotiation") or {}


def risk_buffer(intents: set[str]) -> tuple[float, list[str]]:
    cfg = _ncfg()
    total, why = 0.0, []
    if "OIL_TANK" in intents:
        total += float(cfg.get("oil_tank_buffer", 12000))
        why.append(f"oil tank: {hc.money(cfg.get('oil_tank_buffer', 12000))} reserve for the sweep and removal / closure")
    if "TENANTS" in intents:
        total += float(cfg.get("tenant_buffer", 9000))
        why.append(f"tenants: {hc.money(cfg.get('tenant_buffer', 9000))} holdover and turnover reserve")
    if "CO" in intents:
        total += float(cfg.get("co_buffer", 2500))
        why.append(f"certificate of occupancy: {hc.money(cfg.get('co_buffer', 2500))} for the certificate and typical repairs")
    return total, why


def _floor100(v: float) -> float:
    return float(int(v // 100) * 100)


# -- the analysis -------------------------------------------------------------------------------------------
@dataclass
class Analysis:
    verdict: str                      # GREEN / YELLOW / RED / INFO
    headline: str
    parsed: ParsedReply
    numbers: dict[str, Any]
    reply_subject: str = ""
    reply_text: str = ""
    sms_text: str = ""
    actions: list[str] = field(default_factory=list)
    followup_at: datetime | None = None
    partner: dict[str, Any] | None = None
    warnings: list[str] = field(default_factory=list)
    style: list[str] = field(default_factory=list)
    stage_to: str | None = None

    def to_json(self) -> dict[str, Any]:
        d = asdict(self)
        d["parsed"] = self.parsed.to_json()
        d["followup_at"] = self.followup_at.isoformat() if self.followup_at else None
        return d


def _followup(now: datetime | None, days: int) -> datetime:
    day = (ao._eastern(now) + timedelta(days=days)).date()
    if day.weekday() == 6:
        day += timedelta(days=1)
    return datetime.combine(day, time(10))


def _terms(price: float, f: dict[str, Any], op: dict[str, str], extra: list[str]) -> list[str]:
    ocfg = settings().get("outreach", {}) or {}
    fast, std = int(ocfg.get("fast_close_days", 21)), int(ocfg.get("standard_close_days", 45))
    vacant_hint = "" if f.get("units") and f["units"] > 1 else f" (about {fast} days if the house is vacant)"
    lines = [f"Property: {f['address']}",
             f"Price: {hc.money(price)} cash, as-is, no mortgage or appraisal contingency, close in about {std} days{vacant_hint}.",
             "No repairs, cleanout or credits requested; the seller picks the closing date.",
             "The agreement of sale includes a 3-business-day attorney review and a 10-business-day inspection period.",
             *extra, ao.agent_terms_line(), hc.investor_disclosure(op["company"]),
             *hc.footer_lines(op, 'Reply "no more" and I won\'t send you offers on other listings.')]
    return lines


def _risk_lines(intents: set[str]) -> list[str]:
    lines = []
    if "OIL_TANK" in intents:
        lines.append("Oil tank: the offer is subject to a tank sweep during the inspection period; if an underground tank "
                     "is found, the seller removes or decommissions it with NJDEP closure paperwork before closing, or the "
                     "attorneys agree an escrow holdback at closing.")
    if "TENANTS" in intents:
        lines.append("Tenants: copies of the leases, a rent roll and tenant estoppels before the end of the inspection "
                     "period; security deposits transfer at closing with the interest statement (N.J.S.A. 46:8-19 to -21.1).")
    if "CO" in intents:
        lines.append("Certificate of occupancy: the smoke / carbon monoxide / fire-extinguisher certificate (N.J.A.C. "
                     "5:70-2.3) is the seller's; any municipal CCO repairs are priced into the offer.")
    return lines


def _compose(subject: str, first: str, sentences: list[str], summary: list[str], op: dict[str, str],
             summary_title: str = hc.DIVIDER) -> tuple[str, list[str]]:
    email = hc.compose(subject, f"Hi {first},", [" ".join(sentences)], summary, op=op, summary_title=summary_title)
    problems = hc.lint(email.text, max_words=90)
    head, _ = hc.split_signature(email.text)
    if len(re.findall(r"[.!?](\s|$)", head.split("\n\n", 1)[1] if "\n\n" in head else head)) > 3:
        problems.append("more than three sentences")
    return email.text, problems


def analyze(session: Session, lead: Lead, text: str, *, now: datetime | None = None) -> Analysis:
    """Read the reply, run the math, write the answer. Pure: nothing is written (record() does that)."""
    parsed = parse_reply(text)
    f = lead_facts(lead)
    op = hc.operator()
    agent = ao.agent_contact(lead)
    first = hc.first_name(agent.name) if agent and agent.name else "there"
    st = hc.street(f["address"])
    ncfg = _ncfg()
    nums = deal_numbers(lead) or {}
    me = " ".join(x for x in (op["first"], "with " + op["company"] if op["company"] else "") if x) or "an investor"
    an = Analysis(verdict="INFO", headline="", parsed=parsed, numbers=dict(nums), reply_subject=f"re: {st}")
    intents = parsed.intents
    sms_tail = " Reply STOP to opt out."

    if "OPT_OUT" in intents:
        an.headline = "OPTED OUT"
        an.actions = ["No further messages to this agent; the number and e-mail go on the internal stop list when you save."]
        an.stage_to = None
        return an

    # risk buffers and the numbers they move
    buffer, why = risk_buffer(intents)
    if nums:
        nums["risk_buffer"], nums["risk_reasons"] = buffer, why
        nums["adjusted_walkaway"] = _floor100(nums["mao_walkaway"] - buffer)
        nums["adjusted_buyer_max"] = _floor100(nums["buyer_max"] - buffer)
        an.numbers = nums
    risk_lines = _risk_lines(intents)
    if "TENANTS" in intents:
        an.actions.append("Record the occupancy as TENANT on the lead (occupancy control) so the FRFPA track and the "
                          "walkthrough notice follow the facts.")
    if "ESTATE" in intents:
        an.actions.append("Estate sale: ask which attorney holds the Letters from the Surrogate; the executor signs.")

    # proof of funds
    partner_summary = None
    if "POF_REQUEST" in intents:
        from engine import jv_partner_vault as vault
        partner_summary = vault.deal_partner_summary(session, lead)
        an.partner = partner_summary
        p = partner_summary.get("partner")
        if p and p.get("pof_verified") and p.get("pof_share_ok"):
            an.actions.append(f"Attach the proof of funds on file for {p['name']} (Vault, partner #{p['id']}).")
        elif p and p.get("pof_verified"):
            an.actions.append(f"Ask {p['name']} to send a fresh proof of funds letter for this address (they are verified "
                              "but have not cleared sharing the document).")
        else:
            calls = ", ".join(x["name"] for x in (partner_summary.get("call_now") or [])[:3]) or "the Vault's top targets"
            an.actions.append(f"No verified capital partner covers this deal yet: call your top matched buyers now "
                              f"({calls}) and open the JV Partner Vault (#/partners) to lock one in.")

    counter = parsed.counter_amount
    frfpa_floor = nums.get("frfpa_floor")
    priced = bool(nums) and nums.get("verdict") not in NO_PRICE_VERDICTS and nums.get("mao_walkaway", 0) > 0

    # -- gates: no number we can give --------------------------------------------------------------------------
    if not nums:
        an.headline = "UNDERWRITE FIRST"
        an.actions.insert(0, "This lead has no valuation: run the underwriter before answering with a number.")
        sentences = ["Thanks for getting back to me on " + st + ".",
                     "I'm finishing my numbers on it and will have something in writing for you shortly.",
                     "Is the seller living there, or is it vacant or rented?"]
        an.reply_text, an.style = _compose(an.reply_subject, first, sentences, hc.footer_lines(op), op, hc.DETAILS)
        an.sms_text = f"Hi {first}, it's {me}. Thanks for the reply on {st}; numbers to follow shortly. Is it vacant or occupied?" + sms_tail
        return an
    if not priced or (frfpa_floor and counter and counter < frfpa_floor and nums.get("verdict") != "WORKABLE_82_PCT_FLOOR"):
        v = nums.get("verdict")
        if frfpa_floor and counter and counter < frfpa_floor:
            an.headline = f"BELOW THE FRFPA FLOOR: no offer under {hc.money(frfpa_floor)} on an owner-occupied foreclosure"
            an.actions.insert(0, "Owner-occupied foreclosure (FRFPA): the law sets the minimum at 82% of fair market value; "
                                 "confirm occupancy, then offer at the floor or refer the seller to a listing agent.")
        else:
            an.headline = {"SHORT_SALE_ONLY": "SHORT SALE ONLY: the payoff is above any investor price",
                           "EQUITY_RESCUE_REFERRAL": "REFER: a market sale nets this owner more than any investor offer",
                           "FRFPA_BLOCKED": "FRFPA: no compliant wholesale offer on this one",
                           "PASS": "PASS: the numbers do not leave room for a fee"}.get(v, f"NO PRICE ({v})")
            an.actions.insert(0, f"Valuation verdict {v}: do not quote a number; see the lead page for the track.")
        sentences = [f"Thanks for coming back to me on {st}.",
                     "I can't put a number on this one the way it sits, so I'd rather not waste the seller's time with a lowball.",
                     "If the situation changes, or if the seller would consider a short sale with the lender, I'm glad to take another look."]
        if "SHORT_SALE" in intents or v == "SHORT_SALE_ONLY":
            sentences[2] = ("If the lender is open to a short sale I'll put a cash offer in writing for you to submit; the "
                            "negotiation with the bank stays with you and the seller's attorney.")
        an.reply_text, an.style = _compose(an.reply_subject, first, sentences, hc.footer_lines(op), op, hc.DETAILS)
        an.sms_text = f"Hi {first}, it's {me}. Thanks for the reply on {st}; I can't make a number work on this one as it sits." + sms_tail
        an.followup_at = _followup(now, int(ncfg.get("drip_days", 14)))
        return an

    adj_walk, adj_max = nums["adjusted_walkaway"], nums["adjusted_buyer_max"]
    last_offer = nums.get("last_offer") or nums.get("opening_offer")
    rehab, taxes, carry = nums.get("rehab_total") or nums["repairs"], nums.get("taxes_held") or 0, (nums.get("carry") or 0) + (nums.get("capital_cost") or 0)
    months = nums.get("holding_months", 6)
    cost_line = (f"about {hc.money(rehab)} of work, {hc.money(taxes)} in taxes and {hc.money(carry)} to carry and finance it "
                 f"for {months} months" if taxes else f"about {hc.money(rehab)} of work plus carrying costs for {months} months")

    def priced_reply(price: float, sentences: list[str], extra: list[str]) -> None:
        an.reply_text, an.style = _compose(an.reply_subject, first, sentences, _terms(price, f, op, extra + risk_lines), op)

    # -- a number from them -------------------------------------------------------------------------------------
    if counter:
        c = float(counter)
        if frfpa_floor and c < frfpa_floor:
            c = float(frfpa_floor)
            an.warnings.append(f"their {hc.money(counter)} is below the FRFPA floor; the law makes {hc.money(frfpa_floor)} the minimum")
        an.stage_to = LeadStage.NEGOTIATING
        fee_at_counter = _floor100(adj_max - c)
        nums.update({"counter": c, "fee_at_counter": fee_at_counter})
        econ = qp.counter_economics(c, arv=nums["arv"], repairs=nums["repairs"], annual_taxes=nums.get("annual_taxes"),
                                    buyer_max=adj_max)
        nums.update({k: econ[k] for k in ("buyer_profit_at_max", "buyer_roi_at_max", "buyer_profit_if_paid_counter",
                                          "buyer_roi_if_paid_counter")})
        if c <= adj_walk:
            an.verdict, an.headline = "GREEN", f"ACCEPT & LOCK UP NOW: {hc.money(c)} leaves a {hc.money(fee_at_counter)} fee"
            an.actions = [f"Send the written offer at {hc.money(c)} today (Interested button: the LOI runs through the gates).",
                          "Ask the agent for the seller's contract and attach the attorney-review assignment rider.",
                          "Line up the matched buyer or JV partner for the walkthrough once attorney review ends."] + an.actions
            sentences = [f"{hc.money(c)} works.",
                         "I'll send the written offer at that number today, on your contract with a short assignment rider, "
                         "so the seller's attorney can start review.",
                         "We'll need one walkthrough during the inspection period and that's it."]
            priced_reply(c, sentences, [])
            an.sms_text = f"Hi {first}, it's {me}. {hc.money(c)} works on {st}; written offer coming today." + sms_tail
            an.followup_at = ao._eastern(now)
        else:
            x = _floor100(adj_max - float(ncfg.get("counter_fee_floor", 12000)))
            if x > 0 and x >= float(ncfg.get("counter_within_pct", 0.92)) * c:
                an.verdict, an.headline = "YELLOW", f"COUNTER AT {hc.money(x)} (keeps a {hc.money(_floor100(adj_max - x))} fee)"
                nums.update({"our_counter": x, "fee_at_our_counter": _floor100(adj_max - x)})
                sentences = [f"I ran it again with the real costs, {cost_line}, and that's what keeps me from {hc.money(c)}.",
                             f"I can do {hc.money(x)} cash, as-is, with a quick close.",
                             "If that works for the seller I'll send the written offer today."]
                priced_reply(x, sentences, [])
                an.sms_text = f"Hi {first}, it's {me}. Closest I can get on {st} is {hc.money(x)} cash, as-is; details by e-mail." + sms_tail
                an.actions = [f"Counter at {hc.money(x)}; if they come back under {hc.money(adj_walk)} it's a GREEN."] + an.actions
                an.followup_at = _followup(now, int(ncfg.get("yellow_followup_days", 2)))
            else:
                an.verdict, an.headline = "RED", f"HOLD FIRM AT {hc.money(adj_walk)} / SET {int(ncfg.get('drip_days', 14))}-DAY DRIP"
                nums.update({"our_counter": adj_walk, "fee_at_our_counter": _floor100(adj_max - adj_walk)})
                sentences = [f"Thanks for the number; {hc.money(c)} doesn't work on this one with {cost_line}.",
                             f"My ceiling is {hc.money(adj_walk)} cash, as-is, and I'll leave that on the table for the seller.",
                             "If anything changes in the next couple of weeks I'm one e-mail away."]
                priced_reply(adj_walk, sentences, [])
                an.sms_text = f"Hi {first}, it's {me}. {hc.money(c)} is past me on {st}; my ceiling is {hc.money(adj_walk)} cash, as-is." + sms_tail
                an.actions = [f"Hold at {hc.money(adj_walk)}; the drip follow-up is scheduled."] + an.actions
                an.followup_at = _followup(now, int(ncfg.get("drip_days", 14)))
        if "HIGHEST_AND_BEST" in intents:
            an.warnings.append("Bidding war: never above the walk-away, whatever the other offers are.")
        return an

    # -- no number from them ------------------------------------------------------------------------------------
    if "HIGHEST_AND_BEST" in intents:
        price = adj_walk if not (last_offer and last_offer >= adj_walk) else float(last_offer)
        an.verdict, an.headline = "YELLOW", f"BEST AND FINAL AT {hc.money(price)}"
        an.stage_to = LeadStage.NEGOTIATING
        nums.update({"our_counter": price, "fee_at_our_counter": _floor100(adj_max - price)})
        sentences = [f"My best and final is {hc.money(price)} cash, as-is, no mortgage or appraisal contingency, closing in "
                     f"about {int((settings().get('outreach', {}) or {}).get('fast_close_days', 21))} days.",
                     "You may have bigger numbers; mine comes with no financing risk and no repair requests.",
                     "Either way, thanks for running it by the seller."]
        priced_reply(price, sentences, [])
        an.sms_text = f"Hi {first}, it's {me}. Best and final on {st}: {hc.money(price)} cash, as-is, quick close." + sms_tail
        an.actions = [f"Best and final sent at {hc.money(price)}; nothing above the walk-away."] + an.actions
        an.followup_at = _followup(now, int(ncfg.get("yellow_followup_days", 2)))
        return an
    if "ACCEPT" in intents:
        price = float(last_offer or nums["opening_offer"])
        an.verdict, an.headline = "GREEN", f"THEY ACCEPTED {hc.money(price)}: LOCK UP NOW"
        an.stage_to = LeadStage.NEGOTIATING
        nums.update({"counter": price, "fee_at_counter": _floor100(adj_max - price)})
        an.actions = [f"Send the written offer at {hc.money(price)} today and ask for the seller's contract with the "
                      "assignment rider.", "Open the closing file once the contract is signed (attorney review starts)."] + an.actions
        sentences = ["Great, let's get it signed.",
                     f"I'll send the written offer at {hc.money(price)} today on your contract with a short assignment rider so "
                     "attorney review can start.",
                     "Which attorney is the seller using, so ours can coordinate?"]
        priced_reply(price, sentences, [])
        an.sms_text = f"Hi {first}, it's {me}. Great news on {st}; the written offer at {hc.money(price)} comes today." + sms_tail
        an.followup_at = ao._eastern(now)
        return an
    if "UNDER_CONTRACT" in intents:
        an.headline = "OFF THE TABLE FOR NOW: under contract"
        an.actions = [f"Check back in {int(ncfg.get('under_contract_days', 45))} days; deals fall out of attorney review and inspection."] + an.actions
        sentences = ["Understood, and congratulations on getting it under contract.",
                     "If it falls out in review or inspection I'd appreciate the first call; my number is still good.",
                     "Thanks for letting me know."]
        an.reply_text, an.style = _compose(an.reply_subject, first, sentences, hc.footer_lines(op), op, hc.DETAILS)
        an.sms_text = f"Hi {first}, it's {me}. Understood on {st}; if it falls out of contract I'd love the first call." + sms_tail
        an.followup_at = _followup(now, int(ncfg.get("under_contract_days", 45)))
        return an
    if "POF_REQUEST" in intents and "AVAILABLE" not in intents:
        an.headline = "SEND PROOF OF FUNDS"
        p = (partner_summary or {}).get("partner")
        have = bool(p and p.get("pof_verified"))
        price = float(last_offer) if last_offer else None
        sentences = ["Happy to.",
                     ("Proof of funds from my capital partner is attached." if have and p.get("pof_share_ok") else
                      "I'm getting the proof of funds letter together now and will send it as soon as it's in hand."),
                     (f"The offer stands at {hc.money(price)} cash, as-is, with a quick close." if price else
                      "The written offer can go over with it the same day.")]
        if price:
            priced_reply(price, sentences, [])
        else:
            an.reply_text, an.style = _compose(an.reply_subject, first, sentences, hc.footer_lines(op), op, hc.DETAILS)
        an.sms_text = f"Hi {first}, it's {me}. Proof of funds for {st} is on its way by e-mail." + sms_tail
        an.followup_at = _followup(now, 1)
        return an
    if "AVAILABLE" in intents:
        an.verdict, an.headline = "GREEN", "SEND THE OFFER NOW (Touch 2)"
        an.stage_to = LeadStage.CONTACTED
        packet = ao.build_packet(lead, agent=agent, generate_loi=False, session=session) if agent else None
        if packet is not None:
            an.reply_subject, an.reply_text, an.sms_text = packet.subject, packet.email_body, packet.sms_text
            an.style = hc.lint(packet.email_body)
            an.actions = [("They answered: the priced note is ready on the deal card; send it today." if packet.kind == "offer"
                           else "They answered: ask who lives there first (the occupancy decides the FRFPA rules); the "
                                "no-price inquiry is ready on the deal card.")] + an.actions
            if packet.warnings:
                an.warnings.extend(packet.warnings)
        an.followup_at = ao._eastern(now)
        return an
    if "REJECT" in intents:
        an.verdict, an.headline = "RED", f"HOLD AT {hc.money(adj_walk)} / SET {int(ncfg.get('drip_days', 14))}-DAY DRIP"
        nums.update({"our_counter": adj_walk, "fee_at_our_counter": _floor100(adj_max - adj_walk)})
        sentences = ["Understood, and thanks for the quick answer.",
                     f"For what it's worth my ceiling is {hc.money(adj_walk)} cash, as-is, with no financing contingency.",
                     "If the seller's thinking changes I'm glad to put it in writing."]
        priced_reply(adj_walk, sentences, [])
        an.sms_text = f"Hi {first}, it's {me}. Understood on {st}; my ceiling stays {hc.money(adj_walk)} cash, as-is, if anything changes." + sms_tail
        an.actions = ["Hold; the drip follow-up is scheduled."] + an.actions
        an.followup_at = _followup(now, int(ncfg.get("drip_days", 14)))
        return an
    # a question, or nothing we could classify
    an.headline = "ANSWER THEM, THEN ASK FOR A NUMBER"
    sentences = ["I'm an investor buying for myself or a partner investor, and I'm not working with a buyer's agent.",
                 f"Happy to answer anything about how I work; the written offer on {st} can go over today if the seller wants "
                 "to see a number.",
                 "What would help most?"]
    an.reply_text, an.style = _compose(an.reply_subject, first, sentences, hc.footer_lines(op), op, hc.DETAILS)
    an.sms_text = f"Hi {first}, it's {me}. Happy to answer anything on {st}; want me to send the written offer today?" + sms_tail
    an.actions = ["Answer their question; when they say it's available, Touch 2 (the offer) is ready on the card."] + an.actions
    an.followup_at = _followup(now, int(ncfg.get("yellow_followup_days", 2)))
    return an


# -- persistence --------------------------------------------------------------------------------------------
def record(session: Session, lead: Lead, analysis: Analysis, raw_text: str, *, now: datetime | None = None) -> Interaction:
    """One AGENT_REPLIED interaction, the counter on the lead, the stage and the follow-up the verdict calls for."""
    from engine import crm
    agent = ao.agent_contact(lead)
    when = ao._eastern(now)
    parsed = analysis.parsed
    inter = Interaction(lead_id=lead.id, contact_id=agent.id if agent else None, channel="email_in", outcome=REPLY_OUTCOME,
                        notes=f"{analysis.headline}: {(raw_text or '').strip()[:400]}", next_followup_at=analysis.followup_at,
                        debrief_json={"verdict": analysis.verdict, "headline": analysis.headline,
                                      "counter": parsed.counter_amount, "intents": sorted(parsed.intents),
                                      "numbers": {k: v for k, v in analysis.numbers.items()
                                                  if isinstance(v, (int, float, str, type(None)))}},
                        occurred_at=when)
    session.add(inter)
    if parsed.counter_amount:
        lead.last_counter_amount = parsed.counter_amount
    if analysis.followup_at is not None:
        lead.next_followup_at = analysis.followup_at
    if analysis.stage_to:
        lead.status_stage = crm.advance_stage(lead.status_stage, analysis.stage_to)
    if "OPT_OUT" in parsed.intents and agent is not None:
        ao.opt_out(session, agent, notes=f"agent asked to stop (reply on lead #{lead.id})")
    if "UNDER_CONTRACT" in parsed.intents:
        lead.notes = "\n".join(x for x in (lead.notes, f"{when:%Y-%m-%d}: agent says under contract; check back") if x)
    session.flush()
    return inter


def analyze_and_record(session: Session, lead: Lead, text: str, *, now: datetime | None = None
                       ) -> tuple[Analysis, Interaction]:
    analysis = analyze(session, lead, text, now=now)
    return analysis, record(session, lead, analysis, text, now=now)
