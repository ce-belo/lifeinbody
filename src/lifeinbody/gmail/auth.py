"""Gmail OAuth: installed-app flow, token persistence, and a service factory."""
from __future__ import annotations

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

from ..config import (
    GOOGLE_SCOPES,
    OAUTH_CLIENT_PATH,
    OAUTH_TOKEN_PATH,
    ensure_dirs,
)


class OAuthClientMissing(FileNotFoundError):
    """Raised when credentials/oauth_client.json is not present."""


def _load_saved_credentials() -> Credentials | None:
    if not OAUTH_TOKEN_PATH.exists():
        return None
    creds = Credentials.from_authorized_user_file(str(OAUTH_TOKEN_PATH), GOOGLE_SCOPES)
    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
        OAUTH_TOKEN_PATH.write_text(creds.to_json())
    return creds


def _run_installed_flow() -> Credentials:
    if not OAUTH_CLIENT_PATH.exists():
        raise OAuthClientMissing(
            f"Missing OAuth client JSON at {OAUTH_CLIENT_PATH}."
        )
    flow = InstalledAppFlow.from_client_secrets_file(str(OAUTH_CLIENT_PATH), GOOGLE_SCOPES)
    creds = flow.run_local_server(port=0, prompt="consent")
    ensure_dirs()
    OAUTH_TOKEN_PATH.write_text(creds.to_json())
    return creds


def authenticate(force: bool = False) -> Credentials:
    """Return valid credentials, running the browser flow if needed."""
    if force and OAUTH_TOKEN_PATH.exists():
        OAUTH_TOKEN_PATH.unlink()
    creds = None if force else _load_saved_credentials()
    if creds and creds.valid:
        return creds
    return _run_installed_flow()


def get_service():
    """Return an authenticated Gmail API client (v1)."""
    creds = authenticate()
    return build("gmail", "v1", credentials=creds, cache_discovery=False)
