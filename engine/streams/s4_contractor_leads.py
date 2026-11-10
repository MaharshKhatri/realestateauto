"""Stream 4: weekly rehab trigger reports for contractors (2026-11-09).

Contractors who fix houses (oil-tank removers, roofers, heating / AC companies, general rehab contractors, dumpster
companies) want to know which houses need their trade before the next owner starts work. Two public records say so:

1. the listing's own description ("underground oil tank", "roof leaks", "no heat", "fire damage", "contractor
   special"), read by keyword rules that skip negations and finished work ("new roof", "tank removed", "tank sweep
   clean" and "gas heat" are not triggers); and
2. a fresh purchase by an investor entity (a recorded deed in the last `fresh_days`): a flip about to start.

The product is one weekly one-page "NJ Rehab Trigger Report" per county: town, street, trigger, the listing's own words
(12 or fewer), price, days on market and the listing brokerage, or the entity buyer, month and price of the deed.
A contractor buys a county as a flat monthly subscription.

The 2-step funnel (2026-11-10): Touch 1 is two plain sentences with no attachment, no link and no price. It says what we
did (we read the listing descriptions and recent investor deeds; never "inspections"), names one street from this
week's sheet in the contractor's county with their own trade's work in words the listing supports ("needing roof
work", "with an oil tank in the listing"), gives the true count of the other rows of that trade on the sheet and asks
whether they want the list. Their yes releases the held sample (the county report with their trade's rows first;
rows 3+ have the street, the brokerage and a deed buyer's company hidden) and the flat price. The second cold touch,
a week later, is one sentence in the same thread.

Why it stays legal for an unlicensed operator: it is research data for a flat fee, never pay tied to a job or a
client (N.J.S.A. 45:15-3), and the contractor contacts the brokerage or the owner company on their own. No homeowner
names: an owner appears only when it is a business entity, and every address passes the Daniel's Law / stop
suppression list (`address_ok`). Only public listing text and recorded deeds go in, never what an agent wrote to us.
"""
from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable
from xml.sax.saxutils import escape

from sqlalchemy import select
from sqlalchemy.orm import Session

from core.text import clean, muni_base, postal_title
from db.models import CashBuyer, DeedTransfer, DistressSignal, FirmOutbox, FirmTarget, Listing, Property, SignalType
from engine.streams.base import (
    PRIORITY_SPECIALTY,
    SAMPLE_FULL_ROWS,
    Outbound,
    Stream,
    StreamContext,
    StreamResult,
    address_ok,
    entity_like,
    mask_text,
    money,
    operator_footer,
    render_table_pdf,
    sample_path,
    slug,
)

OIL_TANK, ROOF, HVAC, STRUCTURAL, FULL_GUT, FRESH_FLIP = ("OIL_TANK", "ROOF", "HVAC", "STRUCTURAL", "FULL_GUT",
                                                          "FRESH_FLIP")
ORDER = (OIL_TANK, ROOF, HVAC, STRUCTURAL, FULL_GUT, FRESH_FLIP)
LABELS = {OIL_TANK: "Oil tank", ROOF: "Roof", HVAC: "Heat / AC", STRUCTURAL: "Structural / damage",
          FULL_GUT: "Full rehab", FRESH_FLIP: "Fresh flip"}
# heavier work first in the general report: a gut or a fire job is a bigger ticket than a furnace
WEIGHTS = {FULL_GUT: 3.0, STRUCTURAL: 3.0, OIL_TANK: 2.5, ROOF: 2.0, HVAC: 1.5, FRESH_FLIP: 2.0}
TRADE_TRIGGERS: dict[str, tuple[str, ...]] = {
    "oil_tank_contractor": (OIL_TANK,),
    "roofer": (ROOF,),
    "hvac_contractor": (HVAC,),
    "rehab_general_contractor": (FULL_GUT, STRUCTURAL, FRESH_FLIP),
    "dumpster_rental": (FULL_GUT, FRESH_FLIP, STRUCTURAL),
}
# Touch 1's words for the trade's work, no stronger than what every rule of that trigger reads in a listing ("oil tank
# on property" names a tank, not a removal; "sold as-is, needs work" is rehab work, not a gut); a deed row says who bought
NEED_WORDS = {
    OIL_TANK: "with an oil tank in the listing",
    ROOF: "needing roof work",
    HVAC: "needing heating or AC work",
    STRUCTURAL: "needing damage or structural repair",
    FULL_GUT: "needing rehab work",
}
# the trade's list, by name, in the one-sentence follow-up ("this week's Essex County oil tank list")
TRADE_WORDS = {
    "oil_tank_contractor": "oil tank",
    "roofer": "roof",
    "hvac_contractor": "heating and AC",
    "rehab_general_contractor": "rehab",
    "dumpster_rental": "rehab and cleanout",
}
LISTING_SIGNALS = (SignalType.STALE_LISTING, SignalType.ACTIVE_LISTING)
RES_CLASSES = {"", "2", "4C"}               # MOD-IV residential (and residential-style apartments)
MIN_DEED_PRICE = 40_000                      # nominal / package transfers under this say nothing
MAX_PARCEL_MILES = 1.0                       # a listing linked to a parcel farther away is the wrong parcel


# -- the trigger classifier (pure) --------------------------------------------------------------------------------
_RESOLVED = (r"(?:removed|remediated|decommissioned|closed|pulled|repaired|fixed|resolved|addressed|corrected|"
             r"replaced|done|completed|clean|cleared|negative|passed|performed|upgraded|updated|serviced|none)")
# finished work right after the match: "oil tank removed", "tank sweep done", "roof leak was repaired in 2022"
_AFTER_FIXED = re.compile(
    r"^[\s,(:\-]*(?:(?:and|which|that|it)\s+)?(?:(?:was|were|is|are|has|have|had|been|since|already|recently|"
    r"previously|properly|fully|professionally|completely|sweep|tested|test)\s+){0,4}" + _RESOLVED + r"\b"
    r"|^[^,;]{0,40}?\b(?:was|were|has been|have been|had been)\s+(?:\w+\s+)?" + _RESOLVED + r"\b", re.I)
_NEG = re.compile(r"(?:\b(?:no|not|never|without|none|nor|zero|free of)\b|n't\b)", re.I)
# finished work right before the match: "recently completed foundation repairs", "underwent a gut renovation"
_DONE_BEFORE = re.compile(r"\b(?:recent(?:ly)?|completed|finished|after|following|underwent|prior|previous(?:ly)?|"
                          r"past|former(?:ly)?|repaired|fixed|resolved|removed|remediated|decommissioned)\b", re.I)
_CONJ = re.compile(r"\b(?:and|but|or|while|however|although|though|plus|also)\b", re.I)
_CLAUSE_START = re.compile(r"[.;:!?\n,(•|]")
_SENTENCE_END = re.compile(r"[.;!?\n•|]")


@dataclass(frozen=True)
class Rule:
    trigger: str
    pattern: re.Pattern[str]
    before: re.Pattern[str] | None = None     # also not a trigger when this sits just before the match
    after: re.Pattern[str] | None = None      # ... or right after it


def _r(trigger: str, pattern: str, *, before: str | None = None, after: str | None = None, flags: int = re.I) -> Rule:
    return Rule(trigger, re.compile(pattern, flags), re.compile(before, re.I) if before else None,
                re.compile(after, re.I) if after else None)


# "UST oil tank scan documentation available" / "tank sweep report on file": the search was done and documented
_TANK_DOC = r"^[^.;]{0,25}\b(?:scan|sweep|search)\b[^.;]{0,30}\b(?:documentation|report|certificat\w*|available|on file)\b"
# "roof leak protection" / "no heat warranty": a home-warranty line, not a defect
_WARRANTY = r"^\s*(?:protection|coverage|warranty|insurance|plan)\b"
_DEVICE = (r"(?:hvac|boiler|furnace|heating system|heating|heat|a/c|ac units?|ac|air[- ]conditioning|air conditioner|"
           r"central air|heat pump|condenser|cooling system)")
RULES: tuple[Rule, ...] = (
    # -- oil tanks (NJ: an unremoved underground tank is the classic closing-killer) --
    _r(OIL_TANK, r"\b(?:underground|buried|in-?ground|subsurface)\s+(?:(?:heating\s+|fuel\s+)?oil\s+|fuel\s+|storage\s+)?"
                 r"tanks?\b", after=_TANK_DOC),
    _r(OIL_TANK, r"\bUSTs?\b", after=_TANK_DOC, flags=0),                     # case-sensitive: "UST", never "must" / "ust"
    _r(OIL_TANK, r"\b(?:oil\s+)?tank\s+(?:sweep|scan|search)\s+(?:is\s+|will be\s+)?(?:needed|required|recommended|"
                 r"advised|suggested|necessary|to be (?:done|performed|completed)|by (?:the\s+)?buyer|"
                 r"(?:is\s+)?(?:the\s+)?buyer'?s?\s+responsibility)\b"),
    _r(OIL_TANK, r"\b(?:buyers?|purchasers?)\s+(?:to|must|should|(?:is\s+|are\s+)?responsible for)\s+(?:do\s+|perform\s+|"
                 r"order\s+|obtain\s+|complete\s+|conduct\s+|pay for\s+)?(?:an?\s+|the\s+|any\s+)?(?:oil\s+)?tank\s+"
                 r"(?:sweep|search|scan|test|removal)"),
    _r(OIL_TANK, r"\b(?:needs?|requires?)\s+(?:an?\s+)?(?:oil\s+)?tank\s+(?:sweep|removal)\b"),
    _r(OIL_TANK, r"\boil\s+(?:storage\s+)?tanks?\b",
       before=r"\b(?:above[- ]?ground|double[- ]wall(?:ed)?|indoor|interior|basement|roth|new|newer|replaced|updated|"
              r"upgraded)\b",
       after=r"^[^.;,]{0,25}\b(?:in (?:the )?(?:basement|garage|shed)|above[- ]?ground)\b|" + _TANK_DOC[1:]),
    # -- roofs ("new roof" / "updated roof" / "roof 3 years old" never match: every rule needs a need word) --
    _r(ROOF, r"\b(?:needs?|needing|requires?|required|in need of)\s+(?:an?\s+)?(?:new\s+|full\s+|complete\s+)?"
             r"roof(?:ing)?\b(?!\s*(?:top|deck|terrace))"),
    _r(ROOF, r"\broof(?:ing)?\s+(?:is\s+|has\s+)?(?:leaks?|leaking|leakage|damage|damaged|issues?|problems?|"
             r"needs?|requires?|in need|(?:is\s+)?(?:failing|worn|deteriorat\w*|sagging|caving))\b", after=_WARRANTY),
    _r(ROOF, r"\b(?:new\s+roof|roof\s+replacement|roof\s+repairs?|re-?roof(?:ing)?)\s+(?:is\s+|are\s+|will be\s+)?"
             r"(?:needed|required|necessary|recommended)\b"),
    _r(ROOF, r"\b(?:leaky|leaking|damaged|failing|worn(?:-out| out)?|deteriorating|sagging|collapsed|caved[- ]in)\s+"
             r"roof\b"),
    _r(ROOF, r"\broof\b[^.;,]{0,25}\b(?:end of (?:its )?(?:useful )?life|past its (?:useful )?life)\b"),
    # -- heating / cooling ("new boiler" / "updated HVAC" never match) --
    _r(HVAC, r"\bno\s+(?:working\s+|functioning\s+)?(?:heat|heating(?:\s+system)?|boiler|furnace|hvac|central air|"
             r"a/c|ac)\b", after=r"^\s*(?:or|and|/)\s+\w+(?:\s+\w+)?\s+(?:issues?|problems?|concerns?)|" + _WARRANTY[1:]),
    _r(HVAC, r"\b" + _DEVICE + r"\s+(?:is\s+|are\s+|does\s+|do\s+)?(?:not working|not work|not functional|not functioning|"
             r"not operational|non-?functional|non-?operational|inoperable|inoperative|broken|failed|doesn't work|"
             r"don't work|needs?|requires?|in need|at (?:the )?end of (?:its )?(?:useful )?life)\b"),
    _r(HVAC, r"\b(?:needs?|needing|requires?|in need of)\s+(?:an?\s+)?(?:new\s+)?(?:hvac|boiler|furnace|heating system|"
             r"heat|central air|a/c|ac|air[- ]conditioning|heat pump)\b"),
    _r(HVAC, r"\b(?:broken|non-?functional|inoperable|failed|cracked|dead)\s+(?:boiler|furnace|hvac|heating system|a/c|"
             r"ac|air conditioner|heat pump)\b"),
    _r(HVAC, r"\b(?:boiler|furnace|hvac|heating system)\s+replacement\s+(?:is\s+)?(?:needed|required|necessary)\b"),
    # -- structure and damage ("solid foundation" / "structural updates" never match) --
    _r(STRUCTURAL, r"\bfoundation\s+(?:issues?|problems?|cracks?|cracking|damage|damaged|repairs?|work|settling|failure|"
                   r"concerns?|needs?|requires?|is\s+(?:cracked|failing|settling|compromised))\b"),
    _r(STRUCTURAL, r"\b(?:cracked|crumbling|failing|bowed|bowing|sinking|compromised)\s+(?:foundation|walls?|joists?|"
                   r"beams?|sill)\b"),
    _r(STRUCTURAL, r"\bstructural(?:ly)?\s+(?:issues?|damage|problems?|repairs?|concerns?|defects?|deficienc(?:y|ies)|"
                   r"failure|work|instability|unsound|compromised|integrity issues?)\b"),
    _r(STRUCTURAL, r"\bsagging\s+(?:floors?|roof(?:line)?|ceilings?|beams?|joists?|porch|stairs?|walls?)\b"),
    _r(STRUCTURAL, r"\b(?:floors?|ceilings?|porch|roof(?:line)?)\s+(?:is\s+|are\s+)?sagging\b"),
    _r(STRUCTURAL, r"\b(?:water|fire|smoke|flood|storm|termite)[- ]damaged?\b"),
    # -- full rehab ("gut-renovated" / "underwent a gut renovation in 2018" never match) --
    _r(FULL_GUT, r"\bgut(?:[- ]job|[- ]rehab)\b"),
    _r(FULL_GUT, r"\b(?:needs?|requires?|candidate for|ready for|perfect for|ideal for)\s+(?:an?\s+)?(?:full\s+|"
                 r"complete\s+|total\s+)?gut(?:[- ]renovation|[- ]rehab)?\b|\bneeds? to be gutted\b"),
    _r(FULL_GUT, r"\bneeds?\s+everything\b"),
    _r(FULL_GUT, r"\b(?:needs?|requires?)\s+(?:an?\s+)?(?:complete|full|total|major|extensive|substantial|significant)\s+"
                 r"(?:gut\s+)?(?:renovation|rehab(?:ilitation)?|remodel|overhaul|restoration|work|repairs?)\b"),
    _r(FULL_GUT, r"\b(?:total|full|complete|major)\s+(?:gut\s+)?(?:rehab(?:ilitation)?|renovation)\s+(?:is\s+)?"
                 r"(?:needed|required|necessary)\b"),
    _r(FULL_GUT, r"\btear[- ]?downs?\b"),
    _r(FULL_GUT, r"\b(?:contractor|investor|builder)s?'?\s+special\b"),
)
# "sold as-is" plus a real need ("needs work / repairs / renovation") anywhere in the description = a rehab job
_AS_IS = re.compile(r"\bas[- ]is\b", re.I)
_NEEDS_WORK = _r(FULL_GUT, r"\bneeds?\s+(?:some\s+|lots of\s+|a lot of\s+|major\s+|extensive\s+|significant\s+|"
                           r"substantial\s+)?(?:work|repairs?|renovation|rehab(?:ilitation)?)\b")


def _before(text: str, start: int, words: int = 4) -> str:
    """The few words just before a match, inside its clause and after the last conjunction ("No HOA fees and needs a
    new roof": the "No" belongs to the fees, not to the roof)."""
    head = text[:start]
    cut = max((m.end() for m in _CLAUSE_START.finditer(head)), default=0)
    head = head[cut:]
    conj = list(_CONJ.finditer(head))
    if conj:
        head = head[conj[-1].end():]
    return " ".join(head.split()[-words:])


def _after(text: str, end: int, chars: int = 80) -> str:
    tail = text[end:end + chars]
    m = _SENTENCE_END.search(tail)
    return tail[:m.start()] if m else tail


def _valid(rule: Rule, text: str, m: re.Match[str]) -> bool:
    before, after = _before(text, m.start()), _after(text, m.end())
    if _NEG.search(before) or _DONE_BEFORE.search(before):
        return False                                   # "no oil tank", "never had a roof leak", "after a gut rehab"
    if rule.before is not None and rule.before.search(before):
        return False
    if _AFTER_FIXED.search(after):
        return False                                   # "oil tank removed", "roof leak was repaired"
    if rule.after is not None and rule.after.search(after):
        return False
    return True


_TOKEN = re.compile(r"\S+")
_LINK_TOKEN = re.compile(r"(@|https?:|^\W*www\b|\.(?:com|net|org)\b)", re.I)
_PHONE = re.compile(r"\(?\b\d{3}\)?[\s.-]*\d{3}[\s.-]*\d{4}\b")
_CUE = {"call", "contact", "text", "email", "e-mail", "agent", "agents", "showing", "showings", "appointment",
        "appointments", "listing", "realtor"}


def excerpt(text: str, start: int, end: int, max_words: int = 12) -> str:
    """At most `max_words` of the listing's own words around a match, inside its sentence. Phone numbers, e-mails and
    links are dropped, and the window stops before "call / contact / agent ..." so a name after them never comes
    along. "..." marks a cut (it is never counted as a word)."""
    a = max((m.end() for m in _SENTENCE_END.finditer(text, 0, start)), default=0)
    m_end = _SENTENCE_END.search(text, end)
    b = m_end.start() if m_end else len(text)
    toks = [(t.start() + a, t.end() + a, t.group(0)) for t in _TOKEN.finditer(text[a:b])]
    if not toks:
        return ""
    i = next((k for k, t in enumerate(toks) if t[1] > start), 0)
    j = max(i, max((k for k, t in enumerate(toks) if t[0] < end), default=i))
    if j - i + 1 >= max_words:
        lo, hi = i, i + max_words
    else:
        extra = max_words - (j - i + 1)
        lo = max(0, i - extra // 2)
        hi = min(len(toks), lo + max_words)
        lo = max(0, hi - max_words)

    def cue(k: int) -> bool:
        return re.sub(r"[^a-z-]", "", toks[k][2].lower()) in _CUE

    if any(cue(k) for k in range(lo, i)):
        lo = i
    for k in range(j + 1, hi):
        if cue(k):
            hi = k
            break
    phones = [(p.start(), p.end()) for p in _PHONE.finditer(text, a, b)]
    words = [w for s, e, w in toks[lo:hi]
             if not _LINK_TOKEN.search(w) and not any(s < pe and e > ps for ps, pe in phones)]
    phrase = " ".join(words).replace('"', "'").strip(" ,;:-(")
    if not phrase:
        return ""
    return ("..." if lo > 0 else "") + phrase + ("..." if hi < len(toks) else "")


def phrase_words(phrase: str) -> int:
    return len([w for w in phrase.replace("...", " ").split() if w])


def find_triggers(text: str | None) -> dict[str, str]:
    """Trigger -> the listing's own short phrase that shows it (several triggers can fire on one listing). Pure, so
    every negation rule has a unit test."""
    t = clean(text) if text else ""
    if not t:
        return {}
    hits: dict[str, tuple[int, str]] = {}
    for rule in RULES:
        for m in rule.pattern.finditer(t):
            if _valid(rule, t, m):
                if rule.trigger not in hits or m.start() < hits[rule.trigger][0]:
                    hits[rule.trigger] = (m.start(), excerpt(t, m.start(), m.end()))
                break
    if FULL_GUT not in hits and _AS_IS.search(t):
        for m in _NEEDS_WORK.pattern.finditer(t):
            if _valid(_NEEDS_WORK, t, m):
                hits[FULL_GUT] = (m.start(), excerpt(t, m.start(), m.end()))
                break
    return {k: hits[k][1] for k in ORDER if k in hits}


# -- rows ---------------------------------------------------------------------------------------------------------
@dataclass
class Row:
    county: str
    town: str
    street: str
    triggers: list[str]
    words: str                     # the listing's own phrase, or the deed line
    price: float | None
    days: int | None
    brokerage: str
    source: str                    # "listing" / "deed"
    rank: float = 0.0
    entity: str = ""               # deed rows: the entity buyer (a business, never a person)
    bought: date | None = None

    def cells(self, mask_entity: bool = False) -> list[str]:
        """The report's cells. `mask_entity` hides a deed row's buyer company (a sample's rows 3+: the company and the
        town would find the house as surely as the street)."""
        trig = " + ".join(LABELS[t] for t in self.triggers)
        if self.source == "deed":
            who = mask_text(" ".join(self.entity.replace(",", " ").split())) if mask_entity else self.entity
            words = (f"Bought by <b>{escape(who)}</b> in {self.bought:%b %Y}" if self.bought else
                     f"Bought by <b>{escape(who)}</b>") + " (recorded deed)"
        else:
            words = f'"{escape(self.words)}"'
        return [escape(self.town), f"<b>{escape(self.street)}</b>", trig, words, money(self.price),
                str(self.days) if self.days is not None else "-", escape(self.brokerage) or "-"]


def county_key(name: str | None) -> str:
    """'ESSEX', 'Essex County', ' essex ' -> 'Essex'."""
    s = re.sub(r"\s+county$", "", clean(name or ""), flags=re.I).strip()
    return s.title()


_BROKER_WORDS = re.compile(r"(?i)\b(realt\w*|rlty|real estate|properties|homes?|group|team|agency|associates|brokerage|"
                           r"broker|re/?max|re max|keller|coldwell|century|weichert|compass|exp|fathom|berkshire|bhhs|"
                           r"sotheby'?s|christie'?s|redfin|serhant|corcoran|hanna|era|homesmart|better homes|nrt|vylla|"
                           r"international|residential|partners|advantage|elite|premier|select|professionals|"
                           r"real|solutions|boutique|homeservices)\b")


def brokerage_label(name: str | None) -> str:
    """The listing brokerage as printed on the listing, only when it reads as a business ("Jane Doe" alone could be a
    person, so it is left out)."""
    s = re.sub(r"(?i)[\s,-]*\bbroker\s*$", "", clean(name or "")).strip(" -,")
    if not s:
        return ""
    if entity_like(s) or _BROKER_WORDS.search(s) or len(s.split()) == 1:
        return s[:60]
    return ""


_COMPANY = re.compile(r"\b(LLC|L\.L\.C|PLLC|INC|INCORPORATED|CORP|CORPORATION|COMPANY|CO|LP|L\.P|LLP|LTD|LIMITED)\b")
_PERSONAL = re.compile(r"\b(TRUSTS?|TRUSTEES?|TR|TRS|TTEE|REVOCABLE|LIVING|(?<!REAL )ESTATE|EST|EXECUTORS?|EXECUTRIX|"
                       r"ADMINISTRATORS?|HEIRS|DECEASED|C/O|ETAL|ET AL)\b")


def company_like(name: str | None) -> bool:
    """A business that may be named in a product or an e-mail: an entity (`entity_like`), never an unnamed
    "Owner @ <mailing>" cluster (its "name" is somebody's mailing address), and never a trust or an estate unless it is
    itself an LLC / Inc / LP ("QUINN Q SAMPLE REVOCABLE TRUST" is a person's name; "SAMPLE TRUST HOMES LLC" is not)."""
    n = clean(name or "")
    if not entity_like(n) or n.upper().startswith("OWNER @"):
        return False
    up = n.upper().replace(",", " ")
    return not (_PERSONAL.search(up) and not _COMPANY.search(up))


def deed_street(address: str | None) -> str:
    """County deed exports print a range without its hyphen ('210 12 HILLSIDE RD' is 210-12): put it back."""
    a = clean(address or "").upper()
    m = re.match(r"^(\d+)\s+(\d{1,3})\s+(?=[A-Z])", a)
    if m and len(m.group(2)) < len(m.group(1)) and int(m.group(2)) > int(m.group(1)[-len(m.group(2)):]):
        a = f"{m.group(1)}-{m.group(2)} " + a[m.end():]
    return postal_title(a)


def _norm_addr(a: str | None) -> str:
    return re.sub(r"\W", "", str(a or "").upper())


def _iso(d: Any) -> date | None:
    try:
        return date.fromisoformat(str(d)[:10])
    except (TypeError, ValueError):
        return None


def fresh_purchases(families: dict[str, list[Any]], today: date, days: int = 120) -> list[dict[str, Any]]:
    """Entity purchases recorded in the last `days` (pure). Only buyer records whose own name is a company count
    (`company_like`: never a person, a family trust, an estate or an unnamed "Owner @ <mailing>" cluster). One price
    on one date for 2+ parcels of a family is a package deed and is dropped; the same deed under two records of a
    family counts once."""
    out: list[dict[str, Any]] = []
    since = today - timedelta(days=days)
    for buyers in families.values():
        seen: set[tuple[str, str]] = set()
        fam: list[dict[str, Any]] = []
        for b in buyers:
            name = clean(getattr(b, "entity_name", "") or "")
            if not company_like(name):
                continue
            for x in getattr(b, "recent_purchases_json", None) or []:
                if not isinstance(x, dict) or not x.get("address"):
                    continue
                d = _iso(x.get("deed_date"))
                if d is None or not since <= d <= today:
                    continue
                if str(x.get("class") or "").strip().upper() not in RES_CLASSES:
                    continue
                price = float(x["price"]) if x.get("price") else None
                if price is not None and price < MIN_DEED_PRICE:
                    continue
                k = (_norm_addr(x["address"]), d.isoformat())
                if k in seen:
                    continue
                seen.add(k)
                fam.append({**x, "entity": name, "deed_date": d, "price": price})
        dup = Counter((x["price"], x["deed_date"]) for x in fam if x["price"])
        out += [x for x in fam if not (x["price"] and dup[(x["price"], x["deed_date"])] > 1)]
    return out


def select_rows(rows: list[Row], limit: int = 24, max_flips: int = 6, lead: tuple[str, ...] = ()) -> list[Row]:
    """The report's rows (pure): listings by rank, then up to `max_flips` fresh flips. With `lead` (a trade's
    triggers), the rows of that trade come first, so a sample shows the buyer their own work on top."""
    listings = sorted((r for r in rows if r.source == "listing"), key=lambda r: -r.rank)
    deeds = sorted((r for r in rows if r.source == "deed"), key=lambda r: -r.rank)[:max(0, max_flips)]
    general = listings[:max(0, limit - len(deeds))] + deeds
    general += [r for r in listings if r not in general]
    if not lead:
        return general[:limit]
    first = sorted((r for r in listings + deeds if set(r.triggers) & set(lead)), key=lambda r: -r.rank)
    return (first + [r for r in general if r not in first])[:limit]


# -- PDFs ---------------------------------------------------------------------------------------------------------
def page_count(path: Path) -> int:
    """Pages in a ReportLab PDF (its page objects are written uncompressed), without a PDF library at run time."""
    return len(re.findall(rb"/Type\s*/Page(?![s\w])", path.read_bytes())) or 1


def render_report(path: Path, rows: list[Row], kwargs: dict[str, Any]) -> None:
    """The report (every row in full) and its sample (`sample_path`: rows 1-2 in full; rows 3+ with the street, the
    brokerage and a deed row's buyer company hidden), the sample that Touch 2 sends after a yes."""
    sample_path(path).unlink(missing_ok=True)              # never a stale sample from an earlier render
    if any(r.source == "deed" for r in rows[SAMPLE_FULL_ROWS:]):
        hidden = [r.cells(mask_entity=i >= SAMPLE_FULL_ROWS) for i, r in enumerate(rows)]
        render_table_pdf(path, **{**kwargs, "rows": hidden})     # the sample comes from these cells ...
        render_table_pdf(path, **kwargs, write_sample=False)     # ... and the report itself has every row in full
    else:
        render_table_pdf(path, **kwargs)


def render_fit(path: Path, rows: list[Row], build: Callable[[list[Row]], dict[str, Any]], min_rows: int = 3) -> int:
    """Render, and drop the lowest rows until the report and its sample each fit one page. Returns the rows kept."""
    n = len(rows)
    while True:
        render_report(path, rows[:n], build(rows[:n]))
        sample = sample_path(path)
        if n <= min_rows or (page_count(path) <= 1 and (not sample.exists() or page_count(sample) <= 1)):
            return n
        n = max(min_rows, int(n * 0.85))


HEAD = ["Town", "Street", "Trigger", "The listing's own words / deed record", "Price", "Days listed",
        "Listing brokerage"]
WIDTHS = [1.05, 1.5, 1.05, 3.6, 0.7, 0.55, 1.55]


def report_kwargs(county: str, rows: list[Row], ctx: StreamContext, price: float, fresh_days: int,
                  trade: str | None = None) -> dict[str, Any]:
    n_list = sum(1 for r in rows if r.source == "listing")
    n_deed = len(rows) - n_list
    first = f" · {LABELS[TRADE_TRIGGERS[trade][0]]} rows first" if trade in TRADE_TRIGGERS else ""
    return dict(
        title=f"NJ Rehab Trigger Report: {county} County",
        subtitle=(f"Week of {ctx.now:%B %-d, %Y} · {n_list} active listing{'' if n_list == 1 else 's'} whose public "
                  f"description names a rehab need · {n_deed} fresh investor (business entity) "
                  f"purchase{'' if n_deed == 1 else 's'}{first} · data, not an appraisal"),
        head=HEAD, rows=[r.cells() for r in rows], col_widths=WIDTHS,
        notes=[
            "Triggers are read from each listing's public description by keyword rules that skip negations and finished "
            "work (\"new roof\", \"tank removed\" and \"tank sweep clean\" are not triggers). Quotes are the listing's own "
            "words, 12 or fewer. Price is the list price, or the recorded deed price for a fresh flip.",
            f"Fresh flip: a business entity bought the house in the last {fresh_days} days (recorded deed; county sale data "
            "runs weeks behind). Owners are named only when they are business entities; addresses under a Daniel's Law "
            "or stop request are left out. Contact the listing brokerage or the owner company yourself.",
        ],
        box=(f"<b>{county} County weekly subscription: ${price:,.0f}/month flat.</b> A new report every week, one county "
             "per subscription, no commission and no per-job fee. Reply \"start\" to the e-mail and I'll set it up."),
        footer=operator_footer(ctx.operator))


# -- the 2-step funnel (2026-11-10) -------------------------------------------------------------------------------
def lead_row(rows: list[Row], kind: str) -> tuple[Row, str] | None:
    """The sheet's first row of the contractor's own trade, and the trigger that makes it theirs."""
    lead = TRADE_TRIGGERS.get(kind, ())
    for r in rows:
        trig = next((t for t in lead if t in r.triggers), None)
        if trig is not None:
            return r, trig
    return None


def month_phrase(d: date | None, today: date) -> str:
    """'in October' this calendar year, 'in October 2025' before it (never a month that reads as the wrong year)."""
    if d is None:
        return ""
    return f"in {d:%B}" if d.year == today.year else f"in {d:%B %Y}"


def touch1(county: str, row: Row, trigger: str, more: int, today: date, first: str = "") -> tuple[str, str]:
    """Touch 1 (subject, body): two plain sentences, no price, no link, no attachment. What we did, one street of
    the trade from this week's sheet, the true count of the trade's other rows on it, and a yes/no question."""
    if row.source == "deed" or trigger == FRESH_FLIP:
        what = f"an investor company bought {row.street} in {row.town} {month_phrase(row.bought, today)}".rstrip()
    else:
        what = f"{row.street} in {row.town} came up {NEED_WORDS[trigger]}"
    tail = f" ({more} more on this week's list)" if more > 0 else ""
    said = (f"We read the listing descriptions and recent investor deeds in {county} County, and {what}{tail}. "
            "Want the address list for your trade?")
    subject = (f"{row.street} + {more} more in {county} County" if more > 0 else
               f"{row.street} in {row.town}, {county} County")
    return subject, f"Hello,\n\n{said}\n\n{first}".rstrip()


def followup_body(county: str, kind: str, first: str = "") -> str:
    """The second cold touch, in Touch 1's thread: one sentence, no attachment."""
    said = f"Should I send this week's {county} County {TRADE_WORDS.get(kind, 'rehab')} list over?"
    return f"Hello,\n\n{said}\n\n{first}".rstrip()


def sample_body(county: str, price: float, masked: bool) -> str:
    """Touch 2, sent with the sample only after their yes: what is attached, the flat price, how to start."""
    what = (f"Here's this week's {county} County sheet, attached: {SAMPLE_FULL_ROWS} rows in full, the rest with the "
            "street hidden." if masked else f"Here's this week's {county} County sheet, attached.")
    return (f"{what} The full sheet every week is ${price:,.0f} a month per county plus NJ sales tax, flat, no commission or per-job fee. "
            "Reply \"start\" and I'll set it up.")


def last_thread(session: Session, stream: str, target_id: int) -> tuple[str, str] | None:
    """(Message-ID, subject) of the newest cold e-mail this stream sent the target: the follow-up goes in that thread."""
    row = session.execute(select(FirmOutbox.message_id, FirmOutbox.subject).where(
        FirmOutbox.stream == stream, FirmOutbox.target_id == target_id, FirmOutbox.status == "sent",
        FirmOutbox.kind.in_(("pitch", "followup")), FirmOutbox.message_id.is_not(None))
        .order_by(FirmOutbox.sent_at.desc(), FirmOutbox.id.desc()).limit(1)).first()
    return (str(row[0]), str(row[1] or "")) if row else None


def re_subject(subject: str) -> str:
    return "Re: " + re.sub(r"(?i)^\s*(?:(?:re|fwd?):\s*)+", "", subject or "").strip()


def _first(op: dict[str, str]) -> str:
    return (op.get("first") or (op.get("name") or "").split(" ")[0] or "").strip()


def _target_counties(t: FirmTarget) -> list[str]:
    cs = [county_key(t.county)] if t.county else []
    extra = (t.data_json or {}).get("counties") if isinstance(t.data_json, dict) else None
    if isinstance(extra, list):
        cs += [county_key(str(c)) for c in extra if c]
    return [c for i, c in enumerate(cs) if c and c not in cs[:i]]


def eligible_targets(session: Session, kinds: list[str], stream: str, key_for: Callable[[FirmTarget], str],
                     now: datetime) -> list[FirmTarget]:
    """Targets the outbox would accept now (stage NEW / CONTACTED, an e-mail on no stop list, touches and the pitch gap
    left, this period's key unused), best score first, so a capped run never wastes a slot on a sure skip."""
    from engine import firm_outbox as fo
    c = fo.cfg()
    gap = timedelta(days=float(c.get("pitch_gap_days", 6)))
    used = set(session.scalars(select(FirmOutbox.key).where(FirmOutbox.stream == stream)))
    out: list[FirmTarget] = []
    for t in session.scalars(select(FirmTarget).where(FirmTarget.kind.in_(kinds),
                                                      FirmTarget.stage.in_(("NEW", "CONTACTED")),
                                                      FirmTarget.email.is_not(None))
                             .order_by(FirmTarget.score.desc(), FirmTarget.id)):
        if key_for(t) in used or not fo.pitch_ok(t, now):
            continue
        if t.last_contact_at and t.last_contact_at > now - gap:
            continue
        if fo.stop_reason(session, t.email or "", t):
            continue
        out.append(t)
    return out


# -- the stream ---------------------------------------------------------------------------------------------------
@dataclass
class _Scan:
    rows: list[Row] = field(default_factory=list)
    counts: Counter[str] = field(default_factory=Counter)


def _listing_rows(session: Session, today: date, cap: int, scan: _Scan) -> None:
    """Active listings (a live STALE / ACTIVE listing signal) whose public text names a trigger."""
    from core.geo import haversine_miles
    pairs = session.execute(select(DistressSignal, Property).join(Property, DistressSignal.property_id == Property.id)
                            .where(DistressSignal.signal_type.in_(LISTING_SIGNALS), DistressSignal.active.is_(True))
                            .order_by(DistressSignal.last_seen_at.desc())).all()
    props: dict[int, tuple[Property, list[DistressSignal]]] = {}
    for sig, prop in pairs:
        if prop.id not in props and len(props) >= cap:
            continue
        props.setdefault(prop.id, (prop, []))[1].append(sig)
    refs = sorted({s.source_ref for _, sigs in props.values() for s in sigs if s.source_ref})
    by_ref: dict[str, Listing] = {}
    for i in range(0, len(refs), 500):
        for lst in session.scalars(select(Listing).where(Listing.source_ref.in_(refs[i:i + 500]))):
            by_ref.setdefault(lst.source_ref, lst)
    by_prop: dict[int, Listing] = {}
    ids = sorted(props)
    for i in range(0, len(ids), 500):
        for lst in session.scalars(select(Listing).where(Listing.property_id.in_(ids[i:i + 500]))):
            cur = by_prop.get(lst.property_id or 0)
            if cur is None or (lst.last_seen_at or datetime.min) > (cur.last_seen_at or datetime.min):
                by_prop[lst.property_id or 0] = lst
    for prop, sigs in props.values():
        scan.counts["listings_scanned"] += 1
        sigs.sort(key=lambda s: 0 if s.signal_type == SignalType.STALE_LISTING else 1)
        lst = next((by_ref[s.source_ref] for s in sigs if s.source_ref in by_ref), None) or by_prop.get(prop.id)
        if lst is not None and (lst.status or "for_sale").lower() not in ("for_sale", "active", "coming_soon"):
            continue                                       # pending / contingent / sold: no longer a live listing
        meta: dict[str, Any] = {}
        for s in sigs:
            meta = {**(s.raw_metadata_json or {}), **meta}
        words = [str(k) for k in (meta.get("keywords") or []) if k]
        text = " ".join(x for x in ((lst.description if lst is not None else "") or "",
                                    ". ".join(words) + "." if words else "") if x)
        found = find_triggers(text)
        if not found:
            continue
        if lst is not None and None not in (lst.lat, lst.lon, prop.lat, prop.lon) and \
                haversine_miles(prop.lat, prop.lon, lst.lat, lst.lon) > MAX_PARCEL_MILES:
            scan.counts["wrong_parcel"] += 1               # linked to the wrong parcel: the county isn't certain
            continue
        listed = SimpleNamespace(street_address=(lst.address if lst is not None else None) or prop.street_address,
                                 city=(lst.city if lst is not None else None) or prop.city,
                                 zip_code=(lst.zip_code if lst is not None else None) or prop.zip_code)
        if not address_ok(session, prop) or not address_ok(session, listed):
            scan.counts["suppressed"] += 1
            continue
        county = county_key(prop.county)
        if not county:
            continue
        days = max(0, (today - lst.list_date).days) if lst is not None and lst.list_date else (
            (lst.days_on_market if lst is not None else None) or meta.get("days_on_market"))
        price = (lst.list_price if lst is not None else None) or meta.get("list_price")
        broker = brokerage_label(meta.get("listing_broker"))
        if not broker and prop.lead is not None:
            broker = next((brokerage_label(c.company) for c in prop.lead.contacts
                           if c.contact_type == "LISTING_AGENT" and c.company and brokerage_label(c.company)), "")
        trig = list(found)
        phrase = found[sorted(trig, key=lambda t: -WEIGHTS[t])[0]]
        street = postal_title(listed.street_address) or postal_title(prop.street_address)
        town = postal_title(muni_base(prop.municipality)) or postal_title(prop.city)
        for t in trig:
            scan.counts[f"trig_{t.lower()}"] += 1
        scan.counts["listings_triggered"] += 1
        scan.rows.append(Row(county=county, town=town, street=street, triggers=trig, words=phrase,
                             price=float(price) if price else None, days=int(days) if days is not None else None,
                             brokerage=broker, source="listing",
                             rank=sum(WEIGHTS[t] for t in trig) + min(int(days or 0), 365) / 365))


def _flip_rows(session: Session, today: date, days: int, cap: int, scan: _Scan) -> None:
    """Fresh entity purchases from the recorded deeds behind the cash-buyer list (a flip about to start)."""
    from engine.partner_principals import family_key
    fams: dict[str, list[CashBuyer]] = {}
    for b in session.scalars(select(CashBuyer)):
        fams.setdefault(family_key(b) or f"b{b.id}", []).append(b)
    buys = sorted(fresh_purchases(fams, today, days), key=lambda x: x["deed_date"], reverse=True)[:cap]
    if not buys:
        return
    # county deed exports carry no town: the recorded deed's parcel id (COUNTY-MUN-BLOCK-LOT) supplies it
    recent = session.scalars(select(DeedTransfer).where(DeedTransfer.deed_date >= today - timedelta(days=days)))
    parcels = {(_norm_addr(d.property_location), d.deed_date.isoformat()): d.parcel_id
               for d in recent if d.deed_date and d.property_location}
    towns: dict[str, str] | None = None
    for x in buys:
        scan.counts["flips_scanned"] += 1
        mun, block, lot = str(x.get("mun_code") or ""), str(x.get("block") or ""), str(x.get("lot") or "")
        parcel = parcels.get((_norm_addr(x["address"]), x["deed_date"].isoformat()))
        if parcel and not mun:
            bits = parcel.split("-")
            if len(bits) >= 4:
                mun, block, lot = bits[1], bits[2], bits[3]
        prop = session.scalar(select(Property).where(Property.parcel_id == parcel)) if parcel else None
        if prop is None and mun and block and lot:
            prop = session.scalar(select(Property).where(Property.mun_code == mun, Property.block == block,
                                                         Property.lot == lot).limit(1))
        town_raw = x.get("municipality") or (prop.municipality if prop is not None else None)
        if not town_raw and mun:
            if towns is None:
                towns = {}
                for code, name in session.execute(select(Property.mun_code, Property.municipality).where(
                        Property.mun_code.is_not(None), Property.municipality.is_not(None)).distinct()):
                    towns.setdefault(str(code), name)
            town_raw = towns.get(mun)
        town = postal_title(muni_base(town_raw)) if town_raw else ""
        if not town:
            scan.counts["flips_no_town"] += 1              # can't check the suppression list without the town
            continue
        street = deed_street(x["address"])
        if (prop is not None and not address_ok(session, prop)) or not all(
                address_ok(session, SimpleNamespace(street_address=a, city=town, zip_code=""))
                for a in {street, str(x["address"])}):
            scan.counts["suppressed"] += 1
            continue
        county = county_key(x.get("county") or (prop.county if prop is not None else ""))
        if not county:
            continue
        age = (today - x["deed_date"]).days
        scan.counts["fresh_flips"] += 1
        scan.counts["trig_fresh_flip"] += 1
        scan.rows.append(Row(county=county, town=town, street=street, triggers=[FRESH_FLIP], words="",
                             price=x["price"], days=None, brokerage="", source="deed",
                             rank=WEIGHTS[FRESH_FLIP] + max(0.0, 1 - age / max(days, 1)),
                             entity=x["entity"], bought=x["deed_date"]))


class ContractorTriggerStream(Stream):
    key = "s4"
    name = "Contractor rehab triggers"
    priority = PRIORITY_SPECIALTY
    cadence_hours = 24.0

    def run(self, session: Session, ctx: StreamContext) -> StreamResult:
        lim = ctx.limits or {}
        res = StreamResult(stream=self.key)
        today = ctx.now.date()
        price = float(lim.get("price_monthly", 500))
        fresh_days = int(lim.get("fresh_days", 120))
        limit, max_flips = int(lim.get("report_rows", 24)), int(lim.get("max_flip_rows", 6))
        min_rows = int(lim.get("min_rows", 3))
        only = {county_key(c) for c in (lim.get("counties") or [])}
        scan = _Scan()
        _listing_rows(session, today, int(lim.get("max_listings", 4000)), scan)
        _flip_rows(session, today, fresh_days, int(lim.get("max_flips", 400)), scan)
        by_county: dict[str, list[Row]] = {}
        for r in scan.rows:
            if not only or r.county in only:
                by_county.setdefault(r.county, []).append(r)
        y, w, _ = ctx.now.isocalendar()
        period, tag = f"{y}-W{w}", f"{y}W{w:02d}"
        reports: dict[str, tuple[Path, list[Row]]] = {}
        for county, rows in sorted(by_county.items()):
            if len(rows) < min_rows:
                continue                                   # a near-empty page sells nothing
            chosen = select_rows(rows, limit, max_flips)
            path = ctx.out_dir / f"s4_rehab_triggers_{slug(county).lower()}_{tag}.pdf"
            kept = render_fit(path, chosen, lambda rs, c=county: report_kwargs(c, rs, ctx, price, fresh_days))
            reports[county] = (path, chosen[:kept])
            res.products += 1
            res.found += kept
        res.processed = scan.counts["listings_scanned"] + scan.counts["flips_scanned"]
        pitches = self._pitches(session, ctx, by_county, reports, price, fresh_days, period, tag, res) if ctx.send else 0
        res.metrics.update({k: int(v) for k, v in scan.counts.items()})
        res.metrics.update(counties_reported=len(reports), report_rows=res.found, pitches=pitches)
        res.metrics.setdefault("followups", 0)
        res.notes.append(f"{len(reports)} county reports, {res.found} rows, {pitches} pitches proposed")
        return res

    def _pitches(self, session: Session, ctx: StreamContext, by_county: dict[str, list[Row]],
                 reports: dict[str, tuple[Path, list[Row]]], price: float, fresh_days: int, period: str, tag: str,
                 res: StreamResult) -> int:
        lim = ctx.limits or {}
        if not reports:
            return 0
        cap = int(lim.get("pitches_per_run", 3))
        kinds = [str(k) for k in (lim.get("target_kinds") or list(TRADE_TRIGGERS))]
        limit, max_flips = int(lim.get("report_rows", 24)), int(lim.get("max_flip_rows", 6))
        need = int(lim.get("min_trade_rows", 1))

        def key_for(t: FirmTarget) -> str:
            return f"{self.key}:pitch:{t.id}:{period}"

        samples: dict[tuple[str, str], tuple[Path, list[Row]]] = {}
        first = _first(ctx.operator)
        made = 0
        for t in eligible_targets(session, kinds, self.key, key_for, ctx.now):
            if made >= cap:
                break
            lead = TRADE_TRIGGERS.get(t.kind, ())
            options = []
            for county in _target_counties(t):
                if county not in reports:
                    continue
                n = sum(1 for r in reports[county][1] if set(r.triggers) & set(lead))
                if n >= need:
                    options.append((n, county))
            if not options:
                continue
            n, county = max(options)
            # a target we already wrote to gets the one-sentence follow-up in that thread (about the same county when
            # it still has rows of their trade); the key stays one per target per week, so max_touches holds
            prior = None    # Touch 1 only: the outbox sends the in-thread nudges (firm_outbox.queue_nudges)
            if prior is not None:
                county = next((c for _, c in options if prior[1].rstrip().endswith(f"{c} County")), county)
            if (county, t.kind) not in samples:
                chosen = select_rows(by_county[county], limit, max_flips, lead)
                path = ctx.out_dir / f"s4_rehab_triggers_{slug(county).lower()}_{slug(t.kind).lower()}_{tag}.pdf"
                kept = render_fit(path, chosen, lambda rs, c=county, k=t.kind: report_kwargs(
                    c, rs, ctx, price, fresh_days, trade=k))
                samples[(county, t.kind)] = (path, chosen[:kept])
                res.products += 1
            path, rows = samples[(county, t.kind)]
            masked = sample_path(path).exists()
            # Touch 2, held until they say yes: the masked sample (the report itself if no sample was written)
            touch2, attach = sample_body(county, price, masked), [sample_path(path) if masked else path]
            if prior is not None:
                res.outbound.append(Outbound(
                    stream=self.key, key=key_for(t), to=t.email or "", priority=PRIORITY_SPECIALTY, kind="followup",
                    subject=re_subject(prior[1]), body=followup_body(county, t.kind, first), in_reply_to=prior[0],
                    target_id=t.id, sample_body=touch2, sample_attachments=attach))
                res.metrics["followups"] = int(res.metrics.get("followups", 0)) + 1
            else:
                hit = lead_row(rows, t.kind)
                if hit is None:
                    continue
                row, trig = hit
                more = sum(1 for r in rows if set(r.triggers) & set(lead)) - 1
                subject, body = touch1(county, row, trig, more, ctx.now.date(), first)
                res.outbound.append(Outbound(
                    stream=self.key, key=key_for(t), to=t.email or "", priority=PRIORITY_SPECIALTY, kind="pitch",
                    subject=subject, body=body, target_id=t.id, sample_body=touch2, sample_attachments=attach))
            made += 1
        return made


STREAM = ContractorTriggerStream()
