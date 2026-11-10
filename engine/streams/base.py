"""The contract every revenue stream of the 10-stream firm implements (engine/swarm_firm.py runs them).

A stream reads the database, produces research products (PDF packets, reports) and proposes e-mails. It never sends
anything itself: each proposed e-mail is an `Outbound` that `engine/firm_outbox.py` stores under a natural key (so a
re-run never sends twice), checks against every stop list, allocates by priority under the daily caps and sends from
the operator's Gmail with the signature, postal address, opt-out line and (for a commercial pitch) the ad notice.

Rules every stream follows (they are why the products are legal for an unlicensed operator):
- Research data for a flat fee, never a referral and never pay that depends on a deal or a client (N.J.S.A. 45:15-3:
  one act of buying, selling or negotiating for another for a fee is brokerage; NJ RPC 7.2(c) bars lawyers from paying
  for recommendations, so a pitch to an attorney sells data, not clients).
- No property we don't have under contract is marketed to a buyer; a comparable-sales packet states sales and
  statutory ratios, never an opinion of value (N.J.S.A. 45:14F: an appraisal needs a license).
- No homeowner or defendant name in a B2B product unless the product is about a public court case and the reader is
  an attorney; every address passes the Daniel's Law / stop suppression list; individuals' home addresses are never
  sold as a list. Only public listing text, public records and our own math go into a product, never what an agent
  told us in a private e-mail.
- Counts only in `StreamResult.metrics` / `notes`: the cloud run log is public.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

PRIORITY_REPLY, PRIORITY_PITCH, PRIORITY_SPECIALTY = 1, 2, 3


@dataclass
class Outbound:
    """One e-mail a stream wants sent. `key` is the natural key (one send per key, ever)."""
    stream: str
    key: str
    to: str
    subject: str
    body: str                                   # plain text above the signature; the outbox adds the rest
    priority: int = PRIORITY_SPECIALTY
    kind: str = "pitch"                         # pitch / followup / reply / packet
    attachments: list[Path] = field(default_factory=list)
    target_id: int | None = None
    lead_id: int | None = None
    in_reply_to: str | None = None              # a reply in an existing thread
    commercial: bool = True                     # adds the CAN-SPAM commercial-message line
    # The 2-step funnel (2026-11-10): Touch 1 is plain text with no attachment; the sample (PDF with 2 rows in full,
    # the rest masked, and the flat price) waits as a "held" outbox row and goes out in the prospect's own thread the
    # moment they reply yes. A pitch with attachments but no sample is converted by the outbox (never a cold PDF).
    sample_body: str = ""
    sample_attachments: list[Path] = field(default_factory=list)


@dataclass
class StreamResult:
    stream: str
    processed: int = 0                          # records examined
    found: int = 0                              # opportunities identified
    products: int = 0                           # PDFs / packets / reports produced
    outbound: list[Outbound] = field(default_factory=list)
    metrics: dict[str, Any] = field(default_factory=dict)   # counts only
    notes: list[str] = field(default_factory=list)          # no names, addresses or message text
    internal: list[Path] = field(default_factory=list)      # reports for the operator only (mailed to their own inbox)


@dataclass
class StreamContext:
    now: datetime                               # Eastern, naive
    out_dir: Path                               # where this run's PDFs go (a temp folder on the cloud)
    operator: dict[str, str] = field(default_factory=dict)
    send: bool = True                           # False: propose nothing for sending (dry run / reports only)
    limits: dict[str, Any] = field(default_factory=dict)   # this stream's settings (research_firm.streams.<key>)


class Stream:
    """Subclass, set the attributes and implement `run`."""
    key: str = "s0"
    name: str = ""
    priority: int = PRIORITY_SPECIALTY
    cadence_hours: float = 24.0                 # run at most this often (swarm_firm tracks the last run)
    run_hours: tuple[int, int] = (6, 20)        # Eastern hours [start, end) when a scheduled run may start

    def run(self, session: Session, ctx: StreamContext) -> StreamResult:  # pragma: no cover - interface
        raise NotImplementedError


# -- helpers shared by the streams ---------------------------------------------------------------------------------
def week_of(now: datetime) -> str:
    """The Monday of `now`'s week, for subjects ("week of Nov 9"): the same label all week, never a mid-week date."""
    monday = now - timedelta(days=now.weekday())
    return f"{monday:%b} {monday.day}"


def money(v: float | None) -> str:
    return f"${v:,.0f}" if v else "-"


def kmoney(v: float | None) -> str:
    return f"${v / 1000:,.0f}K" if v else "-"


def address_ok(session: Session, prop: Any) -> bool:
    """False when the property's address or its owner's mailing address is on a Daniel's Law / stop suppression."""
    from engine.compliance import address_suppressed
    addr = f"{getattr(prop, 'street_address', '') or ''}, {getattr(prop, 'city', '') or ''} NJ {getattr(prop, 'zip_code', '') or ''}"
    if address_suppressed(session, addr):
        return False
    mail = getattr(prop, "owner_mailing_address", None)
    if mail and address_suppressed(session, f"{mail}, {getattr(prop, 'owner_city_state_zip', '') or ''}"):
        return False
    return True


def entity_like(name: str | None) -> bool:
    """A business (LLC, Inc, trust company ...), not a private person: only these may appear in a sold list."""
    from core.text import is_entity
    return bool(name) and is_entity(name)


_KEEP_UPPER = {"LLC", "LP", "LLP", "PC", "PA", "NJ", "NY", "II", "III", "IV", "USA", "RE", "MGMT"}


def nice_name(name: str | None) -> str:
    """"42 RUTGERS AVE., LLC" -> "42 Rutgers Ave., LLC" (entity names read like a person typed them)."""
    words = []
    for w in (name or "").split():
        core = w.strip(".,&()")
        initials = bool(re.fullmatch(r"(?:[A-Za-z]\.){1,4}[A-Za-z]?", core)) or (
            2 <= len(core) <= 3 and core.isalpha() and not re.search(r"(?i)[aeiouy]", core))   # "N.B.", "NB", "JC"
        if core.upper() in _KEEP_UPPER or any(c.isdigit() for c in core) or initials:
            words.append(w.upper() if initials else w)
        else:
            words.append(w.capitalize())
    return " ".join(words)


def slug(text: str, n: int = 48) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "_", text or "").strip("_")[:n] or "x"


# -- samples: 2 rows in full, the rest masked (the 2-step funnel's Touch 2) ---------------------------------------------
SAMPLE_FULL_ROWS = 2
_MASK_HEAD = re.compile(r"(?i)\b(property|address|street|docket|sheriff|entity|buyer|brokerage|agent|parcel|block|"
                        r"lot|listing agent)\b")
_KEEP_WORDS = {"st", "st.", "ave", "ave.", "rd", "rd.", "dr", "dr.", "ln", "ct", "pl", "blvd", "way", "ter", "cir",
               "pkwy", "hwy", "llc", "inc", "inc.", "lp", "llp", "corp", "co", "co.", "n", "s", "e", "w", "unit",
               "apt", "#", "the", "of", "and", "&"}


def sample_path(path: Path) -> Path:
    """Where the masked sample of a report lives (a samples/ folder next to it, so report globs never see it)."""
    path = Path(path)
    return path.parent / "samples" / (path.stem + ".sample" + path.suffix)


def _mask_word(w: str) -> str:
    core = re.sub(r"[^\w]", "", w)
    if not core or core.lower() in _KEEP_WORDS or w.lower() in _KEEP_WORDS:
        return w
    if re.search(r"\d", core):
        # house numbers, dockets, sheriff #s: keep only the first character of the whole token ("12-14" -> "1•-••",
        # "#F-8" -> "#F-•"); a one-digit house number is hidden entirely ("5" -> "•")
        first = re.search(r"[\dA-Za-z]", w)
        if len(core) == 1:
            return re.sub(r"[\dA-Za-z]", "\u2022", w)
        i = first.start() if first else 0
        return w[:i + 1] + re.sub(r"[\dA-Za-z]", "\u2022", w[i + 1:])
    return w[0] + re.sub(r"[A-Za-z]", "\u2022", w[1:])


def mask_text(text: str) -> str:
    """'263 Morningside Ave<br/>Montclair' -> '2\u2022\u2022 M\u2022\u2022\u2022 Ave<br/>Montclair': the identifying
    first line is masked, a following town line stays (the buyer still sees where the deal is)."""
    t = str(text or "")
    parts = re.split(r"(<br/>)", t, maxsplit=1)
    first = re.sub(r"</?b>", "", parts[0])
    head, sep, tail = first.partition(", ")
    if sep and (re.search(r"\b(LLC|INC|CORP|CO|LP|LLP|TRUST|HOLDINGS|GROUP|PARTNERS)\b\.?", tail, re.I)
                or not re.search(r"\d", head)):
        head, sep, tail = first, "", ""            # an entity name with a comma: mask all of it
    masked = " ".join(_mask_word(w) for w in head.split(" ")) + (sep + tail if sep else "")
    return masked + "".join(parts[1:])


def masked_rows(head: list[str], rows: list[list[str]], full: int = SAMPLE_FULL_ROWS) -> list[list[str]] | None:
    """Rows for a sample (None when nothing identifying is in the table)."""
    cols = [i for i, h in enumerate(head) if _MASK_HEAD.search(re.sub(r"<[^>]+>", "", str(h)))]
    if not cols or len(rows) <= full:
        return None
    out = [list(r) for r in rows[:full]]
    for r in rows[full:]:
        r = list(r)
        for i in cols:
            if i < len(r):
                r[i] = mask_text(r[i])
        out.append(r)
    return out


def render_table_pdf(path: Path, *, title: str, subtitle: str, head: list[str], rows: list[list[str]],
                     notes: list[str] | None = None, box: str | None = None, footer: list[str] | None = None,
                     col_widths: list[float] | None = None, landscape_page: bool = True,
                     write_sample: bool = True) -> Path:
    """A one-page research packet: title, subtitle, a table, small-print notes, an optional highlighted box and the
    footer lines (operator contact, disclaimers). Inline markup <b>/<br/> allowed in cells. When the table has more
    than 2 rows and an identifying column, a masked sample (`sample_path`) is written next to it."""
    if write_sample:
        mrows = masked_rows(head, rows)
        if mrows is not None:
            render_table_pdf(sample_path(path), title=title,
                             subtitle=f"{subtitle} · Sample: {SAMPLE_FULL_ROWS} of {len(rows)} rows in full, the rest "
                                      "with the street hidden (the weekly report has every row in full)",
                             head=head, rows=mrows, notes=notes, box=box, footer=footer, col_widths=col_widths,
                             landscape_page=landscape_page, write_sample=False)
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import LETTER, landscape
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import inch
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    from engine.contracts import _pdf_safe
    ss = getSampleStyleSheet()
    body = ParagraphStyle("b", parent=ss["BodyText"], fontName="Helvetica", fontSize=8.2, leading=10)
    small = ParagraphStyle("s", parent=body, fontSize=6.8, leading=8.2)
    cell = ParagraphStyle("c", parent=body, fontSize=7, leading=8.4)
    h1 = ParagraphStyle("h", parent=ss["Heading1"], fontName="Helvetica-Bold", fontSize=14, leading=17, spaceAfter=2)
    boxed = ParagraphStyle("x", parent=body, backColor=colors.HexColor("#eef6ff"), borderPadding=5,
                           borderColor=colors.HexColor("#1d4ed8"), borderWidth=0.6, spaceBefore=4)

    def p(t: str, st: ParagraphStyle = cell) -> Paragraph:
        return Paragraph(_pdf_safe(t), st)

    data = [[p(f"<b>{h}</b>") for h in head]] + [[p(str(c)) for c in r] for r in rows]
    if not rows:
        data.append([p("No records this week.")] + [p("") for _ in head[1:]])
    t = Table(data, colWidths=[w * inch for w in col_widths] if col_widths else None, repeatRows=1)
    t.setStyle(TableStyle([("GRID", (0, 0), (-1, -1), 0.3, colors.HexColor("#9ca3af")),
                           ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#e5e7eb")),
                           ("VALIGN", (0, 0), (-1, -1), "TOP"),
                           ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f9fafb")])]))
    story: list[Any] = [p(title, h1), p(subtitle, body), Spacer(1, 5), t, Spacer(1, 4)]
    for n in notes or []:
        story.append(p(n, small))
    if box:
        story += [Spacer(1, 6), p(box, boxed)]
    if footer:
        story.append(Spacer(1, 3))
        story += [p(f, small) for f in footer]
    path.parent.mkdir(parents=True, exist_ok=True)
    size = landscape(LETTER) if landscape_page else LETTER
    SimpleDocTemplate(str(path), pagesize=size, title=title, leftMargin=0.45 * inch, rightMargin=0.45 * inch,
                      topMargin=0.4 * inch, bottomMargin=0.35 * inch).build(story)
    return path


def operator_footer(op: dict[str, str], *, research_only: bool = True) -> list[str]:
    """The standard small print under every packet: who sent it, that it is research, how to stop."""
    contact = " · ".join(x for x in (op.get("name"), op.get("phone"), op.get("email"), op.get("postal")) if x)
    lines = []
    if research_only:
        lines.append(f"{op.get('name') or 'The sender'} provides research data only: not a licensed real estate broker, "
                     "appraiser, attorney or tax adviser; no referral fees and no pay tied to any transaction. Figures "
                     "come from public records, listings and stated formulas: verify before relying on them.")
    lines.append(f"{contact} · Reply \"no more\" and you won't get these again.")
    return lines
