"""Gmail API interactions: detect newsletters, read mail, extract content,
send approval emails to the user, read their replies, and send the final list."""
import base64
import re
from datetime import datetime
from email.utils import parseaddr
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

from bs4 import BeautifulSoup
from googleapiclient.discovery import build

from . import config
from .attachments import extract_text
from .email_template import GREETING_TOKEN


def _service(creds):
    return build("gmail", "v1", credentials=creds, cache_discovery=False)


def _header(headers, name):
    name = name.lower()
    for h in headers:
        if h.get("name", "").lower() == name:
            return h.get("value", "")
    return ""


# ---------------------------------------------------------------------------
# Newsletter detection (onboarding scan)
# ---------------------------------------------------------------------------

def scan_newsletters(creds) -> list[dict]:
    """Scan recent mail and return de-duplicated newsletter senders.
    Signal = List-Unsubscribe header OR Gmail promo/updates category."""
    svc = _service(creds)
    query = f"newer_than:{config.SCAN_DAYS}d (category:promotions OR category:updates OR unsubscribe)"

    message_ids = []
    page_token = None
    while len(message_ids) < config.SCAN_MESSAGE_LIMIT:
        resp = svc.users().messages().list(
            userId="me", q=query,
            maxResults=min(100, config.SCAN_MESSAGE_LIMIT - len(message_ids)),
            pageToken=page_token,
        ).execute()
        message_ids.extend(m["id"] for m in resp.get("messages", []))
        page_token = resp.get("nextPageToken")
        if not page_token:
            break

    senders: dict[str, dict] = {}
    for mid in message_ids:
        msg = svc.users().messages().get(
            userId="me", id=mid, format="metadata",
            metadataHeaders=["From", "List-Unsubscribe"],
        ).execute()
        headers = msg.get("payload", {}).get("headers", [])
        from_raw = _header(headers, "From")
        has_unsub = bool(_header(headers, "List-Unsubscribe"))
        name, email = parseaddr(from_raw)
        email = email.lower().strip()
        if not email:
            continue
        entry = senders.setdefault(email, {
            "email": email, "name": name or email,
            "count": 0, "has_unsubscribe": False,
        })
        entry["count"] += 1
        entry["has_unsubscribe"] = entry["has_unsubscribe"] or has_unsub

    return sorted(senders.values(), key=lambda s: s["count"], reverse=True)


# ---------------------------------------------------------------------------
# Fetching new content from selected senders
# ---------------------------------------------------------------------------

def fetch_new_from_senders(creds, sender_emails: list[str], since: datetime | None) -> list[dict]:
    """Return parsed emails from the given senders newer than `since`."""
    if not sender_emails:
        return []
    svc = _service(creds)

    from_clause = " OR ".join(f"from:{e}" for e in sender_emails)
    query = f"({from_clause})"
    if since:
        query += f" after:{since.strftime('%Y/%m/%d')}"
    else:
        query += " newer_than:2d"

    resp = svc.users().messages().list(userId="me", q=query, maxResults=50).execute()
    ids = [m["id"] for m in resp.get("messages", [])]

    emails = []
    for mid in ids:
        msg = svc.users().messages().get(userId="me", id=mid, format="full").execute()
        emails.append(_parse_message(svc, msg))
    return emails


def _parse_message(svc, msg) -> dict:
    payload = msg.get("payload", {})
    headers = payload.get("headers", [])
    subject = _header(headers, "Subject")
    sender = _header(headers, "From")
    date = _header(headers, "Date")

    body_text = ""
    attachments_text = []

    def walk(part):
        nonlocal body_text
        mime = part.get("mimeType", "")
        filename = part.get("filename", "")
        body = part.get("body", {})

        if part.get("parts"):
            for p in part["parts"]:
                walk(p)
            return

        if filename and body.get("attachmentId"):
            att = svc.users().messages().attachments().get(
                userId="me", messageId=msg["id"], id=body["attachmentId"],
            ).execute()
            data = base64.urlsafe_b64decode(att.get("data", "").encode())
            text = extract_text(filename, mime, data)
            if text.strip():
                attachments_text.append(f"[Attachment: {filename}]\n{text}")
            return

        data = body.get("data")
        if not data:
            return
        decoded = base64.urlsafe_b64decode(data.encode()).decode("utf-8", errors="ignore")
        if mime == "text/html":
            text = BeautifulSoup(decoded, "html.parser").get_text("\n")
            if len(text) > len(body_text):
                body_text = text
        elif mime == "text/plain" and not body_text:
            body_text = decoded

    walk(payload)

    body_text = re.sub(r"\n{3,}", "\n\n", body_text).strip()
    return {
        "sender": sender,
        "subject": subject,
        "date": date,
        "text": body_text[:12000],
        "attachments_text": "\n\n".join(attachments_text)[:16000],
    }


# ---------------------------------------------------------------------------
# Approval emails to the user + reading their replies
# ---------------------------------------------------------------------------

def send_tracked(creds, self_email: str, subject: str, html_body: str) -> tuple[str, str]:
    """Send an email to the user's own inbox. Returns (thread_id, message_id)
    so we can watch that thread for their approval reply."""
    svc = _service(creds)
    message = MIMEMultipart("alternative")
    message["From"] = self_email
    message["To"] = self_email
    message["Subject"] = subject
    message.attach(MIMEText(html_body, "html"))
    raw = base64.urlsafe_b64encode(message.as_bytes()).decode()
    sent = svc.users().messages().send(userId="me", body={"raw": raw}).execute()
    return sent.get("threadId", ""), sent.get("id", "")


def get_new_reply(creds, thread_id: str, awaiting_msg_id: str) -> tuple[str, str] | None:
    """If a message newer than `awaiting_msg_id` exists in the thread (i.e. the
    user replied), return (message_id, reply_text). Otherwise None."""
    if not thread_id:
        return None
    svc = _service(creds)
    thread = svc.users().threads().get(userId="me", id=thread_id, format="full").execute()
    messages = thread.get("messages", [])
    if not messages:
        return None
    last = messages[-1]
    if last.get("id") == awaiting_msg_id:
        return None  # nothing newer than our last send
    return last["id"], _extract_reply_text(last)


def _extract_reply_text(msg) -> str:
    """Plain-text body of a message with quoted history stripped."""
    payload = msg.get("payload", {})
    raw_text = ""

    def walk(part):
        nonlocal raw_text
        if part.get("parts"):
            for p in part["parts"]:
                walk(p)
            return
        mime = part.get("mimeType", "")
        data = part.get("body", {}).get("data")
        if not data:
            return
        decoded = base64.urlsafe_b64decode(data.encode()).decode("utf-8", errors="ignore")
        if mime == "text/plain" and not raw_text:
            raw_text = decoded
        elif mime == "text/html" and not raw_text:
            raw_text = BeautifulSoup(decoded, "html.parser").get_text("\n")

    walk(payload)

    lines = []
    for line in raw_text.splitlines():
        s = line.strip()
        if s.startswith(">"):
            break
        if re.match(r"^On .+ wrote:$", s):
            break
        if s.startswith("-----Original Message-----") or s.startswith("____"):
            break
        lines.append(line)
    return "\n".join(lines).strip()


# ---------------------------------------------------------------------------
# Final send to the recipient list
# ---------------------------------------------------------------------------

def first_name_from_email(email: str) -> str:
    """Best-effort first name from an address: rohan@x.com -> 'Rohan',
    krushna.patel@x.com -> 'Krushna', info@x.com -> 'Info'."""
    local = (email or "").split("@")[0]
    token = re.split(r"[._\-+0-9]+", local)[0] if local else ""
    return token.capitalize() if token else "there"


def send_personalized(creds, from_email: str, to_emails: list[str], subject: str, html_template: str) -> int:
    """Send ONE personalized copy per recipient. The greeting token
    {{FIRST_NAME}} is replaced with a name derived from each address; if the
    template has no token, a 'Hi <name>,' line is prepended."""
    svc = _service(creds)
    sent = 0
    for addr in to_emails:
        name = first_name_from_email(addr)
        if GREETING_TOKEN in html_template:
            body = html_template.replace(GREETING_TOKEN, name)
        else:
            body = f"<p>Hi {name},</p>\n{html_template}"
        message = MIMEMultipart("alternative")
        message["From"] = from_email
        message["To"] = addr
        message["Subject"] = subject
        message.attach(MIMEText(body, "html"))
        raw = base64.urlsafe_b64encode(message.as_bytes()).decode()
        svc.users().messages().send(userId="me", body={"raw": raw}).execute()
        sent += 1
    return sent
