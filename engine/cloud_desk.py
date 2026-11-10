"""The desk without the Mac (learned 2026-11-03: the operator can't keep a laptop plugged in and open).

GitHub Actions (.github/workflows/desk.yml) runs the desk every 15 minutes on GitHub's servers, which reach Gmail
directly (SMTP 465 / IMAP 993). Three repository secrets turn it on: SMTP_USER, SMTP_APP_PASSWORD and DESK_KEY (any
long random string). Nothing else is needed from the operator.

State between runs:
* The working copy (database + operator settings + the calendar sign-in) lives in the Actions cache as ONE file,
  `data/cloud/state.enc`, AES-256-GCM under a key derived from DESK_KEY. The repository is public: the cache is never
  readable without that secret, and no run prints anything but counts (logs are public too).
* Every evening the run e-mails itself an encrypted backup (to its own Gmail Sent folder). If the cache is ever lost,
  the next run restores the newest backup.

The handoff from the Mac (no command, no file to move):
1. The first cloud run finds no state and e-mails itself "[njw-cloud] ready for the handoff" with its public key
   (X25519, derived from DESK_KEY; the private half never leaves the run).
2. The Mac's autopilot daemon sees that message in Sent (read-only IMAP), packs its database + settings + calendar
   sign-in, seals them to that public key (only the cloud can open them), mails them to itself in parts under Gmail's
   size limit, and goes idle (it keeps only self-update and the config hand-over).
3. The next cloud run opens the bundle and takes over. Later calendar sign-ins on the Mac reach the cloud the same way
   ("config" bundles).
"""
from __future__ import annotations

import base64
import email
import hashlib
import imaplib
import io
import json
import logging
import os
import re
import secrets
import smtplib
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
from datetime import date, datetime, timedelta
from email.message import EmailMessage
from email import policy
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

log = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent.parent
STATE_DIR = ROOT / "data" / "cloud"
FLAG = ROOT / "data" / "runtime_cloud.json"          # on the Mac: the desk now runs in the cloud
SUBJECT_READY = "[njw-cloud] ready for the handoff"
SUBJECT_BUNDLE = "[njw-bundle]"
PART_BYTES = 12_000_000                              # x 4/3 for base64 = 16 MB, under Gmail's 25 MB per message
MAGIC_SEAL, MAGIC_LOCK = b"NJWS1", b"NJWL1"
# What a bundle may carry, and nothing else (no .env: the cloud has its own secrets).
DB_NAME = "wholesaling.db"
CONFIG_FILES = ("config/settings.local.yaml", "config/token.json", "config/credentials.json")
_SUBJECT = re.compile(r"\[njw-bundle\] (db|config) (\d{8}T\d{6}) (\d+)/(\d+)")


class CloudError(RuntimeError):
    pass


# -- keys --------------------------------------------------------------------------------------------------------
def _hkdf(secret: bytes, info: bytes) -> bytes:
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=b"njw-cloud-desk", info=info).derive(secret)


def private_key(desk_key: str) -> X25519PrivateKey:
    return X25519PrivateKey.from_private_bytes(_hkdf(desk_key.encode(), b"handoff x25519"))


def public_hex(desk_key: str) -> str:
    return private_key(desk_key).public_key().public_bytes(serialization.Encoding.Raw,
                                                           serialization.PublicFormat.Raw).hex()


def seal(data: bytes, public_key_hex: str) -> bytes:
    """Encrypt to the cloud's public key: only the holder of DESK_KEY can open it (ephemeral X25519 + AES-256-GCM)."""
    eph = X25519PrivateKey.generate()
    eph_pub = eph.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    key = _hkdf(eph.exchange(X25519PublicKey.from_public_bytes(bytes.fromhex(public_key_hex))), b"seal")
    nonce = secrets.token_bytes(12)
    head = MAGIC_SEAL + eph_pub + nonce
    return head + AESGCM(key).encrypt(nonce, data, head)


def unseal(blob: bytes, desk_key: str) -> bytes:
    if not blob.startswith(MAGIC_SEAL):
        raise CloudError("not a sealed bundle")
    eph_pub, nonce = blob[5:37], blob[37:49]
    key = _hkdf(private_key(desk_key).exchange(X25519PublicKey.from_public_bytes(eph_pub)), b"seal")
    return AESGCM(key).decrypt(nonce, blob[49:], blob[:49])


def lock(data: bytes, desk_key: str) -> bytes:
    nonce = secrets.token_bytes(12)
    return MAGIC_LOCK + nonce + AESGCM(_hkdf(desk_key.encode(), b"cache")).encrypt(nonce, data, MAGIC_LOCK)


def unlock(blob: bytes, desk_key: str) -> bytes:
    if not blob.startswith(MAGIC_LOCK):
        raise CloudError("not a locked state file")
    return AESGCM(_hkdf(desk_key.encode(), b"cache")).decrypt(blob[5:17], blob[17:], MAGIC_LOCK)


# -- bundles -----------------------------------------------------------------------------------------------------
def make_bundle(root: Path = ROOT, *, db: bool = True, preset: int = 6) -> bytes:
    """tar.xz of a consistent database copy (VACUUM INTO, safe while it's in use) + the operator's local config."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:xz", preset=preset) as tar, tempfile.TemporaryDirectory() as tmp:
        if db:
            src = root / DB_NAME
            if not src.is_file():
                raise CloudError(f"no database at {src}")
            copy = Path(tmp) / DB_NAME
            con = sqlite3.connect(str(src), timeout=120)
            try:
                con.execute("VACUUM INTO ?", (str(copy),))
            finally:
                con.close()
            tar.add(copy, arcname=DB_NAME)
        for name in CONFIG_FILES:
            p = root / name
            if p.is_file():
                tar.add(p, arcname=name)
    return buf.getvalue()


def apply_bundle(data: bytes, root: Path = ROOT, *, db: bool = True) -> list[str]:
    """Write the bundle's files into place. Only the known names are accepted (no paths from outside)."""
    allowed = set(CONFIG_FILES) | ({DB_NAME} if db else set())
    written = []
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:xz") as tar:
        for m in tar.getmembers():
            if not m.isfile() or m.name not in allowed:
                continue
            f = tar.extractfile(m)
            if f is None:
                continue
            dest = root / m.name
            dest.parent.mkdir(parents=True, exist_ok=True)
            tmp = dest.with_name(dest.name + ".incoming")
            body = f.read()
            if m.name == "config/settings.local.yaml" and dest.is_file():
                body = _keep_desk_settings(dest.read_text(), body)
            tmp.write_bytes(body)
            os.replace(tmp, dest)
            if m.name == "config/token.json":
                dest.chmod(0o600)
            written.append(m.name)
    return written


def _keep_desk_settings(current: str, incoming: bytes) -> bytes:
    """A config bundle from the Mac brings its settings.local.yaml (a new calendar sign-in), but the operator details
    changed on the desk from a chat note stay (2026-11-06: the Mac's re-connect put the old phone back into every
    script and e-mail). Those keys are listed under cloud_desk.operator_keys; everything else comes from the Mac."""
    import yaml
    from engine.operator_setup import HEADER
    try:
        old = yaml.safe_load(current) or {}
        new = yaml.safe_load(incoming.decode("utf-8")) or {}
    except (yaml.YAMLError, UnicodeDecodeError):
        return incoming
    if not isinstance(old, dict) or not isinstance(new, dict):
        return incoming
    keep = old.get("cloud_desk") if isinstance(old.get("cloud_desk"), dict) else {}
    keys = [k for k in (keep.get("operator_keys") or []) if k in OPERATOR_FIELDS]
    old_op = old.get("operator") if isinstance(old.get("operator"), dict) else {}
    if not keys or not any(k in old_op for k in keys):
        return incoming
    op = new.get("operator") if isinstance(new.get("operator"), dict) else {}
    for k in keys:
        if k in old_op:
            op[k] = old_op[k]
    new["operator"], new["cloud_desk"] = op, keep
    return (HEADER + yaml.safe_dump(new, sort_keys=False, allow_unicode=True)).encode("utf-8")


# -- Gmail as the courier ----------------------------------------------------------------------------------------
def _smtp(smtp_factory: Any = None) -> Any:
    from engine import agent_outreach as ao
    return (smtp_factory or ao.smtp_connection)()


def _send_self(user: str, password: str, subject: str, body: str, attachment: bytes | None = None,
               filename: str = "part.bin", smtp_factory: Any = None) -> None:
    from core.mail import clean_app_password
    msg = EmailMessage()
    msg["From"], msg["To"], msg["Subject"] = user, user, subject
    msg.set_content(body)
    if attachment is not None:
        msg.add_attachment(attachment, maintype="application", subtype="octet-stream", filename=filename)
    with _smtp(smtp_factory) as smtp:
        smtp.login(user, clean_app_password(password))
        smtp.send_message(msg)


def _send_self_files(user: str, password: str, subject: str, body: str, files: list[Path],
                     smtp_factory: Any = None) -> None:
    """A note to the operator's own inbox with PDFs attached (the weekly deal digests)."""
    from core.mail import clean_app_password
    msg = EmailMessage()
    msg["From"], msg["To"], msg["Subject"] = user, user, subject
    msg.set_content(body)
    for f in files:
        msg.add_attachment(Path(f).read_bytes(), maintype="application", subtype="pdf", filename=Path(f).name)
    with _smtp(smtp_factory) as smtp:
        smtp.login(user, clean_app_password(password))
        smtp.send_message(msg)


SUBJECT_DIGEST = "[njw-digest]"


def send_digests(user: str, password: str, *, now: datetime | None = None, smtp_factory: Any = None,
                 out_dir: Path | None = None, per_mail: int = 10, digests: bool = True) -> dict[str, int]:
    """Task `digest` (2026-11-09 pivot): one-page Weekly Deal Digests for the named flippers and the top multi-buy
    LLCs (engine/deal_desk.py), mailed to the operator's own inbox in batches as "[njw-digest] <date> i/n". The
    operator forwards each PDF to the buyer it names. Counts only come back (the run log is public)."""
    import tempfile
    from db.database import session_scope
    from engine import deal_desk
    from engine import gmail_autopilot as gm
    day = gm.now_eastern(now).date()
    with tempfile.TemporaryDirectory() as tmp:
        folder = Path(out_dir or tmp)
        with session_scope() as s:
            if digests:
                pairs = deal_desk.write_all(s, folder, today=day)
            else:
                pairs = [(d, folder / "-") for d in deal_desk.build_digests(
                    s, deal_desk.select_buyers(s, today=day), day)]
            deal_desk.seed_prospects(s, [d for d, _ in pairs])
            agents = deal_desk.flipper_agents(s, 50, today=day)
            stages = deal_desk.pipeline_counts(s)
        batches = [pairs[i:i + per_mail] for i in range(0, len(pairs), per_mail)] if digests else []
        for i, batch in enumerate(batches, 1):
            _send_self_files(user, password, f"{SUBJECT_DIGEST} {day:%Y-%m-%d} {i}/{len(batches)}",
                             deal_desk.summary_text(batch), [p for _, p in batch], smtp_factory)
        csv_path = folder / f"flipper_connected_agents_{day:%Y%m%d}.csv"
        csv_path.write_bytes(deal_desk.agents_csv(agents))
        _send_self_files(user, password, f"[njw-agents] {day:%Y-%m-%d} top {len(agents)} flipper-connected agents",
                         "Listing agents closest to the flippers (score: +5 per flip resale they list, +0.5 per active "
                         "cash-buyer family in the town for each fixer listing, capped at 12, +3 if they already wrote back to you).\n"
                         "Pipeline: " + ", ".join(f"{k} {v}" for k, v in stages.items()), [csv_path], smtp_factory)
    return {"digests": len(pairs), "digest_rows": sum(len(d.rows) for d, _ in pairs), "digest_mails": len(batches),
            "flipper_agents": len(agents), **{f"prospects_{k}": v for k, v in stages.items()}}


def send_bundle(user: str, password: str, sealed: bytes, kind: str, *, now: datetime | None = None,
                smtp_factory: Any = None) -> int:
    stamp = (now or datetime.now()).strftime("%Y%m%dT%H%M%S")
    digest = hashlib.sha256(sealed).hexdigest()
    parts = [sealed[i:i + PART_BYTES] for i in range(0, len(sealed), PART_BYTES)] or [b""]
    for i, part in enumerate(parts, 1):
        _send_self(user, password, f"{SUBJECT_BUNDLE} {kind} {stamp} {i}/{len(parts)}",
                   "Encrypted deal-desk bundle (it opens only in your cloud desk). Keep it: it is also a backup.\n"
                   f"sha256={digest}\nbytes={len(sealed)}\n", part, f"njw-{kind}-{stamp}-{i}.bin", smtp_factory)
    return len(parts)


def _sent_subjects(user: str, password: str, days: int) -> tuple[Any, list[tuple[int, str]]]:
    from engine import gmail_autopilot as gm
    mb = gm.Mailbox(user, password, mailbox="\\Sent")
    mb.__enter__()
    since = date.today() - timedelta(days=days)
    uids = mb.search(0, since)
    out = []
    for uid, raw in mb.headers(uids).items():
        head = gm.parse_headers(raw, uid, mb.uidvalidity)
        out.append((uid, head.subject or ""))
    return mb, out


def ready_key(user: str, password: str, days: int = 30) -> str | None:
    """The cloud's public key from its newest "ready for the handoff" message, if there is one."""
    mb, subjects = _sent_subjects(user, password, days)
    try:
        for uid, subject in sorted(subjects, reverse=True):
            if subject.startswith(SUBJECT_READY):
                m = re.search(r"public_key=([0-9a-f]{64})", _body_text(mb.message(uid)))
                if m:
                    return m.group(1)
        return None
    finally:
        mb.__exit__(None, None, None)


def post_ready(user: str, password: str, desk_key: str, *, smtp_factory: Any = None) -> None:
    _send_self(user, password, SUBJECT_READY,
               "Your cloud deal desk is set up and waiting for the database from your Mac.\n"
               "Nothing to do: the next time your Mac is open, it hands everything over by itself and goes quiet.\n\n"
               f"public_key={public_hex(desk_key)}\n", smtp_factory=smtp_factory)


def _body_text(raw: bytes) -> str:
    msg = email.message_from_bytes(raw, policy=policy.default)
    part = msg.get_body(preferencelist=("plain",))
    return part.get_content() if part is not None else ""


def _attachment(raw: bytes) -> bytes:
    msg = email.message_from_bytes(raw, policy=policy.default)
    for part in msg.iter_attachments():
        return part.get_payload(decode=True) or b""
    return b""


def latest_bundle(user: str, password: str, kind: str, *, newer_than: str | None = None,
                  days: int = 30) -> tuple[str, bytes] | None:
    """(stamp, sealed bytes) of the newest complete bundle of this kind in Sent, checked against its SHA-256."""
    mb, subjects = _sent_subjects(user, password, days)
    try:
        sets: dict[str, dict[int, int]] = {}
        totals: dict[str, int] = {}
        for uid, subject in subjects:
            m = _SUBJECT.search(subject)
            if m and m.group(1) == kind:
                stamp, i, n = m.group(2), int(m.group(3)), int(m.group(4))
                sets.setdefault(stamp, {})[i] = uid
                totals[stamp] = n
        for stamp in sorted(sets, reverse=True):
            if newer_than and stamp <= newer_than:
                return None
            parts = sets[stamp]
            if set(parts) != set(range(1, totals[stamp] + 1)):
                continue                                  # still arriving, or a part is missing: try an older one
            raws = [mb.message(parts[i]) for i in range(1, totals[stamp] + 1)]
            want = re.search(r"sha256=([0-9a-f]{64})", _body_text(raws[0]))
            blob = b"".join(_attachment(r) for r in raws)
            if want and hashlib.sha256(blob).hexdigest() == want.group(1):
                return stamp, blob
            log.warning("cloud desk: bundle %s %s failed its checksum", kind, stamp)
        return None
    finally:
        mb.__exit__(None, None, None)


# -- the Mac side ------------------------------------------------------------------------------------------------
def mac_handoff(*, root: Path = ROOT, now: datetime | None = None, smtp_factory: Any = None) -> dict[str, Any]:
    """Called by the Mac's daemon. "local": keep running the desk here; "handed_off" / "idle": the cloud runs it."""
    from engine import gmail_autopilot as gm
    flag = root / FLAG.relative_to(ROOT)
    creds = gm.credentials()
    if not creds:
        return {"state": "local", "reason": "no Gmail credentials"}
    user, pw = creds
    token = root / "config" / "token.json"
    if flag.exists():
        info = json.loads(flag.read_text())
        mtime = token.stat().st_mtime if token.exists() else 0
        if mtime > float(info.get("token_mtime") or 0):
            # A new calendar sign-in on the Mac (setup-calendar): pass it to the cloud.
            send_bundle(user, pw, seal(make_bundle(root, db=False), info["public_key"]), "config", now=now,
                        smtp_factory=smtp_factory)
            info["token_mtime"] = mtime
            flag.write_text(json.dumps(info))
            return {"state": "idle", "sent": "config"}
        return {"state": "idle"}
    pub = ready_key(user, pw)
    if not pub:
        return {"state": "local"}
    parts = send_bundle(user, pw, seal(make_bundle(root, db=True), pub), "db", now=now, smtp_factory=smtp_factory)
    flag.parent.mkdir(parents=True, exist_ok=True)
    flag.write_text(json.dumps({"public_key": pub, "handed_off_at": (now or datetime.now()).isoformat(timespec="seconds"),
                                "parts": parts, "token_mtime": token.stat().st_mtime if token.exists() else 0}))
    return {"state": "handed_off", "parts": parts}


# -- the cloud side ----------------------------------------------------------------------------------------------
def _env() -> tuple[str, str, str] | None:
    user, pw, key = (os.environ.get(k, "").strip() for k in ("SMTP_USER", "SMTP_APP_PASSWORD", "DESK_KEY"))
    return (user, pw, key) if user and pw and len(key) >= 16 else None


def _state_get(key: str) -> Any:
    from db.database import session_scope
    from db.models import SwarmState
    with session_scope() as s:
        row = s.get(SwarmState, "cloud_desk")
        return (row.value_json or {}).get(key) if row else None


def _state_set(**values: Any) -> None:
    from db.database import session_scope
    from db.models import SwarmState
    with session_scope() as s:
        row = s.get(SwarmState, "cloud_desk")
        if row is None:
            row = SwarmState(key="cloud_desk", value_json={})
            s.add(row)
        row.value_json = {**(row.value_json or {}), **values}


def cloud_run(task: str = "run", *, root: Path = ROOT, now: datetime | None = None, smtp_factory: Any = None,
              approve_id: int | None = None, lead_id: int | None = None, note: str | None = None) -> dict[str, Any]:
    """One GitHub Actions run. Returns counts only (the run log is public)."""
    env = _env()
    if env is None:
        return {"state": "not_configured"}
    user, pw, desk_key = env
    state = root / STATE_DIR.relative_to(ROOT) / "state.enc"
    # The public key is safe to print: a Claude chat seals its notes to it (see open_note).
    out: dict[str, Any] = {"state": "ok", "pub": public_hex(desk_key)}
    if state.exists():
        apply_bundle(unlock(state.read_bytes(), desk_key), root)
    if not (root / DB_NAME).exists():
        got = latest_bundle(user, pw, "db")
        if got is None:
            if not ready_key(user, pw):
                post_ready(user, pw, desk_key, smtp_factory=smtp_factory)
                return {"state": "waiting_for_handoff", "ready_posted": True}
            return {"state": "waiting_for_handoff"}
        apply_bundle(unseal(got[1], desk_key), root)
        out["restored"] = got[0]
    _reload(root)
    from db.database import init_db
    init_db()
    try:
        cfg = latest_bundle(user, pw, "config", newer_than=_state_get("config_stamp"))
    except (OSError, imaplib.IMAP4.error) as exc:
        # Gmail dropped the line while looking for a config bundle from the Mac (2026-11-05: three runs failed with
        # TimeoutError / "abort" here, before any work). It's looked for again on the next run.
        cfg = None
        out["config_check"] = type(exc).__name__
    if cfg is not None:
        apply_bundle(unseal(cfg[1], desk_key), root, db=False)
        _state_set(config_stamp=cfg[0])
        _reload(root)
        out["config"] = cfg[0]
        if (root / "config" / "token.json").exists():
            # A new calendar sign-in from the Mac: the 7-day 🔑 reminder counts from now, not from the old one.
            from db.database import session_scope
            from engine import gcal_sync
            with session_scope() as s:
                gcal_sync.note_reconnected(s, gm_now(now))
    try:
        out.update(_work(task, approve_id=approve_id, lead_id=lead_id, now=now, smtp_factory=smtp_factory, user=user,
                         pw=pw, desk_key=desk_key, root=root, note=note))
    finally:
        state.parent.mkdir(parents=True, exist_ok=True)
        state.write_bytes(lock(make_bundle(root, db=True, preset=1), desk_key))     # fast: it's rewritten every run
    return out


def calendar_code(error: str) -> str:
    """A calendar sync error as a short code that is safe for the public run log (no Google message text, no event
    ids): busy-429 / busy-403 (Google's limits), offline-TimeoutError, auth (sign in again), error-400, error."""
    e = error or ""
    m = re.search(r"Google is busy \((\d{3})\)", e)
    if m:
        return "busy-" + m.group(1)
    m = re.search(r"can't reach Google \((\w+)\)", e)
    if m:
        return "offline-" + m.group(1)
    if "sign-in expired" in e or "refused the request" in e or "setup-calendar" in e:
        return "auth"
    m = re.search(r"Google said (\d{3})", e)
    return "error-" + m.group(1) if m else "error"


def gm_now(now: datetime | None) -> datetime:
    from engine.gmail_autopilot import now_eastern
    return now_eastern(now)


# -- notes from the operator's Claude chat -------------------------------------------------------------------------
def seal_note(items: list[dict[str, Any]] | dict[str, Any], public_key_hex: str) -> str:
    """What a Claude chat sends after the operator's calls: JSON sealed to the desk's public key, base64 for the
    workflow input. The public run page shows only ciphertext, and only repository writers can dispatch a run."""
    data = json.dumps(items if isinstance(items, list) else [items]).encode()
    return base64.b64encode(seal(data, public_key_hex)).decode()


def open_note(blob: str, desk_key: str) -> list[dict[str, Any]]:
    data = json.loads(unseal(base64.b64decode(blob), desk_key))
    return data if isinstance(data, list) else [data]


OPERATOR_FIELDS = ("phone", "name", "company_name", "mailing_address", "intro")


def set_operator(values: dict[str, Any]) -> dict[str, str]:
    """Change the operator's own details on the desk (a new business phone, 2026-11-04). Only the known fields; the
    phone must be a 10-digit US number and is stored as (xxx) xxx-xxxx, like My Setup."""
    from engine import operator_setup
    clean: dict[str, str] = {}
    for key, value in values.items():
        if key not in OPERATOR_FIELDS or value is None or not str(value).strip():
            continue
        text = re.sub(r"\s+", " ", str(value)).strip()
        if key == "phone":
            d = operator_setup._digits(text)
            if len(d) != 10:
                raise ValueError("phone must have 10 digits")
            text = f"({d[:3]}) {d[3:6]}-{d[6:]}"
        clean[key] = text
    for key, text in clean.items():
        operator_setup.save_preference("operator", key, text)
    if clean:
        # Remembered, so a later config bundle from the Mac (its own, older settings.local.yaml) doesn't undo them.
        from core.config import settings
        had = list((settings().get("cloud_desk") or {}).get("operator_keys") or [])
        operator_setup.save_preference("cloud_desk", "operator_keys", sorted(set(had) | set(clean)))
    return clean


def fallback_code(reason: str) -> str:
    """A short public code for why the desk wrote no number (the reason text itself can name a price)."""
    r = reason.lower()
    for code, words in (("old_comps", ("comparable sales are old", "old comps")), ("no_sqft", ("living area",)),
                        ("hold", ("hold", "pre-flight")), ("retail", ("retail", "move-in")),
                        ("frfpa", ("frfpa", "occupancy", "82%")), ("sent_above_final", ("already", "above")),
                        ("not_underwritten", ("not underwritten", "no valuation")), ("verdict", ("verdict", "skip"))):
        if any(w in r for w in words):
            return code
    return "other"


def apply_note(blob: str, desk_key: str, *, now: datetime | None = None, smtp_factory: Any = None) -> dict[str, Any]:
    """Each item names a lead and what the operator said or wants, applied through the normal gates:

    - "reply": text for the agent, sent in their Gmail thread with the signature, mailing address and opt-out
      (`send_custom_reply`); "price" attaches the 1-page offer at that price (refused above the walk-away unless
      "over_walkaway", which only the operator decides, e.g. when the stored valuation is for the wrong parcel);
    - "call": a call outcome (NO_ANSWER, LEFT_VM, SPOKE_WARM_FOLLOWUP, SPOKE_NEEDS_OFFER, SPOKE_NOT_INTERESTED, ...)
      with "notes" and "followup_at" (Eastern, ISO), logged with `crm.log_call` after any reply, so its follow-up
      (the next call on the calendar) wins; "followup_at" with no "call" sets the lead's next touch after the reply
      (a check-in date the agent agreed to);
    - "agent_phone": a better number for the listing agent (a cell the office gave out), saved on the agent's card
      ("agent_name" with it: the office named a different person, saved as her own contact);
    - "code" (+ "result", "notes", "date"): a call-sheet result, resolved on that day's frozen sheet;
    - "street" in place of "lead": the lead found by its street ("2507 Columbia Ave"), a unique match only;
    - "agent_email" (+ "agent_name"): the person now handling the listing (an agent's "contact Geri, I've CC'd her"),
      saved as the lead's listing-agent e-mail; a "reply" in the same item goes to that address in the thread;
    - "operator" (no "lead"): the operator's own details for every e-mail, LOI, script and voicemail line, e.g.
      {"operator": {"phone": "9085550100"}} (validated like My Setup, saved in the desk's settings.local.yaml).
    - "sqft" / "beds" / "baths": facts the agent gave (an e-mail, a call), saved on the property, and the house is
      re-valued live, so an "agree to nothing (the living area isn't on record)" row can get a real number;
    - "close_replies": a short reason; every reply still waiting on the lead is closed (dismissed) with it, so the
      desk sends nothing more on it (the operator or Claude settled it: a call, a polite no, a "sharing with the
      sellers"); "channel" (call / email) is how the "call" outcome was heard;
    - "price": "firm" / "final" / "sent" with "{price}" in the reply: the desk's own locked number is written in and
      the LOI attached at it (`offer_math.note_price`); a house that may not get a number in writing (HOLD, no living
      area, old comps, retail, FRFPA, offered above today's final) is refused and nothing is sent;
    - "cold_guard": "reset" (no "lead"): start the bounce count over (engine/cold_guard.reset);
    - "daniels_law" + "done" (no "lead"): the operator looked at that Daniel's Law request; "suppress_address" /
      "suppress_phone": a home address / phone the request named that the parser missed (source daniels_law);
    - "prospect" (no "lead"): a flipper's public business contacts / pitch stage for the Deal Desk pipeline
      ({"entity", "stage", "email", "website", "phone", "linkedin", "source", "notes", "next_touch", "fee"}).
    Counts only come back: the run log is public."""
    from db.database import session_scope
    from db.models import Lead
    from engine import agent_outreach as ao
    from engine import crm
    from engine import gmail_autopilot as gm
    try:
        items = open_note(blob, desk_key)
    except Exception:                              # noqa: BLE001 - a bad note is reported, never printed
        return {"note_error": "unreadable"}
    done, errors, facts, firmed, priced, fallbacks = 0, [], 0, 0, 0, 0
    why: list[str] = []
    for item in [i for i in items if isinstance(i, dict) and isinstance(i.get("operator"), dict)]:
        try:
            set_operator(item["operator"])
            done += 1
        except Exception as exc:                   # noqa: BLE001 - reported by name only
            errors.append(type(exc).__name__)
        items = [i for i in items if i is not item]
    for item in [i for i in items if isinstance(i, dict) and i.get("cold_guard") == "reset"]:
        # {"cold_guard": "reset"}: the operator restarts the bounce count (engine/cold_guard.reset); bounced addresses
        # stay off every list, a Gmail block-code pause stays.
        try:
            from engine import cold_guard
            with session_scope() as s:
                cold_guard.reset(s, now)
            done += 1
        except Exception as exc:                   # noqa: BLE001 - reported by name only
            errors.append(type(exc).__name__)
        items = [i for i in items if i is not item]
    for item in [i for i in items if isinstance(i, dict) and (i.get("daniels_law") or i.get("suppress_address")
                                                                 or i.get("suppress_phone"))]:
        # Daniel's Law (engine/daniels_law.py): {"daniels_law": "<id>", "done": true} after the operator looked at
        # the request; {"suppress_address": "12 Main St, Edison NJ 08817"} / {"suppress_phone": "..."} for anything
        # the parser missed (a scanned attachment), always with source daniels_law.
        try:
            from engine import compliance
            from engine import daniels_law as dlaw
            with session_scope() as s:
                if item.get("suppress_address"):
                    if compliance.suppress_address(s, str(item["suppress_address"]), source="daniels_law",
                                                   notes=f"added from the chat {now:%Y-%m-%d}") is None:
                        raise ValueError("unreadable address")
                if item.get("suppress_phone"):
                    compliance.add_internal(s, str(item["suppress_phone"]), source="daniels_law",
                                            notes=f"added from the chat {now:%Y-%m-%d}")
                if item.get("daniels_law") and item.get("done") and not dlaw.confirm(s, str(item["daniels_law"])):
                    raise ValueError("no such Daniel's Law request")
            done += 1
        except Exception as exc:                   # noqa: BLE001 - reported by name only
            errors.append(type(exc).__name__)
        items = [i for i in items if i is not item]
    for item in [i for i in items if isinstance(i, dict) and isinstance(i.get("prospect"), dict)]:
        # A flipper's public business contacts or where the Deal Desk pitch stands (engine/deal_desk.py).
        try:
            from engine import deal_desk
            with session_scope() as s:
                deal_desk.update_prospect(s, item["prospect"], now=now)
            done += 1
        except Exception as exc:                   # noqa: BLE001 - reported by name only
            errors.append(type(exc).__name__)
        items = [i for i in items if i is not item]
    for item in [i for i in items if isinstance(i, dict) and (i.get("firm_reply") or isinstance(i.get("firm_target"), dict))]:
        # The research firm (engine/firm_outbox.py): an answer in a B2B prospect's thread, or a prospect's details /
        # stage ("TRIAL" when they said yes, "PAYING" once they pay, "LOST").
        try:
            from engine import firm_outbox as fo
            with session_scope() as s:
                if item.get("firm_reply"):
                    if (item.get("reply") or "").strip():
                        fo.answer_reply(s, int(item["firm_reply"]), str(item["reply"]), stage=item.get("stage"), now=now)
                    else:
                        from db.models import FirmReply, FirmTarget
                        r = s.get(FirmReply, int(item["firm_reply"]))
                        if r is None:
                            raise ValueError("no such firm reply")
                        r.status = "closed"
                        t = s.get(FirmTarget, r.target_id) if r.target_id else None
                        if t is not None and item.get("stage"):
                            fo.set_stage(s, t, str(item["stage"]))
                else:
                    t = dict(item["firm_target"])
                    row = fo.upsert_target(s, kind=str(t.get("kind") or "other"), stream=str(t.get("stream") or "s2"),
                                           email=t.get("email"), org=t.get("org"), name=t.get("name"),
                                           phone=t.get("phone"), website=t.get("website"), town=t.get("town"),
                                           county=t.get("county"), source=t.get("source"),
                                           data=t.get("data") if isinstance(t.get("data"), dict) else None)
                    if row is None:
                        raise ValueError("firm target needs an e-mail or a website / phone")
                    if t.get("stage"):
                        fo.set_stage(s, row, str(t["stage"]))
            done += 1
        except Exception as exc:                   # noqa: BLE001 - reported by name only
            errors.append(type(exc).__name__)
        items = [i for i in items if i is not item]
    codes = [i for i in items if isinstance(i, dict) and i.get("code")]
    if codes:
        # Call-block results ("A07 vm", "A04 340"): resolved on that day's frozen call sheet.
        from engine import call_sheet as cs
        with session_scope() as s:
            res = cs.apply_codes(s, codes, now=now, smtp_factory=smtp_factory)
        done += int(res.get("logged") or 0)
        if res.get("unknown"):
            errors.append(f"unknown_code x{res['unknown']}")
        items = [i for i in items if i not in codes]
    for item in items:
        try:
            with session_scope() as s:
                lead_id = int(item["lead"]) if item.get("lead") else gm.resolve_lead(s, str(item["street"]))
            with session_scope() as s:
                lead = s.get(Lead, lead_id)
                if lead is None:
                    raise ValueError("no such lead")
                if item.get("agent_phone"):
                    # A better number for the listing agent heard on a call ("the office gave me his cell"): saved on
                    # the agent's card as a wireless business line, so the call sheet and call-backs dial it first.
                    from core.text import normalize_phone
                    from db.repository import upsert_contact
                    num = normalize_phone(str(item["agent_phone"]))
                    base = ao.agent_contact(lead) or next(
                        (x for x in lead.contacts if x.contact_type == "LISTING_AGENT"), None)
                    if num and base is not None:
                        # "agent_name": the office handed us the real listing agent ("Patricia handles it"): a new
                        # person with her own number, not the office's e-mail.
                        other = str(item.get("agent_name") or "").strip()
                        c, _ = upsert_contact(s, lead=lead, contact_type="LISTING_AGENT", source="chat_note",
                                              name=other or base.name, phone=num, email=None if other else base.email)
                        c.phone_type, c.company = "wireless", c.company or base.company
                        c.confidence_score = max(float(c.confidence_score or 0), 0.9)
                        s.flush()
                new_email = str(item.get("agent_email") or "").strip().lower()
                if new_email:
                    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[a-z]{2,}", new_email):
                        raise ValueError("bad agent_email")
                    from db.repository import upsert_contact
                    base = ao.agent_contact(lead) or next(
                        (x for x in lead.contacts if x.contact_type == "LISTING_AGENT"), None)
                    c, _ = upsert_contact(s, lead=lead, contact_type="LISTING_AGENT", source="chat_note",
                                          name=str(item.get("agent_name") or (base.name if base else "")) or None,
                                          email=new_email)
                    c.company = c.company or (base.company if base else None)
                    c.confidence_score = max(float(c.confidence_score or 0), 0.95)
                    s.flush()
                if item.get("sqft") or item.get("beds") or item.get("baths"):
                    # Facts an agent gave us (2026-11-06: "2 beds, 2 full baths" by e-mail, the size on a call): most
                    # "agree to nothing" rows wait only on the living area. Saved on the property and the house is
                    # re-valued live, so the next script / reply can say a real number.
                    tier = save_facts(s, lead, item)
                    facts += 1
                    firmed += int(tier == "firm")
                if (item.get("reply") or "").strip():
                    text, price = str(item["reply"]), item.get("price") or None
                    over = bool(item.get("over_walkaway"))
                    if isinstance(price, str) and not re.fullmatch(r"[\d,.]+", price.strip()):
                        # "firm" / "final" / "sent": the desk's own locked number fills "{price}" (2026-11-09: warm
                        # threads answered from the chat without a figure typed from memory); refused with the reason
                        # when the house may not get a number in writing. It never exceeds the final.
                        from engine import offer_math
                        from engine import human_copy as hc
                        if "{price}" not in text:
                            raise ValueError("a symbolic price needs {price} in the reply")
                        try:
                            price = offer_math.note_price(s, lead, price, today=gm.now_eastern(now).date(),
                                                          at_least=float(item.get("at_least") or 0))
                            text, over = text.replace("{price}", hc.money(price)), True
                            priced += 1
                        except ValueError as refused:
                            # No number allowed in writing on this house today: the item's "fallback" (a no-price
                            # answer, e.g. the one fact the number needs) goes instead, so the thread is still answered.
                            if not (item.get("fallback") or "").strip():
                                raise
                            text, price, fallbacks = str(item["fallback"]), None, fallbacks + 1
                            why.append(fallback_code(str(refused)))
                    elif isinstance(price, str):
                        price = float(price.replace(",", ""))
                    gm.send_custom_reply(s, lead_id, text, price=price, now=now,
                                         smtp_factory=smtp_factory, to=new_email or None, allow_over_walkaway=over)
                if (item.get("close_replies") or "").strip():
                    gm.close_open_replies(s, lead_id, "closed from the chat: " + str(item["close_replies"])[:120])
                if item.get("followup_at") and not item.get("call"):
                    # A reply with no call: the next touch is this date (a 30-day check-in the agent agreed to).
                    lead.next_followup_at = datetime.fromisoformat(item["followup_at"]).replace(tzinfo=None)
                if item.get("call"):
                    when = datetime.fromisoformat(item["followup_at"]) if item.get("followup_at") else None
                    channel = str(item.get("channel") or "call")
                    crm.log_call(s, lead, str(item["call"]), notes=str(item.get("notes") or "")[:600],
                                 channel=channel if channel in ("call", "email", "sms") else "call",
                                 contact=ao.agent_contact(lead) or next(
                                     (x for x in lead.contacts if x.contact_type == "LISTING_AGENT"), None),
                                 followup_at=when, debrief={"source": "chat_note"})
            done += 1
        except Exception as exc:                   # noqa: BLE001 - one bad item never stops the rest
            errors.append(type(exc).__name__)
    out: dict[str, Any] = {"note_done": done}
    if facts:
        out["facts_saved"], out["now_firm"] = facts, firmed
    if priced:
        out["desk_priced"] = priced
    if fallbacks:
        out["desk_fallback"] = fallbacks
        out["desk_fallback_why"] = ",".join(why)          # short codes only (the public log names no house)
    if errors:
        out["note_errors"] = ",".join(errors)
    return out


def save_facts(session: Any, lead: Any, item: dict[str, Any]) -> str:
    """Living area / beds / baths an agent stated, saved on the property ("agent" as the source; a public record or
    listing figure already there is replaced, since the agent's sheet describes the house as it is now) and the lead
    re-valued live (`offer_math.firm_up`). Returns the new tier."""
    from engine import offer_math
    prop = lead.property
    sqft = int(float(str(item.get("sqft") or 0).replace(",", "") or 0))
    if sqft:
        if not 300 <= sqft <= 15000:
            raise ValueError("sqft out of range")
        prop.sqft, prop.sqft_source = sqft, "agent"
    if item.get("beds"):
        prop.beds = float(item["beds"])
    if item.get("baths"):
        prop.baths = float(item["baths"])
    session.flush()
    return offer_math.firm_up(session, lead).tier


def _reload(root: Path) -> None:
    from core import config
    from engine import operator_setup
    config.reload_settings()
    operator_setup.sync_from_disk()


INBOX_RETRY_WAIT = 10


def _inbox_once_more(step: Any) -> Any:
    """The inbox step, tried once more after a short pause when Gmail times out or drops the line (2026-11-09: 6 of
    29 daytime runs lost their inbox check to a TimeoutError, two in a row). Safe to repeat: replies are stored once
    per Message-ID, the next read starts after the last UID seen, and a queued answer is claimed before it is sent."""
    import socket
    import time as _time
    try:
        return step()
    except (TimeoutError, socket.timeout, imaplib.IMAP4.abort, ConnectionResetError):
        _time.sleep(INBOX_RETRY_WAIT)
        return step()


def _work(task: str, *, approve_id: int | None, now: datetime | None, smtp_factory: Any, user: str, pw: str,
          desk_key: str, root: Path, lead_id: int | None = None, note: str | None = None) -> dict[str, Any]:
    from db.database import session_scope
    from engine import gcal_sync
    from engine import gmail_autopilot as gm
    out: dict[str, Any] = {}
    t = gm.now_eastern(now)
    if task == "refresh":
        # With live listings the full pipeline outgrew 45 minutes (2026-11-04: a midday refresh was cut off at 45 min
        # and the run failed). Before 7 AM it may take 80 minutes (nobody is waiting on the inbox); in the day 40. A
        # cut-off is not a failure: every agent commits as it goes, and the rest of this run (re-price, firm-up, the
        # sheet, the inbox, the calendar) still happens.
        limit = (80 if t.hour < 7 else 40) * 60
        try:
            r = subprocess.run([sys.executable, "-m", "swarm.orchestrator", "run-all"], cwd=root, timeout=limit,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            out["refresh"] = "ok" if r.returncode == 0 else f"exit {r.returncode}"
        except subprocess.TimeoutExpired:
            out["refresh"] = f"cut off at {limit // 60} min"
    from engine.offer_math import PRICING_VERSION
    if task in ("recalibrate", "refresh") or _state_get("pricing_version") != PRICING_VERSION:
        # New pricing rules (or fresh data): re-price every stored valuation offline (~20 s for 2,000) before any send.
        from engine import quant_pricing
        with session_scope() as s:
            rc = quant_pricing.recalibrate_all(s)
        _state_set(pricing_version=PRICING_VERSION)
        out.update(repriced=int(rc.get("repriced") or 0), reprice_errors=int(rc.get("errors") or 0))
    if task in ("refresh", "firmup"):
        # Near-firm listings whose only gap is stale comps / no living area: re-valued live with recent sold MLS
        # comps and the listing's own record, so the call sheet and e-mails carry more firm numbers
        # (engine/offer_math.py::firm_up_pass). Task `firmup` does only this, today's call-sheet houses first
        # (~13 s each), without the 45-minute refresh.
        from engine import call_sheet as cs
        from engine.offer_math import firm_up_pass
        from core.config import settings
        cfg = settings().get("call_sheet") or {}
        with session_scope() as s:
            sheet = cs.lead_ids_on(s, t.date()) if task == "firmup" else set()
            fu = firm_up_pass(s, int(cfg.get("firm_up_now", 45) if task == "firmup" else cfg.get("firm_up_daily", 30)),
                              budget_seconds=900 if task == "firmup" else 600, first=sheet)
        out.update(firm_up=int(fu.get("revalued") or 0), now_firm=int(fu.get("now_firm") or 0))
    if task == "run" and t.weekday() < 6 and 8 <= t.hour < 20:
        # Supply grows all day, not only at the 5:17 refresh: a few unvalued listings and listings flagged for a
        # re-underwrite are valued live in each run (small budget, daily cap; engine/offer_math.py::trickle).
        from engine.offer_math import trickle
        try:
            with session_scope() as s:
                out.update(trickle(s))
        except Exception:                          # noqa: BLE001 - never stops the inbox or the sends
            out["trickle_error"] = 1
        try:
            from swarm.orchestrator import run_agent
            r = run_agent("value_new_listings", cap=4, max_minutes=1.0)
            out["new_valued"] = int(((r.metrics_json or {}).get("valued") or 0) if r is not None else 0)
        except Exception:                          # noqa: BLE001
            out["new_value_error"] = 1
    if task == "value":
        # Price the listings a harvest brought in (2026-11-04: the first cloud harvest found 1,567 unvalued listings
        # and used 42 of the refresh's 45 minutes): after 9 PM / before 7 AM up to 70 minutes, in the day 30.
        from swarm.orchestrator import run_agent
        night = t.hour >= 21 or t.hour < 7
        r = run_agent("value_new_listings", cap=600, max_minutes=70 if night else 30)
        m = (r.metrics_json or {}) if r is not None else {}
        out.update(new_valued=int(m.get("valued") or 0), unvalued_left=int(m.get("left_for_next_run") or 0))
    if task == "stats":
        # Where the pipeline stands (engine/outreach_stats.py): counts only, safe for the public run log.
        from engine.outreach_stats import flat, funnel
        with session_scope() as s:
            out.update(flat(funnel(s, t.replace(tzinfo=None))))
    if os.environ.get("CALL_SHEET", "on").strip().lower() == "off":
        # Paused (2026-11-09, the operator stopped the 100-call MLS sheet): no new sheet, and today's frozen one is
        # cleared so its call blocks and catch-up event leave the calendar on the next sync.
        from db.models import SwarmState
        from engine import call_sheet as cs
        with session_scope() as s:
            row = s.get(SwarmState, cs.STATE_KEY)
            if row is not None and (row.value_json or {}).get("rows"):
                row.value_json = {}
        out["call_sheet"] = 0
    elif t.weekday() < 6 and 7 <= t.hour < 21:
        # The day's call sheet (engine/call_sheet.py), frozen at its first build so codes mean the same call all day;
        # task `sheet` rebuilds the blocks nobody has called yet with today's rules.
        from engine import call_sheet as cs
        with session_scope() as s:
            rows = cs.rebuild_unstarted(s, t.replace(tzinfo=None)) if task == "sheet" else cs.today(s, t.replace(tzinfo=None))
            out["call_sheet"] = len(rows)
    if task == "approve" and approve_id:
        try:
            with session_scope() as s:
                gm.approve(s, approve_id, now=now, smtp_factory=smtp_factory)
            out["approved"] = 1
        except (gm.ApprovalError, RuntimeError) as exc:
            out["approve_error"] = type(exc).__name__
    if task == "dismiss" and approve_id:
        # After a call that settled it ("they took another offer", "I'll call back"): the reply stops waiting.
        try:
            with session_scope() as s:
                gm.dismiss(s, approve_id, "handled on a call")
            out["dismissed"] = 1
        except gm.ApprovalError as exc:
            out["dismiss_error"] = type(exc).__name__
    if task == "note" and note:
        out.update(apply_note(note, desk_key, now=now, smtp_factory=smtp_factory))
    if task in ("digest", "agents"):
        # "agents": only the flipper-connected agents report + the prospect pipeline (no digests re-sent).
        try:
            out.update(send_digests(user, pw, now=now, smtp_factory=smtp_factory, digests=task == "digest"))
        except Exception as exc:                   # noqa: BLE001 - reported by name only (public log)
            out["digest_error"] = type(exc).__name__
    if task == "offer" and lead_id:
        # After a call ("send it in writing"): the lead's next e-mail, the same one `gmail --send-offer` sends (Touch 2
        # at the desk's own opening price, with the Offer Summary and the LOI, in the agent's thread). Only ids travel
        # through the public workflow inputs; the price never does, so nothing here can go above the walk-away.
        from engine import autopilot
        try:
            with session_scope() as s:
                r = autopilot.send(s, lead_id, "smtp", now=now, smtp_factory=smtp_factory)
            out["offer_sent"] = 1 if r.get("kind") else 0
        except (autopilot.AutopilotError, gm.ApprovalError, RuntimeError) as exc:
            out["offer_error"] = type(exc).__name__
    if 8 <= t.hour < 21:
        try:
            r = _inbox_once_more(lambda: gm.run_inbox(send=gm.in_send_hours(now), now=now, smtp_factory=smtp_factory))
            p = r.get("poll") or {}
            out.update(new=int(p.get("checked") or 0), replies=int(p.get("matched") or 0),
                       sent=int(r.get("sent_now", 0)) + int(p.get("sent") or 0),
                       waiting=sum(1 for x in r.get("rows", []) if x["status"] == "needs_approval"))
            if p.get("daniels_law"):
                out["daniels_law"] = int(p["daniels_law"])     # nondisclosure requests honored (counts only)
            if r.get("send_error") or not r.get("ok"):
                out["gmail_error"] = True
            if gm.in_send_hours(now):
                a = gm.auto_approve(now=now, smtp_factory=smtp_factory)
                out["auto_answered"] = len(a["sent"])
                d = gm.daily_openers(int(os.environ.get("DAILY_OPENERS", "30")),
                                     at=os.environ.get("OPENER_WAVES", "10:30,14:00,16:30"), now=now,
                                     smtp_factory=smtp_factory, background=False)
                if d is not None:
                    out["openers"] = int(d.get("sent") or 0)
        except (OSError, imaplib.IMAP4.error, smtplib.SMTPException) as exc:
            # Gmail dropped the line (2026-11-05: a TimeoutError and an IMAP "abort" each failed a whole run). What was
            # read or sent is committed per message; the next run ~6 minutes later picks up from the last UID it saw.
            # The calendar still syncs and the run stays green.
            out["gmail_error"] = type(exc).__name__
    if os.environ.get("FIRM", "on").strip().lower() != "off" and (7 <= t.hour < 21 or task == "firm-run"):
        # The 10-stream research firm (engine/swarm_firm.py): due streams inside a time budget, then the priority
        # outbox. task `firm-run` runs every stream now with a long budget.
        try:
            from engine import swarm_firm
            r = swarm_firm.tick(now=now, smtp_factory=smtp_factory, send=True, force=task == "firm-run",
                                budget_seconds=900 if task == "firm-run" else None)
            ran = {k: v for k, v in (r.get("streams") or {}).items() if isinstance(v, dict) and "status" not in v}
            out["firm_streams_run"] = len(ran)
            errs = [k for k, v in ran.items() if v.get("error")]
            if errs:
                out["firm_errors"] = ",".join(errs)
            box = r.get("outbox") or {}
            out["firm_sent"] = int(box.get("sent") or 0)
            if box.get("error"):
                out["firm_send_error"] = str(box["error"])[:40]
            if box.get("blocked"):
                out["firm_blocked"] = str(box["blocked"])[:60]
            if r.get("targets_seeded"):
                out["firm_targets_seeded"] = int(r["targets_seeded"])
        except Exception as exc:                   # noqa: BLE001 - the firm never stops the desk
            out["firm_error"] = type(exc).__name__
    if task in ("firm", "firm-run"):
        from engine import swarm_firm
        with session_scope() as s:
            out.update(swarm_firm.flat(swarm_firm.status(s, t.replace(tzinfo=None))))
    if gcal_sync.configured():
        try:
            with session_scope() as s:
                res = gcal_sync.sync(s, now=t.replace(tzinfo=None))
            out["calendar"] = "ok" if not res.error else calendar_code(res.error)
            if res.error:
                out["calendar_queued"] = int(res.queued or 0)
        except Exception as exc:                   # noqa: BLE001 - the calendar never stops the desk
            out["calendar"] = type(exc).__name__
    if t.hour >= 20 and _state_get("backup_day") != t.date().isoformat():
        parts = send_bundle(user, pw, seal(make_bundle(root, db=True), public_hex(desk_key)), "db", now=t,
                            smtp_factory=smtp_factory)
        _state_set(backup_day=t.date().isoformat())
        out["backup_parts"] = parts
    return out
