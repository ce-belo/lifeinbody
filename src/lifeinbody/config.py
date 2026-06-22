"""Centralized config: paths, env vars, and the business-context block."""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parents[2]
load_dotenv(REPO_ROOT / ".env")

CREDENTIALS_DIR = Path(os.environ.get("LIFEINBODY_CREDENTIALS_DIR") or REPO_ROOT / "credentials")
DATA_DIR = Path(os.environ.get("LIFEINBODY_DATA_DIR") or REPO_ROOT / "data")

OAUTH_CLIENT_PATH = CREDENTIALS_DIR / "oauth_client.json"
OAUTH_TOKEN_PATH = CREDENTIALS_DIR / "token.json"
SERVICE_ACCOUNT_PATH = CREDENTIALS_DIR / "service_account.json"

DB_PATH = DATA_DIR / "lifeinbody.sqlite"
LOG_PATH = DATA_DIR / "lifeinbody.log"
SHEET_SNAPSHOT_PATH = DATA_DIR / "sheet_snapshot.json"
DASHBOARD_HTML_PATH = DATA_DIR / "dashboard.html"

GMAIL_ADDRESS = os.environ.get("LIFEINBODY_GMAIL_ADDRESS", "team@lifeinbody.com")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
ANTHROPIC_MODEL = "claude-sonnet-4-6"
SHEET_ID = os.environ.get("LIFEINBODY_SHEET_ID", "")
DAILY_SUMMARY_RECIPIENT = os.environ.get("DAILY_SUMMARY_RECIPIENT", "")

GMAIL_SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.compose",
    "https://www.googleapis.com/auth/gmail.labels",
]

# Extra Gmail labels (beyond INBOX) to pull during sync. Configurable per-user.
EXTRA_GMAIL_LABELS: list[str] = []

BUSINESS_CONTEXT = """
Life in Body is a small tutoring/coaching practice.
Tone: warm, professional, concise. Use the client's first name.
Sign-off: "Warmly, the Life in Body team".
Never quote prices or commit to dates without the user's approval.
"""


def ensure_dirs() -> None:
    CREDENTIALS_DIR.mkdir(parents=True, exist_ok=True)
    DATA_DIR.mkdir(parents=True, exist_ok=True)
