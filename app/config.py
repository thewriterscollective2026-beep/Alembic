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
