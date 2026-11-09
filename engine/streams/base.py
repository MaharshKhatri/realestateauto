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
from datetime import datetime
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


@dataclass
class StreamResult:
    stream: str
    processed: int = 0                          # records examined
    found: int = 0                              # opportunities identified
    products: int = 0                           # PDFs / packets / reports produced
    outbound: list[Outbound] = field(default_factory=list)
    metrics: dict[str, Any] = field(default_factory=dict)   # counts only
    notes: list[str] = field(default_factory=list)          # no names, addresses or message text


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


def slug(text: str, n: int = 48) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "_", text or "").strip("_")[:n] or "x"


def render_table_pdf(path: Path, *, title: str, subtitle: str, head: list[str], rows: list[list[str]],
                     notes: list[str] | None = None, box: str | None = None, footer: list[str] | None = None,
                     col_widths: list[float] | None = None, landscape_page: bool = True) -> Path:
    """A one-page research packet: title, subtitle, a table, small-print notes, an optional highlighted box and the
    footer lines (operator contact, disclaimers). Inline markup <b>/<br/> allowed in cells."""
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
