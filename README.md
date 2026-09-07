# Alembic

A multi-user tool where each person signs in with their **own Google account**,
picks the newsletters they already receive, and the tool:

1. **Scans** their Gmail and detects newsletter senders (via the `List-Unsubscribe`
   header + Gmail's Promotions/Updates categories).
2. **Categorizes** senders into *newsletter / marketing / ai / other* (Claude).
3. Lets the user **select** which senders to include.
4. On a schedule (daily / 3x-week / 2x-week / weekly / 2x-monthly), **reads** new emails
   from those senders — **including PDF/DOCX attachments** — and **summarizes** each with
   Claude.
5. **Researches** the real themes surfaced by that summary using Claude's live web search,
   then **repurposes** everything into one fresh, personalized newsletter — genuinely new
   content, not a repackage of the source newsletters — run through an embedded
   *repurpose* pass and an *anti-AI-writing* pass.
6. Holds the draft at a **review gate** — nothing sends without the user clicking
   **Approve**.
7. On approval, **sends** the newsletter from the user's own Gmail to their recipient list.

> Design choices (per your requirements): **Google-only**, multi-user via
> "Sign in with Google" in **Testing mode** (≤100 users, no verification review),
> and sending stays **inside Gmail** for small lists.

---

## Setup (about 10 minutes)

### 1. Python deps
```bash
pip install -r requirements.txt
```

### 2. Google Cloud project (one-time, by you)
1. Go to https://console.cloud.google.com/ → create a project.
2. **APIs & Services → Enable APIs** → enable **Gmail API**.
3. **OAuth consent screen** → User type **External** → fill app name/email →
   add scopes `gmail.readonly`, `gmail.send`, `userinfo.email`, `userinfo.profile` →
   **Publishing status: Testing** → add each user's Google address under **Test users**
   (up to 100).
4. **Credentials → Create Credentials → OAuth client ID → Web application**.
   - Authorized redirect URI: `http://localhost:8000/auth/callback`
   - Copy the **Client ID** and **Client secret**.

### 3. Anthropic key
Get an API key at https://console.anthropic.com/.

### 4. Configure `.env`
```bash
cp .env.example .env
```
Fill in `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`, `ANTHROPIC_API_KEY`, `SESSION_SECRET`.
Generate a `FERNET_KEY` (encrypts stored Google tokens):
```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

### 5. Run
```bash
python run.py
```
Open http://localhost:8000 and click **Sign in with Google**.
(In Testing mode Google shows an "unverified app" screen — click *Advanced → continue*.)

---

## How each requirement maps to the code

| Requirement | Where |
|---|---|
| Per-user login (Google) | `app/auth.py`, `/auth/callback` in `app/main.py` |
| Auto-list subscribed newsletters | `gmail_service.scan_newsletters` |
| Categorize + let user choose | `ai_service.categorize_senders`, `templates/onboarding.html` |
| Daily read + attachments | `gmail_service.fetch_new_from_senders`, `app/attachments.py` |
| Summarize all | `ai_service.build_summary` (one batched call for all sources) |
| Research themes (live web search) | `ai_service.build_newsletter_from_summary` / `revise_newsletter` (Claude's `web_search` tool) |
| Repurpose + no-AI rewrite | `ai_service.build_newsletter_from_summary` (see `REPURPOSE_GUIDANCE` / `NO_AI_GUIDANCE`) |
| Personalized newsletter | `gmail_service.send_personalized` (`{{FIRST_NAME}}` token) |
| **Review gate before send** | `Draft.stage` (`summary_review` → `email_review` → `sent`), approved by replying to email — see `app/pipeline.py` |
| Ask for recipient list | `templates/settings.html`, `app/excel_recipients.py` |
| Send from same Gmail | `gmail_service.send_personalized` |
| Loop on chosen frequency | `app/scheduler.py` |

### Tuning the `/repurpose` and `/no-ai` voice rules
Open `app/ai_service.py` and edit `REPURPOSE_GUIDANCE` and `NO_AI_GUIDANCE` — these are
prompt constants distilled into the newsletter-writing system prompt (`_writer_system`).
Edit them directly to change tone/style rules; sensible defaults are already in place.

---

## Important real-world notes
- **Consent & law:** only add recipients who agreed to receive this. Bulk email is
  regulated (CAN-SPAM / GDPR / CASL) — real newsletters need consent + an unsubscribe
  line + a mailing address. This tool sends what you write; compliance is on you.
- **Gmail limits:** ~500 sends/day (consumer), ~2,000 (Workspace). Beyond that, swap
  `gmail_service.send_personalized` for an ESP (Resend/SendGrid/SES).
- **Tokens** are encrypted at rest with `FERNET_KEY`. Keep `.env` out of git (it is
  in `.gitignore`).
- **Scheduler** runs in-process, per user, on their chosen frequency/hour; a separate
  poll job checks for approval replies every few minutes (`app/scheduler.py`). For
  heavier production load, move the DB to Postgres and run the scheduler as its own
  worker.

---

## Deployment

A `Dockerfile` and `Procfile` are included so this runs the same way on most hosts:

```bash
docker build -t newsletter-repurposer .
docker run -p 8000:8000 --env-file .env newsletter-repurposer
```

Notes for any production host:
- Set the real environment variables on the host (not a checked-in `.env`).
- Don't run with `--reload` (that's `run.py`'s local-dev entrypoint only) — the
  `Dockerfile`/`Procfile` already invoke plain `uvicorn`.
- `data/app.db` (SQLite) needs a **persistent volume** if the host's filesystem is
  ephemeral (most PaaS containers are) — otherwise every redeploy wipes user data.
- `OAUTH_REDIRECT_URI` must be updated to the deployed HTTPS URL and added as an
  Authorized redirect URI in the Google Cloud OAuth client.
