"""Email-driven pipeline.

A run flows entirely over the user's own inbox — they never return to the tool:

  scrape (by frequency window) -> research + write the full newsletter
    -> EMAIL the drafted newsletter to user             [stage: review]
    -> user replies "approved"                          -> send to recipient list [stage: sent]
    -> user replies with feedback                        -> research further if the feedback
                                                              calls for it, fix, re-send  [stage: review]
    -> user replies "cancel"                             -> stop                 [stage: cancelled]

Replies are read from the same Gmail thread. Only a short, clear "approved"
proceeds; anything else is treated as change requests (revise + re-send) - the
revision step decides for itself whether the feedback needs fresh web research
or just a rewrite (see ai_service.revise_newsletter). Revisions are capped to
avoid runaway loops.
"""
import json
from datetime import datetime, timedelta

from .database import SessionLocal
from .models import User, Sender, Draft, Contact, Campaign, CampaignRecipient
from . import config, gmail_service, ai_service, provider, email_template, resend_service

# Frequency -> how many days back to scrape each run
FREQUENCY_LOOKBACK_DAYS = {
    "daily": 1,
    "thrice_week": 2,
    "twice_week": 3,
    "weekly": 7,
    "twice_monthly": 15,
}
MAX_REVISIONS = 5
MAX_SOURCE_EMAILS = 15  # cap emails per run so it stays fast and low-cost
REVIEW_STAGES = ("review",)


# ---------------------------------------------------------------------------
# Reply interpretation
# ---------------------------------------------------------------------------

def _first_line(text: str) -> str:
    """First non-empty line, lowercased and de-punctuated. People reply with the
    decision on line 1 and often a signature/quoted text below — judge line 1."""
    for line in (text or "").splitlines():
        s = line.strip()
        if s:
            return s.lower().strip(" .!\t\"'")
    return ""


_APPROVE_FIRST_WORDS = {"approved", "approve", "yes", "yep", "yeah", "ok", "okay",
                        "okey", "send", "go", "ship", "confirm", "confirmed", "👍"}
_APPROVE_PHRASES = {"looks good", "good to go", "lgtm", "send it", "go ahead", "all good"}


def _is_cancel(text: str) -> bool:
    return _first_line(text).startswith(("cancel", "stop", "abort"))


def _is_approval(text: str) -> bool:
    first = _first_line(text)
    if not first:
        return False
    if first in _APPROVE_PHRASES:
        return True
    words = first.split()
    if words and words[0] in _APPROVE_FIRST_WORDS:
        return True
    # "approve"/"approved" appearing in a short first line
    return "approv" in first and len(first) <= 25


def active_contacts(db, user_id: int) -> list[Contact]:
    """Every non-suppressed Contact - the send list. Replaces the old flat
    User.recipients string; a Contact only leaves "active" via a webhook-driven
    bounce/complaint or the unsubscribe link, never by re-uploading."""
    return db.query(Contact).filter_by(user_id=user_id, status="active").all()


def active_contact_emails(db, user_id: int) -> list[str]:
    return [c.email for c in active_contacts(db, user_id)]


# ---------------------------------------------------------------------------
# Approval email bodies
# ---------------------------------------------------------------------------

_INSTRUCTIONS = (
    '<hr><p style="color:#555;font-size:14px">'
    '<strong>Reply <span style="color:#12a150">APPROVED</span></strong> to continue, '
    'or just reply with the changes you want and I\'ll revise and resend. '
    'Reply <strong>CANCEL</strong> to stop this run.</p>'
)


def _wrap(user, sections: dict, theme: str = "general", subject: str = "", draft_id: int | None = None) -> str:
    """Render the structured newsletter content into the branded, email-safe HTML shell."""
    return email_template.render_email(
        sections,
        brand_name=user.brand_name or user.name,
        brand_signature=user.brand_signature,
        subscribe_url=user.subscribe_url,
        address=user.business_address,
        from_email=user.email,
        theme=theme,
        subject=subject,
        issue_number=f"Issue #{draft_id}" if draft_id else "",
        layout=user.newsletter_layout,
        author_name=user.brand_name or user.name,
        avatar_url=f"{config.APP_BASE_URL}/avatar/{user.id}" if user.avatar_image else "",
    )


def _draft_email_html(user, sections: dict, recipient_count: int, theme: str = "general",
                      subject: str = "", draft_id: int | None = None) -> str:
    # Preview exactly what recipients get (branded shell), with the {{FIRST_NAME}}
    # token filled with a sample name.
    preview = _wrap(user, sections, theme, subject, draft_id).replace(
        email_template.GREETING_TOKEN, "Rohan")
    return (
        f'<p>Your newsletter draft is ready. Once you approve, it goes to your '
        f'<strong>{recipient_count}</strong> recipient(s), each addressed by their own '
        f'first name (shown here as a sample "Rohan").</p>'
        f'{preview}'
        f'{_INSTRUCTIONS}'
    )


# ---------------------------------------------------------------------------
# Stage 1: start a run  (scrape -> research -> write the newsletter -> email for review)
# ---------------------------------------------------------------------------

def start_run(user_id: int) -> dict:
    db = SessionLocal()
    try:
        user = db.get(User, user_id)
        if not user:
            return {"ok": False, "reason": "user not found"}

        selected = [s.email for s in db.query(Sender).filter_by(user_id=user_id, selected=True).all()]
        if not selected:
            return {"ok": False, "reason": "no senders selected"}

        handle = provider.get_handle(user)
        days = FREQUENCY_LOOKBACK_DAYS.get(user.frequency, 1)
        since = datetime.utcnow() - timedelta(days=days)
        emails = handle.fetch_new(selected, since)[:MAX_SOURCE_EMAILS]
        if not emails:
            # Fallback: nothing in the short window -> widen to 30 days so a run
            # isn't empty just because no newsletter arrived in the last day or two.
            emails = handle.fetch_new(selected, datetime.utcnow() - timedelta(days=30))[:MAX_SOURCE_EMAILS]

        if not emails:
            db.add(Draft(user_id=user_id, stage="no_content"))
            user.last_run = datetime.utcnow()
            db.commit()
            return {"ok": True, "reason": f"no new emails in the last {days} day(s)"}

        # Research step: reads the sources directly, finds cross-source themes/gaps,
        # researches them. This is internal now - not emailed for approval on its own.
        summary = ai_service.build_summary(emails)
        if not summary.strip():
            db.add(Draft(user_id=user_id, stage="no_content", source_count=len(emails)))
            user.last_run = datetime.utcnow()
            db.commit()
            return {"ok": True, "reason": "no readable content"}

        result = ai_service.build_newsletter_from_summary(
            summary, user.brand_name or user.name, user.brand_signature)

        draft = Draft(
            user_id=user_id, stage="review", source_summary=summary,
            subject=result["subject"], content_json=json.dumps(result["sections"]),
            theme=result["theme"], source_count=len(emails),
        )
        db.add(draft)
        db.flush()  # assigns draft.id, needed for the issue number in the email itself

        recips = active_contact_emails(db, user_id)
        subject = f"[Approve to send] {result['subject']}"
        thread_id, msg_id = handle.send_tracked(
            user.email, subject,
            _draft_email_html(user, result["sections"], len(recips), result["theme"],
                              result["subject"], draft.id)
        )
        draft.thread_id, draft.awaiting_msg_id = thread_id, msg_id
        user.last_run = datetime.utcnow()
        db.commit()
        db.refresh(draft)
        return {"ok": True, "draft_id": draft.id, "stage": "review"}

    except Exception as e:  # noqa: BLE001
        db.rollback()
        try:
            db.add(Draft(user_id=user_id, stage="error", error=str(e)))
            db.commit()
        except Exception:
            db.rollback()
        return {"ok": False, "reason": str(e)}
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Stage 2/3: read replies and advance each active run
# ---------------------------------------------------------------------------

def poll_run_replies(user_id: int) -> dict:
    db = SessionLocal()
    acted = []
    try:
        user = db.get(User, user_id)
        if not user:
            return {"ok": False, "reason": "user not found"}
        active = db.query(Draft).filter(
            Draft.user_id == user_id, Draft.stage.in_(REVIEW_STAGES)
        ).all()
        if not active:
            return {"ok": True, "acted": []}

        handle = provider.get_handle(user)
        for draft in active:
            reply = handle.get_new_reply(draft.thread_id, draft.awaiting_msg_id)
            if not reply:
                continue
            msg_id, text = reply
            draft.awaiting_msg_id = msg_id  # don't reprocess this reply

            if _is_cancel(text):
                draft.stage = "cancelled"
                acted.append((draft.id, "cancelled"))
                db.commit()
                continue

            approved = _is_approval(text)

            if approved:
                _do_final_send(db, handle, user, draft)
                acted.append((draft.id, draft.stage))
            else:
                _revise(db, handle, user, draft, text)
                acted.append((draft.id, "revised"))

        return {"ok": True, "acted": acted}
    except Exception as e:  # noqa: BLE001
        db.rollback()
        return {"ok": False, "reason": str(e), "acted": acted}
    finally:
        db.close()


def _do_final_send(db, handle, user, draft):
    """The recipient blast goes through Resend (resend_service), NOT the client's
    own mailbox - see Newsletter_Tool_Email_Sending_Plan.docx. `handle` (the
    client's Gmail/IMAP) is only used here for the system-email nudges below."""
    contacts = active_contacts(db, user.id)
    if not contacts:
        # Can't send with no list — nudge the user and keep waiting.
        note = ('<p>You approved the newsletter, but no recipient list is uploaded yet. '
                'Upload an Excel of recipient emails in the tool (Settings), then reply '
                '<strong>APPROVED</strong> here again.</p>')
        tid, mid = handle.send_tracked(
            user.email, f"[Action needed] Upload recipients — {draft.subject}", note
        )
        draft.thread_id, draft.awaiting_msg_id = tid, mid
        db.commit()
        return

    sections = json.loads(draft.content_json) if draft.content_json else {}
    html = _wrap(user, sections, draft.theme, draft.subject, draft.id)

    try:
        send_results = resend_service.send_campaign(
            contacts, draft.subject, html, limit=user.warmup_limit or None)
    except resend_service.ResendNotConfigured:
        note = ('<p>You approved the newsletter, but the bulk-sending service '
                '(Resend) isn\'t configured yet. Set RESEND_API_KEY and '
                'RESEND_FROM_EMAIL, then reply <strong>APPROVED</strong> here again.</p>')
        tid, mid = handle.send_tracked(user.email, f"[Action needed] Sender not configured — {draft.subject}", note)
        draft.thread_id, draft.awaiting_msg_id = tid, mid
        db.commit()
        return

    campaign = Campaign(
        draft_id=draft.id, user_id=user.id, subject=draft.subject,
        recipient_count=len(send_results), sent_at=datetime.utcnow(),
    )
    db.add(campaign)
    db.flush()
    for r in send_results:
        db.add(CampaignRecipient(
            campaign_id=campaign.id, contact_id=r["contact_id"],
            resend_message_id=r["resend_message_id"], status="sent",
        ))

    draft.stage = "sent"
    draft.sent_count = len(send_results)
    draft.sent_at = datetime.utcnow()
    db.commit()


def _revise(db, handle, user, draft, feedback):
    """Apply reviewer feedback to the drafted newsletter and re-send for review.
    ai_service.revise_newsletter decides for itself whether the feedback needs
    fresh web research (e.g. "add a recent stat on X") or just a rewrite."""
    if draft.revision_count >= MAX_REVISIONS:
        note = (f'<p>We\'ve hit the revision limit ({MAX_REVISIONS}) for this run. '
                f'Reply <strong>APPROVED</strong> to accept the current version, or '
                f'<strong>CANCEL</strong> to stop.</p>')
        tid, mid = handle.send_tracked(user.email, "[Revision limit reached]", note)
        draft.thread_id, draft.awaiting_msg_id = tid, mid
        db.commit()
        return

    draft.revision_count += 1
    current_sections = json.loads(draft.content_json) if draft.content_json else {}
    result = ai_service.revise_newsletter(
        current_sections, feedback, draft.subject, draft.theme,
        user.brand_name or user.name, user.brand_signature)
    draft.subject, draft.theme = result["subject"], result["theme"]
    draft.content_json = json.dumps(result["sections"])
    recips = active_contact_emails(db, user.id)
    subject = f"[Approve to send — revised v{draft.revision_count}] {draft.subject}"
    html = _draft_email_html(user, result["sections"], len(recips), draft.theme, draft.subject, draft.id)

    tid, mid = handle.send_tracked(user.email, subject, html)
    draft.thread_id, draft.awaiting_msg_id = tid, mid
    db.commit()


def poll_all_pending() -> dict:
    """Scheduler entry point: advance every run with a pending review, all users."""
    db = SessionLocal()
    try:
        user_ids = [uid for (uid,) in db.query(Draft.user_id)
                    .filter(Draft.stage.in_(REVIEW_STAGES)).distinct().all()]
    finally:
        db.close()
    for uid in user_ids:
        poll_run_replies(uid)
    return {"ok": True, "polled_users": len(user_ids)}
