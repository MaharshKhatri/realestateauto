"""Stream 13: the weekly builder teardown digest (2026-11-10).

NJ custom-home builders and small developers buy older houses on good lots, knock them down and build new. Two public
records point at those houses while they are on the market:

1. the listing's own description, read by keyword rules that skip negations and the noise around the words: a
   listing that addresses builders ("builder's opportunity", "ideal for a builder", "investors and builders"),
   "teardown" / "tear down" / "knock down", "build your dream home", "land value" / "value is in the land" / "sold for
   land", "subdivide", "buildable", "two lots", and the weaker "oversized lot" and "R-2 zone". "Builder's warranty",
   "built by XYZ Builders", "knock down a wall", "no teardown needed", "not buildable" and new-construction listings
   never count; and
2. the MOD-IV tax record behind the listing: the land carries `min_land_share` (60%) or more of the assessment
   (assessed land / (land + improvements)) on a house built in `max_year_built` (1960) or earlier, when the listing
   doesn't describe a renovated, move-in-ready house.

Only live listings count: an active harvested listing (for sale / coming soon, seen in the last
`max_listing_age_days`) whose listing signal has not been retired (`scoring.listing_off_market`). Condo / townhouse /
co-op / mobile units, farms and non-residential parcels are left out, and a parcel more than a mile from the listing's
own point is the wrong parcel (its tax facts are not used).

The product: one weekly landscape sheet per county with 3+ rows ("NJ Teardown and Lot Listings: Union County",
s13_teardowns_<county>_<date>.pdf) and one all-county sheet with every row (s13_teardowns_all_counties_<date>.pdf),
best rows first, as many pages as they need: street and town, land area, land share of the assessment, year built,
living area, list price, days listed, the listing's own words (12 or fewer, around the matched phrase, with any street
address cut out) and the listing brokerage (a business, never an agent's name or phone). Lots listed one by one under
one description appear once. A builder buys one county ($249 a month) or all of them ($499 a month), flat.

The 2-step funnel: Touch 1 is two plain sentences with no attachment, link or price: one street from the builder's
county sheet (else the all-county sheet, led by a street from their county or a neighboring one), what its listing
says, its lot size, the true count of rows on that sheet and "Want this week's <County> sheet, free?". Their yes
releases the held sample: the sheet's first page with that street on top (rows 1-2 in full; the street, the brokerage
and the street's name inside a quote hidden after that; one page, because the outbox's nudge calls it "one page"), and
the flat prices. The outbox sends the in-thread nudge and close-the-loop note by itself; this stream proposes Touch 1
only.

Why it stays legal for an unlicensed operator: public listing text, public tax records and arithmetic for a flat
fee; no opinion of value (N.J.S.A. 45:14F: the land share is the town's own assessment split, printed as a ratio), no
pay tied to a purchase (N.J.S.A. 45:15-3), and zoning, subdivision and demolition approvals are the buyer's to confirm
with the town. No owner names; every address passes the Daniel's Law / stop suppression list (`address_ok`).
"""
from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from xml.sax.saxutils import escape

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from core.text import clean, muni_base, normalize_street, postal_title, street_key
from db.models import Contact, ContactType, DistressSignal, FirmTarget, Lead, Listing, Property, SignalType, utcnow
from engine.streams.base import (
    PRIORITY_SPECIALTY,
    SAMPLE_FULL_ROWS,
    Outbound,
    Stream,
    StreamContext,
    StreamResult,
    address_ok,
    money,
    operator_footer,
    render_table_pdf,
    sample_path,
    slug,
)
from engine.streams.s4_contractor_leads import brokerage_label, county_key, eligible_targets, phrase_words

TARGET_KIND = "builder_developer"
ALL = "ALL"                                   # the all-county sheet's scope key
DEFAULTS: dict[str, Any] = {
    "price_monthly": 249,                     # one county, a month, flat
    "price_all": 499,                         # every county the stream covers, a month, flat
    "pitches_per_run": 2,
    "report_rows": 40,                        # rows on a county sheet (best first)
    "all_rows": 400,                          # rows on the all-county sheet (every row found, best first)
    "min_rows": 3,                            # a county sheet with fewer rows isn't written (its rows stay on the
                                              # all-county sheet)
    "min_land_share": 0.6,
    "max_year_built": 1960,
    "max_listing_age_days": 30,               # a listing the harvest hasn't seen in this long is not called live
    "new_build_years": 3,                     # built in the last 3 years: a new house, not a teardown
    "max_lot_acres": 40,                      # a calculated acreage above this is a whole-complex parcel: not shown
    "max_listings": 30000,
}
ACTIVE_STATUSES = ("for_sale", "active", "coming_soon")
OK_TYPES = {"", "single_family", "multi_family", "duplex_triplex", "land"}
OK_CLASSES = {"", "1", "2"}                   # MOD-IV vacant land and residential
MAX_PARCEL_MILES = 1.0
NUMBER_WORDS = {2: "two", 3: "three", 4: "four", 5: "five", 6: "six", 7: "seven", 8: "eight", 9: "nine", 10: "ten",
                11: "eleven", 12: "twelve"}
# A street from a nearby county leads the all-county pitch for a builder whose own county has no sheet.
NEIGHBORS = {
    "Ocean": ("Monmouth", "Middlesex", "Mercer"),
    "Morris": ("Essex", "Passaic", "Union", "Bergen"),
    "Somerset": ("Middlesex", "Union", "Mercer"),
    "Hunterdon": ("Mercer", "Middlesex"),
    "Sussex": ("Passaic", "Bergen"),
    "Warren": ("Passaic", "Mercer"),
    "Burlington": ("Camden", "Mercer"),
    "Gloucester": ("Camden",),
    "Atlantic": ("Camden",),
    "Union": ("Essex", "Middlesex"),
    "Essex": ("Union", "Hudson", "Passaic", "Bergen"),
    "Bergen": ("Passaic", "Hudson", "Essex"),
    "Hudson": ("Essex", "Bergen"),
    "Passaic": ("Bergen", "Essex"),
    "Middlesex": ("Union", "Monmouth", "Mercer"),
    "Monmouth": ("Middlesex", "Mercer"),
    "Mercer": ("Middlesex", "Monmouth", "Camden"),
    "Camden": ("Mercer",),
}

# -- the listing-text classifier (pure) ---------------------------------------------------------------------------
BUILDER, TEARDOWN, DREAM, LAND_VALUE, SUBDIVIDE, BUILDABLE, TWO_LOTS, OVERSIZED, ZONE = (
    "BUILDER", "TEARDOWN", "DREAM", "LAND_VALUE", "SUBDIVIDE", "BUILDABLE", "TWO_LOTS", "OVERSIZED", "ZONE")
# the phrase a Touch-1 quote prefers, strongest first
ORDER = (TEARDOWN, LAND_VALUE, BUILDER, SUBDIVIDE, TWO_LOTS, BUILDABLE, DREAM, OVERSIZED, ZONE)
STRONG = {BUILDER, TEARDOWN, DREAM, LAND_VALUE, SUBDIVIDE, BUILDABLE, TWO_LOTS}
WEAK = {OVERSIZED, ZONE}
# words that stand even in a listing that reads as a renovated, move-in-ready house (it says teardown, addresses
# builders, prices the land or offers a lot to split off); the rest count only when it doesn't
RETAIL_PROOF = {TEARDOWN, BUILDER, LAND_VALUE, SUBDIVIDE, BUILDABLE}

_NEG = re.compile(r"(?:\b(?:no|not|never|without|none|nor|zero|cannot|non)\b|n't\b)", re.I)
_DONE_BEFORE = re.compile(r"\b(?:after|following|prior|previous(?:ly)?|former(?:ly)?|completed|finished)\b", re.I)
_CONJ = re.compile(r"\b(?:and|but|or|while|however|although|though|plus|also)\b", re.I)
_CLAUSE_START = re.compile(r"[.;:!?\n,(•|]")
_SENTENCE_END = re.compile(r"[.;!?\n•|]")
_AUD = r"(?:investors?|developers?|contractors?|rehabbers?|flippers?)"
_JOIN = r"\s*(?:,|/|&|\+|\band\b|\bor\b)\s*"


@dataclass(frozen=True)
class Rule:
    kind: str
    pattern: re.Pattern[str]
    before: re.Pattern[str] | None = None     # not a hit when this sits just before the match (inside its clause)
    after: re.Pattern[str] | None = None      # ... or right after it


def _r(kind: str, pattern: str, *, before: str | None = None, after: str | None = None) -> Rule:
    return Rule(kind, re.compile(pattern, re.I), re.compile(before, re.I) if before else None,
                re.compile(after, re.I) if after else None)


# "builder's warranty", "Builders Design", "builder is available to construct your home": a builder's product, not a
# listing that addresses builders
_NOT_AUDIENCE = (r"^\W{0,3}(?:'s\b\W*)?(?:warrant\w*|grade|model|upgrades?|finish\w*|design\w*|spec\b|incentives?|"
                 r"is available|will build|can build|to build|of choice|built)")
RULES: tuple[Rule, ...] = (
    # -- listings that address builders ("builder" alone is everywhere in new-construction ads: never enough) --
    _r(BUILDER, r"\bbuilders?(?:'s|s')?\s+(?:opportunit(?:y|ies)|special|lot|dream|delight|paradise|alert)\b",
       after=r"^\s*(?:kitchen|bath\w*|closet|grade|model)\b"),
    _r(BUILDER, r"\b(?:attention|calling all|paging)\s+(?:all\s+)?(?:" + _AUD + _JOIN + r")?builders?\b",
       after=_NOT_AUDIENCE),
    _r(BUILDER, r"\b(?:ideal|perfect|great|excellent|good|suited|perfectly suited|opportunity)\s+for\s+"
                r"(?:an?\s+|the\s+)?(?:" + _AUD + _JOIN + r")?(?:" + _AUD + _JOIN + r")?builders?\b",
       after=_NOT_AUDIENCE),
    _r(BUILDER, r"\b" + _AUD + _JOIN + r"(?:(?:an?\s+)?" + _AUD + _JOIN + r")?(?:an?\s+)?builders?\b",
       after=_NOT_AUDIENCE),
    _r(BUILDER, r"\bbuilders?" + _JOIN + r"(?:an?\s+)?(?:" + _AUD + r"|end[- ]users?|buyers?|homeowners?)\b",
       before=r"\b(?:by|from|with|renowned|respected|reputable|award[- ]winning|local|custom|experienced|trusted|"
              r"quality|premier|master|licensed|our|your own|the)\s*$"),
    _r(BUILDER, r"\bbuilders?\s+(?:looking|searching|seeking|wanting)\b",
       before=r"\b(?:by|renowned|respected|our|the)\s*$"),
    _r(BUILDER, r"\bbring\s+(?:your\s+)?(?:own\s+)?builders?\b", after=_NOT_AUDIENCE),
    # -- teardowns ("knock down a wall" is a floor plan) --
    _r(TEARDOWN, r"\btear[- ]?downs?\b|\bknock[- ]?downs?\b|\bto be (?:torn|knocked) down\b",
       after=r"^\s+(?:an?\s+|the\s+|some\s+|one\s+|two\s+|interior\s+|non-?load[- ]bearing\s+)?"
             r"(?:walls?|partitions?)\b"),
    # -- build new --
    # ("build new memories" is a retail line: "new" counts only before home / house / construction)
    _r(DREAM, r"\bbuild\s+(?:your|ur|you'?re|the|a)\s+(?:own\s+)?(?:new\s+|custom\s+|future\s+|forever\s+|perfect\s+|"
              r"luxury\s+)?(?:dream\s+)?(?:home|house|estate)\b|\bbuild\s+(?:a\s+)?new\s+(?:home|house|construction)\b|"
              r"\bbuild\s+the\s+(?:home|house)\s+of\s+your\s+dreams\b|\bbuild\s+your\s+dreams?\b(?!\s+(?:kitchen|bath\w*|"
              r"closet|garden|deck|patio|basement|backyard|yard|pool|office|space|room|career|business|life|family)\b)"),
    # -- the land is the point ("rare land value, this turnkey gem" is marketing: the price has to be the land's) --
    _r(LAND_VALUE, r"\bvalue\s+(?:is\s+)?in\s+the\s+(?:land|lot)\b|\b(?:land|lot)\s+value\s+only\b|\bland\s+only\b|"
                   r"\b(?:priced|sold|selling|offered|listed)\s+(?:at|for|below|near|as)\s+(?:the\s+)?(?:land|lot)"
                   r"(?:\s+value)?\b|\b(?:for|at)\s+(?:the\s+)?(?:land|lot)\s+value\b|\b(?:land|lot)\s+value\s+"
                   r"(?:price|pricing|priced)\b"),
    # -- subdivision potential ("the area was subdivided into the North End" is history; "in the Oaks subdivision" a
    # neighborhood) --
    _r(SUBDIVIDE, r"\bsub-?dividable\b|\bsub-?divide\b|\b(?:can|could|may|might)\s+(?:possibly\s+|potentially\s+)?"
                  r"be\s+sub-?divided\b|\b(?:already|been|recently)\s+sub-?divided\b|\b(?:potential|possible|"
                  r"possibility|option|room|ability|minor|major|preliminary|approved)\s+(?:to\s+|for\s+(?:a\s+)?|of\s+"
                  r"(?:a\s+)?)?sub-?divi(?:de|sion)\b|\bsub-?division\s+(?:potential|possible|approval|approved|"
                  r"opportunit\w*)\b",
       before=r"\b(?:cannot|can't|not|never)\s+(?:be\s+)?$|\b(?:area|neighborhood|section|tract|town|community|"
              r"development|farm|estate)\b[^.;]*$"),
    _r(BUILDABLE, r"\bbuild-?able(?:\s+(?:lot|land|parcel|acreage|site))?\b", before=r"(?:non|un)-?\s*$"),
    _r(TWO_LOTS, r"\b(?:two|2|three|3)\s+(?:separate\s+|adjacent\s+|adjoining\s+|deeded\s+|tax\s+|buildable\s+|"
                 r"building\s+)?(?:lots|parcels)\b|\bdouble\s+lot\b|\b(?:lots|parcels)\s+(?:being\s+)?sold\s+together\b",
       after=r"^\s+(?:from|away|down|over|off|up)\b"),
    # -- weaker words --
    _r(OVERSIZED, r"\b(?:oversized?|over-sized|extra[- ]large|extra[- ]deep)\s+(?:corner\s+)?"
                  r"(?:lot|parcel|property)\b"),
    _r(ZONE, r"\b(?:zoned\s+(?:for\s+)?)?R-?\d{1,2}[A-Z]?(?:-\d)?\s+(?:zon(?:e|ed|ing)|district)\b|"
             r"\bzoned\s+(?:for\s+)?R-?\d{1,2}[A-Z]?\b"),
)
# a new house (or one to be built): never a teardown unless the listing itself says teardown / land value
NEW_BUILD = re.compile(r"\b(?:new(?:er)? construction|newly[- ](?:built|constructed)|newer[- ]built|brand[- ]new "
                       r"(?:construction|home|build)|to be built|under construction|construction phase|"
                       r"pre-?construction|proposed construction|builder'?s?'? warranty|(?:10|ten)[- ]year "
                       r"(?:builder'?s? )?warranty|estimated (?:delivery|completion)|new build|built in "
                       r"20(?:1[5-9]|[2-9]\d))\b", re.I)
# noun-like phrases read as "<street> is listed as "<phrase>""; anything else as "the listing for <street> says ..."
_NOUN = re.compile(r"^(?:builders?(?:'s|s')?\s+(?:opportunit\w*|special|lot|dream|delight|paradise)|tear[- ]?downs?|"
                   r"knock[- ]?downs?|build-?able(?:\s+(?:lot|land|parcel|acreage|site))?|sub-?dividable|"
                   r"(?:land|lot)\s+value(?:\s+only)?|land only|(?:two|2|three|3)\s+(?:\w+\s+)?(?:lots|parcels)|"
                   r"double lot|(?:oversized?|over-sized|extra[- ]large|extra[- ]deep)\s+(?:corner\s+)?(?:lot|parcel|"
                   r"property)|(?:zoned\s+(?:for\s+)?)?R-?\d.*)$", re.I)
# a builder phrase that reads well inside quotes ("ideal for a builder", "attention builders"); a list of buyers
# ("investors and builders", "builder searching") is said in our words instead (`quote_mode`)
_CLEAN_AUDIENCE = re.compile(r"^(?:(?:ideal|perfect|great|excellent|good|suited|perfectly suited) for (?:an? |the )?"
                             r"builders?|(?:attention|calling all|paging) (?:all )?builders?|bring (?:your )?(?:own )?"
                             r"builders?)$", re.I)
_ZONE_CODE = re.compile(r"\br-?\d{1,2}[a-z]?(?:-\d)?\b", re.I)


def _before(text: str, start: int, words: int = 4) -> str:
    """The few words just before a match, inside its clause and after the last conjunction."""
    head = text[:start]
    cut = max((m.end() for m in _CLAUSE_START.finditer(head)), default=0)
    head = head[cut:]
    conj = list(_CONJ.finditer(head))
    if conj:
        head = head[conj[-1].end():]
    return " ".join(head.split()[-words:])


def _after(text: str, end: int, chars: int = 60) -> str:
    tail = text[end:end + chars]
    m = _SENTENCE_END.search(tail)
    return tail[:m.start()] if m else tail


def _valid(rule: Rule, text: str, m: re.Match[str]) -> bool:
    raw_before = text[max(0, m.start() - 40):m.start()]
    before, after = _before(text, m.start()), _after(text, m.end())
    if _NEG.search(before) or _DONE_BEFORE.search(before):
        return False                                   # "no teardown", "not buildable", "after the knockdown"
    if rule.before is not None and (rule.before.search(raw_before) or rule.before.search(before)):
        return False
    return not (rule.after is not None and rule.after.search(after))


@dataclass(frozen=True)
class Hit:
    kind: str
    phrase: str                                # the matched words exactly as the listing wrote them
    start: int
    end: int


def normalize(text: str | None) -> str:
    """One space between words, straight apostrophes (match offsets refer to this text)."""
    return clean((text or "").replace("\u2019", "'").replace("\u2018", "'"))


def find_hits(text: str | None) -> dict[str, Hit]:
    """Kind -> the first valid match of that kind in the listing text. Pure, so every negation has a unit test."""
    t = normalize(text)
    if not t:
        return {}
    hits: dict[str, Hit] = {}
    for rule in RULES:
        for m in rule.pattern.finditer(t):
            if _valid(rule, t, m):
                if rule.kind not in hits or m.start() < hits[rule.kind].start:
                    hits[rule.kind] = Hit(rule.kind, m.group(0), m.start(), m.end())
                break
    return {k: hits[k] for k in ORDER if k in hits}


def quote_text(phrase: str) -> str:
    """The matched words as a quote: one space between words, straight quotes, lower case (zone codes stay "R-2")."""
    q = " ".join(phrase.replace('"', "'").split()).strip(" ,;:-")
    q = q.lower()
    return _ZONE_CODE.sub(lambda m: m.group(0).upper(), q)


def noun_phrase(quote: str) -> bool:
    return bool(_NOUN.match(quote))


def new_build(text: str | None) -> bool:
    return bool(text and NEW_BUILD.search(text))


_AS_IS = re.compile(r"(?i)^(?:sold\s+)?as[- ]is$")


def retail(text: str | None) -> bool:
    """The listing reads as a renovated / move-in-ready house: the firm's own words (offer_math.RETAIL) with no
    distress word (offer_math.DISTRESS) other than "sold as-is", which NJ listings print on renovated houses too."""
    from engine.offer_math import DISTRESS, RETAIL
    if not text or not RETAIL.search(text):
        return False
    return all(_AS_IS.match(m.group(0)) for m in DISTRESS.finditer(text))


_SUFFIX = (r"(?:St|Street|Ave|Avenue|Rd|Road|Dr|Drive|Ln|Lane|Ct|Court|Pl|Place|Blvd|Boulevard|Way|Ter|Terrace|Pkwy|"
           r"Parkway|Hwy|Highway|Cir|Circle|Sq|Square|Row|Trl|Trail|Pike|Tpke|Turnpike)")
_ADDR_IN_TEXT = re.compile(r"\(?\b\d+[A-Za-z]?(?:\s*(?:&|and|-|,|/)\s*\d+[A-Za-z]?)*\s+(?:[NSEW]\.?\s+)?"
                           r"(?:[A-Z][A-Za-z0-9'.-]*\s+){1,3}(?i:" + _SUFFIX + r")\b\.?\)?")
_BLOCK_LOT = re.compile(r"\(?\bblock\s+[\w.-]+\s*,?\s*(?:and\s+)?lots?\s+[\d.]+(?:\s*(?:&|and|,)\s*[\d.]+)*\)?", re.I)


# A sentence ends at ".", ";", "!" or "?", never at a decimal point (".79 acre" is not "79 acre").
_END = re.compile(r"(?<!\d)\.(?!\d)|\.(?=\s|$)|[;!?\n\u2022|]")
_TOKEN = re.compile(r"\S+")
_LINK_TOKEN = re.compile(r"(@|https?:|^\W*www\b|\.(?:com|net|org)\b)", re.I)
_PHONE = re.compile(r"\(?\b\d{3}\)?[\s.-]*\d{3}[\s.-]*\d{4}\b")
_CUE = {"call", "contact", "text", "email", "e-mail", "agent", "agents", "showing", "showings", "appointment",
        "appointments", "listing", "realtor"}


def excerpt(text: str, start: int, end: int, max_words: int = 12) -> str:
    """At most `max_words` of the listing's own words around a match, inside its sentence (s4's rule, with decimal
    points kept inside the sentence). Phone numbers, e-mails and links are dropped, and the window stops before
    "call / contact / agent ..." so a name after them never comes along. "..." marks a cut."""
    a = max((m.end() for m in _END.finditer(text, 0, start)), default=0)
    m_end = _END.search(text, end)
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
    phones = [(m.start(), m.end()) for m in _PHONE.finditer(text, a, b)]
    words = [w for s0, e0, w in toks[lo:hi]
             if not _LINK_TOKEN.search(w) and not any(s0 < pe and e0 > ps for ps, pe in phones)]
    phrase = " ".join(words).replace('"', "'").strip(" ,;:-(")
    if not phrase:
        return ""
    return ("..." if lo > 0 else "") + phrase + ("..." if hi < len(toks) else "")


def scrub(phrase: str) -> str:
    """No street address and no block / lot number in a quoted phrase (the sample hides the street; a quote must not
    give it back)."""
    p = _BLOCK_LOT.sub(" ", _ADDR_IN_TEXT.sub(" ", phrase or ""))
    p = re.sub(r"\(\s*\)", " ", p)
    return re.sub(r"\s+([,.;:!?])", r"\1", " ".join(p.split())).strip(" ,;:-")


# -- facts --------------------------------------------------------------------------------------------------------
def land_share(prop: Any) -> float | None:
    """Assessed land / (land + improvements) from the town's tax list (MOD-IV LAND_VAL / IMPRVT_VAL), else land /
    total. A ratio of the town's own figures, never an opinion of value."""
    land = getattr(prop, "assessed_land", None)
    imp = getattr(prop, "assessed_improvement", None)
    total = getattr(prop, "assessed_total", None)
    try:
        land = float(land) if land is not None else None
        base = float(land + float(imp)) if (land is not None and imp is not None) else (
            float(total) if total is not None else None)
    except (TypeError, ValueError):
        return None
    if not land or land <= 0 or not base or base <= 0 or land > base * 1.001:
        return None
    return min(1.0, land / base)


def lot_words(acres: float | None) -> str:
    """'0.31-acre' / '1.2-acre' / '4,400-square-foot' for "on a <x> lot"."""
    if not acres or acres <= 0:
        return ""
    if acres >= 10:
        return f"{acres:,.0f}-acre"
    if acres >= 1:
        return f"{acres:.1f}".rstrip("0").rstrip(".") + "-acre"
    if acres >= 0.1:
        return f"{acres:.2f}-acre"
    return f"{round(acres * 43560 / 100) * 100:,.0f}-square-foot"


def lot_cell(acres: float | None) -> str:
    if not acres or acres <= 0:
        return "-"
    sqft = acres * 43560
    if acres >= 1:
        return f"{acres:.2f} acres"
    return f"{acres:.2f} ac ({round(sqft / 10) * 10:,.0f} sq ft)"


def number_word(n: int) -> str:
    return NUMBER_WORDS.get(n, str(n))


# -- rows ---------------------------------------------------------------------------------------------------------
@dataclass
class Row:
    county: str
    town: str
    street: str
    zip_code: str = ""
    acres: float | None = None
    share: float | None = None
    year: int | None = None
    sqft: int | None = None
    price: float | None = None
    days: int | None = None
    words: str = ""                # the PDF's quote: <= 12 of the listing's words around the match
    quote: str = ""                # Touch 1's quote: the matched phrase
    quote_kind: str = ""           # the rule kind that matched it
    kinds: list[str] = field(default_factory=list)
    brokerage: str = ""
    land: bool = False             # a vacant-land listing
    by_words: bool = False
    by_share: bool = False
    rank: float = 0.0

    def cells(self, all_county: bool = False) -> list[str]:
        town = f"{self.town} ({self.county})" if all_county else self.town
        if self.words:
            said = f'"{escape(self.words)}"'
        else:
            said = "(tax-record match: no listing words)"
        built = str(self.year) if self.year else ("vacant land" if self.land else "-")
        return [f"<b>{escape(self.street)}</b><br/>{escape(town)}", lot_cell(self.acres),
                f"{self.share:.0%}" if self.share is not None else "-", built,
                f"{self.sqft:,} sq ft" if self.sqft else "-", money(self.price),
                str(self.days) if self.days is not None else "-", said, escape(self.brokerage) or "-"]


def row_rank(r: Row, max_year: int = 1960) -> float:
    """Best first: listing words that name a teardown or lot (3) or a weaker word (1), the tax-record match (2), the
    land share itself, an older house, up to an acre of land and time on the market."""
    strong = any(k in STRONG for k in r.kinds)
    weak = any(k in WEAK for k in r.kinds)
    return round((3.0 if strong else 0.0) + (1.0 if weak and not strong else 0.0) + (2.0 if r.by_share else 0.0)
                 + (r.share or 0.0) + (1.0 if r.year and r.year <= max_year else 0.0) + min(r.acres or 0.0, 1.0) * 1.5
                 + min(int(r.days or 0), 365) / 365 * 0.5, 4)


@dataclass
class Scan:
    rows: list[Row] = field(default_factory=list)
    counts: Counter[str] = field(default_factory=Counter)


# Realtor.com's city names that aren't NJ town names
TOWN_FIX = {"ELIZABETH CITY": "Elizabeth", "DOWNTOWN JERSEY CITY": "Jersey City"}
_FEED_JUNK = re.compile(r"(?i)\s*\b(?:do\s*n'?t|do not)\s+re-?open\b.*$")


def town_name(city: str | None) -> str:
    c = postal_title(city)
    return TOWN_FIX.get(c.upper(), c)


def brokerage(name: str | None) -> str:
    """The listing brokerage as a business name (s4.brokerage_label), without a feed's internal notes ("... DONT
    REOPEN")."""
    return brokerage_label(_FEED_JUNK.sub("", clean(name or "")))


def _zip_counties(session: Session) -> dict[str, str]:
    """ZIP -> the county most of our parcels in that ZIP sit in (a listing with no parcel gets its county from it)."""
    votes: dict[str, Counter[str]] = {}
    for z, county, n in session.execute(select(func.substr(Property.zip_code, 1, 5), Property.county, func.count())
                                        .where(Property.zip_code.is_not(None), Property.county.is_not(None))
                                        .group_by(func.substr(Property.zip_code, 1, 5), Property.county)):
        c = county_key(county)
        if z and c and c.lower() != "unknown":
            votes.setdefault(str(z), Counter())[c] += int(n)
    return {z: v.most_common(1)[0][0] for z, v in votes.items()}


def same_listing_text(a: str, b: str, threshold: float = 0.9) -> bool:
    """Two descriptions that differ only in a few lot numbers or street names: one listing posted lot by lot."""
    from difflib import SequenceMatcher
    a, b = re.sub(r"\d+", "#", a.lower()), re.sub(r"\d+", "#", b.lower())
    if a == b:
        return True
    m = SequenceMatcher(None, a, b, autojunk=False)
    return m.quick_ratio() >= threshold and m.ratio() >= threshold


def _addr_key(street: str | None, zip_code: str | None) -> tuple[str, str]:
    return normalize_street(street), str(zip_code or "")[:5]


def _target_counties(lim: dict[str, Any]) -> list[str]:
    from core.config import settings
    raw = lim.get("counties") or (settings().get("markets") or {}).get("counties") or []
    out: list[str] = []
    for c in raw:
        k = county_key(str(c))
        if k and k not in out:
            out.append(k)
    return out


def collect(session: Session, today: date, lim: dict[str, Any]) -> Scan:
    """Every live listing in the target counties that reads as a teardown or lot, or whose tax record shows the land
    carrying most of an older house's assessment."""
    from core.geo import haversine_miles
    from engine.scoring import listing_off_market
    scan = Scan()
    counties = set(_target_counties(lim))
    since = utcnow() - timedelta(days=float(lim["max_listing_age_days"]))
    listings = list(session.scalars(select(Listing).where(
        or_(Listing.status.in_(ACTIVE_STATUSES), Listing.status.is_(None)), Listing.last_seen_at >= since)
        .order_by(Listing.last_seen_at.desc()).limit(int(lim["max_listings"]))))
    if not listings:
        return scan
    # the parcel behind each listing: its listing signal (by source_ref), a direct link, else the same street + ZIP
    signals: dict[str, list[DistressSignal]] = {}
    refs = sorted({x.source_ref for x in listings if x.source_ref})
    for i in range(0, len(refs), 500):
        for sig in session.scalars(select(DistressSignal).where(
                DistressSignal.signal_type.in_((SignalType.STALE_LISTING, SignalType.ACTIVE_LISTING)),
                DistressSignal.source_ref.in_(refs[i:i + 500]))):
            signals.setdefault(sig.source_ref, []).append(sig)
    zips = sorted({str(x.zip_code or "")[:5] for x in listings if x.zip_code})
    by_addr: dict[tuple[str, str], int] = {}
    for i in range(0, len(zips), 200):
        for pid, street, z in session.execute(select(Property.id, Property.street_address, Property.zip_code).where(
                func.substr(Property.zip_code, 1, 5).in_(zips[i:i + 200]), Property.street_address.is_not(None))):
            by_addr.setdefault(_addr_key(street, z), pid)
    zip_county: dict[str, str] | None = None
    seen: set[tuple[str, str]] = set()
    texts: dict[tuple[str, float | None], list[str]] = {}
    for lst in listings:
        scan.counts["listings_seen"] += 1
        ptype = (lst.property_type or "").lower().strip()
        if ptype not in OK_TYPES:
            scan.counts["skipped_type"] += 1               # condo / townhouse / co-op / mobile / farm
            continue
        sigs = signals.get(lst.source_ref or "", [])
        if sigs and not any(s.active for s in sigs):
            scan.counts["skipped_off_market"] += 1         # this listing's signal was retired: it left the market
            continue
        live = [s for s in sigs if s.active]
        prop: Property | None = live[0].property if live else None
        by_address = False
        if prop is None and lst.property_id:
            prop = session.get(Property, lst.property_id)
        if prop is None:
            pid = by_addr.get(_addr_key(lst.address, lst.zip_code))
            prop, by_address = (session.get(Property, pid), True) if pid else (None, False)
        if prop is not None and not by_address and listing_off_market(prop):
            # the parcel's listing signals were all retired (pending / sold / missing from the harvests). A parcel
            # found only by its street keeps this newer listing: its retired signals may be an older listing's.
            scan.counts["skipped_off_market"] += 1
            continue
        if prop is not None and None not in (lst.lat, lst.lon, prop.lat, prop.lon) and \
                haversine_miles(float(prop.lat), float(prop.lon), float(lst.lat), float(lst.lon)) > MAX_PARCEL_MILES:
            scan.counts["wrong_parcel"] += 1               # linked to another house: its tax facts aren't this one's
            prop = None
        if prop is not None:
            cls = str(prop.property_class or "").strip().upper()
            qual = str(prop.qual or "").strip().upper()
            if cls not in OK_CLASSES or qual.startswith("C") or "COND" in str(prop.building_desc or "").upper():
                scan.counts["skipped_type"] += 1           # non-residential parcel or a condo unit
                continue
        county = county_key(prop.county) if prop is not None and prop.county else ""
        if not county or county.lower() == "unknown":
            if zip_county is None:
                zip_county = _zip_counties(session)
            county = zip_county.get(str(lst.zip_code or "")[:5], "")
        if not county:
            scan.counts["no_county"] += 1
            continue
        if counties and county not in counties:
            continue
        street = postal_title(lst.address) or (postal_title(prop.street_address) if prop is not None else "")
        town = town_name(lst.city) or (postal_title(muni_base(prop.municipality)) if prop is not None else "")
        if not street or not town:
            continue
        key = (street_key(street) or normalize_street(street), town.upper())
        if key in seen:
            scan.counts["skipped_duplicate"] += 1          # one row per house ("12 3rd St" = "12 W 3rd St"):
            continue                                       # its newest listing decides
        seen.add(key)
        text = normalize(lst.description)
        hits = find_hits(text)
        is_land = ptype == "land" or (prop is not None and str(prop.property_class or "").strip() == "1")
        year = (prop.year_built if prop is not None else None) or lst.year_built
        if not is_land and (new_build(text) or (year and year >= today.year - int(lim["new_build_years"]))) and \
                not ({TEARDOWN, LAND_VALUE} & set(hits)):
            scan.counts["skipped_new_build"] += 1
            continue
        is_retail = retail(text)
        kinds = [k for k in hits if k in RETAIL_PROOF or not is_retail]
        by_words = bool(kinds)
        share = land_share(prop) if prop is not None else None
        by_share = bool(not is_land and not is_retail and prop is not None and
                        str(prop.property_class or "").strip() in ("", "2") and share is not None and
                        share >= float(lim["min_land_share"]) and year and year <= int(lim["max_year_built"]))
        if not (by_words or by_share):
            if hits and is_retail:
                scan.counts["skipped_retail"] += 1
            continue
        listed = SimpleNamespace(street_address=lst.address or street, city=lst.city or town,
                                 zip_code=lst.zip_code or (prop.zip_code if prop is not None else ""))
        if (prop is not None and not address_ok(session, prop)) or not address_ok(session, listed):
            scan.counts["skipped_suppressed"] += 1
            continue
        siblings = texts.setdefault((town.upper(), float(lst.list_price) if lst.list_price else None), [])
        if len(text) >= 80 and any(same_listing_text(text, t) for t in siblings):
            scan.counts["skipped_duplicate"] += 1          # a package of lots listed one by one under one description
            continue                                       # (only the lot numbers differ): one row
        if len(text) >= 80:
            siblings.append(text)
        acres = float(prop.lot_size) if prop is not None and prop.lot_size else None
        if not acres:
            lot_sqft = (lst.raw_json or {}).get("lot_sqft") if isinstance(lst.raw_json, dict) else None
            try:
                acres = float(lot_sqft) / 43560 if lot_sqft else None
            except (TypeError, ValueError):
                acres = None
        if acres is not None and not (0 < acres <= float(lim["max_lot_acres"])):
            acres = None
        meta: dict[str, Any] = {}
        for s in live:
            meta = {**(s.raw_metadata_json or {}), **meta}
        days = max(0, (today - lst.list_date).days) if lst.list_date else (
            lst.days_on_market if lst.days_on_market is not None else meta.get("days_on_market"))
        price = lst.list_price or meta.get("list_price")
        broker = brokerage(meta.get("listing_broker"))
        if not broker and prop is not None:
            lead_id = session.scalar(select(Lead.id).where(Lead.property_id == prop.id))
            if lead_id is not None:
                for (company,) in session.execute(select(Contact.company).where(
                        Contact.lead_id == lead_id, Contact.contact_type == ContactType.LISTING_AGENT,
                        Contact.company.is_not(None))):
                    broker = brokerage(company)
                    if broker:
                        break
        best = next((hits[k] for k in ORDER if k in kinds), None)
        words = quote = ""
        if best is not None:
            words = scrub(excerpt(text, best.start, best.end))
            quote = scrub(quote_text(best.phrase))
            if phrase_words(words) > 12:
                words = " ".join(words.replace("...", " ").split()[:12]) + "..."
        sqft = lst.sqft or (prop.sqft if prop is not None else None)
        row = Row(county=county, town=town, street=street, zip_code=str(lst.zip_code or "")[:5], acres=acres,
                  share=share, year=int(year) if year else None, sqft=int(sqft) if sqft else None,
                  price=float(price) if price else None, days=int(days) if days is not None else None, words=words,
                  quote=quote, quote_kind=best.kind if best is not None else "", kinds=kinds, brokerage=broker,
                  land=is_land, by_words=by_words, by_share=by_share)
        row.rank = row_rank(row, int(lim["max_year_built"]))
        scan.counts["word_hits"] += int(by_words)
        scan.counts["land_share_hits"] += int(by_share)
        for k in kinds:
            scan.counts[f"word_{k.lower()}"] += 1
        scan.rows.append(row)
    scan.rows.sort(key=lambda r: (-r.rank, r.county, r.town, r.street))
    return scan


# -- sheets -------------------------------------------------------------------------------------------------------
NOTES = (
    ("Listing words are read by keyword rules that skip negations and new-construction ads (\"builder's "
     "warranty\", \"knock down a wall\", \"not buildable\" don't count); \"oversized lot\", \"two lots\", \"build "
     "your dream home\" and zoning codes count only when the listing doesn't describe a renovated, move-in-ready "
     "house. Quotes are the listing's own words, 12 or fewer, with any street address left out."),
    ("Land share = the town's assessed land value / (land + improvements) on the MOD-IV tax list: the assessor's "
     "split, not an opinion of value, and assessment practice varies by town. Land area is MOD-IV's calculated "
     "acreage (else the listing's stated lot size) and may differ from the deed or survey. Built and living area come "
     "from the tax record or the listing."),
    ("Only listings still for sale in the latest harvest; condo / townhouse units and non-residential parcels are "
     "left out, and lots listed one by one under one description appear once. No owner names; addresses under a "
     "Daniel's Law or stop request are left out. Contact the listing brokerage yourself; confirm zoning, setbacks, "
     "subdivision, demolition permits, utilities and environmental conditions with the town before relying on any "
     "row."),
)
TOP_LINE = ("Public-record and public-listing facts; not an appraisal, BPO or CMA; no opinion of value. Zoning, "
            "subdivision and demolition approvals are the buyer's to confirm with the town.")
HEAD = ["Property", "Land area", "Land share", "Built", "Living area", "List price", "Days listed",
        "The listing's own words", "Listing brokerage"]
WIDTHS = [1.75, 1.1, 0.55, 0.5, 0.7, 0.75, 0.5, 2.9, 1.35]
WORDS_COL = HEAD.index("The listing's own words")


@dataclass
class Sheet:
    """One sheet this week: the full PDF a subscriber gets (every row, pages as needed) and its rows in order. A
    pitch's sample is a one-page teaser of it (`sample_for`)."""
    scope: str                                 # a county name, or ALL
    path: Path
    rows: list[Row]


def price_words(lim: dict[str, Any]) -> tuple[str, str, str]:
    """('$249', '$499', ' plus NJ sales tax'): the flat prices and the sales-tax words
    (research_firm.sales_tax_line)."""
    from engine.firm_outbox import cfg as firm_cfg
    tax = " plus NJ sales tax" if firm_cfg().get("sales_tax_line", True) else ""
    return f"${float(lim['price_monthly']):,.0f}", f"${float(lim['price_all']):,.0f}", tax


_STREET_WORDS = {"st", "street", "ave", "avenue", "rd", "road", "dr", "drive", "ln", "lane", "ct", "court", "pl",
                 "place", "blvd", "boulevard", "way", "ter", "terrace", "pkwy", "parkway", "hwy", "highway", "cir",
                 "circle", "sq", "square", "row", "trl", "trail", "pike", "tpke", "turnpike", "and", "unit", "apt", "n",
                 "s", "e", "w", "north", "south", "east", "west", "the", "of", "route", "rt"}


# "127 Samplewood" in a quote is another parcel of the same sale: a number before a capitalized word is a street unless
# the word is a count of something
_NUMBERED = re.compile(r"\b\d+[A-Za-z]?(?:\s*(?:&|and|-)\s*\d+[A-Za-z]?)*\s+(?:[NSEW]\.?\s+)?([A-Z][a-z]+|[A-Z]{2,})\b")
_COUNT_WORDS = {"lot", "lots", "acre", "acres", "family", "families", "bed", "beds", "bedroom", "bedrooms", "bath",
                "baths", "bathroom", "bathrooms", "car", "cars", "story", "stories", "unit", "units", "sq", "sqft",
                "ft", "feet", "foot", "year", "years", "homes", "houses", "parcels", "buildings", "rooms", "room",
                "levels", "floors", "minutes", "miles", "blocks", "million", "thousand", "zone", "zoning", "x"}


def hide_street(words: str, street: str) -> str:
    """A sample row's quote without its own street's name or house number ("...141 Samplewood offers a rare ..." loses
    "141" and "Samplewood") or another numbered street ("included in sale 127 Oakview"): rows 3+ of a sample hide the
    street, so their quotes must not give it back."""
    out = _NUMBERED.sub(lambda m: m.group(0) if m.group(1).lower() in _COUNT_WORDS else " ", words)
    for tok in re.findall(r"[A-Za-z0-9'.-]+", street or ""):
        core = tok.strip(".'-")
        if not core or core.lower() in _STREET_WORDS or (core.isalpha() and len(core) < 3):
            continue
        out = re.sub(rf"(?i)(?<![\w-]){re.escape(core)}(?![\w-])", " ", out)
    return re.sub(r"\s+([,.;:!?])", r"\1", " ".join(out.split())).strip(" ,;:-") or "..."


def render_sheet(path: Path, scope: str, rows: list[Row], ctx: StreamContext, lim: dict[str, Any],
                 n_counties: int, *, sample: bool = True, total: int | None = None) -> Path:
    """The sheet (every row in full) and its sample (`sample_path`: rows 1-2 in full; rows 3+ with the street, the
    brokerage and the street's own name in the quote hidden), which Touch 2 sends after a yes. `total`: the rows of
    the whole sheet when this is its first page (a pitch's teaser)."""
    today = ctx.now.date()
    all_county = scope == ALL
    one, every, tax = price_words(lim)
    n_words = sum(1 for r in rows if r.by_words)
    n_share = sum(1 for r in rows if r.by_share)
    where = (f"the {number_word(n_counties)} counties covered" if all_county else f"{scope} County")
    if all_county and rows:
        where += f" (this week: {', '.join(sorted({r.county for r in rows}))})"
    title = "NJ Teardown and Lot Listings: " + ("All Counties" if all_county else f"{scope} County")
    plural = "" if len(rows) == 1 else "s"
    count = (f"first page: {len(rows)} of the {total} active listings" if total and total > len(rows) else
             f"{len(rows)} active listing{plural}")
    monday = today - timedelta(days=today.weekday())
    subtitle = (f"{TOP_LINE}<br/>Week of {monday:%B %-d, %Y} · {count} in {where}: "
                f"{n_words} whose public description reads as a teardown or a lot, {n_share} where the tax record "
                f"puts {float(lim['min_land_share']):.0%}+ of the assessment in the land on a house built "
                f"{int(lim['max_year_built'])} or earlier · Sources: public listing descriptions (Realtor.com), NJ "
                "MOD-IV tax records")
    notes = list(NOTES)
    box = (f"<b>Weekly sheet: {one}/month for one county or {every}/month for all {number_word(n_counties)}, "
           f"flat{tax}.</b> A new sheet every week, no commission and no per-deal fee. Reply \"start\" to the e-mail "
           "and I'll set it up.")
    kw = dict(title=title, subtitle=subtitle, head=HEAD, notes=notes, box=box, footer=operator_footer(ctx.operator),
              col_widths=WIDTHS)
    full = [r.cells(all_county) for r in rows]
    sample_path(path).unlink(missing_ok=True)          # never a stale sample from an earlier render
    if sample and len(rows) > SAMPLE_FULL_ROWS:
        teaser = [list(c) for c in full]
        for i, r in enumerate(rows):
            if i >= SAMPLE_FULL_ROWS and r.words:
                teaser[i][WORDS_COL] = f'"{escape(hide_street(r.words, r.street))}"'
        render_table_pdf(path, rows=teaser, **kw)      # the sample is written from these cells ...
        render_table_pdf(path, rows=full, write_sample=False, **kw)   # ... the sheet itself has every row in full
    else:
        render_table_pdf(path, rows=full, write_sample=sample, **kw)
    return path


def render_fit(path: Path, scope: str, rows: list[Row], ctx: StreamContext, lim: dict[str, Any], n_counties: int,
               total: int | None = None) -> list[Row]:
    """The most rows (best first) whose sheet and sample each fit one page (the in-thread nudge calls the sample "one
    page"), found by halving; the files left on disk are that render. Returns the rows kept."""
    from engine.streams.s4_contractor_leads import page_count
    rendered = 0

    def fits(n: int) -> bool:
        nonlocal rendered
        render_sheet(path, scope, rows[:n], ctx, lim, n_counties, total=total)
        rendered = n
        smp = sample_path(path)
        return page_count(path) <= 1 and (not smp.exists() or page_count(smp) <= 1)

    if len(rows) <= 1 or fits(len(rows)):
        return rows
    lo, hi = 1, len(rows)                      # one row always fits; all of them don't
    while hi - lo > 1:
        mid = (lo + hi) // 2
        lo, hi = (mid, hi) if fits(mid) else (lo, mid)
    if rendered != lo:
        fits(lo)
    return rows[:lo]


def sheet_name(scope: str, today: date) -> str:
    """'s13_teardowns_union_2026-11-10.pdf' (firm_outbox.delivery_files finds a subscriber's county in the name);
    the all-county sheet is 's13_teardowns_all_counties_<date>.pdf'."""
    tag = "all_counties" if scope == ALL else slug(scope).lower()
    return f"s13_teardowns_{tag}_{today:%Y-%m-%d}.pdf"


# -- the 2-step funnel --------------------------------------------------------------------------------------------
_ADDRESSABLE = re.compile(r"^[1-9]\d*[A-Za-z]?(?:-[1-9]\d*[A-Za-z]?)?\s+[A-Za-z0-9][A-Za-z0-9 .'-]*$")


def addressable(street: str) -> bool:
    """A street Touch 1 may name: a house number and a street ("12 Sample St", "141-143 Sample Ave"), never a lot with
    no number ("Sample Ave"), a unit, a package ("432 And 434 Sample St") or a placeholder ("0-SE 10th St")."""
    s = street or ""
    return bool(_ADDRESSABLE.match(s)) and not re.search(r"(?i)\b(?:and|unit|apt|lot|lots|rear|bldg)\b|#", s)


def _lead_group(r: Row) -> int:
    """0: a strong phrase quoted that passes the copy linter; 1: a strong phrase with a word the linter flags
    ("builder's opportunity": still the listing's own words); 2: a strong phrase said in our words (a list of buyers,
    "mentions subdividing"); 3: a weaker phrase ("oversized lot", "R-2 zone"); 4: a tax-record row."""
    from engine import human_copy as hc
    mode = quote_mode(r.quote, r.quote_kind)
    if not mode:
        return 4
    if r.quote_kind in WEAK:
        return 3
    if mode in ("noun", "says", "says_it"):
        return 0 if not hc.lint(r.quote) else 1
    return 2


def lead_row(rows: list[Row], prefer: tuple[str, ...] = ()) -> Row | None:
    """The row Touch 1 names: a quoted listing phrase first (a known lot size next), from the preferred counties when
    any has one; a tax-record row only when no row of the sheet has listing words. Only addressable streets."""
    def best(rs: list[Row]) -> Row | None:
        pool = [r for r in rs if addressable(r.street) and
                (r.quote or (r.by_share and r.year and r.share is not None))]
        if not pool:
            return None
        return min(pool, key=lambda r: (_lead_group(r), 0 if r.acres else 1, -r.rank))
    for county in prefer:
        hit = best([r for r in rows if r.county == county])
        if hit is not None:
            return hit
    return best(rows)


def _words(text: str) -> int:
    from engine import human_copy as hc
    return hc.word_count(text)


_SAYS_IT = re.compile(r"^(?:can|could|may|might)\b", re.I)
_MENTIONS = re.compile(r"^(?:sub-?divide|(?:(?:already|been|recently)\s+)+sub-?divided)$", re.I)


def quote_mode(quote: str, kind: str = "") -> str:
    """How Touch 1 carries a phrase:
    - "noun": '<street> in <town> is listed as "teardown"';
    - "says": 'The listing for <street> in <town> says "value is in the land"';
    - "says_it": '... says it "can be subdivided"';
    - "marketed": a listing that names builders among its buyers ("investors and builders", "builder searching"):
      '<street> in <town> is marketed to builders and investors', never a clipped quote;
    - "mentions": a bare "subdivide" / "already been subdivided": 'The listing for <street> in <town> mentions
      subdividing'."""
    if not quote:
        return ""
    if noun_phrase(quote):
        return "noun"
    if kind == BUILDER and not _CLEAN_AUDIENCE.match(quote):
        return "marketed"
    if kind == SUBDIVIDE and _MENTIONS.match(quote):
        return "mentions"
    if _SAYS_IT.match(quote):
        return "says_it"
    return "says"


def touch1(scope: str, row: Row, n: int, n_counties: int, greeting: str = "Hello,", first: str = "",
           max_words: int = 45) -> tuple[str, str]:
    """(subject, body): two plain sentences, no price, no link, no attachment. One street from the sheet, what its
    listing (or tax record) says, its lot size, the true count of rows on the sheet and a yes/no question. The
    longest version that fits `max_words` is used (the provenance drops first, then the lot size, never a fact)."""
    where = f"{scope} County" if scope != ALL else f"the {number_word(n_counties)} counties I cover"
    sheet = f"{scope} County sheet" if scope != ALL else "all-county sheet"
    lot = lot_words(row.acres)
    lot_in_quote = bool(re.search(r"\b(?:lots?|parcels?|acreage)\b", row.quote, re.I))
    tail = f" teardown and lot listings I found in {where} this week"
    src = " from the public listings and tax records"
    mode = quote_mode(row.quote, row.quote_kind)

    def first_sentence(with_lot: bool, with_src: bool) -> str:
        count = f"one of {n}{tail}{src if with_src else ''}"
        on = f" on a {lot} lot" if with_lot and lot and not lot_in_quote else ""
        if mode == "noun":
            return f'{row.street} in {row.town} is listed as "{row.quote}"{on}, {count}.'
        paren = f" (a {lot} lot)" if on else ""
        if mode in ("says", "says_it"):
            it = " it" if mode == "says_it" else ""
            return f'The listing for {row.street} in {row.town} says{it} "{row.quote}"{paren}, {count}.'
        if mode == "mentions":
            return f"The listing for {row.street} in {row.town} mentions subdividing{paren}, {count}."
        if mode == "marketed":
            who = "builders and investors" if re.search(r"(?i)\binvestors?\b", row.quote) else "builders"
            return f"{row.street} in {row.town} is marketed to {who}{on}, {count}."
        return (f"{row.street} in {row.town}, built in {row.year}{on} with the land at {row.share:.0%} of its tax "
                f"assessment, is {count}.")

    ask = f"Want this week's {sheet}, free?"
    body_text = ""
    for with_lot, with_src in ((True, True), (True, False), (False, True), (False, False)):
        body_text = f"{first_sentence(with_lot, with_src)} {ask}"
        if _words(body_text) <= max_words:
            break
    subject = f"[{row.town}] teardown lots"
    return subject, f"{greeting}\n\n{body_text}" + (f"\n\n{first}" if first else "")


def sample_body(scope: str, lim: dict[str, Any], masked: bool, n_counties: int, shown: int | None = None,
                total: int | None = None) -> str:
    """Touch 2, sent with the sample only after their yes: what is attached, the flat prices, how to start. A sample
    that is the first page of a longer sheet says so ("13 of 25 listings")."""
    one, every, tax = price_words(lim)
    nine = number_word(n_counties)
    what = f"the {scope} County sheet" if scope != ALL else f"this week's sheet for all {nine} counties"
    if shown and total and shown < total:
        what = f"the first page of {what} ({shown} of {total} listings)"
    attached = (f"Attached is {what}: the first {SAMPLE_FULL_ROWS} listings in full, the rest with the street hidden."
                if masked else f"Attached is {what}.")
    return (f"{attached} It's {one} a month{tax} for one county or {every}{tax} for all {nine}, with no commission or "
            "per-deal fee. Reply \"start\" and I'll set it up.")


def _greeting(t: FirmTarget) -> str:
    from engine.streams.s2_deal_desk_saas import greeting
    return greeting(t.name) if t.name else "Hello,"


def lead_first(rows: list[Row], lead: Row | None) -> list[Row]:
    """The sheet with Touch 1's row on top, so the sample a yes releases shows that street in full."""
    return rows if lead is None else [lead] + [r for r in rows if r is not lead]


def sample_for(sheet: Sheet, row: Row, ctx: StreamContext, lim: dict[str, Any], n_counties: int,
               cache: dict[tuple[str, int], tuple[Path, int]]) -> tuple[Path, bool, int]:
    """(attachment, masked, rows shown) for Touch 2: the sheet's first page with Touch 1's row on top (shown in full)
    and its masked sample, under leads/, which subscriber delivery never picks up."""
    key = (sheet.scope, id(row))
    if key not in cache:
        name = sheet_name(sheet.scope, ctx.now.date()).replace(".pdf", f"_{slug(row.street, 32).lower()}.pdf")
        path = ctx.out_dir / "leads" / name
        kept = render_fit(path, sheet.scope, lead_first(sheet.rows, row), ctx, lim, n_counties,
                          total=len(sheet.rows))
        cache[key] = (path, len(kept))
    path, shown = cache[key]
    return (sample_path(path), True, shown) if sample_path(path).exists() else (path, False, shown)


def pitches(session: Session, ctx: StreamContext, lim: dict[str, Any], sheets: dict[str, Sheet],
            n_counties: int) -> list[Outbound]:
    """At most `pitches_per_run` Touch 1s to builders (kind builder_developer) the outbox would accept now
    (`firm_outbox.pitch_ok`, no stop reason, this week's key unused, nothing already queued): their county's sheet,
    else the all-county sheet led by a street from a neighboring county. One key per target per ISO week."""
    from engine.streams.s2_deal_desk_saas import queued_targets
    if not sheets or not ctx.send:
        return []
    y, w, _ = ctx.now.isocalendar()
    period = f"{y}-W{w:02d}"
    cap = int(lim["pitches_per_run"])
    first = (ctx.operator or {}).get("first") or ((ctx.operator or {}).get("name") or "").split(" ")[0]

    def key_for(t: FirmTarget) -> str:
        return f"s13:pitch:{t.id}:{period}"

    busy = queued_targets(session)
    variants: dict[tuple[str, int], tuple[Path, int]] = {}
    out: list[Outbound] = []
    for t in eligible_targets(session, [TARGET_KIND], "s13", key_for, ctx.now):
        if len(out) >= cap:
            break
        if t.id in busy:
            continue
        county = county_key(t.county)
        if county in sheets:
            sheet, prefer = sheets[county], (county,)
        elif ALL in sheets:
            # their county's rows too thin for its own sheet (or a county we don't cover): the all-county sheet, led by
            # a street from their county, else a neighboring one
            sheet, prefer = sheets[ALL], (county, *NEIGHBORS.get(county, ()))
        else:
            continue
        row = lead_row(sheet.rows, prefer)
        if row is None:
            continue
        attach, masked, shown = sample_for(sheet, row, ctx, lim, n_counties, variants)
        subject, body = touch1(sheet.scope, row, len(sheet.rows), n_counties, _greeting(t), first)
        out.append(Outbound(stream="s13", key=key_for(t), to=t.email or "", priority=PRIORITY_SPECIALTY, kind="pitch",
                            subject=subject, body=body, target_id=t.id, attachments=[],
                            sample_body=sample_body(sheet.scope, lim, masked, n_counties, shown,
                                                    len(sheet.rows)),
                            sample_attachments=[attach]))
    return out


# -- the stream ---------------------------------------------------------------------------------------------------
class BuilderTeardownStream(Stream):
    key = "s13"
    name = "Builder teardown digest"
    priority = PRIORITY_SPECIALTY
    cadence_hours = 24.0

    def run(self, session: Session, ctx: StreamContext) -> StreamResult:
        lim = {**DEFAULTS, **{k: v for k, v in (ctx.limits or {}).items() if v is not None}}
        res = StreamResult(stream=self.key)
        today = ctx.now.date()
        scan = collect(session, today, lim)
        n_counties = len(_target_counties(lim)) or len({r.county for r in scan.rows})
        by_county: dict[str, list[Row]] = {}
        for r in scan.rows:
            by_county.setdefault(r.county, []).append(r)
        sheets: dict[str, Sheet] = {}
        min_rows = int(lim["min_rows"])
        for county in sorted(by_county):
            rows = by_county[county][:int(lim["report_rows"])]
            if len(rows) < min_rows:
                continue                                   # too thin for its own sheet: on the all-county sheet only
            rows = lead_first(rows, lead_row(rows, (county,)))
            path = render_sheet(ctx.out_dir / sheet_name(county, today), county, rows, ctx, lim, n_counties,
                                sample=False)
            sheets[county] = Sheet(scope=county, path=path, rows=rows)
        # every row found (best first), so the all-county subscriber never sees less than a county one. Sheets take
        # the pages they need; a pitch's sample is a one-page teaser (`sample_for`)
        all_rows = scan.rows[:int(lim["all_rows"])]
        if len(all_rows) >= min_rows:
            # written last, so a subscriber with no county (firm_outbox.delivery_files: the newest files) gets it first
            all_rows = lead_first(all_rows, lead_row(all_rows))
            path = render_sheet(ctx.out_dir / sheet_name(ALL, today), ALL, all_rows, ctx, lim, n_counties,
                                sample=False)
            sheets[ALL] = Sheet(scope=ALL, path=path, rows=all_rows)
        res.processed = scan.counts["listings_seen"]
        res.found = len(scan.rows)
        res.products = len(sheets)
        res.outbound = pitches(session, ctx, lim, sheets, n_counties)
        res.metrics.update({
            "listings_seen": scan.counts["listings_seen"], "word_hits": scan.counts["word_hits"],
            "land_share_hits": scan.counts["land_share_hits"], "rows": len(scan.rows), "pdfs": len(sheets),
            "pitches": len(res.outbound), "skipped_suppressed": scan.counts["skipped_suppressed"],
            "counties": len([s for s in sheets if s != ALL]),
        })
        for k, v in scan.counts.items():
            if k.startswith(("skipped_", "word_")) or k in ("wrong_parcel", "no_county"):
                res.metrics.setdefault(k, int(v))
        res.notes.append(f"{len(sheets)} sheets ({res.metrics['counties']} county + {int(ALL in sheets)} "
                         f"all-county), {len(scan.rows)} rows, {len(res.outbound)} pitches proposed")
        return res


STREAM = BuilderTeardownStream()
