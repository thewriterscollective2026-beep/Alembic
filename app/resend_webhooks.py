"""Resend webhook handling: delivery/engagement events update CampaignRecipient
status, and bounce/complaint events also suppress the underlying Contact from
future sends - this is the actual list-hygiene enforcement point in the plan.

Verification and payload shape confirmed live against resend.com/docs (not
guessed - third-party API):
- Headers: svix-id, svix-timestamp, svix-signature (Resend uses Svix).
- svix.webhooks.Webhook(secret).verify(raw_body, headers) -> None (raises
  WebhookVerificationError on failure; does NOT return the parsed payload -
  json.loads the raw body yourself after a successful verify).
- Payload: {"type": "email.<event>", "created_at": ..., "data": {"email_id": ...,
  ...}} - data.email_id is the id returned when the email was sent/batch-sent,
  which is what we stored as CampaignRecipient.resend_message_id.
- Full event type list includes: email.sent, email.delivered, email.delivery_delayed,
  email.opened, email.clicked, email.bounced, email.complained, email.failed,
  email.suppressed (no email.unsubscribed - unsubscribe is handled by our own
  link, see the Phase 4 unsubscribe route, not a Resend webhook event).
"""
import json
from datetime import datetime

from svix.webhooks import Webhook, WebhookVerificationError

from . import config
from .models import CampaignRecipient, Contact

# Event types that also suppress the underlying Contact from future sends.
SUPPRESSING_EVENTS = {"email.bounced": "bounced", "email.complained": "complained"}


class InvalidSignature(Exception):
    pass


def verify_and_parse(raw_body: bytes, headers: dict) -> dict:
    """Verify the webhook signature and return the parsed event dict.
    Raises InvalidSignature on a bad/missing signature or missing config."""
    if not config.RESEND_WEBHOOK_SECRET:
        raise InvalidSignature("RESEND_WEBHOOK_SECRET not configured")
    try:
        Webhook(config.RESEND_WEBHOOK_SECRET).verify(raw_body, headers)
    except WebhookVerificationError as e:
        raise InvalidSignature(str(e)) from e
    return json.loads(raw_body)


def apply_event(db, event: dict) -> bool:
    """Update the matching CampaignRecipient (and suppress the Contact, for
    bounce/complaint) from one parsed webhook event. Returns True if a
    matching CampaignRecipient was found - False is not an error, just means
    this event doesn't correspond to a campaign send we tracked (e.g. a system
    email, or an event type we don't otherwise act on)."""
    event_type = event.get("type", "")
    email_id = (event.get("data") or {}).get("email_id", "")
    if not email_id:
        return False

    recipient = db.query(CampaignRecipient).filter_by(resend_message_id=email_id).first()
    if not recipient:
        return False

    if event_type.startswith("email."):
        recipient.status = event_type.split(".", 1)[1]
    recipient.last_event_at = datetime.utcnow()

    suppress_status = SUPPRESSING_EVENTS.get(event_type)
    if suppress_status:
        contact = db.get(Contact, recipient.contact_id)
        if contact:
            contact.status = suppress_status

    db.commit()
    return True
