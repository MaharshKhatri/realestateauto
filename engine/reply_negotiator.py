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

from sqlalchemy import select
from sqlalchemy.orm import Session

from core.config import settings
from db.models import Contact, Interaction, Lead, LeadStage
from engine import agent_outreach as ao
from engine import human_copy as hc
from engine import quant_pricing as qp
from engine.scripts import lead_facts

REPLY_OUTCOME = ao.REPLY_OUTCOME
NO_PRICE_VERDICTS = {"PASS", "FRFPA_BLOCKED", "EQUITY_RESCUE_REFERRAL", "SHORT_SALE_ONLY"}
INTENTS = ("COUNTER", "ACCEPT", "REJECT", "AVAILABLE", "UNDER_CONTRACT", "POF_REQUEST", "HIGHEST_AND_BEST", "OIL_TANK",
           "TENANTS", "CO", "SHORT_SALE", "ESTATE", "OPT_OUT", "QUESTION",
           # what an agent adds to "it's available" (2026-11-02 live replies); Touch 2 answers each in its first line
           "ASKS_ROLE", "ASKS_SHOWING", "CALL_ME", "BACKUP", "EXPIRED")
TOUCH2_FLAGS = ("ASKS_ROLE", "ASKS_SHOWING", "CALL_ME", "BACKUP")

# -- amounts ------------------------------------------------------------------------------------------
_PHONE = re.compile(r"\(?\b\d{3}\)?[-.\s]\d{3}[-.\s]\d{4}\b")
# A signature's "License #: 0897105" / "Lic# 1234567" / "MLS # 3901234" / "NMLS 12345" is an ID, not a price
# (2026-11-03: a Redfin agent's license number was read as an $897K counter on 8 Manhattan Ave).
_ID_NUMBER = re.compile(r"\b(?:licen[cs]e|lic|mls|nmls|id|ref(?:erence)?|listing|file|account|acct|order|case|docket)"
                        r"\.?\s*(?:no\.?|number|num|#)?\s*[#:]?\s*[A-Z]{0,3}\d[\d-]{3,}", re.I)
_MONEY = re.compile(r"(?<![\w.])(\$)?\s?(\d{1,3}(?:,\d{3})+|\d+(?:\.\d+)?)\s*(k\b|m\b|mm\b|thousand\b|million\b)?", re.I)
_BAND = re.compile(r"\b(?:(low|lower|mid|middle|high|upper)[\s-]*)?(\d)(\d{2})\s?'?s\b", re.I)
_UNIT_AFTER = re.compile(r"^\s*(sq\.?\s?ft|square|sqft|/\s?mo\b|a month|per month|monthly|%|percent|days?\b|business day|"
                         r"bedroom|bath|\bbr\b|\bba\b|years?\b|acres?\b|units?\b|families|family)", re.I)
_LABEL_BEFORE = re.compile(r"(tax(es)?|rent|hoa|deposit|emd|earnest|commission|fee|mortgage|balance|owed|payoff|"
                           r"lien|arrears|repairs?|rehab|estimate)\W{0,6}(?:(?:is|are|of|was|were|runs?|about|around|"
                           r"approx\.?|roughly)\W{0,6})?$", re.I)
_ASK_WORDS = re.compile(r"(wants?|needs?|is at|would take|will take|would accept|'d take|take less than|counter(ed|offer)?|"
                        r"came back|come back|looking for|looking at|won't go|will not go|not go below|lowest|bottom line|"
                        r"minimum|firm at|has to be|needs to be|no less than|at least|asking|is firm|their number|"
                        r"the number is|can do|could do|would do|would sell|will sell|sell for|let it go for|"
                        # "We are at 470.00" (the price they're under contract at), "sitting at", "accepted at"
                        r"are at|we'?re at|sitting at|(?:contract|accepted|deal|offers?) (?:at|for))", re.I)
# "Prob 550" / "around 480" / "maybe 425" in answer to "what would the seller need to net?" (2026-11-04: Juliet's
# "Prob 550" on a $575K ask was dropped as $550): thousands, but a guess, so never "stated plainly".
_HEDGE_BEFORE = re.compile(r"(?:^|\s)(prob(?:ably)?|maybe|around|about|roughly|approx(?:imately)?|likely|ballpark|~)\s*$",
                           re.I)
_NEG = re.compile(r"\b(not|no|isn't|aren't|haven't|hasn't|never|nope|without|nor)\b", re.I)
_ASKS_MAX = re.compile(r"what(?:'s| is|s)? (?:your|ur|the) (?:max(?:imum)?|best|highest|top|ceiling)\b|how high (?:can|could|will|would) "
                       r"you go|best you can do", re.I)

PHRASES: dict[str, list[str]] = {
    "ACCEPT": [r"accept(s|ed)? (your|the|that) (offer|number|price)", r"we have a deal", r"let'?s do it", r"deal\b.*\bworks",
               r"send (over )?(the|a|your) (contract|paperwork|agreement|psa)", r"seller (has )?agreed", r"they'?ll take (it|that)",
               r"he'?ll take (it|that)", r"she'?ll take (it|that)", r"that works for (us|them|the seller|me)", r"works for the seller",
               r"you got a deal", r"we'?re good at", r"go ahead and (send|write) (it|the offer)", r"accepted\b(?! an)"],
    "REJECT": [r"not interested", r"no thanks", r"too low", r"won'?t work", r"\bpass\b", r"insult", r"not even close",
               r"no way", r"declined", r"rejected", r"lowball", r"way off", r"not going to work", r"isn'?t going to work",
               r"nowhere near", r"never (?:going to |gonna |would |will )?happen", r"not happening", r"won'?t happen",
               r"no chance", r"not a chance", r"seller (?:won'?t|will not|would never|isn'?t going to) (?:go|take|accept|do)",
               # "No where close to what the seller needs. Best of luck" (Nov 4): a polite no, never a fresh fact-check
               r"no ?where close", r"(?:best|good) (?:of|if) luck|good luck"],
    "AVAILABLE": [r"\A\W*(?:hello|hi|hey)?\b[^.!?\n]{0,30}?\b(yes|yep|yup|yeah|absolutely)\b(?![^.!?\n]*\b(but|however|under contract|"
                  r"attorney|pending|sold)\b)",          # "Yes it is." answers our "is it still available?"
                  r"still available", r"(it'?s|it is|yes,? it'?s|yes it is|yes,? still) available", r"\bavailable\b",
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
    "ESTATE": [r"(?<!real )\bestate\b(?! (agent|broker|office|team|group|company|professional))", r"\bprobate\b", r"surrogate", r"executor", r"executrix", r"administrat(or|rix)", r"passed away",
               r"\bdeceased\b", r"\bheirs?\b", r"letters testamentary"],
    "OPT_OUT": [r"remove me", r"^\s*stop\b", r"please stop", r"\bno more\b", r"unsubscribe", r"do not contact", r"don'?t contact",
                r"take me off", r"don'?t e-?mail", r"stop e-?mailing", r"stop contacting", r"not interested in (any|other|more)"],
    "ASKS_ROLE": [r"are you (a |an )?(buyer|investor|realtor|agent|broker)", r"(buyer|investor) or (a |an )?(realtor|agent|broker)",
                  r"(realtor|agent|broker) or (a |an )?(buyer|investor)", r"do you have a (buyer|client)",
                  r"(looking to )?show it to (someone|a client|your client|a buyer)", r"who (do you|are you) represent",
                  r"are you (working with|representing) (a|the) buyer"],
    "ASKS_SHOWING": [r"(interested in|want to|like to|care to) (see|seeing|view|viewing|tour|touring) (it|the (house|home|property))",
                     r"schedule (a )?(showing|tour|walk-?through|viewing)", r"come (see|look at) it", r"want a showing",
                     r"when (can|would) you (like to )?(see|view|tour) it"],
    "CALL_ME": [r"(please |pls |kindly )?(call|phone|ring) me", r"give me a (call|ring|buzz)", r"call (to|and) discuss",
                r"let'?s (talk|chat|speak)", r"(call|reach) me (at|on)", r"best to call", r"easier to (talk|call)"],
    "BACKUP": [r"(have|has|got) a contract,? but", r"not sure (it'?s|it is|if it'?s|if it is|it will|it'?ll) (going|go) through",
               r"(might|may|could) fall (through|apart)", r"back-?up offer", r"second position", r"in case (it|the deal) falls"],
    "EXPIRED": [r"listing (has )?expired", r"\bexpired\b", r"no longer (listed|on the market|with (me|us|our office))",
                r"(i|we) (no longer|don'?t) (have|represent) (it|the listing|that listing)", r"listing (was )?cancell?ed"],
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
    counter_explicit: bool = False      # the number sits right after "wants" / "is at" / "would take" ... (not a guess)

    def to_json(self) -> dict[str, Any]:
        return {"amounts": self.amounts, "counter_amount": self.counter_amount, "intents": sorted(self.intents),
                "deadline": self.deadline, "notes": self.notes}


def _negated(text: str, start: int) -> bool:
    # "No, sorry it sold" / "Nope - pending": a leading "no" answers our question, it doesn't negate what follows.
    window = re.sub(r"\b(no|nope|sorry)\s*[,.!;:\-]+", " ", text[max(0, start - 14):start], flags=re.I)
    return bool(_NEG.search(window))


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
    if "," not in number and 100 <= n < 1000 and (ask_before or (dollar and "." not in number)):
        return n * 1000           # "$355" / "seller wants 310" / "we are at 470.00" / "wants 469.9" mean thousands
    return n


def parse_reply(text: str) -> ParsedReply:
    """Amounts, the seller's counter and what the message is doing (intents), from plain agent-speak."""
    out = ParsedReply()
    raw = text or ""
    scrubbed = _ID_NUMBER.sub(" ", _PHONE.sub(" ", raw))
    scored: list[tuple[float, float, int]] = []          # (value, phrase-score, position)
    band_spans = [(b.start(), b.end()) for b in _BAND.finditer(scrubbed)]
    for m in _MONEY.finditer(scrubbed):
        if any(a <= m.start() < z for a, z in band_spans):
            continue                                   # "mid 300s" is read as a band below, not as 300
        dollar, number, suffix = m.group(1), m.group(2), m.group(3)
        window_before = re.split(r"[.;!?\n]", scrubbed[max(0, m.start() - 45):m.start()])[-1]   # same sentence only
        window_after = re.split(r"[.;!?\n]", scrubbed[m.end():m.end() + 25], maxsplit=1)[0]
        ask_before = bool(_ASK_WORDS.search(window_before))
        bare = scrubbed.strip(" \t\n.!,~") == m.group(0).strip()           # the whole reply is "550"
        hedged = not ask_before and (bare or bool(_HEDGE_BEFORE.search(window_before)))
        value = _amount_value(dollar, number, suffix, ask_before or hedged)
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
        score = 1.0 if ask_before else 0.8 if hedged and value >= 20000 else 0.0
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
        out.counter_explicit = best[1] >= 1.0
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
    # Our opener asks "still available, or already in attorney review?". A short answer that names the attorney
    # ("Yes. Attorney", "attorney review", "in attorney") most likely means it is in attorney review: treat it as
    # under contract, but ask them to confirm rather than congratulate.
    words = re.findall(r"[a-z']+", low)
    if "attorney" in words and len(words) <= 8 and "UNDER_CONTRACT" not in out.intents and not out.counter_amount \
            and not re.search(r"still available|not (yet )?(in|under)|no attorney|send|offer|forward|e-?mail|number|"
                              r"price|contract|call|my attorney|your attorney|seller'?s attorney", low):
        out.intents.discard("AVAILABLE")
        out.intents.add("BACKUP")
        out.notes.append("attorney_ambiguous: a short reply naming the attorney, probably in attorney review: the offer "
                         "goes as a backup")
    if "BACKUP" in out.intents:
        # "We have a contract but not sure it's going through, did you want to submit an offer": a backup offer is
        # exactly what they asked for, so the under-contract reading must not stop it.
        out.intents.discard("UNDER_CONTRACT")
    if "EXPIRED" in out.intents:
        out.intents.discard("AVAILABLE")
    if out.intents & set(TOUCH2_FLAGS) and "QUESTION" in out.intents:
        out.intents.discard("QUESTION")                   # the question is one Touch 2 answers
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
    # The same final / offer every other surface states (engine/offer_math.py), locked to what already went out: no
    # re-trade below a written offer, nothing above the final (2026-11-03).
    from sqlalchemy.orm import object_session

    from engine.offer_math import firm_offer, locked
    firm = locked(lead, firm_offer(lead, object_session(lead), check_certainty=False))
    walk = float(firm.final or q.get("mao_walkaway") or v.mao_aggressive or 0)
    opening = float(firm.offer or lead.offer_amount or q.get("opening_offer") or v.anchor_offer or 0)
    return {
        "arv": v.arv_estimate, "repairs": v.est_repair_cost or 0.0, "annual_taxes": lead.property.annual_taxes,
        "buyer_max": float(buyer_max), "mao_walkaway": walk,
        "mao_target": float(firm.offer or q.get("mao_target") or v.mao_conservative or 0),
        "opening_offer": opening, "fee_target": q.get("fee_target") or v.target_assignment_fee,
        "verdict": v.verdict, "frfpa_floor": a.get("frfpa_floor") if a.get("frfpa_applies") else None,
        "compliant_offer": a.get("compliant_offer"), "quant": bool(q),
        "rehab_total": (buyer.get("rehab") or v.est_repair_cost or 0) + (buyer.get("rehab_contingency") or 0),
        "taxes_held": buyer.get("taxes_held"), "carry": buyer.get("carry"), "capital_cost": buyer.get("capital_cost"),
        "holding_months": buyer.get("holding_months") or int(qp.costs()["end_buyer"]["holding_months"]),
        "last_offer": lead.offer_amount, "ask": firm.ask,
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
    kind: str = ""                    # "touch2" (the priced offer), "fact_check" (a no-price question), "" otherwise

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


def _call_slot(now: datetime | None) -> datetime:
    """"Please call me": the next sensible calling time (in 3 hours, inside 9am-8pm Eastern, never on Sunday)."""
    t = ao._eastern(now) + timedelta(hours=3)
    if t.hour >= 20 or t.hour < 9:
        day = t.date() + timedelta(days=1) if t.hour >= 20 else t.date()
        t = datetime.combine(day, time(10))
    if t.weekday() == 6:
        t = datetime.combine(t.date() + timedelta(days=1), time(10))
    return t.replace(minute=(t.minute // 15) * 15, second=0, microsecond=0)


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


# Pre-flight findings one fact from the agent settles (2026-11-02: a 1-bedroom condo priced off other units' sales, a
# 3-family priced off 1- and 2-family sales, a listing linked to a house 10 miles away). Until the fact is in, the answer
# to "it's available" asks for it instead of quoting a number built on the wrong unit, unit count or house.
FACT_CHECKS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"condo / townhouse unit", re.I), "condo"),
    (re.compile(r"unit count", re.I), "units"),
    (re.compile(r"wrong parcel|above every comp|outlier|confidence [0-4]\d/100|not a 1-4|non-residential", re.I), "basics"),
)
_FACT_ASK = {
    "condo": ["Before I put a number in writing on {st}, what have same-size units in the building sold for lately?",
              "And what's the monthly HOA, and is any special assessment coming?"],
    "units": ["Before I put a number in writing on {st}, could you send the rent roll: current rents, lease end dates and "
              "which units would be vacant at closing?"],
    "basics": ["Before I put a number in writing on {st}, could you confirm the bedrooms, baths and square footage?",
               "And roughly what condition is it in, and what does the seller need to net?"],
}
_FACT_SMS = {"condo": "what same-size units sold for and the HOA", "units": "the rent roll (rents, leases, vacancies)",
             "basics": "beds, baths, sqft and condition"}


_ACK = re.compile(r"^(?:(?:ok(?:ay)?|sounds good|sounds great|great|perfect|wonderful|awesome|will do|noted|got it|"
                  r"thanks?(?: again)?(?: you)?(?: so much| very much| a lot)?|thank you(?: so much| very much)?|ty|"
                  r"appreciate (?:it|you|that)|you too|same to you|cheers|have a (?:good|great|nice) "
                  r"(?:day|one|night|weekend|evening)|best|regards|\U0001F44D|\U0001F64F)[\s!.,]*)+$", re.I)


def is_ack(text: str) -> bool:
    """A plain thank-you / "sounds good" with nothing else (no question, number, or other topic), e.g. Jennifer's
    "Sounds good! Thank you!" after our backup note (2026-11-04). Only "nothing to answer" when our last e-mail named
    no price: after a priced offer "sounds good" may be an acceptance, which always waits for the operator."""
    lines = [x.strip() for x in (text or "").splitlines() if x.strip()]
    lines = [x for x in lines if not re.match(r"(sent from|get outlook|sent via|-{2,})", x, re.I)]
    if not lines or len(lines) > 3:
        return False
    if len(lines) > 1 and re.fullmatch(r"[A-Za-z.'-]+(?: [A-Za-z.'-]+)?", lines[-1]) and not _ACK.match(lines[-1]):
        lines = lines[:-1]                         # a first name under the thank-you
    body = " ".join(lines)
    return len(body) <= 80 and "?" not in body and not re.search(r"\d", body) and bool(_ACK.match(body))


_CHECKIN_OK = re.compile(r"^(?:(?:sure(?: thing)?|yes|yeah|yep|absolutely|of course|definitely|certainly|ok(?:ay)?|"
                         r"fine|that'?s fine|that works|no problem|np|please do|feel free|you can|you may|go ahead|"
                         r"by all means|will do|sounds good|sounds great|great|perfect|thanks?(?: you)?|thank you|"
                         r"appreciate it|(?:please )?(?:do )?(?:check|reach out|follow up)(?: back)?(?: with me)?"
                         r"(?: then| anytime| any time)?)[\s!.,]*)+$", re.I)


def is_checkin_ok(text: str) -> bool:
    """A short yes to OUR "mind if I check back in about 30 days?" (Geri, 2026-11-04: "Sure, you can absolutely" was
    read as "still available" and got a second "what would the seller need to net?" right after she gave a number).
    Only meaningful when our last answer asked to check back: after an opener, "Sure" means available."""
    lines = [x.strip() for x in (text or "").splitlines() if x.strip()]
    lines = [x for x in lines if not re.match(r"(sent from|get outlook|sent via|-{2,})", x, re.I)]
    if not lines or len(lines) > 3:
        return False
    if len(lines) > 1 and re.fullmatch(r"[A-Za-z.'-]+(?: [A-Za-z.'-]+)?", lines[-1]) and not _CHECKIN_OK.match(lines[-1]):
        lines = lines[:-1]                         # a first name under the yes
    body = " ".join(lines)
    return len(body) <= 100 and "?" not in body and not re.search(r"\d", body) and bool(_CHECKIN_OK.match(body))


def fact_check_issue(pf: Any) -> tuple[str, str] | None:
    """(kind, the pre-flight issue) when a fact from the agent must come before a price; a HOLD always needs one."""
    for issue in pf.issues:
        for rx, kind in FACT_CHECKS:
            if rx.search(issue):
                return kind, issue
    if pf.status == "HOLD":
        return "basics", (pf.issues[0] if pf.issues else "re-underwrite first")
    return None


def _fact_note(an: Analysis, kind: str, intents: set[str], st: str, first: str, op: dict[str, str], me: str,
               now: datetime | None) -> None:
    """A no-price answer: it answers what they asked (role, showing, call, backup), then asks for the one fact."""
    lead_in = []
    if "CALL_ME" in intents:
        lead_in.append("Thanks, I'll give you a call.")
    if "ASKS_ROLE" in intents:
        lead_in.append("I'm the buyer: an investor buying for myself or a partner investor, not a realtor, and I'm not "
                       "working with a buyer's agent.")
    if "BACKUP" in intents and len(lead_in) < 2:
        lead_in.append("Thanks, and yes, I'd be glad to be the backup.")
    if "ASKS_SHOWING" in intents and len(lead_in) < 2:
        lead_in.append("Yes, I'm interested; a few photos or a short video would help first, and it can be walked during "
                       "the inspection period.")
    if not lead_in:
        lead_in.append(f"Thanks for getting back to me on {st}.")
    ask = [x.format(st=st) for x in _FACT_ASK[kind]]
    close = ["I'll get you the written offer the same day."] if len(lead_in) + len(ask) < 3 else []
    an.reply_text, an.style = _compose(an.reply_subject, first, lead_in + ask + close, hc.footer_lines(op), op,
                                       hc.DETAILS)
    an.sms_text = f"Hi {first}, it's {me}. Thanks on {st}; before I send numbers, can you share {_FACT_SMS[kind]}?" + \
        " Reply STOP to opt out."
    an.followup_at = _call_slot(now) if "CALL_ME" in intents else _followup(now, 2)
    an.kind = "fact_check"


def _backup_context(session: Session, lead: Lead, intents: set[str]) -> bool:
    """They're under contract with someone else and we're the backup (said now, or in an earlier reply)."""
    if {"BACKUP", "UNDER_CONTRACT"} & intents:
        return True
    from db.models import InboundReply
    return any("BACKUP" in (r or []) for r in session.scalars(
        select(InboundReply.intents_json).where(InboundReply.lead_id == lead.id)))


def analyze(session: Session, lead: Lead, text: str, *, now: datetime | None = None,
            agent: Contact | None = None) -> Analysis:
    """Read the reply, run the math, write the answer. Pure: nothing is written (record() does that). `agent` is the
    person who wrote (a colleague the listing agent handed the thread to), greeted by name; default the card's agent."""
    parsed = parse_reply(text)
    f = lead_facts(lead)
    op = hc.operator()
    agent = agent or ao.agent_contact(lead)
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
        # A risk found after our written offer doesn't lower it (no re-trading): the offer's own terms cover a found
        # tank or open permits (removed by the seller or escrowed); the buffer only trims a final not yet stated.
        nums["adjusted_walkaway"] = min(nums["mao_walkaway"], max(_floor100(nums["mao_walkaway"] - buffer),
                                                                 float(nums.get("last_offer") or 0)))
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
    ref = max(float(ao.listing_gap(lead).get("list_price") or 0), float(nums.get("arv") or 0))
    if counter and ref and not 0.2 * ref <= counter <= 2.5 * ref:
        # "Over 20 Million Dollar Sales Volume" in a signature, a lot size, an MLS number: not a price for this house.
        an.warnings.append(f"Ignored {hc.money(counter)}: it isn't a price for this house (likely from a signature "
                           "or another listing). Check their message.")
        parsed.counter_amount, counter = None, None
        parsed.intents.discard("COUNTER")
    frfpa_floor = nums.get("frfpa_floor")
    priced = bool(nums) and nums.get("verdict") not in NO_PRICE_VERDICTS and nums.get("mao_walkaway", 0) > 0

    # -- gates: no number we can give --------------------------------------------------------------------------
    from engine import preflight
    pf = preflight.check(lead)
    if nums and pf.status == "HOLD" and not ({"OPT_OUT", "UNDER_CONTRACT", "EXPIRED", "REJECT"} & intents):
        # The numbers can't be defended yet (wrong parcel match, failed comps, ...): no price goes out, ask instead.
        an.headline = "NUMBERS ON HOLD: " + (pf.issues[0] if pf.issues else "re-underwrite first")
        an.actions.insert(0, "Don't quote a price yet: " + "; ".join(pf.issues[:2]) + ". Call them or use this no-price note.")
        kind = (fact_check_issue(pf) or ("basics", ""))[0]
        _fact_note(an, kind, intents, st, first, op, me, now)
        return an
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
            # An accepted number is final (2026-11-03): say what the valuation couldn't verify before it goes in writing.
            from engine.offer_math import certainty_issues
            for issue in certainty_issues(lead, session):
                an.warnings.append(f"Before you accept, confirm: {issue}")
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
            # One move only (2026-11-03: offers are final and on the record): our counter IS the best and final.
            x = adj_walk
            final_said = bool(last_offer and float(last_offer) >= adj_walk and nums.get("last_offer"))
            if x > 0 and not final_said and x >= float(ncfg.get("counter_within_pct", 0.92)) * c:
                an.verdict, an.headline = "YELLOW", f"BEST AND FINAL AT {hc.money(x)} (keeps a {hc.money(_floor100(adj_max - x))} fee)"
                nums.update({"our_counter": x, "fee_at_our_counter": _floor100(adj_max - x)})
                sentences = [f"I ran it again with the real costs, {cost_line}, and that's what keeps me from {hc.money(c)}.",
                             f"My best and final is {hc.money(x)} cash, as-is, with a quick close.",
                             "If that works for the seller I'll send the written offer today."]
                priced_reply(x, sentences, [])
                an.sms_text = f"Hi {first}, it's {me}. My best and final on {st} is {hc.money(x)} cash, as-is; details by e-mail." + sms_tail
                an.actions = [f"Best and final at {hc.money(x)}: nothing above it, ever."] + an.actions
                an.followup_at = _followup(now, int(ncfg.get("yellow_followup_days", 2)))
            elif adj_walk < float(ncfg.get("state_ceiling_min_pct", 0.80)) * c and not nums.get("last_offer"):
                # Far apart and we never put a number in writing: naming a ceiling this far under theirs reads as a
                # lowball and ends the thread (2026-11-03). No number; keep the door open and check back in a month.
                # (Once an offer is on the record, restating the ceiling is consistent, not an insult.)
                days = int(ncfg.get("far_apart_days", 30))
                an.verdict, an.headline = "RED", f"TOO FAR APART AT {hc.money(c)} (our final {hc.money(adj_walk)}): NO NUMBER, CHECK BACK IN {days} DAYS"
                nums.update({"our_counter": None, "fee_at_our_counter": None})
                sentences = [f"Thanks for the number on {st}.",
                             f"As-is, with {cost_line}, I'm too far from {hc.money(c)} to send something worth the seller's "
                             "time right now.",
                             f"If the seller's number moves, I can close cash with no financing or appraisal contingency; mind "
                             f"if I check back in about {days} days?"]
                if _backup_context(session, lead, intents):
                    sentences = ["Understood, thanks for letting me know where it stands.",
                                 "If the current deal falls through, let me know and I'll put a clean cash offer together "
                                 "the same day."]
                    an.headline = f"TOO FAR APART (UNDER CONTRACT AT {hc.money(c)}): NO NUMBER, CHECK BACK LATER"
                an.reply_text, an.style = _compose(an.reply_subject, first, sentences, hc.footer_lines(op), op, hc.DETAILS)
                an.sms_text = f"Hi {first}, it's {me}. Thanks on {st}; I'm too far from {hc.money(c)} as-is. OK if I check back in a few weeks?" + sms_tail
                an.actions = [f"No number sent (our final {hc.money(adj_walk)} is under {float(ncfg.get('state_ceiling_min_pct', 0.80)):.0%} "
                              f"of their {hc.money(c)}). Check-in set for {days} days."] + an.actions
                an.followup_at = _followup(now, days)
            else:
                an.verdict, an.headline = "RED", f"HOLD FIRM AT {hc.money(adj_walk)} / SET {int(ncfg.get('drip_days', 14))}-DAY DRIP"
                nums.update({"our_counter": adj_walk, "fee_at_our_counter": _floor100(adj_max - adj_walk)})
                sentences = [f"Thanks for the number; {hc.money(c)} doesn't work on this one with {cost_line}.",
                             f"My ceiling is {hc.money(adj_walk)} cash, as-is, and I'll leave that on the table for the seller.",
                             "If anything changes in the next couple of weeks I'm one e-mail away."]
                if _backup_context(session, lead, intents):
                    # They're under contract at that number with someone else: stay the clean backup, no chasing.
                    sentences = ["Understood, thanks for letting me know where it stands.",
                                 f"{hc.money(c)} is past what I can pay as-is with {cost_line}, so I'll stay in the backup "
                                 f"spot: my ceiling is {hc.money(adj_walk)} cash, as-is, with no financing or appraisal "
                                 "contingency.",
                                 "If the current deal falls through, I can move fast."]
                    an.headline = f"BACKUP AT {hc.money(adj_walk)}: they're under contract at {hc.money(c)}"
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
    if "EXPIRED" in intents:
        an.headline = "LISTING EXPIRED: the owner may still sell"
        an.actions = ["The agent no longer has it listed. If the owner's mailing address is on the lead, a letter goes "
                      "out from Inspector & Mail; check back in 30 days in case it relists."] + an.actions
        sentences = ["Thanks for letting me know.",
                     "If the owner still wants to sell, I'm glad to put an as-is cash offer in writing, so feel free to "
                     "pass along my number.",
                     "And if it comes back on the market, I'd appreciate a heads-up."]
        an.reply_text, an.style = _compose(an.reply_subject, first, sentences, hc.footer_lines(op), op, hc.DETAILS)
        an.sms_text = f"Hi {first}, it's {me}. Thanks on {st}; if the owner still wants to sell I'm glad to make an offer." + sms_tail
        an.followup_at = _followup(now, 30)
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
    if "AVAILABLE" in intents or "BACKUP" in intents:
        an.verdict = "GREEN"
        an.headline = ("SEND A BACKUP OFFER NOW (Touch 2)" if "BACKUP" in intents else "SEND THE OFFER NOW (Touch 2)")
        an.stage_to = LeadStage.CONTACTED
        packet = ao.build_packet(lead, agent=agent, generate_loi=False, session=session,
                                 reply_flags=set(intents) | {"ANSWERED"}) if agent else None
        fact = fact_check_issue(pf)
        if packet is not None and packet.kind == "offer" and fact:
            kind, issue = fact
            an.verdict, an.headline = "INFO", f"CONFIRM THE FACTS FIRST: {issue}"
            an.actions = [f"No price yet ({issue}). This no-price note answers them and asks for exactly that; when they "
                          f"answer, check the numbers, then send the offer (`gmail --send-offer {lead.id}`)."] + an.actions
            if "CALL_ME" in intents:
                an.actions.insert(0, "They asked for a call: call at the follow-up time (it's on your calendar).")
            _fact_note(an, kind, intents, st, first, op, me, now)
            an.numbers = dict(nums, fact_check=kind)
            return an
        an.kind = "touch2" if packet is not None and packet.kind == "offer" else ""
        if packet is not None:
            an.reply_subject, an.reply_text, an.sms_text = packet.subject, packet.email_body, packet.sms_text
            an.style = hc.lint(packet.email_body)
            an.actions = [("They answered: the priced note is ready on the deal card; send it today." if packet.kind == "offer"
                           else "They answered: ask who lives there first (the occupancy decides the FRFPA rules); the "
                                "no-price inquiry is ready on the deal card.")] + an.actions
            if packet.warnings:
                an.warnings.extend(packet.warnings)
        an.followup_at = (_call_slot(now) if "CALL_ME" in intents else ao._eastern(now))
        if "CALL_ME" in intents:
            an.actions.insert(0, "They asked for a call: the written numbers go first, then call them at the follow-up time "
                                 "(it is on your calendar).")
        return an
    ask = nums.get("ask")
    if "REJECT" in intents and not nums.get("last_offer") and ask and \
            adj_walk < float(ncfg.get("state_ceiling_min_pct", 0.80)) * float(ask):
        # We never named a price (ask-first) and our final is far under the ask: a number now would read as a lowball.
        days = int(ncfg.get("far_apart_days", 30))
        an.verdict, an.headline = "RED", f"NOT NOW: NO NUMBER, CHECK BACK IN {days} DAYS"
        sentences = ["Understood, and thanks for the quick answer.",
                     f"If the seller's thinking changes I can close cash, as-is, with no financing contingency; mind if I "
                     f"check back in about {days} days?"]
        an.reply_text, an.style = _compose(an.reply_subject, first, sentences, hc.footer_lines(op), op, hc.DETAILS)
        an.sms_text = f"Hi {first}, it's {me}. Understood on {st}; OK if I check back in a few weeks?" + sms_tail
        an.actions = [f"No number sent; check-in set for {days} days."] + an.actions
        an.followup_at = _followup(now, days)
        return an
    if "REJECT" in intents:
        an.verdict, an.headline = "RED", f"HOLD AT {hc.money(adj_walk)} / SET {int(ncfg.get('drip_days', 14))}-DAY DRIP"
        nums.update({"our_counter": adj_walk, "fee_at_our_counter": _floor100(adj_max - adj_walk)})
        sentences = ["Understood, and thanks for the quick answer.",
                     f"For what it's worth my ceiling is {hc.money(adj_walk)} cash, as-is, with no financing contingency.",
                     "If the seller's thinking changes I'm glad to put it in writing."]
        if _backup_context(session, lead, intents):
            sentences = ["Understood, thanks for letting me know.",
                         f"If the current deal falls through, my ceiling is {hc.money(adj_walk)} cash, as-is, with no "
                         "financing or appraisal contingency, and I can move fast."]
            an.headline = f"BACKUP AT {hc.money(adj_walk)}: they turned it down for now"
        priced_reply(adj_walk, sentences, [])
        an.sms_text = f"Hi {first}, it's {me}. Understood on {st}; my ceiling stays {hc.money(adj_walk)} cash, as-is, if anything changes." + sms_tail
        an.actions = ["Hold; the drip follow-up is scheduled."] + an.actions
        an.followup_at = _followup(now, int(ncfg.get("drip_days", 14)))
        return an
    if _ASKS_MAX.search(text or "") and adj_walk:
        # "What is your max?" (Juliet, 2026-11-04, after our "too far apart"): the honest answer is the best and final,
        # once; asking for their number again would ignore the question. Priced, so it waits for the operator.
        an.verdict, an.headline = "YELLOW", f"THEY ASKED YOUR MAX: BEST AND FINAL {hc.money(adj_walk)}"
        nums.update({"our_counter": adj_walk, "fee_at_our_counter": _floor100(adj_max - adj_walk)})
        sentences = ["Fair question.",
                     f"As-is, my max on {st} is {hc.money(adj_walk)} cash, with no financing or appraisal contingency "
                     "and a quick close.",
                     "If the seller can work with that, I'll send the written offer the same day."]
        priced_reply(adj_walk, sentences, [])
        an.sms_text = f"Hi {first}, it's {me}. My max on {st} is {hc.money(adj_walk)} cash, as-is." + sms_tail
        an.actions = [f"Their question was your max: {hc.money(adj_walk)} is the best and final, nothing above it."] + an.actions
        an.followup_at = _followup(now, int(ncfg.get("yellow_followup_days", 2)))
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
