"""Plain, human outreach copy (e-mail + SMS) and the linter that keeps it that way.

Listing agents, sellers and buyers get dozens of templated "investor" e-mails a day. The ones that get answered
read like a quick note from a person, so every message this system drafts is built here:

  * a low-key subject (``question on 12 Main St``, ``12 Main St (saw the Nov 12 sale date)``);
  * 3-5 short plain sentences above the sign-off (aim for under 85 words; 110 is the hard limit);
  * a plain signature: first name, then ``Full Name | Company``, then the phone number;
  * everything the law or our own rules require (investor / may-assign disclosure, unrepresented-buyer wording,
    offer terms, CAN-SPAM postal address and opt-out) under a ``--- Offer Summary ---`` divider below the
    signature, where it is complete and easy to find without turning the note into a form letter.

`lint()` rejects stock phrases ("I hope this email finds you well", "opportunity", "synergy", ...), em-dashes,
markdown, bullet lists and long bodies. tests/test_human_copy.py runs it over every template and offer track.
"""
from __future__ import annotations

import re
import zlib
from dataclasses import dataclass, field
from urllib.parse import quote

from core.config import settings
from core.text import format_phone, postal_title

MAX_WORDS = 110          # hard limit above the sign-off
TARGET_WORDS = 85        # what the templates aim for
SMS_MAX_CHARS = 320      # two SMS segments
DIVIDER = "--- Offer Summary ---"
DETAILS = "--- Details ---"            # same block for a no-price inquiry
SIGNOFFS = ("Thanks,", "Best,", "Thank you,")

# Phrases that mark a message as mass-produced (or as legalese) the moment an agent opens it.
FORBIDDEN = (
    "i hope this email finds you well", "hope this finds you", "hope you're doing well", "hope you are doing well",
    "i am writing to", "i'm writing to", "i am reaching out", "i'm reaching out", "reaching out", "reach out",
    "excited", "opportunity", "opportunities", "synergy", "letter of intent", "formal offer", "formal",
    "touch base", "circle back", "at your earliest convenience", "do not hesitate", "don't hesitate", "kindly",
    "pursuant", "herein", "hereby", "whereas", "aforementioned", "heretofore", "win-win", "seamless", "leverage",
    "delighted", "thrilled", "game changer", "game-changer", "value proposition", "mutually beneficial",
    "please be advised", "rest assured", "i trust this", "as per", "esteemed", "dear sir", "dear madam",
    "to whom it may concern",
)
_FORBIDDEN_RE = re.compile(r"\b(" + "|".join(re.escape(p) for p in FORBIDDEN) + r")\b", re.I)
_BULLET_RE = re.compile(r"^\s*(?:[-*•·]|\d+[.)])\s+")


# -- operator / formatting ---------------------------------------------------------------------------
def operator() -> dict[str, str]:
    op = settings().operator
    digits = "".join(ch for ch in (op.get("phone") or "") if ch.isdigit())
    name = (op.get("name") or "").strip()
    return {"name": name, "first": name.split()[0] if name else "", "company": (op.get("company_name") or "").strip(),
            "phone": format_phone(digits) or "", "email": (op.get("email") or "").strip(),
            "postal": (op.get("mailing_address") or "").strip()}


def money(v: float | None) -> str:
    return f"${v:,.0f}" if v else "?"


def street(address: str | None) -> str:
    """'12 SAMPLE ST, Newark' / '12 Sample St, Newark' -> '12 Sample St' (what a person types in a subject)."""
    return postal_title((address or "").split(",")[0].strip())


def first_name(name: str | None, fallback: str = "there") -> str:
    parts = (name or "").replace(",", " ").split()
    return parts[0].title() if parts else fallback


def pick(options: list[str] | tuple[str, ...], key: int | str | None) -> str:
    """Deterministic variety: the same lead always gets the same wording, different leads get different ones."""
    if not options:
        return ""
    k = key if isinstance(key, int) else zlib.crc32(str(key or "").encode())
    return options[k % len(options)]


def plain(text: str) -> str:
    """Last-line defence for dynamic values: no em-dashes, no markdown emphasis."""
    text = re.sub(r"\s*—\s*", ", ", text)
    return text.replace("**", "").replace("__", "")


def short_date(d) -> str:
    """date -> 'Nov 12' (how people write dates in a subject line)."""
    return f"{d:%b} {d.day}"


# -- composition -------------------------------------------------------------------------------------
@dataclass
class Email:
    subject: str
    greeting: str
    paragraphs: list[str]
    signoff: str = "Thanks,"
    signature: list[str] = field(default_factory=list)
    summary: list[str] = field(default_factory=list)
    summary_title: str = DIVIDER

    @property
    def above_signature(self) -> str:
        return "\n\n".join([self.greeting, *self.paragraphs])

    @property
    def text(self) -> str:
        parts = [self.above_signature, "\n".join([self.signoff, *self.signature])]
        if self.summary:
            parts.append("\n".join([self.summary_title, *self.summary]))
        return "\n\n".join(parts)

    def mailto(self, to: str | None) -> str | None:
        return f"mailto:{to}?subject={quote(self.subject)}&body={quote(self.text)}" if to else None


def signature_lines(op: dict[str, str] | None = None) -> list[str]:
    op = op or operator()
    who = " | ".join(x for x in (op["name"], op["company"]) if x)
    lines = [op["first"]] if op["first"] else []
    if who and who != op["first"]:
        lines.append(who)
    if op["phone"]:
        lines.append(op["phone"])
    return lines


def compose(subject: str, greeting: str, paragraphs: list[str], summary: list[str] | None = None, *,
            signoff: str = "Thanks,", summary_title: str = DIVIDER, op: dict[str, str] | None = None) -> Email:
    return Email(subject=plain(subject), greeting=plain(greeting), paragraphs=[plain(p) for p in paragraphs if p],
                 signoff=signoff, signature=signature_lines(op),
                 summary=[plain(s) for s in (summary or []) if s], summary_title=summary_title)


def footer_lines(op: dict[str, str] | None = None, opt_out: str = 'Reply "no more" and I won\'t e-mail you again.'
                 ) -> list[str]:
    """CAN-SPAM: the sender's postal address and a working opt-out on every commercial e-mail."""
    op = op or operator()
    return [x for x in (op["email"] and f"E-mail: {op['email']}", op["postal"] and f"Mail: {op['postal']}", opt_out)
            if x]


def investor_disclosure(company: str) -> str:
    who = f'"{company} and/or assigns"' if company else '"the buyer and/or assigns"'
    return (f"Who's buying: we're real estate investors buying as principals, not licensed agents. The buyer is {who}; "
            "we may assign the contract to another investor, and the seller's price and terms stay the same.")


# -- lint --------------------------------------------------------------------------------------------
def split_signature(text: str) -> tuple[str, str]:
    """(everything above the sign-off, the sign-off and below). Falls back to the divider, then the whole text."""
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if line.strip() in SIGNOFFS:
            return "\n".join(lines[:i]), "\n".join(lines[i:])
    for divider in (DIVIDER, DETAILS):
        if divider in text:
            head, _, tail = text.partition(divider)
            return head, divider + tail
    return text, ""


def word_count(text: str) -> int:
    return len(re.findall(r"[A-Za-z0-9$][A-Za-z0-9$,.'/&%-]*", text))


def lint(text: str, *, max_words: int = MAX_WORDS, sms: bool = False) -> list[str]:
    """Problems that make a message read as spam, AI or legalese. Empty list = ship it."""
    problems: list[str] = []
    if "—" in text:
        problems.append("em-dash")
    if "**" in text or re.search(r"(^|\s)__\w", text) or re.search(r"^#{1,6}\s", text, re.M):
        problems.append("markdown")
    for m in sorted({m.group(1).lower() for m in _FORBIDDEN_RE.finditer(text)}):
        problems.append(f"forbidden phrase: {m!r}")
    if sms:
        if len(text) > SMS_MAX_CHARS:
            problems.append(f"SMS is {len(text)} characters (max {SMS_MAX_CHARS})")
        if "STOP" not in text:
            problems.append("SMS has no STOP opt-out")
        return problems
    head, _ = split_signature(text)
    if any(_BULLET_RE.match(line) for line in head.splitlines()):
        problems.append("bullet list above the signature")
    n = word_count(head)
    if n > max_words:
        problems.append(f"{n} words above the signature (max {max_words})")
    return problems
