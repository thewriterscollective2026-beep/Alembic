"""IMAP/SMTP mail engine — the non-Google path.

Mirrors the function surface of gmail_service so the pipeline can drive either
provider through the same calls:

  scan_newsletters(conn)                          -> [ {email,name,count,has_unsubscribe} ]
  fetch_new_from_senders(conn, senders, since)    -> [ parsed email dict ]
  send_tracked(conn, self_email, subject, html)   -> (ref_id, ref_id)     # for approval emails
  get_new_reply(conn, ref_id, awaiting_id)        -> (msg_id, text) | None
  send_personalized(conn, from_email, tos, subj, html_template) -> int

A "conn" is a dict: {email, password, imap_host, imap_port, smtp_host, smtp_port}.
"""
import email
import imaplib
import re
import smtplib
import ssl
import uuid
from datetime import datetime, timedelta
from email.header import decode_header, make_header
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import parseaddr, formatdate, make_msgid

from bs4 import BeautifulSoup

from . import config
from .attachments import extract_text
from .gmail_service import first_name_from_email, GREETING_TOKEN  # reuse personalization


# ---------------------------------------------------------------------------
# Known provider presets (auto-fill server settings from the email domain)
# ---------------------------------------------------------------------------

PROVIDER_PRESETS = {
    "hostinger": {"imap_host": "imap.hostinger.com", "imap_port": 993,
                  "smtp_host": "smtp.hostinger.com", "smtp_port": 465},
    "gmail":     {"imap_host": "imap.gmail.com", "imap_port": 993,
                  "smtp_host": "smtp.gmail.com", "smtp_port": 465},
    "zoho":      {"imap_host": "imap.zoho.com", "imap_port": 993,
                  "smtp_host": "smtp.zoho.com", "smtp_port": 465},
    "outlook":   {"imap_host": "outlook.office365.com", "imap_port": 993,
                  "smtp_host": "smtp.office365.com", "smtp_port": 587},
    "yahoo":     {"imap_host": "imap.mail.yahoo.com", "imap_port": 993,
                  "smtp_host": "smtp.mail.yahoo.com", "smtp_port": 465},
}

# Map common mail-domains / MX hints to a preset key
_DOMAIN_HINTS = {
    "hostinger.com": "hostinger", "titan.email": "hostinger",
    "gmail.com": "gmail", "googlemail.com": "gmail",
    "zoho.com": "zoho", "zohomail.com": "zoho",
    "outlook.com": "outlook", "hotmail.com": "outlook", "office365.com": "outlook",
    "yahoo.com": "yahoo",
}


def guess_settings(email_addr: str, mx_hosts: list[str] | None = None) -> dict:
    """Best-effort server settings from the address domain (and optional MX hosts).
    Defaults to Hostinger, since that's this deployment's primary case."""
    domain = (email_addr.split("@")[-1] or "").lower()
    for hint, key in _DOMAIN_HINTS.items():
        if domain.endswith(hint):
            return dict(PROVIDER_PRESETS[key])
    for host in (mx_hosts or []):
        for hint, key in _DOMAIN_HINTS.items():
            if hint in host.lower():
                return dict(PROVIDER_PRESETS[key])
    return dict(PROVIDER_PRESETS["hostinger"])


# ---------------------------------------------------------------------------
# Connections
# ---------------------------------------------------------------------------

def _imap(conn) -> imaplib.IMAP4_SSL:
    m = imaplib.IMAP4_SSL(conn["imap_host"], int(conn.get("imap_port", 993)))
    m.login(conn["email"], conn["password"])
    return m


def verify_login(conn) -> tuple[bool, str]:
    """Try an IMAP login so we can fail fast with a clear message at connect time."""
    try:
        m = _imap(conn)
        m.logout()
        return True, ""
    except Exception as e:  # noqa: BLE001
        return False, str(e)


def _decode(value: str) -> str:
    if not value:
        return ""
    try:
        return str(make_header(decode_header(value)))
    except Exception:  # noqa: BLE001
        return value


# ---------------------------------------------------------------------------
# Newsletter detection
# ---------------------------------------------------------------------------

def scan_newsletters(conn) -> list[dict]:
    """Newsletter senders = messages carrying a List-Unsubscribe header in the
    last SCAN_DAYS. Groups by sender."""
    m = _imap(conn)
    try:
        m.select("INBOX", readonly=True)
        since = (datetime.utcnow() - timedelta(days=config.SCAN_DAYS)).strftime("%d-%b-%Y")
        typ, data = m.search(None, f'(SINCE {since})')
        ids = data[0].split() if data and data[0] else []
        ids = ids[-config.SCAN_MESSAGE_LIMIT:]  # most recent N

        senders: dict[str, dict] = {}
        for num in reversed(ids):
            typ, msg_data = m.fetch(num, "(BODY.PEEK[HEADER.FIELDS (FROM LIST-UNSUBSCRIBE)])")
            if typ != "OK" or not msg_data or not msg_data[0]:
                continue
            raw = msg_data[0][1]
            hdr = email.message_from_bytes(raw)
            has_unsub = bool(hdr.get("List-Unsubscribe"))
            if not has_unsub:
                continue  # strong newsletter signal only
            name, addr = parseaddr(_decode(hdr.get("From", "")))
            addr = addr.lower().strip()
            if not addr:
                continue
            entry = senders.setdefault(addr, {
                "email": addr, "name": _decode(name) or addr,
                "count": 0, "has_unsubscribe": True,
            })
            entry["count"] += 1
        return sorted(senders.values(), key=lambda s: s["count"], reverse=True)
    finally:
        try:
            m.logout()
        except Exception:  # noqa: BLE001
            pass


# ---------------------------------------------------------------------------
# Fetching new content
# ---------------------------------------------------------------------------

def fetch_new_from_senders(conn, sender_emails: list[str], since: datetime | None) -> list[dict]:
    if not sender_emails:
        return []
    m = _imap(conn)
    out = []
    try:
        m.select("INBOX", readonly=True)
        since_str = (since or (datetime.utcnow() - timedelta(days=2))).strftime("%d-%b-%Y")
        seen_ids = set()
        for sender in sender_emails:
            typ, data = m.search(None, f'(FROM "{sender}" SINCE {since_str})')
            if typ != "OK":
                continue
            for num in (data[0].split() if data and data[0] else [])[:50]:
                if num in seen_ids:
                    continue
                seen_ids.add(num)
                typ, msg_data = m.fetch(num, "(RFC822)")
                if typ != "OK" or not msg_data or not msg_data[0]:
                    continue
                out.append(_parse_message(email.message_from_bytes(msg_data[0][1])))
        return out
    finally:
        try:
            m.logout()
        except Exception:  # noqa: BLE001
            pass


def _parse_message(msg) -> dict:
    subject = _decode(msg.get("Subject", ""))
    sender = _decode(msg.get("From", ""))
    date = msg.get("Date", "")

    body_text = ""
    attachments_text = []

    for part in msg.walk():
        if part.is_multipart():
            continue
        ctype = part.get_content_type()
        disp = str(part.get("Content-Disposition") or "")
        filename = part.get_filename()

        if filename or "attachment" in disp:
            try:
                data = part.get_payload(decode=True) or b""
                text = extract_text(_decode(filename or ""), ctype, data)
                if text.strip():
                    attachments_text.append(f"[Attachment: {_decode(filename or '')}]\n{text}")
            except Exception:  # noqa: BLE001
                pass
            continue

        try:
            payload = part.get_payload(decode=True)
            if payload is None:
                continue
            charset = part.get_content_charset() or "utf-8"
            decoded = payload.decode(charset, errors="ignore")
        except Exception:  # noqa: BLE001
            continue

        if ctype == "text/html":
            text = BeautifulSoup(decoded, "html.parser").get_text("\n")
            if len(text) > len(body_text):
                body_text = text
        elif ctype == "text/plain" and not body_text:
            body_text = decoded

    body_text = re.sub(r"\n{3,}", "\n\n", body_text).strip()
    return {
        "sender": sender, "subject": subject, "date": date,
        "text": body_text[:12000],
        "attachments_text": "\n\n".join(attachments_text)[:16000],
    }


# ---------------------------------------------------------------------------
# Sending (SMTP) + approval replies
# ---------------------------------------------------------------------------

def _smtp(conn) -> smtplib.SMTP:
    port = int(conn.get("smtp_port", 465))
    if port == 465:
        s = smtplib.SMTP_SSL(conn["smtp_host"], port, context=ssl.create_default_context())
    else:
        s = smtplib.SMTP(conn["smtp_host"], port)
        s.starttls(context=ssl.create_default_context())
    s.login(conn["email"], conn["password"])
    return s


def _send(conn, msg) -> None:
    s = _smtp(conn)
    try:
        s.send_message(msg)
    finally:
        try:
            s.quit()
        except Exception:  # noqa: BLE001
            pass


def send_tracked(conn, self_email: str, subject: str, html_body: str) -> tuple[str, str]:
    """Email the user (to self) an approval request. Returns (ref_id, ref_id)
    where ref_id is the Message-ID replies will reference."""
    msg = MIMEMultipart("alternative")
    ref_id = make_msgid(domain=self_email.split("@")[-1])
    msg["Message-ID"] = ref_id
    msg["From"] = self_email
    msg["To"] = self_email
    msg["Subject"] = subject
    msg["Date"] = formatdate(localtime=True)
    msg.attach(MIMEText(html_body, "html"))
    _send(conn, msg)
    return ref_id, ref_id


def get_new_reply(conn, ref_id: str, awaiting_id: str) -> tuple[str, str] | None:
    """Find a reply that references our approval email's Message-ID and is newer
    than awaiting_id. Returns (its Message-ID, reply text) or None."""
    if not ref_id:
        return None
    m = _imap(conn)
    try:
        m.select("INBOX", readonly=True)
        # Messages that reference our approval email
        typ, data = m.search(None, f'(HEADER REFERENCES "{ref_id}")')
        ids = data[0].split() if (typ == "OK" and data and data[0]) else []
        if not ids:
            typ, data = m.search(None, f'(HEADER IN-REPLY-TO "{ref_id}")')
            ids = data[0].split() if (typ == "OK" and data and data[0]) else []
        if not ids:
            return None
        # newest matching message
        num = ids[-1]
        typ, msg_data = m.fetch(num, "(RFC822)")
        if typ != "OK" or not msg_data or not msg_data[0]:
            return None
        reply = email.message_from_bytes(msg_data[0][1])
        msg_id = reply.get("Message-ID", "")
        if msg_id and msg_id == awaiting_id:
            return None  # already processed
        return msg_id or num.decode(), _extract_reply_text(reply)
    finally:
        try:
            m.logout()
        except Exception:  # noqa: BLE001
            pass


def _extract_reply_text(msg) -> str:
    raw_text = ""
    for part in msg.walk():
        if part.is_multipart():
            continue
        ctype = part.get_content_type()
        try:
            payload = part.get_payload(decode=True)
            if payload is None:
                continue
            decoded = payload.decode(part.get_content_charset() or "utf-8", errors="ignore")
        except Exception:  # noqa: BLE001
            continue
        if ctype == "text/plain" and not raw_text:
            raw_text = decoded
        elif ctype == "text/html" and not raw_text:
            raw_text = BeautifulSoup(decoded, "html.parser").get_text("\n")

    lines = []
    for line in raw_text.splitlines():
        s = line.strip()
        if s.startswith(">") or re.match(r"^On .+ wrote:$", s) or s.startswith("-----Original"):
            break
        lines.append(line)
    return "\n".join(lines).strip()


def send_personalized(conn, from_email: str, to_emails: list[str], subject: str, html_template: str) -> int:
    """One personalized copy per recipient over SMTP (single connection)."""
    s = _smtp(conn)
    sent = 0
    try:
        for addr in to_emails:
            name = first_name_from_email(addr)
            body = (html_template.replace(GREETING_TOKEN, name)
                    if GREETING_TOKEN in html_template else f"<p>Hi {name},</p>\n{html_template}")
            msg = MIMEMultipart("alternative")
            msg["Message-ID"] = make_msgid(domain=from_email.split("@")[-1])
            msg["From"] = from_email
            msg["To"] = addr
            msg["Subject"] = subject
            msg["Date"] = formatdate(localtime=True)
            msg.attach(MIMEText(body, "html"))
            s.send_message(msg)
            sent += 1
        return sent
    finally:
        try:
            s.quit()
        except Exception:  # noqa: BLE001
            pass
