"""Gmail plumbing shared by the SMTP sender (engine/agent_outreach.py) and the IMAP reader (engine/gmail_autopilot.py).

* ssl_context(): certificates from certifi when it is installed. The python.org macOS installer ships without a
  system certificate store (until "Install Certificates.command" is run), and then every TLS connection to
  smtp.gmail.com / imap.gmail.com fails with SSLCertVerificationError.
* clean_app_password(): Google shows the app password as "abcd efgh ijkl mnop"; spaces and hyphens are not part of it.
* explain(): the plain-English reason behind an SMTP / IMAP / network failure. The dashboard shows it instead of a
  bare 500. The password is never part of the text.
"""
from __future__ import annotations

import re
import smtplib
import socket
import ssl
from typing import Any


class GmailSendError(RuntimeError):
    """Gmail did not take the message; str() is the sentence to show the operator. fatal: every other e-mail
    would fail the same way (sign-in, certificates, network), so a batch stops instead of trying the rest."""

    def __init__(self, message: str, *, fatal: bool = False):
        super().__init__(message)
        self.fatal = fatal


_RESERVED_TLDS = {"test", "example", "invalid", "localhost", "local"}
_DOMAIN_CACHE: dict[str, bool] = {}


def domain_missing(addr: str | None) -> bool:
    """True only when DNS says the recipient's domain does not exist (NXDOMAIN): a cold e-mail there can only bounce
    (2026-11-10: "the domain atbnj.com couldn't be found" was the third bounce in 48 hours and paused all cold mail),
    and every bounce counts against the one Gmail account. A temporary DNS failure, a domain with mail records but no
    website (EAI_NODATA), a resolver that can't find gmail.com either, and the reserved test domains are never
    "missing". One lookup per domain per process."""
    domain = (addr or "").rpartition("@")[2].strip().lower().rstrip(".")
    if not domain or "." not in domain or domain.rsplit(".", 1)[-1] in _RESERVED_TLDS:
        return False
    if domain not in _DOMAIN_CACHE:
        _DOMAIN_CACHE[domain] = _nxdomain(domain) and not _nxdomain("gmail.com")
    return _DOMAIN_CACHE[domain]


def _nxdomain(domain: str) -> bool:
    try:
        socket.getaddrinfo(domain, None)
        return False
    except socket.gaierror as exc:
        return exc.errno == socket.EAI_NONAME
    except OSError:
        return False


def is_fatal(exc: BaseException) -> bool:
    if isinstance(exc, (smtplib.SMTPRecipientsRefused, smtplib.SMTPDataError)):
        return False                       # this one address / message only
    return isinstance(exc, (smtplib.SMTPAuthenticationError, smtplib.SMTPSenderRefused, ssl.SSLError, OSError,
                            smtplib.SMTPServerDisconnected))


def is_transient(exc: BaseException) -> bool:
    """A failure worth one more try a few seconds later: Gmail dropped the line (it does this after a burst of
    sign-ins, on a flaky Wi-Fi, or with a 421 "try again later"), or answered with a temporary 4xx code. A refused
    sign-in, a certificate problem, a refused address or a blocked port is not: it fails the same way every time."""
    if isinstance(exc, (smtplib.SMTPAuthenticationError, smtplib.SMTPRecipientsRefused, ssl.SSLError,
                        ConnectionRefusedError, socket.gaierror)):
        return False
    if isinstance(exc, (smtplib.SMTPServerDisconnected, smtplib.SMTPConnectError, ConnectionResetError,
                        ConnectionAbortedError, BrokenPipeError)):
        return True
    code = getattr(exc, "smtp_code", None)
    return isinstance(code, int) and 400 <= code < 500


def ssl_context() -> ssl.SSLContext:
    try:
        import certifi
    except ImportError:                                   # pragma: no cover - certifi ships with requests
        return ssl.create_default_context()
    return ssl.create_default_context(cafile=certifi.where())


def clean_app_password(password: str | None) -> str:
    return re.sub(r"[\s\-]", "", password or "").strip()


def _google_text(exc: Any) -> str:
    raw = getattr(exc, "smtp_error", b"") or b""
    text = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else str(raw)
    text = re.sub(r"\s+", " ", text).strip()
    text = re.sub(r"\s*https?://\S+", "", text)
    return f"{getattr(exc, 'smtp_code', '')} {text}".strip()[:160]


def explain(exc: BaseException, user: str = "", host: str = "smtp.gmail.com") -> str:
    """One or two sentences the operator can act on."""
    if isinstance(exc, smtplib.SMTPAuthenticationError):
        return (f"Gmail refused the sign-in for {user or 'your Gmail address'} ({_google_text(exc)}). In ⚙️ My Setup use a "
                "16-letter app password created on that same Google account (Google Account > Security > 2-Step "
                "Verification on > App passwords); your normal Gmail password won't work.")
    if isinstance(exc, ssl.SSLCertVerificationError) or "CERTIFICATE_VERIFY_FAILED" in str(exc):
        return ("This Mac's Python couldn't verify Gmail's security certificate. In Terminal run "
                "`cd ~/realestateauto && .venv/bin/pip install -U certifi`, then restart the dashboard (or double-click "
                "'Install Certificates.command' in Applications > Python 3.x).")
    if isinstance(exc, smtplib.SMTPRecipientsRefused):
        to = ", ".join(exc.recipients) if getattr(exc, "recipients", None) else "the agent"
        return f"Gmail refused the agent's address ({to}): it may be mistyped or closed. Call or text them instead."
    if isinstance(exc, smtplib.SMTPSenderRefused):
        return f"Gmail refused to send from {user or 'this address'} ({_google_text(exc)})."
    if isinstance(exc, smtplib.SMTPDataError):
        text = _google_text(exc)
        if "limit" in text.lower() or str(getattr(exc, "smtp_code", "")).startswith("55"):
            return f"Gmail stopped this message ({text}). If it mentions a sending limit, wait a day before sending more."
        return f"Gmail rejected the message ({text})."
    if isinstance(exc, smtplib.SMTPServerDisconnected):
        return ("Gmail closed the connection before the message went out, so nothing was sent. It goes out on "
                "the next inbox check, or run the same command again in a minute.")
    if isinstance(exc, socket.gaierror):
        return f"Couldn't find {host}: check the internet connection and try again."
    if isinstance(exc, (TimeoutError, socket.timeout)):
        return (f"{host} didn't answer in time. Check the internet connection (school and work Wi-Fi sometimes block "
                "e-mail ports) and try again.")
    if isinstance(exc, ConnectionRefusedError):
        return f"{host} refused the connection: this network may block e-mail ports. Try another Wi-Fi or a hotspot."
    if isinstance(exc, ssl.SSLError):
        return f"The secure connection to {host} failed ({exc.__class__.__name__}: {exc}). Try again in a minute."
    if isinstance(exc, smtplib.SMTPException):
        return f"Gmail error: {exc}"
    if isinstance(exc, OSError):
        return f"Couldn't reach {host} ({exc}). Check the internet connection and try again."
    return f"{type(exc).__name__}: {exc}"
