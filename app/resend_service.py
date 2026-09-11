"""Bulk newsletter sending via Resend's transactional Emails API - the FINAL
blast to the recipient list only (see Newsletter_Tool_Email_Sending_Plan.docx).

System emails (the drafted-newsletter review request, approval thread) are NOT
sent through here - those stay on the client's own Gmail/IMAP mailbox via
app/provider.py, unchanged. Resend is not a mailbox the client signs into; it's
our own transactional sending account, so it gets its own module instead of
fitting into the provider abstraction.

Per the plan: use the transactional Emails API (batch endpoint) with our own
Contact table as the list - never Resend's Broadcasts product (billed by
contacts stored, not what we want).

API shape below was fetched live from resend.com/docs (not guessed from
training data - this is a third-party API):
- POST https://api.resend.com/emails/batch, up to 100 emails per call,
  `Authorization: Bearer <key>`, JSON array body of email objects
  ({from, to, subject, html, ...}), response {"data": [{"id": ...}, ...]}
  in the same order as the request array.
- Default rate limit: 10 requests/second/team. On 429, back off using the
  `retry-after` header (seconds) rather than a fixed delay.

RESEND_API_KEY / RESEND_FROM_EMAIL are pure config (app/config.py) - this
module never hardcodes a domain or address, since the sending domain in use
today is a TEST domain that will be swapped for the real owner's domain.
"""
import time

import requests

from . import config, unsubscribe
from .email_template import GREETING_TOKEN, UNSUBSCRIBE_TOKEN
from .gmail_service import first_name_from_email

API_URL = "https://api.resend.com/emails/batch"
BATCH_SIZE = 100  # Resend's documented max per batch call


class ResendNotConfigured(RuntimeError):
    pass


def send_campaign(contacts: list, subject: str, html_template: str, limit: int | None = None) -> list[dict]:
    """Send one personalized copy per contact via Resend's batch API.

    `contacts` is a list of Contact ORM objects (uses .id, .email, .first_name).
    `limit`, when set, sends only to the first N contacts - a manual warm-up
    ramp for the first campaign on a fresh sending domain/subdomain (see the
    docstring on User.warmup_limit and the sending plan's ramp schedule).
    Returns a list of {"contact_id": int, "resend_message_id": str} in the same
    order as the (possibly limited) contacts - the caller uses this to create
    CampaignRecipient rows. Raises on any batch that fails outright (after
    retries) - a partial send is surfaced to the caller via however many
    results made it into the return list before the exception, not silently
    swallowed.
    """
    if not config.RESEND_API_KEY or not config.RESEND_FROM_EMAIL:
        raise ResendNotConfigured(
            "RESEND_API_KEY / RESEND_FROM_EMAIL not set - see .env.example")

    if limit is not None:
        contacts = contacts[:limit]

    headers = {
        "Authorization": f"Bearer {config.RESEND_API_KEY}",
        "Content-Type": "application/json",
    }
    results = []
    for i in range(0, len(contacts), BATCH_SIZE):
        chunk = contacts[i:i + BATCH_SIZE]
        payload = [_email_payload(c, subject, html_template) for c in chunk]
        data = _post_batch_with_retry(headers, payload)
        for contact, item in zip(chunk, data):
            results.append({"contact_id": contact.id, "resend_message_id": item.get("id", "")})
    return results


def _email_payload(contact, subject: str, html_template: str) -> dict:
    name = contact.first_name or first_name_from_email(contact.email)
    body = html_template.replace(GREETING_TOKEN, name) if GREETING_TOKEN in html_template \
        else f"<p>Hi {name},</p>\n{html_template}"
    unsub_url = unsubscribe.unsubscribe_url(contact.id)
    body = body.replace(UNSUBSCRIBE_TOKEN, unsub_url)
    return {
        "from": config.RESEND_FROM_EMAIL,
        "to": contact.email,
        "subject": subject,
        "html": body,
        # RFC 8058 one-click unsubscribe - mail clients that support it show a
        # native "Unsubscribe" action that POSTs here with no confirmation page.
        "headers": {
            "List-Unsubscribe": f"<{unsub_url}>",
            "List-Unsubscribe-Post": "List-Unsubscribe=One-Click",
        },
    }


def _post_batch_with_retry(headers: dict, payload: list, max_retries: int = 3) -> list:
    resp = None
    for attempt in range(max_retries + 1):
        resp = requests.post(API_URL, headers=headers, json=payload, timeout=30)
        if resp.status_code == 429 and attempt < max_retries:
            time.sleep(float(resp.headers.get("retry-after", "2")))
            continue
        resp.raise_for_status()
        return resp.json().get("data", [])
    resp.raise_for_status()
    return []  # unreachable - raise_for_status always raises on a non-2xx response
