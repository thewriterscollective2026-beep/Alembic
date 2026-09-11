# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A multi-user FastAPI app ("Alembic"): each user connects their own mailbox
(Google OAuth or plain IMAP/SMTP), the tool scans it for newsletter subscriptions, and on a
schedule it reads new mail from the senders the user picked, summarizes it with Claude, and
repurposes the summaries into one personalized newsletter — with **every step approved by the
user replying to an email**, before anything goes to their recipient list.

## Commands

```bash
pip install -r requirements.txt   # install deps (use the .venv already in this repo)
python run.py                     # run the app (http://localhost:8000), auto-reloads
```

There is no test suite, linter, or build step configured in this repo.

Required setup before running: copy `.env.example` to `.env` and fill in
`GOOGLE_CLIENT_ID`/`GOOGLE_CLIENT_SECRET` (Google Cloud OAuth client), `ANTHROPIC_API_KEY`,
`SESSION_SECRET`, and `FERNET_KEY` (generate via
`python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`).
See `README.md` for the full Google Cloud OAuth consent screen setup.

## Architecture

**The core design point: nothing is a web dashboard flow past onboarding — the pipeline runs
over email itself.** A run scrapes mail, emails the user a summary, waits for their reply,
emails them the drafted newsletter, waits for another reply, then sends to the recipient list.
The web UI (`templates/`, routes in `app/main.py`) is only for login, picking senders, and
configuring settings — it is not where approvals happen.

### Provider abstraction (`app/provider.py`)

Every mail operation (scan, fetch, send-tracked, read-reply, send-personalized) goes through
a `_GmailHandle` or `_ImapHandle`, selected by `provider.get_handle(user)` based on
`User.provider` (`google` or `imap`). `app/pipeline.py` and `app/main.py` only ever call the
handle's interface, never `gmail_service`/`imap_service` directly — when adding a new
capability to the pipeline, add it to both handles and both underlying service modules
(`app/gmail_service.py`, `app/imap_service.py`).

### The email-approval pipeline (`app/pipeline.py`)

`Draft.stage` is the state machine, driven by two entry points:

- `start_run(user_id)` — scrapes mail from the user's selected `Sender`s over a frequency
  window (`FREQUENCY_LOOKBACK_DAYS`), builds a digest summary (`ai_service.build_summary`),
  and emails it to the user. Creates a `Draft` in `summary_review`, tracking the Gmail/IMAP
  thread it's watching (`thread_id`, `awaiting_msg_id`).
- `poll_run_replies(user_id)` — for every `Draft` in `summary_review` or `email_review`,
  checks the watched thread for a new reply and interprets it:
  - **cancel** (`_is_cancel`) → stage becomes `cancelled`.
  - **approval** (`_is_approval`, a short first line like "approved"/"lgtm"/"ok") → advances
    the stage: `summary_review` writes the newsletter and emails it for review
    (`_advance_to_email_review`); `email_review` sends to the real recipient list
    (`_do_final_send`).
  - **anything else** → treated as revision feedback; `_revise` re-runs the relevant Claude
    call with the feedback and re-emails, capped at `MAX_REVISIONS`.

`app/scheduler.py` (APScheduler, in-process) runs two kinds of jobs: one cron job per user
that calls `start_run` on their chosen frequency/hour, and one global `IntervalTrigger` job
(`poll_all_pending`, every `POLL_MINUTES`) that calls `poll_run_replies` for every user with
an active draft. Both the manual "Run now"/"Check replies" buttons in `app/main.py` and the
scheduler call the exact same `pipeline` functions — there's no separate manual code path.

### Claude usage (`app/ai_service.py`)

All calls go through `_complete()`, which tags every call, logs its token usage (the
`ai_service.usage` logger — input/output/cache tokens per call site, since this is the only
spend visibility the app has), and routes to a model: `config.CLAUDE_MODEL` (Sonnet 5) or
`config.CLAUDE_MODEL_LIGHT` (Haiku 4.5, currently only `categorize_senders`).

**Research happens at the summary stage, not at final-write time.** `build_summary` reads
the raw source newsletters directly (not a pre-condensed version), identifies cross-source
themes and where sources agree/disagree, spots gaps none of them cover, and uses Claude's
server-side `web_search` tool (capped by `config.WEB_SEARCH_MAX_USES`) to research those gaps
— so the brief the user approves by email is already research-backed, not just a digest.
`build_newsletter_from_summary` (the first draft) then does NOT search — it writes/structures
from the already-researched summary. `revise_newsletter` and `revise_summary` both keep
`web_search` available, since user feedback on either stage ("add a recent stat on X") is a
legitimate on-demand research trigger. The newsletter-writing/revision calls set
`config.CLAUDE_WRITER_EFFORT` (default `medium`) to control thinking-token spend.
`REPURPOSE_GUIDANCE` and `NO_AI_GUIDANCE` are prompt constants distilled from the project
owner's own content-repurposing and anti-AI-writing style rules — edit these to change the
newsletter's voice/style rules, not the per-call prompts.

**The newsletter body is structured data, not HTML.** `build_newsletter_from_summary` /
`revise_newsletter` return `{theme, subject, sections}` — `sections` is a JSON object
(`note`, `summary_points`, `feature` story, secondary `stories`, an optional `quote`,
`further_reading`) per the schema in `_SECTIONS_SCHEMA_RULE`. All content fields are plain
text; `app/email_template.py` supplies every HTML tag when rendering. `Draft.content_json`
stores this; `Draft.theme` (`tech`/`business`/`general`) picks the color variant. The
`{{FIRST_NAME}}` greeting token (`email_template.GREETING_TOKEN`) is rendered by the template
itself, not authored by the model, and is substituted per-recipient only at final send —
every other stage (drafts, review emails) shows the literal token or a sample name, never a
fabricated real send.

### Two separate sending paths — do not mix them up

**System emails** (the drafted-newsletter review request, the approval thread) go through the
client's own connected mailbox — `app/provider.py`'s `_GmailHandle`/`_ImapHandle`
(`handle.send_tracked`/`get_new_reply`), used throughout `app/pipeline.py`.

**The final blast to the recipient list** goes through `app/resend_service.py`
(`send_campaign`), a transactional-email account we control (Resend), NOT the client's
mailbox — see `Newsletter_Tool_Email_Sending_Plan.docx` for why (deliverability, analytics,
suppression, a 6,000+ recipient list doesn't belong on a personal inbox). Only
`pipeline._do_final_send` calls it. `RESEND_API_KEY`/`RESEND_FROM_EMAIL` are pure `.env`
config — never hardcode a sending domain/address in code, since the domain in use during
early development is a test domain that gets swapped for the real owner's domain later.

**Analytics come back via `POST /webhooks/resend`** (`app/resend_webhooks.py`, wired in
`main.py`) — a public route (Resend's servers call it, not a browser session), signature-
verified with the `svix` package against `RESEND_WEBHOOK_SECRET`. Each event updates the
matching `CampaignRecipient` (matched by `resend_message_id` == the webhook's `data.email_id`)
and, for `email.bounced`/`email.complained`, also flips the underlying `Contact.status` -
that's the actual list-hygiene enforcement point. `/campaigns` and `/campaigns/{id}` show the
resulting counts. There's no `email.unsubscribed` webhook event (Resend doesn't emit one for
this integration pattern) - unsubscribe is our own link/route instead (Phase 4 of the sending
plan, not yet built as of this writing).

### Data model (`app/models.py`)

- `User` — one row per connected mailbox. `provider` picks Google vs IMAP; credentials are
  stored encrypted (`token_enc`/`refresh_token_enc` for Google, `imap_conn_enc` as JSON for
  IMAP) via `app/crypto.py` (Fernet, key from `FERNET_KEY`). Also holds schedule settings and
  branding fields used to build the newsletter's voice and footer. `recipients`/
  `recipients_file` are legacy/unused (superseded by `Contact`) except `recipients_file`,
  which is still updated as a "last upload" display caption.
- `Sender` — a detected newsletter sender from the onboarding scan, categorized by Claude,
  with `selected` marking the user's opt-in.
- `Contact` — one recipient with suppression state (`status`: active/bounced/unsubscribed/
  complained). The actual send list is `pipeline.active_contacts(db, user_id)` — status
  `active` only. Uploading an Excel (`main.py`'s `/settings/recipients`) upserts by
  `(user_id, email)`: new emails get added as `active`, but an existing contact's status is
  **never** reset by a re-upload — only a Resend webhook event or the unsubscribe link
  changes it after that.
- `Campaign` / `CampaignRecipient` — one `Campaign` per final send (`pipeline._do_final_send`
  creates it), one `CampaignRecipient` per contact recording `resend_message_id` and delivery/
  engagement `status` (sent/delivered/opened/clicked/bounced/complained) - the analytics
  source of truth, updated by `app/resend_webhooks.py`.
- `Draft` — one pipeline run; see the stage lifecycle in its docstring and in "The
  email-approval pipeline" above.

`app/database.py` runs a small hand-rolled auto-migration (`_migrate()`) on startup that adds
missing columns to an existing SQLite `data/app.db` — when adding a column to `User` or
`Draft`, add the corresponding `ALTER TABLE` there too (there's no migration *framework* here
despite the project's own name — no Alembic-the-tool, just this hand-rolled function). Brand
new tables (like `Contact`/`Campaign`/`CampaignRecipient`/`UsageLog`) don't need an `ALTER
TABLE` entry — `Base.metadata.create_all()` in `init_db()` creates any missing table
automatically.

### Other modules

- `app/auth.py` — Google OAuth2 flow (`google-auth-oauthlib`), stores/reloads `Credentials`.
- `app/imap_service.py` — IMAP/SMTP equivalent of `gmail_service.py` for non-Google mailboxes
  (e.g. Hostinger webmail); `guess_settings`/`verify_login` back the `/connect` flow.
- `app/resend_service.py` — bulk newsletter sending via Resend's batch Emails API (see "Two
  separate sending paths" above). Third-party API — if you need to change how it calls Resend,
  fetch their current docs live rather than trusting a remembered shape.
- `app/resend_webhooks.py` — verifies + applies inbound Resend delivery/engagement events (see
  "Analytics come back via..." above). Also third-party-API-shaped; same rule applies.
- `app/attachments.py` — extracts text from PDF/DOCX attachments so they feed into
  summarization.
- `app/excel_recipients.py` — pulls every email-looking string out of an uploaded `.xlsx`,
  cell-by-cell, regardless of column layout.
- `app/email_template.py` — renders `Draft.content_json`'s structured sections into the
  branded, email-safe editorial layout (masthead, numbered highlights, feature story, story
  cards, quote, further reading) in one of three color `THEMES`, used for both the real send
  and the review-email preview. No photos/illustrations by design — see its module docstring.

## Notes worth remembering

- Sending is real Gmail/SMTP send, gated only by a user's email reply — there is no dashboard
  "send" button. Be careful with anything that could trigger `_do_final_send` unintentionally.
- Google OAuth is configured for **Testing** publishing status (≤100 test users, no
  verification), per `README.md` — this is a deliberate scope choice, not a TODO.
- The scheduler and DB session for background jobs (`SessionLocal`) run in-process; there is
  no separate worker process.
- The `ANTHROPIC_API_KEY` in `.env` is reserved for real/production research calls, not for
  ad-hoc dev testing — verify prompt/rendering changes with synthetic data (a hand-built
  `sections` dict through `email_template.render_email`) instead of live API calls where
  possible; check `ai_service.usage` log output for real per-call cost before assuming a
  change is cheap.
