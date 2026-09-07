"""FastAPI application: login, onboarding, settings (Excel upload), and an
email-driven review flow (runs email the user; approvals happen by reply)."""
from pathlib import Path

from fastapi import FastAPI, Request, Depends, Form, HTTPException, UploadFile, File
from fastapi.responses import RedirectResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware
from sqlalchemy.orm import Session

import json
from . import config, auth, gmail_service, ai_service, scheduler, excel_recipients, provider, imap_service, crypto, email_template
from .database import init_db, get_session
from .models import User, Sender, Draft
from .pipeline import start_run, poll_run_replies, _parse_recipients

BASE_DIR = Path(__file__).resolve().parent.parent
app = FastAPI(title="Alembic")
app.add_middleware(SessionMiddleware, secret_key=config.SESSION_SECRET, max_age=60 * 60 * 24 * 14)
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
templates.env.globals["freq_labels"] = scheduler.FREQUENCY_LABELS

STAGE_LABELS = {
    "summary_review": "Awaiting summary approval (check your inbox)",
    "email_review": "Awaiting newsletter approval (check your inbox)",
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
        "recipient_count": len(_parse_recipients(user.recipients)),
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
    return templates.TemplateResponse("settings.html", {
        "request": request, "user": user,
        "frequencies": scheduler.FREQUENCY_LABELS,
        "recipient_count": len(_parse_recipients(user.recipients)),
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
    user.recipients = "\n".join(emails)
    user.recipients_file = file.filename or "recipients.xlsx"
    db.commit()
    request.session["flash"] = f"Loaded {len(emails)} recipient(s) from {user.recipients_file}."
    return RedirectResponse("/settings", status_code=303)


@app.post("/settings")
def save_settings(
    request: Request,
    frequency: str = Form(""),
    run_hour: int = Form(7),
    schedule_active: str = Form(""),
    brand_name: str = Form(""),
    brand_signature: str = Form(""),
    subscribe_url: str = Form(""),
    business_address: str = Form(""),
    db: Session = Depends(get_session),
):
    user = require_user(request, db)
    user.frequency = frequency if frequency in scheduler.FREQUENCY_CRON else ""
    user.run_hour = max(0, min(23, int(run_hour)))
    user.schedule_active = bool(schedule_active) and bool(user.frequency)
    user.brand_name = brand_name.strip()
    user.brand_signature = brand_signature.strip()
    user.subscribe_url = subscribe_url.strip()
    user.business_address = business_address.strip()
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
def run_now(request: Request, db: Session = Depends(get_session)):
    user = require_user(request, db)
    result = start_run(user.id)
    if result.get("draft_id"):
        request.session["flash"] = (
            "Summary emailed to your inbox — reply APPROVED there to continue.")
    else:
        request.session["flash"] = result.get("reason", "Run complete.")
    return RedirectResponse("/", status_code=303)


@app.post("/poll-now")
def poll_now(request: Request, db: Session = Depends(get_session)):
    """Manually check for approval replies now (otherwise auto every 10 min)."""
    user = require_user(request, db)
    result = poll_run_replies(user.id)
    acted = result.get("acted") or []
    request.session["flash"] = (
        f"Checked replies — {len(acted)} update(s): {[a[1] for a in acted]}" if acted
        else "Checked replies — nothing new yet.")
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
        )
    return templates.TemplateResponse("review.html", {
        "request": request, "user": user, "draft": draft, "rendered_html": rendered_html,
        "recipient_count": len(_parse_recipients(user.recipients)),
    })
