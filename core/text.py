"""Text normalization for NJ public records: addresses, owner names, municipalities."""
from __future__ import annotations

import re
from dataclasses import dataclass

US_STATES = {
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "DC", "FL", "GA", "HI", "ID", "IL", "IN", "IA",
    "KS", "KY", "LA", "ME", "MD", "MA", "MI", "MN", "MS", "MO", "MT", "NE", "NV", "NH", "NJ", "NM",
    "NY", "NC", "ND", "OH", "OK", "OR", "PA", "RI", "SC", "SD", "TN", "TX", "UT", "VT", "VA", "WA",
    "WV", "WI", "WY", "PR", "VI", "GU",
}

STREET_SUFFIXES = {
    "STREET": "ST", "STR": "ST", "AVENUE": "AVE", "AV": "AVE", "AVN": "AVE", "ROAD": "RD",
    "DRIVE": "DR", "PLACE": "PL", "COURT": "CT", "LANE": "LN", "BOULEVARD": "BLVD", "TERRACE": "TER",
    "TERR": "TER", "PARKWAY": "PKWY", "HIGHWAY": "HWY", "CIRCLE": "CIR", "SQUARE": "SQ",
    "TURNPIKE": "TPKE", "PLAZA": "PLZ", "EXTENSION": "EXT", "HEIGHTS": "HTS",
}
DIRECTIONS = {"NORTH": "N", "SOUTH": "S", "EAST": "E", "WEST": "W", "NO": "N", "SO": "S"}
UNIT_WORDS = r"(?:UNIT|APT|APARTMENT|STE|SUITE|#|FL|FLOOR|BLDG)"

ENTITY_PATTERNS = re.compile(
    r"\b(LLC|L\.L\.C|INC|INCORPORATED|CORP|CORPORATION|CO\b|COMPANY|LP|L\.P|LLP|LTD|TRUST|TRUSTEE|"
    r"TR\b|HOLDINGS|PROPERTIES|REALTY|INVESTMENTS?|CAPITAL|VENTURES?|GROUP|PARTNERS|PARTNERSHIP|LIMITED|ASSOCIATES|"
    r"MANAGEMENT|MGMT|DEVELOPMENT|BANK|MORTGAGE|FUNDING|ASSOCIATION|ASSOC|ASSN|CONDOMINIUM|CONDO|CO-?OP|"
    r"CHURCH|AUTHORITY|HOUSING|"
    r"ENTERPRISES?|SERIES|FUND)\b")
ESTATE_PATTERNS = re.compile(
    r"\b(ESTATE OF|EST OF|EST\b|ESTATE|EXECUTOR|EXECUTRIX|EXR|EXRS|EXTRX|ADMINISTRATOR|ADMR|ADMX|"
    r"HEIRS|DECEASED|DEC'D|DECD|C/O)\b")
GOVT_PATTERNS = re.compile(r"\b(CITY OF|TOWNSHIP OF|BOROUGH OF|COUNTY OF|STATE OF|HOUSING AUTHORITY|"
                           r"BOARD OF EDUCATION|REDEVELOPMENT|UNITED STATES|INTERNAL REVENUE|DEPARTMENT OF|"
                           r"DIVISION OF|SECRETARY OF|COMMISSIONER)\b")

LENDER_PATTERNS = re.compile(
    r"\b(BANK|MORTGAGE|FEDERAL NATIONAL|FANNIE MAE|FREDDIE MAC|SECRETARY OF HOUSING|SAVINGS|LOAN SERVIC|"
    r"TRUST CO|NATIONAL ASSO|N\.A\.?|SERVICING|LENDING|FINANCIAL SERVICES|FUNDING)\b")
# Escrowed tax bills go to the mortgage servicer's tax department, not the owner.
SERVICER_MAIL_PATTERNS = re.compile(
    r"TAX\s*(DEPT|DEPARTMENT|SERVICE|PROCESSING|UNIT)\b|ESCROW|LERETA|CORELOGIC|\bMORTGAGE\b|SERVICING|"
    r"\bLOAN\s+SERV|111 E\.? WISCONSIN AVE")

MUNI_SUFFIX = re.compile(r"\s+(TWP|TOWNSHIP|CITY|BORO|BOROUGH|TOWN|VILLAGE|VLG)$")


def clean(value: str | None) -> str:
    return re.sub(r"\s+", " ", (value or "").replace("\xa0", " ")).strip()


US_STATES = {"AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "DC", "FL", "GA", "HI", "ID", "IL", "IN", "IA", "KS", "KY",
             "LA", "ME", "MD", "MA", "MI", "MN", "MS", "MO", "MT", "NE", "NV", "NH", "NJ", "NM", "NY", "NC", "ND", "OH",
             "OK", "OR", "PA", "PR", "RI", "SC", "SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV", "WI", "WY"}
KEEP_UPPER = US_STATES | {"PO", "NW", "SE", "SW", "LLC", "LP", "LLP"}


def postal_title(text: str | None) -> str:
    """'306 13TH STREET' -> '306 13th Street'; 'BROOKLYN, NY 11215' -> 'Brooklyn, NY 11215'. For envelopes/letters."""
    def word(m: re.Match[str]) -> str:
        w = m.group(0)
        up = w.upper()
        if up in KEEP_UPPER and len(w) <= 3:
            return up
        if re.fullmatch(r"\d+(ST|ND|RD|TH)", up):
            return w.lower()
        if re.fullmatch(r"\d+[A-Z]?", up):
            return up
        head, sep, tail = w.partition("'")
        if sep and len(head) == 1 and len(tail) > 1:                 # O'BRIEN -> O'Brien
            return head.upper() + sep + tail[:1].upper() + tail[1:].lower()
        return w[:1].upper() + w[1:].lower()
    return re.sub(r"[A-Za-z0-9']+", word, clean(text))


def normalize_street(street: str | None) -> str:
    """'186 Newark Avenue, Unit 2' -> '186 NEWARK AVE UNIT 2'. Used for matching, not display."""
    s = clean(street).upper().replace(".", "").replace(",", " ")
    s = re.sub(r"(\d+)\s*-\s*(\d+)", r"\1-\2", s)
    words = []
    for w in s.split():
        w = STREET_SUFFIXES.get(w, w)
        w = DIRECTIONS.get(w, w)
        words.append(w)
    return " ".join(words)


def street_key(street: str | None) -> str:
    """Matching key: house number + first street word, units stripped. '186 NEWARK AVE' -> '186 NEWARK'."""
    s = normalize_street(street)
    s = re.split(rf"\s{UNIT_WORDS}\b", s)[0]
    m = re.match(r"^(\d+[A-Z]?)(?:-\d+[A-Z]?)?\s+(.*)$", s)
    if not m:
        return s
    rest = [w for w in m.group(2).split() if w not in DIRECTIONS.values()]
    return f"{m.group(1)} {rest[0] if rest else ''}".strip()


@dataclass
class ParsedAddress:
    street: str
    city: str
    state: str
    zip: str


ADDR_RE = re.compile(
    r"^(?P<street>.+?)\s*,?\s+(?P<city>[A-Z][A-Z .'\-]+?)\s*,?\s+(?P<state>[A-Z]{2})\s*,?\s*(?P<zip>\d{5})(?:-?\d{4})?\s*$")


def parse_one_line_address(text: str | None, known_cities: list[str] | None = None) -> ParsedAddress | None:
    """Parse '186 NEWARK AVE BLOOMFIELD NJ 07003' / '12 Main St, Edison, NJ 08817'.

    Without commas the street/city boundary is ambiguous; if ``known_cities`` is supplied we
    take the longest known city that ends the street portion.
    """
    s = clean(text).upper()
    if not s:
        return None
    m = ADDR_RE.match(s)
    if not m:
        return None
    street, city, state, zip_ = m.group("street"), m.group("city"), m.group("state"), m.group("zip")
    if "," not in s and known_cities:
        combined = f"{street} {city}"
        for kc in sorted(known_cities, key=len, reverse=True):
            kc_u = kc.upper()
            if combined.endswith(" " + kc_u):
                street, city = combined[: -len(kc_u) - 1].strip(), kc_u
                break
    return ParsedAddress(street=street.strip(" ,"), city=city.strip(" ,"), state=state, zip=zip_)


def mailing_state(city_state: str | None) -> str | None:
    """Extract the 2-letter state from NJGIN CITY_STATE values like 'NEWARK, NJ' or 'MIAMI FL 33101'."""
    s = clean(city_state).upper()
    m = re.search(r"(?:,|\s)\s*([A-Z]{2})\.?(?:\s+\d{5}(?:-?\d{4})?)?\s*$", s)
    if m and m.group(1) in US_STATES:
        return m.group(1)
    return None


def mailing_city(city_state: str | None) -> str:
    s = clean(city_state).upper()
    s = re.sub(r"(?:,|\s)\s*[A-Z]{2}\.?(?:\s+\d{5}(?:-?\d{4})?)?\s*$", "", s)
    return s.strip(" ,")


def is_entity(name: str | None) -> bool:
    return bool(name) and bool(ENTITY_PATTERNS.search(clean(name).upper().replace(",", " ")))


def is_estate(name: str | None) -> bool:
    return bool(name) and bool(ESTATE_PATTERNS.search(clean(name).upper().replace(",", " ")))


def is_lender(name: str | None) -> bool:
    return bool(name) and bool(LENDER_PATTERNS.search(clean(name).upper()))


def is_servicer_mailing(street: str | None, city_state: str | None = None) -> bool:
    return bool(SERVICER_MAIL_PATTERNS.search(f"{clean(street)} {clean(city_state)}".upper()))


def is_government(name: str | None) -> bool:
    return bool(name) and bool(GOVT_PATTERNS.search(clean(name).upper()))


def muni_base(name: str | None) -> str:
    """'BLOOMFIELD TWP' -> 'BLOOMFIELD'; 'Township of Edison' -> 'EDISON'."""
    s = clean(name).upper()
    s = re.sub(r"^(TOWNSHIP|BOROUGH|CITY|TOWN|VILLAGE) OF\s+", "", s)
    s = MUNI_SUFFIX.sub("", s)
    return s.strip()


def strip_et_al(name: str | None) -> str:
    s = clean(name)
    s = re.sub(r"[;,]?\s*(ET\.?\s*AL\.?|ET\s+UX\.?|ET\s+VIR\.?)\s*$", "", s, flags=re.I)
    return s.strip(" ;,")


def money(text: str | None) -> float | None:
    if text is None:
        return None
    m = re.search(r"\$?\s*([\d,]+(?:\.\d{1,2})?)", str(text))
    if not m:
        return None
    try:
        return float(m.group(1).replace(",", ""))
    except ValueError:
        return None


PHONE_RE = re.compile(r"(?<!\d)(?:\+?1[\s.-]?)?\(?([2-9]\d{2})\)?[\s.-]?([2-9]\d{2})[\s.-]?(\d{4})(?!\d)")


def normalize_phone(value: str | None) -> str | None:
    """'(908) 208-7376' / '+1 908.208.7376' -> '9082087376' (US 10-digit), else None."""
    m = PHONE_RE.search(value or "")
    return "".join(m.groups()) if m else None


def format_phone(digits: str | None) -> str:
    return f"({digits[:3]}) {digits[3:6]}-{digits[6:]}" if digits and len(digits) == 10 else (digits or "")


def split_person_name(name: str | None) -> tuple[str, str]:
    """('QUINN', 'SAMPLE') from 'SAMPLE, QUINN A' / 'Quinn A. Sample Jr' / 'SAMPLE QUINN & MARY' (best effort)."""
    s = clean(name).upper()
    s = re.sub(r"\b(JR|SR|II|III|IV|ESQ|MD)\b\.?", "", s)
    people = [x.strip(" ,.") for x in re.split(r"\s*(?:&|\bAND\b|;|/)\s*", s) if x.strip(" ,.")]
    if not people:
        return "", ""
    s = people[0]
    if len(people) > 1 and "," not in s and len(s.split()) == 1 and len(people[1].split()) >= 2 \
            and "," not in people[1]:
        return s.title(), people[1].split()[-1].title()      # 'ROBERT & LINDA JONES' → Robert Jones
    if "," in s:
        last, _, rest = s.partition(",")
        first = rest.strip().split(" ")[0] if rest.strip() else ""
        return first.title(), last.strip().title()
    parts = [p for p in s.replace(".", " ").split() if p]
    if len(parts) == 1:
        return "", parts[0].title()
    return parts[0].title(), parts[-1].title()
