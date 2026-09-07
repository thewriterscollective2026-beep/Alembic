"""Email-driven pipeline.

A run flows entirely over the user's own inbox — they never return to the tool:

  scrape (by frequency window) -> summary -> EMAIL summary to user
    -> user replies "approved" (or edits)         [stage: summary_review]
  -> write newsletter -> EMAIL draft to user
    -> user replies "approved" (or edits)         [stage: email_review]
  -> send to recipient list                       [stage: sent]

Replies are read from the same Gmail thread. Only a short, clear "approved"
proceeds; anything else is treated as change requests (revise + re-send). A
"cancel" reply stops the run. Revisions are capped to avoid runaway loops.
"""
import html as _html
import json
import re
from datetime import datetime, timedelta

from .database import SessionLocal
from .models import User, Sender, Draft
from . import gmail_service, ai_service, provider, email_template

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
REVIEW_STAGES = ("summary_review", "email_review")


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


def _parse_recipients(raw: str) -> list[str]:
    parts = re.split(r"[,\n;\s]+", raw or "")
    seen, out = set(), []
    for p in parts:
        p = p.strip().lower()
        if p and "@" in p and p not in seen:
            seen.add(p)
            out.append(p)
    return out


# ---------------------------------------------------------------------------
# Approval email bodies
# ---------------------------------------------------------------------------

_INSTRUCTIONS = (
    '<hr><p style="color:#555;font-size:14px">'
    '<strong>Reply <span style="color:#12a150">APPROVED</span></strong> to continue, '
    'or just reply with the changes you want and I\'ll revise and resend. '
    'Reply <strong>CANCEL</strong> to stop this run.</p>'
)


def _summary_email_html(summary: str, count: int) -> str:
    body = _html.escape(summary).replace("\n", "<br>")
    return (
        f'<p>Here is the summary from your <strong>{count}</strong> selected newsletter '
        f'email(s). Review it below.</p>'
        f'<div style="background:#f6f8fb;border:1px solid #e3e9f0;border-radius:10px;'
        f'padding:16px;font-size:15px;line-height:1.6">{body}</div>'
        f'{_INSTRUCTIONS}'
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
# Stage 1: start a run  (scrape -> summary -> email to user)
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

        summary = ai_service.build_summary(emails)
        if not summary.strip():
            db.add(Draft(user_id=user_id, stage="no_content", source_count=len(emails)))
            user.last_run = datetime.utcnow()
            db.commit()
            return {"ok": True, "reason": "no readable content"}

        subject = f"[Approve summary] Newsletter digest — {datetime.utcnow():%b %d}"
        thread_id, msg_id = handle.send_tracked(
            user.email, subject, _summary_email_html(summary, len(emails))
        )
        draft = Draft(
            user_id=user_id, stage="summary_review", source_summary=summary,
            source_count=len(emails), thread_id=thread_id, awaiting_msg_id=msg_id,
        )
        db.add(draft)
        user.last_run = datetime.utcnow()
        db.commit()
        db.refresh(draft)
        return {"ok": True, "draft_id": draft.id, "stage": "summary_review"}

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

            if draft.stage == "summary_review":
                if approved:
                    _advance_to_email_review(db, handle, user, draft)
                    acted.append((draft.id, "summary_approved"))
                else:
                    _revise(db, handle, user, draft, text, kind="summary")
                    acted.append((draft.id, "summary_revised"))

            elif draft.stage == "email_review":
                if approved:
                    _do_final_send(db, handle, user, draft)
                    acted.append((draft.id, draft.stage))
                else:
                    _revise(db, handle, user, draft, text, kind="email")
                    acted.append((draft.id, "email_revised"))

        return {"ok": True, "acted": acted}
    except Exception as e:  # noqa: BLE001
        db.rollback()
        return {"ok": False, "reason": str(e), "acted": acted}
    finally:
        db.close()


def _advance_to_email_review(db, handle, user, draft):
    result = ai_service.build_newsletter_from_summary(
        draft.source_summary, user.brand_name or user.name, user.brand_signature)
    draft.subject = result["subject"]
    draft.content_json = json.dumps(result["sections"])
    draft.theme = result["theme"]
    draft.revision_count = 0
    recips = _parse_recipients(user.recipients)
    subject = f"[Approve to send] {draft.subject}"
    tid, mid = handle.send_tracked(
        user.email, subject,
        _draft_email_html(user, result["sections"], len(recips), draft.theme, draft.subject, draft.id)
    )
    draft.thread_id, draft.awaiting_msg_id = tid, mid
    draft.stage = "email_review"
    db.commit()


def _do_final_send(db, handle, user, draft):
    recips = _parse_recipients(user.recipients)
    if not recips:
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
    handle.send_personalized(
        user.email, recips, draft.subject,
        _wrap(user, sections, draft.theme, draft.subject, draft.id))
    draft.stage = "sent"
    draft.sent_count = len(recips)
    draft.sent_at = datetime.utcnow()
    db.commit()


def _revise(db, handle, user, draft, feedback, kind: str):
    if draft.revision_count >= MAX_REVISIONS:
        note = (f'<p>We\'ve hit the revision limit ({MAX_REVISIONS}) for this run. '
                f'Reply <strong>APPROVED</strong> to accept the current version, or '
                f'<strong>CANCEL</strong> to stop.</p>')
        tid, mid = handle.send_tracked(user.email, "[Revision limit reached]", note)
        draft.thread_id, draft.awaiting_msg_id = tid, mid
        db.commit()
        return

    draft.revision_count += 1
    if kind == "summary":
        draft.source_summary = ai_service.revise_summary(draft.source_summary, feedback)
        subject = f"[Approve summary — revised v{draft.revision_count}] Newsletter digest"
        html = _summary_email_html(draft.source_summary, draft.source_count)
    else:
        current_sections = json.loads(draft.content_json) if draft.content_json else {}
        result = ai_service.revise_newsletter(
            current_sections, feedback, draft.subject, draft.theme,
            user.brand_name or user.name, user.brand_signature)
        draft.subject, draft.theme = result["subject"], result["theme"]
        draft.content_json = json.dumps(result["sections"])
        recips = _parse_recipients(user.recipients)
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
