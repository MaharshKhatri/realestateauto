"""Direct-to-seller letters for leads we can't call yet (no phone before skip-tracing).

Three letters, chosen per lead:
  * pre_auction — sheriff-sale / lis-pendens owners: the sale date, their options (list, sell as-is, talk to
    a free HUD counselor or Legal Services), FRFPA protections if it is their home, and an explicit statement
    that we cannot and will not try to stop or delay the sale (a regulated "distressed property service");
  * estate — heirs / executors: condolences, as-is purchase with the estate attorney setting the pace;
  * landlord — absentee / out-of-state owners: sell as-is with tenants in place.

Every letter says we are investors acting as principals (not licensed agents), that we may assign the
contract, that the owner should involve an attorney, and how to stop further mail. Suppressed addresses
(Daniel's Law nondisclosure requests, stop-mail requests — engine/compliance.suppress_address) and
do-not-contact leads are never included; servicer/escrow tax-bill addresses fall back to the property.

Output (git-ignored): deals/mail_batches/<date>/ — <type>.pdf (one letter per page), letters.html,
mailing_list.csv (name / address columns that map onto Click2Mail or any print-and-mail upload) and
manifest.json. `manage.py mail-merge --mark-mailed <dir>` logs a LETTER_SENT interaction per lead.
"""
from __future__ import annotations

import csv
import html
import json
import re
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from core.config import settings
from core.dates import long_date
from core.text import format_phone, is_servicer_mailing, postal_title
from db.models import ContactType, Interaction, Lead, SignalType
from engine import compliance
from engine.contracts import DEALS_DIR
from engine.scripts import lead_facts, speaking_name

MAIL_DIR = DEALS_DIR / "mail_batches"
DEAL_VERDICTS = {"STRONG", "WORKABLE", "WORKABLE_82_PCT_FLOOR", "TIGHT"}
TERMINAL = {"DEAD", "DO_NOT_CONTACT", "CLOSED", "UNDER_CONTRACT", "ASSIGNED"}
PROOF_MARK = "PROOF - set operator name, phone and mailing address before mailing"


@dataclass
class Letter:
    lead_id: int
    kind: str                 # pre_auction | estate | landlord
    recipient: str
    address_lines: list[str]
    salutation: str
    paragraphs: list[str]
    property_address: str
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _op() -> dict[str, str]:
    op = settings().operator
    digits = "".join(ch for ch in (op.get("phone") or "") if ch.isdigit())
    return {"name": op.get("name") or "", "company": op.get("company_name") or "",
            "phone": format_phone(digits) or "", "email": op.get("email") or "",
            "postal": op.get("mailing_address") or ""}


def operator_ready() -> bool:
    op = _op()
    return bool(op["phone"] and op["postal"] and op["name"] and op["name"] != "Operator")


def letter_kind(lead: Lead) -> str | None:
    p = lead.property
    types = {s.signal_type for s in p.active_signals}
    estate = bool(p.is_estate_owned) or bool(types & {SignalType.ESTATE_OWNER, SignalType.PROBATE}) or any(
        c.contact_type == ContactType.HEIR_EXECUTOR for c in lead.contacts)
    if estate:
        return "estate"
    if types & {SignalType.SHERIFF_SALE, SignalType.LIS_PENDENS}:
        return "pre_auction"
    if p.is_absentee or p.is_out_of_state or types & {SignalType.LONG_TERM_ABSENTEE, SignalType.OUT_OF_STATE_OWNER}:
        return "landlord"
    return None


def mailing_block(lead: Lead) -> tuple[str, list[str], list[str]]:
    """(recipient, address lines, notes). Servicer/escrow tax-bill addresses fall back to the property."""
    p = lead.property
    notes: list[str] = []
    kind = letter_kind(lead)
    name = speaking_name(p.owner_name) if p.owner_name else ""
    executor = next((c for c in lead.contacts if c.contact_type == ContactType.HEIR_EXECUTOR and c.name), None)
    if kind == "estate":
        recipient = (f"{executor.name}, Personal Representative of the Estate of {name}" if executor and name else
                     f"The Family of {name}" if name else "To the Family of the Owner")
    else:
        recipient = name or "Current Owner"
    street, csz = p.owner_mailing_address, p.owner_city_state_zip
    if street and not is_servicer_mailing(street, csz):
        lines = [postal_title(street), postal_title(csz)]
    else:
        if street:
            notes.append("tax bill goes to a loan servicer — mailed to the property instead")
        lines = [postal_title(p.street_address), f"{postal_title(p.city or p.municipality)}, NJ {p.zip_code or ''}".strip()]
    lines = [re.sub(r"\s+", " ", x).strip() for x in lines if x and x.strip()]
    return recipient, lines, notes


def build_letter(lead: Lead) -> Letter | None:
    kind = letter_kind(lead)
    if kind is None:
        return None
    f = lead_facts(lead)
    op = _op()
    recipient, lines, notes = mailing_block(lead)
    prop_addr = f["address"]
    contact_line = f"call or text me at {op['phone']}" if op["phone"] else "call or text me"
    disclosure = (f"So you know who we are: {op['company']} is a real estate investment company. We are not licensed "
                  "real estate agents and we don't represent you. If we agree on a price, we may buy the house "
                  "ourselves or assign our contract to another investor, and you should have your own attorney review "
                  "any agreement — there is a 3-business-day attorney review period before anything is binding.")
    stop = (f"If you'd rather not hear from us, {contact_line} and say STOP, or write to us at the address above, "
            "and we will remove your address right away.")
    if kind == "pre_auction":
        sale = f"on {f['sale_date']}" if f.get("sale_date") else "soon"
        paragraphs = [
            f"I'm writing about {prop_addr}. Public records show the property is scheduled for a sheriff's sale {sale}. "
            "I know that can be a stressful thing to see in writing, and I'm not writing to pressure you.",
            "You have options before a sale date, and the right one depends on your situation: some owners catch up "
            "with their lender, some list the house with an agent, and some sell it as-is to an investor so they can "
            "choose the price, the timing and the move-out date instead of the auction. Free, confidential help is "
            "available from a HUD-approved housing counselor (1-800-569-4287) and Legal Services of New Jersey "
            "(1-888-576-5529), and owner-occupants may qualify for the NJ Foreclosure Mediation Program.",
            "We buy houses as-is — no repairs, no cleanout, no commissions — and close through a New Jersey title "
            f"company or attorney. If selling is something you're considering, {contact_line} and I'll walk through "
            "the numbers with you, with no obligation.",
            "We cannot stop, postpone or negotiate your foreclosure, and we are not offering to. If this house is your "
            "home, any purchase agreement with us is drafted by an attorney and gives you the protections of New "
            "Jersey's Foreclosure Rescue Fraud Prevention Act, including 10 business days to cancel.",
            disclosure, stop]
    elif kind == "estate":
        paragraphs = [
            f"I'm sorry for your loss. I'm writing about {prop_addr}, which public records show is part of an estate.",
            "Handling a house during probate can be a lot: taxes, insurance, utilities, belongings and repairs, often "
            "from out of town. We buy inherited houses as-is — take what matters to you and leave the rest — and we "
            "work on the estate attorney's timeline, including waiting for Letters Testamentary or Administration.",
            f"There's no rush and no obligation. If the family is considering selling, {contact_line} whenever it's a "
            "good time" + (f" (we can also reach you at {op['email']})." if op["email"] else "."),
            disclosure, stop]
        if f.get("sale_date"):
            paragraphs.insert(1, f"Public records also show a sheriff's sale scheduled on {f['sale_date']}. A sale "
                                 "before that date lets the estate keep its equity; the estate attorney or the sheriff's "
                                 "office can explain the process. We cannot stop or delay the sale.")
    else:
        taxes = f" — and NJ property taxes of about ${f['annual_taxes']:,.0f} a year" if f.get("annual_taxes") else ""
        paragraphs = [
            f"I'm writing about your property at {prop_addr}. Owning a rental from a distance takes work: tenants, "
            f"repairs, inspections{taxes}.",
            "If you've thought about selling, we buy as-is with tenants in place (leases and deposits transfer at "
            "closing), with no repairs, showings or commissions, and we close on your schedule through a New Jersey "
            "title company.",
            f"If the timing is right, {contact_line} for a no-obligation number. If not, keep this letter for later.",
            disclosure, stop]
    return Letter(lead_id=lead.id, kind=kind, recipient=recipient, address_lines=lines,
                  salutation=f"Dear {recipient.split(',')[0]}," if recipient != "Current Owner" else "Hello,",
                  paragraphs=paragraphs, property_address=prop_addr, notes=notes)


def eligible(session: Session, lead: Lead, *, all_verdicts: bool = False,
             include_callable: bool = False) -> tuple[bool, str]:
    if lead.status_stage in TERMINAL:
        return False, f"stage {lead.status_stage}"
    kind = letter_kind(lead)
    if kind is None:
        return False, "no letter fits"
    v = lead.property.current_valuation
    if not all_verdicts and (v is None or v.verdict not in DEAL_VERDICTS):
        return False, "no offer verdict"
    if not include_callable and any(c.phone and c.contact_type != ContactType.LISTING_AGENT and c.dnc_status != "dnc"
                                    and (c.confidence_score or 0) > 0 for c in lead.contacts):
        return False, "has a phone — call instead"
    recipient, lines, _ = mailing_block(lead)
    if len(lines) < 2:
        return False, "no mailing address"
    if compliance.address_suppressed(session, ", ".join(lines)) or compliance.address_suppressed(
            session, f"{lead.property.street_address}, {lead.property.city or ''}"):
        return False, "address suppressed"
    return True, "ok"


def select_leads(session: Session, top: int = 50, kinds: set[str] | None = None, **kw: Any) -> list[Lead]:
    stmt = select(Lead).where(Lead.status_stage.notin_(list(TERMINAL))).order_by(Lead.priority_rank_score.desc())
    out: list[Lead] = []
    for lead in session.scalars(stmt):
        if eligible(session, lead, **kw)[0] and (not kinds or letter_kind(lead) in kinds):
            out.append(lead)
        if len(out) >= top:
            break
    return out


# -- rendering ---------------------------------------------------------------------------------------
def letter_html(letter: Letter, proof: bool) -> str:
    op = _op()
    ret = "<br>".join(html.escape(x) for x in [op["name"], op["company"], op["postal"]] if x)
    paras = "".join(f"<p>{html.escape(p)}</p>" for p in letter.paragraphs)
    mark = f"<div class='mark'>{html.escape(PROOF_MARK)}</div>" if proof else ""
    return (f"<section class='letter'>{mark}<div class='ret'>{ret}</div><div class='date'>{long_date(date.today())}</div>"
            f"<div class='to'>{html.escape(letter.recipient)}<br>{'<br>'.join(html.escape(x) for x in letter.address_lines)}</div>"
            f"<p>{html.escape(letter.salutation)}</p>{paras}<p>Sincerely,<br><br>{html.escape(op['name'])}<br>"
            f"{html.escape(op['company'])}{('<br>' + html.escape(op['phone'])) if op['phone'] else ''}</p></section>")


def write_pdf(letters: list[Letter], path: Path, proof: bool) -> None:
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import LETTER
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.units import inch
    from reportlab.platypus import PageBreak, Paragraph, SimpleDocTemplate, Spacer

    op = _op()
    body = ParagraphStyle("b", fontName="Times-Roman", fontSize=11, leading=14.5, spaceAfter=7)
    small = ParagraphStyle("s", parent=body, fontSize=10, leading=12, spaceAfter=0)
    story: list[Any] = []
    for i, letter in enumerate(letters):
        if i:
            story.append(PageBreak())
        for line in [op["name"], op["company"], op["postal"]]:
            if line:
                story.append(Paragraph(html.escape(line), small))
        story += [Spacer(1, 14), Paragraph(f"{long_date(date.today())}", small), Spacer(1, 14)]
        for line in [letter.recipient, *letter.address_lines]:
            story.append(Paragraph(html.escape(line), small))
        story += [Spacer(1, 16), Paragraph(html.escape(letter.salutation), body)]
        story += [Paragraph(html.escape(p), body) for p in letter.paragraphs]
        story += [Paragraph("Sincerely,", body), Spacer(1, 18), Paragraph(html.escape(op["name"]), small),
                  Paragraph(html.escape(op["company"]), small)]
        if op["phone"]:
            story.append(Paragraph(html.escape(op["phone"]), small))

    def decorate(canvas, _doc) -> None:
        if proof:
            canvas.saveState()
            canvas.setFont("Helvetica-Bold", 22)
            canvas.setFillColor(colors.Color(0.8, 0.1, 0.1, alpha=0.15))
            canvas.translate(4.25 * inch, 5.5 * inch)
            canvas.rotate(35)
            canvas.drawCentredString(0, 0, PROOF_MARK)
            canvas.restoreState()

    SimpleDocTemplate(str(path), pagesize=LETTER, leftMargin=1 * inch, rightMargin=1 * inch, topMargin=0.8 * inch,
                      bottomMargin=0.8 * inch, title="Letters").build(story, onFirstPage=decorate, onLaterPages=decorate)


def split_city_state_zip(line: str) -> tuple[str, str, str]:
    """'Brooklyn, NY 11215' -> ('Brooklyn', 'NY', '11215'); best effort for odd formats."""
    m = re.match(r"^\s*(.*?)[,\s]+([A-Za-z]{2})\s+(\d{5}(?:-\d{4})?)\s*$", line or "")
    if m:
        return m.group(1).strip(" ,"), m.group(2).upper(), m.group(3)
    return (line or "").strip(), "", ""


def write_batch(session: Session, top: int = 50, out_dir: Path | None = None, **kw: Any) -> tuple[Path, list[Letter]]:
    leads = select_leads(session, top, **kw)
    letters = [lt for lt in (build_letter(lead) for lead in leads) if lt]
    out_dir = out_dir or MAIL_DIR / datetime.now().strftime("%Y-%m-%d_%H%M%S")
    out_dir.mkdir(parents=True, exist_ok=True)
    proof = not operator_ready()
    by_kind: dict[str, list[Letter]] = {}
    for lt in letters:
        by_kind.setdefault(lt.kind, []).append(lt)
    for kind, group in by_kind.items():
        write_pdf(group, out_dir / f"{kind}.pdf", proof)
    css = ("body{font-family:Georgia,serif;background:#eee}.letter{background:#fff;max-width:7in;margin:20px auto;"
           "padding:.8in;page-break-after:always}.to,.ret,.date{margin-bottom:14px}.mark{color:#b91c1c;font-weight:bold}")
    (out_dir / "letters.html").write_text(
        f"<!doctype html><meta charset='utf-8'><title>Letters</title><style>{css}</style>"
        + "".join(letter_html(lt, proof) for lt in letters), encoding="utf-8")
    with (out_dir / "mailing_list.csv").open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        # Column names Click2Mail's address-list upload maps automatically.
        w.writerow(["Name", "Address1", "Address2", "City", "State", "Zip", "lead_id", "letter", "property_address"])
        for lt in letters:
            city, state, zipc = split_city_state_zip(lt.address_lines[-1] if len(lt.address_lines) > 1 else "")
            middle = lt.address_lines[1:-1]
            w.writerow([lt.recipient, lt.address_lines[0], " ".join(middle), city, state, zipc,
                        lt.lead_id, lt.kind, lt.property_address])
    (out_dir / "manifest.json").write_text(json.dumps(
        {"created": datetime.now().isoformat(timespec="seconds"), "proof": proof,
         "letters": [{"lead_id": lt.lead_id, "kind": lt.kind} for lt in letters]}, indent=1))
    return out_dir, letters


def mark_mailed(session: Session, batch_dir: Path, mailed_on: date | None = None) -> int:
    manifest = json.loads((batch_dir / "manifest.json").read_text())
    if manifest.get("proof"):
        raise ValueError("this batch is a PROOF (operator details missing) — regenerate it before mailing")
    when = datetime.combine(mailed_on or date.today(), datetime.min.time())
    n = 0
    for item in manifest["letters"]:
        lead = session.get(Lead, item["lead_id"])
        if lead is None:
            continue
        session.add(Interaction(lead_id=lead.id, channel="mail", outcome="LETTER_SENT",
                                notes=f"{item['kind']} letter ({batch_dir.name})", occurred_at=when))
        lead.last_contact_at = when
        n += 1
    session.flush()
    return n
