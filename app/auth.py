"""Google OAuth 2.0 flow + credential helpers."""
import json
from google_auth_oauthlib.flow import Flow
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from . import config, crypto
from .models import User


def _client_config() -> dict:
    return {
        "web": {
            "client_id": config.GOOGLE_CLIENT_ID,
            "client_secret": config.GOOGLE_CLIENT_SECRET,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": [config.OAUTH_REDIRECT_URI],
        }
    }


def build_flow(state: str | None = None) -> Flow:
    flow = Flow.from_client_config(
        _client_config(),
        scopes=config.OAUTH_SCOPES,
        state=state,
    )
    flow.redirect_uri = config.OAUTH_REDIRECT_URI
    return flow


def authorization_url() -> tuple[str, str]:
    """Return (url, state). Requests offline access for a refresh token."""
    flow = build_flow()
    url, state = flow.authorization_url(
        access_type="offline",
        include_granted_scopes="true",
        prompt="consent",  # ensures we get a refresh_token
    )
    return url, state


def exchange_code(code: str, state: str) -> Credentials:
    flow = build_flow(state=state)
    flow.fetch_token(code=code)
    return flow.credentials


def credentials_to_json(creds: Credentials) -> str:
    return json.dumps({
        "token": creds.token,
        "refresh_token": creds.refresh_token,
        "token_uri": creds.token_uri,
        "client_id": creds.client_id,
        "client_secret": creds.client_secret,
        "scopes": creds.scopes,
    })


def store_credentials(user: User, creds: Credentials):
    user.token_enc = crypto.encrypt(credentials_to_json(creds))
    if creds.refresh_token:
        user.refresh_token_enc = crypto.encrypt(creds.refresh_token)


def load_credentials(user: User) -> Credentials:
    """Rebuild Credentials from storage, refreshing the access token if needed."""
    data = json.loads(crypto.decrypt(user.token_enc))
    # Prefer the separately-stored refresh token (survives re-logins that omit it)
    if user.refresh_token_enc and not data.get("refresh_token"):
        data["refresh_token"] = crypto.decrypt(user.refresh_token_enc)

    creds = Credentials(
        token=data.get("token"),
        refresh_token=data.get("refresh_token"),
        token_uri=data.get("token_uri", "https://oauth2.googleapis.com/token"),
        client_id=config.GOOGLE_CLIENT_ID,
        client_secret=config.GOOGLE_CLIENT_SECRET,
        scopes=data.get("scopes", config.OAUTH_SCOPES),
    )
    if not creds.valid and creds.refresh_token:
        creds.refresh(Request())
    return creds
