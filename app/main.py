"""FastAPI application: login, onboarding, settings (Excel upload), and an
email-driven review flow (runs email the user; approvals happen by reply)."""
from pathlib import Path

from fastapi import FastAPI, Request, Depends, Form, HTTPException, UploadFile, File, BackgroundTasks
from fastapi.responses import RedirectResponse, HTMLResponse, PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware
from sqlalchemy.orm import Session

import base64
import json
from . import (config, auth, gmail_service, ai_service, scheduler, excel_recipients, provider,
              imap_service, crypto, email_template, resend_webhooks, unsubscribe)
from .database import init_db, get_session
from .models import User, Sender, Draft, UsageLog, Contact, Campaign, CampaignRecipient
from .pipeline import begin_run, resume_run, poll_run_replies, active_contact_emails

BASE_DIR = Path(__file__).resolve().parent.parent
app = FastAPI(title="Alembic")
app.add_middleware(SessionMiddleware, secret_key=config.SESSION_SECRET, max_age=60 * 60 * 24 * 14)
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
templates.env.globals["freq_labels"] = scheduler.FREQUENCY_LABELS
# Cache-busts /static/style.css (base.html appends ?v=<this>) - Cloudflare/the
# browser can otherwise keep serving a stale copy of the CSS after a deploy,
# at the same URL, with no signal that anything changed.
try:
    templates.env.globals["asset_version"] = int((BASE_DIR / "static" / "style.css").stat().st_mtime)
except OSError:
    templates.env.globals["asset_version"] = 0

STAGE_LABELS = {
    "running": "Researching & writing… (usually a few minutes)",
    "review": "Awaiting newsletter approval (check your inbox)",
    "sent": "Sent to your list",
    "cancelled": "Cancelled",
    "no_content": "No new emails found",
    "error": "Error",
}
templates.env.globals["stage_labels"] = STAGE_LABELS


@app.on_event("startup")
def _startup():
    init_db()
    scheduler.start()


def current_user(request: Request, db: Session) -> User | None:
    uid = request.session.get("user_id")
    return db.get(User, uid) if uid else None


def require_user(request: Request, db: Session) -> User:
    user = current_user(request, db)
    if not user:
        raise HTTPException(status_code=401, detail="Not logged in")
    return user


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
def home(request: Request, db: Session = Depends(get_session)):
    user = current_user(request, db)
    missing = config.missing_required()
    if not user:
        google_ok = bool(config.GOOGLE_CLIENT_ID and config.GOOGLE_CLIENT_SECRET)
        core_missing = [m for m in missing if m in ("ANTHROPIC_API_KEY", "FERNET_KEY")]
        return templates.TemplateResponse("login.html", {
            "request": request, "missing": core_missing, "google_ok": google_ok})
    if not user.onboarded:
        return RedirectResponse("/onboarding", status_code=303)
    drafts = db.query(Draft).filter_by(user_id=user.id).order_by(Draft.created_at.desc()).limit(20).all()
    selected_count = db.query(Sender).filter_by(user_id=user.id, selected=True).count()
    return templates.TemplateResponse("dashboard.html", {
        "request": request, "user": user, "drafts": drafts,
        "selected_count": selected_count,
        "recipient_count": len(active_contact_emails(db, user.id)),
    })


@app.get("/login")
def login(request: Request):
    if config.missing_required():
        return RedirectResponse("/", status_code=303)
    url, state = auth.authorization_url()
    request.session["oauth_state"] = state
    return RedirectResponse(url, status_code=303)


@app.get("/auth/callback")
def auth_callback(request: Request, code: str = "", state: str = "", db: Session = Depends(get_session)):
    saved_state = request.session.get("oauth_state")
    if not code or not saved_state or state != saved_state:
        raise HTTPException(status_code=400, detail="Invalid OAuth state")

    creds = auth.exchange_code(code, saved_state)

    from googleapiclient.discovery import build
    oauth2 = build("oauth2", "v2", credentials=creds, cache_discovery=False)
    info = oauth2.userinfo().get().execute()
    sub = info["id"]

    user = db.query(User).filter_by(google_sub=sub).first()
    if not user:
        user = User(google_sub=sub, email=info.get("email", ""), name=info.get("name", ""))
        db.add(user)
    else:
        user.email = info.get("email", user.email)
        user.name = info.get("name", user.name)
    auth.store_credentials(user, creds)
    db.commit()

    request.session["user_id"] = user.id
    return RedirectResponse("/onboarding" if not user.onboarded else "/", status_code=303)


@app.get("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/", status_code=303)


# ---------------------------------------------------------------------------
# IMAP / SMTP connect (non-Google, e.g. Hostinger webmail)
# ---------------------------------------------------------------------------

@app.get("/connect", response_class=HTMLResponse)
def connect_page(request: Request):
    return templates.TemplateResponse("connect.html", {"request": request})


@app.post("/connect")
def connect_submit(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    imap_host: str = Form(""),
    imap_port: int = Form(0),
    smtp_host: str = Form(""),
    smtp_port: int = Form(0),
    db: Session = Depends(get_session),
):
    email = email.strip().lower()
    guess = imap_service.guess_settings(email)
    conn = {
        "email": email,
        "password": password,
        "imap_host": imap_host.strip() or guess["imap_host"],
        "imap_port": int(imap_port) or guess["imap_port"],
        "smtp_host": smtp_host.strip() or guess["smtp_host"],
        "smtp_port": int(smtp_port) or guess["smtp_port"],
    }
    ok, err = imap_service.verify_login(conn)
    if not ok:
        request.session["flash"] = f"Couldn't sign in to that mailbox: {err}"
        return RedirectResponse("/connect", status_code=303)

    key = f"imap:{email}"
    user = db.query(User).filter_by(google_sub=key).first()
    if not user:
        user = User(google_sub=key, provider="imap", email=email,
                    name=email.split("@")[0].capitalize())
        db.add(user)
    user.provider = "imap"
    user.imap_conn_enc = crypto.encrypt(json.dumps(conn))
    db.commit()

    request.session["user_id"] = user.id
    return RedirectResponse("/onboarding" if not user.onboarded else "/", status_code=303)


# ---------------------------------------------------------------------------
# Onboarding: scan + choose senders
# ---------------------------------------------------------------------------

@app.get("/onboarding", response_class=HTMLResponse)
def onboarding(request: Request, rescan: int = 0, db: Session = Depends(get_session)):
    user = require_user(request, db)
    senders = db.query(Sender).filter_by(user_id=user.id).order_by(Sender.count.desc()).all()

    if not senders or rescan:
        if rescan:
            db.query(Sender).filter_by(user_id=user.id).delete()
            db.commit()
        handle = provider.get_handle(user)
        detected = handle.scan_newsletters()
        categories = ai_service.categorize_senders(
            [{"email": d["email"], "name": d["name"]} for d in detected]
        )
        for d in detected:
            db.add(Sender(
                user_id=user.id, email=d["email"], name=d["name"],
                count=d["count"], category=categories.get(d["email"], "other"),
                selected=False,
            ))
        db.commit()
        senders = db.query(Sender).filter_by(user_id=user.id).order_by(Sender.count.desc()).all()

    grouped: dict[str, list[Sender]] = {}
    for s in senders:
        grouped.setdefault(s.category, []).append(s)
    order = ["newsletter", "ai", "marketing", "other"]
    grouped = {k: grouped[k] for k in order if k in grouped}

    return templates.TemplateResponse("onboarding.html", {
        "request": request, "user": user, "grouped": grouped, "total": len(senders),
    })


@app.post("/senders/save")
async def save_senders_async(request: Request, db: Session = Depends(get_session)):
    user = require_user(request, db)
    form = await request.form()
    selected_ids = {int(v) for k, v in form.multi_items() if k == "sender"}
    for s in db.query(Sender).filter_by(user_id=user.id).all():
        s.selected = s.id in selected_ids
    user.onboarded = True
    db.commit()
    return RedirectResponse("/settings", status_code=303)


# ---------------------------------------------------------------------------
# Settings: Excel recipients + frequency + run time
# ---------------------------------------------------------------------------

@app.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request, db: Session = Depends(get_session)):
    user = require_user(request, db)
    statuses = [s for (s,) in db.query(Contact.status).filter_by(user_id=user.id).all()]
    contact_counts = {
        "active": statuses.count("active"),
        "bounced": statuses.count("bounced"),
        "unsubscribed": statuses.count("unsubscribed"),
        "complained": statuses.count("complained"),
    }
    return templates.TemplateResponse("settings.html", {
        "request": request, "user": user,
        "frequencies": scheduler.FREQUENCY_LABELS,
        "recipient_count": contact_counts["active"],
        "contact_counts": contact_counts,
        "hours": list(range(24)),
    })


@app.post("/settings/recipients")
async def upload_recipients(request: Request, file: UploadFile = File(...), db: Session = Depends(get_session)):
    user = require_user(request, db)
    data = await file.read()
    try:
        emails = excel_recipients.extract_emails_from_xlsx(data)
    except Exception as e:  # noqa: BLE001
        request.session["flash"] = f"Could not read that file: {e}"
        return RedirectResponse("/settings", status_code=303)
    if not emails:
        request.session["flash"] = "No email addresses found in that file."
        return RedirectResponse("/settings", status_code=303)

    filename = file.filename or "recipients.xlsx"
    existing = {c.email: c for c in db.query(Contact).filter_by(user_id=user.id).all()}
    added = 0
    for email in emails:
        if email in existing:
            continue  # never reset an existing contact's status (e.g. unsubscribed/bounced) on re-upload
        db.add(Contact(user_id=user.id, email=email, source=filename))
        added += 1
    user.recipients_file = filename  # last-upload filename, for display only
    db.commit()
    request.session["flash"] = (
        f"Loaded {len(emails)} recipient(s) from {filename} — {added} new, "
        f"{len(emails) - added} already on the list.")
    return RedirectResponse("/settings", status_code=303)


AVATAR_MAX_BYTES = 2 * 1024 * 1024  # 2MB - plenty for a small headshot, keeps DB rows small
AVATAR_ALLOWED_MIMES = {"image/jpeg", "image/png", "image/webp"}


@app.post("/settings/avatar")
async def upload_avatar(request: Request, file: UploadFile = File(...), db: Session = Depends(get_session)):
    """Author photo for the 'banner' layout's byline (email_template._initial_avatar).
    Stored as base64 in the DB, not on disk - this app runs on hosts with no
    persistent disk. Served back publicly at GET /avatar/{user.id} below, since a
    recipient's mail client fetches it directly with no session."""
    user = require_user(request, db)
    data = await file.read()
    mime = file.content_type or ""
    if mime not in AVATAR_ALLOWED_MIMES:
        request.session["flash"] = "Photo must be a JPEG, PNG, or WebP image."
        return RedirectResponse("/settings", status_code=303)
    if len(data) > AVATAR_MAX_BYTES:
        request.session["flash"] = "Photo is too large — please use one under 2MB."
        return RedirectResponse("/settings", status_code=303)
    user.avatar_image = base64.b64encode(data).decode("ascii")
    user.avatar_mime = mime
    db.commit()
    request.session["flash"] = "Profile photo updated."
    return RedirectResponse("/settings", status_code=303)


@app.post("/settings/avatar/remove")
def remove_avatar(request: Request, db: Session = Depends(get_session)):
    user = require_user(request, db)
    user.avatar_image = ""
    user.avatar_mime = ""
    db.commit()
    request.session["flash"] = "Profile photo removed — back to the initial-circle avatar."
    return RedirectResponse("/settings", status_code=303)


@app.get("/avatar/{user_id}")
def avatar_image(user_id: int, db: Session = Depends(get_session)):
    """Public (no login) - referenced by <img> in sent newsletters, so a
    recipient's mail client (not a browser session) fetches this directly."""
    user = db.get(User, user_id)
    if not user or not user.avatar_image:
        raise HTTPException(404, "No avatar")
    return Response(
        content=base64.b64decode(user.avatar_image),
        media_type=user.avatar_mime or "image/jpeg",
        headers={"Cache-Control": "public, max-age=86400"},
    )


@app.post("/settings")
def save_settings(
    request: Request,
    frequency: str = Form(""),
    run_hour: int = Form(7),
    schedule_active: str = Form(""),
    brand_name: str = Form(""),
    author_name: str = Form(""),
    brand_signature: str = Form(""),
    subscribe_url: str = Form(""),
    business_address: str = Form(""),
    newsletter_layout: str = Form("editorial"),
    warmup_limit: int = Form(0),
    custom_note_enabled: str = Form(""),
    custom_note_title: str = Form("A Note"),
    custom_note_text: str = Form(""),
    custom_note_font: str = Form("default"),
    custom_note_size: int = Form(16),
    custom_note_color: str = Form("#201e1d"),
    custom_note_position: str = Form("top"),
    db: Session = Depends(get_session),
):
    user = require_user(request, db)
    user.frequency = frequency if frequency in scheduler.FREQUENCY_CRON else ""
    user.run_hour = max(0, min(23, int(run_hour)))
    user.schedule_active = bool(schedule_active) and bool(user.frequency)
    user.brand_name = brand_name.strip()
    user.author_name = author_name.strip()
    user.brand_signature = brand_signature.strip()
    user.subscribe_url = subscribe_url.strip()
    user.business_address = business_address.strip()
    user.newsletter_layout = newsletter_layout if newsletter_layout in ("editorial", "banner") else "editorial"
    user.warmup_limit = max(0, int(warmup_limit))
    user.custom_note_enabled = bool(custom_note_enabled)
    user.custom_note_title = custom_note_title.strip() or "A Note"
    user.custom_note_text = custom_note_text.strip()
    user.custom_note_font = (
        custom_note_font if custom_note_font in email_template.CUSTOM_NOTE_FONTS else "default")
    user.custom_note_size = max(
        email_template.CUSTOM_NOTE_SIZE_MIN, min(email_template.CUSTOM_NOTE_SIZE_MAX, int(custom_note_size)))
    user.custom_note_color = email_template.safe_hex_color(
        custom_note_color, email_template.DEFAULT_CUSTOM_NOTE_COLOR)
    user.custom_note_position = (
        custom_note_position if custom_note_position in email_template.CUSTOM_NOTE_POSITIONS else "top")
    db.commit()

    if user.schedule_active:
        scheduler.schedule_user(user.id, user.frequency, user.run_hour)
    else:
        scheduler.unschedule_user(user.id)
    request.session["flash"] = "Settings saved."
    return RedirectResponse("/", status_code=303)


# ---------------------------------------------------------------------------
# Runs (email-driven)
# ---------------------------------------------------------------------------

@app.post("/run-now")
def run_now(request: Request, background_tasks: BackgroundTasks, db: Session = Depends(get_session)):
    user = require_user(request, db)
    # Research + writing takes minutes - far longer than a proxy tolerates for one
    # HTTP request (Cloudflare 520 on Render), so the actual work (resume_run)
    # happens in the background. But begin_run() - creating the visible
    # "running" placeholder - happens HERE, synchronously, before the redirect:
    # BackgroundTasks only start after this response is sent, so creating that
    # row inside the background task instead would race the browser reloading
    # the dashboard before the row exists (worse on a cold Neon connection),
    # which is exactly the "nothing happens on screen" symptom this fixes.
    draft_id = begin_run(user.id)
    if draft_id is None:
        request.session["flash"] = "Could not start a run — try logging in again."
        return RedirectResponse("/", status_code=303)
    background_tasks.add_task(resume_run, user.id, draft_id)
    request.session["flash"] = (
        "Run started — see it under Recent runs below (this page refreshes itself "
        "while it's in progress). You'll get the draft by email once it's done.")
    return RedirectResponse("/", status_code=303)


@app.post("/poll-now")
def poll_now(request: Request, db: Session = Depends(get_session)):
    """Manually check for approval replies now (otherwise auto every 10 min)."""
    user = require_user(request, db)
    result = poll_run_replies(user.id)
    acted = result.get("acted") or []
    if not result.get("ok"):
        request.session["flash"] = f"Check replies failed: {result.get('reason')}"
    elif acted:
        request.session["flash"] = f"Checked replies — {len(acted)} update(s): {[a[1] for a in acted]}"
    else:
        request.session["flash"] = "Checked replies — nothing new yet."
    return RedirectResponse("/", status_code=303)


@app.get("/review/{draft_id}", response_class=HTMLResponse)
def review(draft_id: int, request: Request, db: Session = Depends(get_session)):
    user = require_user(request, db)
    draft = db.get(Draft, draft_id)
    if not draft or draft.user_id != user.id:
        raise HTTPException(404, "Draft not found")
    rendered_html = ""
    if draft.content_json:
        rendered_html = email_template.render_email(
            json.loads(draft.content_json),
            brand_name=user.brand_name or user.name, brand_signature=user.brand_signature,
            subscribe_url=user.subscribe_url, address=user.business_address,
            from_email=user.email, theme=draft.theme, subject=draft.subject,
            issue_number=f"Issue #{draft.id}",
            layout=user.newsletter_layout, author_name=user.author_name or user.brand_name or user.name,
            avatar_url=f"{config.APP_BASE_URL}/avatar/{user.id}" if user.avatar_image else "",
            custom_note=({
                "title": user.custom_note_title, "text": user.custom_note_text,
                "font": user.custom_note_font, "size": user.custom_note_size,
                "color": user.custom_note_color, "position": user.custom_note_position,
            } if user.custom_note_enabled else None),
        )
    return templates.TemplateResponse("review.html", {
        "request": request, "user": user, "draft": draft, "rendered_html": rendered_html,
        "recipient_count": len(active_contact_emails(db, user.id)),
    })


# ---------------------------------------------------------------------------
# Dev: per-call Claude usage log (timestamp, tokens, why each call was made)
# ---------------------------------------------------------------------------

@app.get("/dev/usage", response_class=HTMLResponse)
def dev_usage(request: Request, db: Session = Depends(get_session)):
    user = require_user(request, db)  # any logged-in user - single-deployment dev tool
    rows = db.query(UsageLog).order_by(UsageLog.created_at.desc()).limit(300).all()
    totals = {
        "input": sum(r.input_tokens for r in rows),
        "output": sum(r.output_tokens for r in rows),
        "cache_read": sum(r.cache_read_tokens for r in rows),
        "cache_write": sum(r.cache_write_tokens for r in rows),
    }
    return templates.TemplateResponse("usage_log.html", {
        "request": request, "user": user, "rows": rows, "totals": totals,
    })


# ---------------------------------------------------------------------------
# Resend webhook: delivery/engagement events -> CampaignRecipient + Contact
# suppression. Public route - Resend's servers call this, not a browser
# session, so no require_user here. Signature verification is what proves
# a request is genuinely from Resend (see resend_webhooks.py).
# ---------------------------------------------------------------------------

@app.post("/webhooks/resend")
async def webhooks_resend(request: Request, db: Session = Depends(get_session)):
    raw_body = await request.body()
    try:
        event = resend_webhooks.verify_and_parse(raw_body, dict(request.headers))
    except resend_webhooks.InvalidSignature:
        raise HTTPException(status_code=401, detail="Invalid webhook signature")
    resend_webhooks.apply_event(db, event)
    return PlainTextResponse("ok")


# ---------------------------------------------------------------------------
# Campaign analytics
# ---------------------------------------------------------------------------

_ANALYTICS_STATUSES = ("sent", "delivered", "opened", "clicked", "bounced", "complained")


@app.get("/campaigns", response_class=HTMLResponse)
def campaigns_list(request: Request, db: Session = Depends(get_session)):
    user = require_user(request, db)
    campaigns = db.query(Campaign).filter_by(user_id=user.id).order_by(Campaign.created_at.desc()).all()
    rows = []
    for c in campaigns:
        statuses = [s for (s,) in db.query(CampaignRecipient.status).filter_by(campaign_id=c.id).all()]
        rows.append({"campaign": c, "counts": {s: statuses.count(s) for s in _ANALYTICS_STATUSES}})
    return templates.TemplateResponse("campaigns.html", {
        "request": request, "user": user, "rows": rows,
    })


@app.get("/campaigns/{campaign_id}", response_class=HTMLResponse)
def campaign_detail(campaign_id: int, request: Request, db: Session = Depends(get_session)):
    user = require_user(request, db)
    campaign = db.get(Campaign, campaign_id)
    if not campaign or campaign.user_id != user.id:
        raise HTTPException(404, "Campaign not found")
    recipients = (
        db.query(CampaignRecipient, Contact)
        .join(Contact, CampaignRecipient.contact_id == Contact.id)
        .filter(CampaignRecipient.campaign_id == campaign.id)
        .order_by(CampaignRecipient.last_event_at.desc())
        .all()
    )
    statuses = [cr.status for cr, _ in recipients]
    counts = {s: statuses.count(s) for s in _ANALYTICS_STATUSES}
    return templates.TemplateResponse("campaign_detail.html", {
        "request": request, "user": user, "campaign": campaign,
        "recipients": recipients, "counts": counts,
    })


# ---------------------------------------------------------------------------
# Unsubscribe: public route, no login. Handles both a normal link click (GET)
# and RFC 8058 one-click unsubscribe (POST, sent by supporting mail clients
# with no user interaction) - both take effect immediately, no confirmation
# step, per the one-click spec.
# ---------------------------------------------------------------------------

@app.api_route("/unsubscribe/{token}", methods=["GET", "POST"], response_class=HTMLResponse)
def unsubscribe_contact(token: str, request: Request, db: Session = Depends(get_session)):
    contact_id = unsubscribe.parse_token(token)
    contact = db.get(Contact, contact_id) if contact_id else None
    if contact and contact.status not in ("bounced", "complained"):
        contact.status = "unsubscribed"
        db.commit()
    return templates.TemplateResponse("unsubscribed.html", {
        "request": request, "found": bool(contact),
    })
