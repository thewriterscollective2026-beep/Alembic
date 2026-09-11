"""Central configuration, loaded from environment / .env file."""
import logging
import os
from pathlib import Path
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")

# INFO-level logs (incl. ai_service's per-call token usage) reach stdout/server.out.log
# even though uvicorn's own logging config doesn't raise the root logger above WARNING.
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")

# Google adds the 'openid' scope to the granted set; relax the strict check so
# the token exchange doesn't raise "Scope has changed".
os.environ.setdefault("OAUTHLIB_RELAX_TOKEN_SCOPE", "1")

# --- Google OAuth ---
GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET", "")
OAUTH_REDIRECT_URI = os.getenv("OAUTH_REDIRECT_URI", "http://localhost:8000/auth/callback")

# oauthlib refuses non-HTTPS redirect URIs by default; permit http://localhost
# for local development only.
if OAUTH_REDIRECT_URI.startswith("http://"):
    os.environ.setdefault("OAUTHLIB_INSECURE_TRANSPORT", "1")

# Scopes: read mail, send mail, and identify the signed-in user.
OAUTH_SCOPES = [
    "openid",
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/userinfo.profile",
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.send",
]

# --- Anthropic / Claude ---
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
CLAUDE_MODEL = os.getenv("CLAUDE_MODEL", "claude-sonnet-5")          # newsletter writing/research - quality-sensitive
CLAUDE_MODEL_LIGHT = os.getenv("CLAUDE_MODEL_LIGHT", "claude-haiku-4-5")  # classification/extraction - mechanical, cheap
# Thinking depth for the newsletter-writing/research call (low/medium/high/xhigh/max).
# "medium" matches "high" quality on research-shaped tasks at a fraction of the cost.
CLAUDE_WRITER_EFFORT = os.getenv("CLAUDE_WRITER_EFFORT", "medium")

# --- App secrets ---
SESSION_SECRET = os.getenv("SESSION_SECRET", "dev-insecure-secret-change-me")
FERNET_KEY = os.getenv("FERNET_KEY", "")

# --- Scan tuning ---
SCAN_DAYS = int(os.getenv("SCAN_DAYS", "60"))
SCAN_MESSAGE_LIMIT = int(os.getenv("SCAN_MESSAGE_LIMIT", "400"))

# --- Research tuning ---
# Max web searches Claude may run per newsletter-writing call (cost/latency guardrail).
WEB_SEARCH_MAX_USES = int(os.getenv("WEB_SEARCH_MAX_USES", "4"))

# --- Resend (bulk newsletter sending - see Newsletter_Tool_Email_Sending_Plan.docx) ---
# System emails (review/approval) are NOT affected by this - those stay on the
# client's own Gmail/IMAP mailbox via app/provider.py. This is only for the final
# blast to the recipient list. TEST values today - swap when the real owner
# domain is ready; never hardcode a domain/address anywhere in code.
RESEND_API_KEY = os.getenv("RESEND_API_KEY", "")
RESEND_FROM_EMAIL = os.getenv("RESEND_FROM_EMAIL", "")
# Signing secret for the /webhooks/resend endpoint (Resend dashboard -> Webhooks ->
# create an endpoint -> copy its secret, whsec_...). Verified via the svix package.
RESEND_WEBHOOK_SECRET = os.getenv("RESEND_WEBHOOK_SECRET", "")
# Publicly reachable base URL of THIS app (no trailing slash) - used to build the
# one-click unsubscribe link embedded in each sent newsletter. Defaults to local
# dev; must be set to the real deployed HTTPS URL once this is deployed, since a
# recipient's mail client can't reach localhost.
APP_BASE_URL = os.getenv("APP_BASE_URL", "http://localhost:8000")

# --- Storage ---
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(exist_ok=True)
DB_PATH = DATA_DIR / "app.db"
DATABASE_URL = f"sqlite:///{DB_PATH}"


def missing_required():
    """Return a list of required settings that are not configured."""
    missing = []
    if not GOOGLE_CLIENT_ID:
        missing.append("GOOGLE_CLIENT_ID")
    if not GOOGLE_CLIENT_SECRET:
        missing.append("GOOGLE_CLIENT_SECRET")
    if not ANTHROPIC_API_KEY:
        missing.append("ANTHROPIC_API_KEY")
    if not FERNET_KEY:
        missing.append("FERNET_KEY")
    return missing
