"""Scan git-tracked files for personal names that exist in wholesaling.db (owners, defendants, contacts).

The database holds the private individuals we work with; nothing from it may land in git. This finds any
full name (FIRST LAST or LAST, FIRST, case-insensitive) of a non-entity person from the database inside
tracked files, so a commit can be checked before it is pushed:

    python manage.py pii-scan          # exit code 1 when anything is found
"""
from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from core.config import ROOT
from core.text import clean, is_entity, is_government, split_person_name
from db.models import ClerkFiling, Contact, DistressSignal, Property

SKIP_SUFFIXES = {".db", ".zip", ".png", ".jpg", ".pdf", ".pyc"}
# Words that turn up as "names" when a party line is a government, a trustee clause or boilerplate.
NOT_NAMES = {"NEW", "JERSEY", "STATE", "UNITED", "STATES", "AMERICA", "NOT", "CAPACITY", "INDIVIDUAL", "TRUST",
             "TRUSTEE", "BANK", "CITY", "COUNTY", "TOWNSHIP", "BOROUGH", "HEIRS", "ESTATE", "UNKNOWN", "SPOUSE",
             "OCCUPANTS", "TENANTS", "TENANT", "OCCUPANT", "MRS", "MR", "DOE", "ROE", "THE", "AND", "ITS", "SOLELY",
             "DEPARTMENT", "TREASURY", "REVENUE", "INTERNAL", "SERVICE", "ASSOCIATION", "NATIONAL",
             # securitization-trust fragments that lender names leave behind ("Asset-Backed Pass Through Certificates")
             "CERTIFICATES", "CERTIFICATE", "CERTIFICATEHOLDERS", "SECURITIES", "SERIES", "PASS", "THROUGH",
             "ASSET-BACKED", "MORTGAGE", "LOAN", "LOANS", "HOLDERS", "FUNDING", "ACQUISITION"}
MIN_PART = 3


@dataclass
class Hit:
    path: str
    line: int
    name: str
    text: str


def person_names(session: Session) -> set[tuple[str, str]]:
    """(first, last) pairs, upper case, for every individual named in the database."""
    from agents.scrapers.sheriff_sale_agent import first_defendant

    raw: set[str] = set()
    raw.update(n for (n,) in session.execute(select(Property.owner_name).where(Property.owner_name.is_not(None))))
    raw.update(n for (n,) in session.execute(select(Contact.name).where(Contact.name.is_not(None),
                                                                         Contact.contact_type != "LISTING_AGENT")))
    for (d,) in session.execute(select(DistressSignal.defendant).where(DistressSignal.defendant.is_not(None))):
        raw.add(first_defendant(d))
    from agents.scrapers.lis_pendens_agent import JUNIOR_DEFENDANT_RE
    for (parties,) in session.execute(select(ClerkFiling.defendants)):     # lis pendens, matched or not
        raw.update(re.sub(r"\(?ESTATE OF\)?", "", p) for p in (parties or []) if isinstance(p, str)
                   and not JUNIOR_DEFENDANT_RE.search(p)
                   and not re.search(r"\d|LLC|\bN\.?A\.?$|TRUST|\bINC\b|CORP", p.upper()))
    out: set[tuple[str, str]] = set()
    for name in raw:
        up = clean(name).upper()
        if not up or is_entity(up) or is_government(up):
            continue
        first, last = split_person_name(up)
        first, last = first.upper(), last.upper()
        if len(first) >= MIN_PART and len(last) >= MIN_PART and first != last \
                and not {first, last} & NOT_NAMES:
            out.add((first, last))
    return out


def tracked_files(root: Path = ROOT) -> list[Path]:
    """Files git tracks plus new files it would commit (untracked, not ignored) — scan before `git add`."""
    res = subprocess.run(["git", "ls-files", "--cached", "--others", "--exclude-standard"], cwd=root,
                         capture_output=True, text=True, check=True)
    return [root / p for p in dict.fromkeys(res.stdout.splitlines())
            if Path(p).suffix.lower() not in SKIP_SUFFIXES and (root / p).is_file()]


def scan(session: Session, files: list[Path] | None = None) -> list[Hit]:
    names = person_names(session)
    if not names:
        return []
    by_last: dict[str, set[str]] = {}
    for first, last in names:
        by_last.setdefault(last, set()).add(first)
    hits: list[Hit] = []
    for path in files or tracked_files():
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        upper = text.upper()
        candidates = [last for last in by_last if last in upper]
        if not candidates:
            continue
        for lineno, line in enumerate(upper.splitlines(), 1):
            for last in candidates:
                if last not in line:
                    continue
                for first in by_last[last]:
                    pat = rf"\b{re.escape(first)}\b[\w .'-]{{0,20}}\b{re.escape(last)}\b|\b{re.escape(last)},\s*{re.escape(first)}\b"
                    if re.search(pat, line):
                        hits.append(Hit(str(path.relative_to(ROOT)), lineno, f"{first} {last}",
                                        text.splitlines()[lineno - 1].strip()[:160]))
    return hits
