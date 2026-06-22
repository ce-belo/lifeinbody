"""OAuth-backed Google Sheets client + raw snapshot pull.

Reuses the same OAuth token as Gmail (see gmail/auth.py); the token's scope
list includes sheets.readonly, so one `lifeinbody auth` grants both.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import gspread

from .. import config
from ..gmail.auth import OAuthClientMissing, authenticate

log = logging.getLogger("lifeinbody.sheet.client")


class SheetIdMissing(RuntimeError):
    pass


def get_client() -> gspread.Client:
    """Authorize gspread using the shared OAuth token."""
    creds = authenticate()
    return gspread.authorize(creds)


def fetch_snapshot(sheet_id: str | None = None) -> dict[str, Any]:
    """Pull every tab in the workbook as a list-of-lists. Returns a JSON-ready dict."""
    sheet_id = sheet_id or config.SHEET_ID
    if not sheet_id:
        raise SheetIdMissing("LIFEINBODY_SHEET_ID is not set in .env")

    client = get_client()
    workbook = client.open_by_key(sheet_id)

    tabs: list[dict[str, Any]] = []
    for ws in workbook.worksheets():
        rows = ws.get_all_values()
        tabs.append({
            "name": ws.title,
            "sheet_id": ws.id,
            "row_count": len(rows),
            "col_count": max((len(r) for r in rows), default=0),
            "rows": rows,
        })
        log.info("pulled tab %r: %d rows", ws.title, len(rows))

    return {
        "workbook_id": sheet_id,
        "workbook_title": workbook.title,
        "synced_at": datetime.now(timezone.utc).isoformat(),
        "tab_count": len(tabs),
        "tabs": tabs,
    }


def write_snapshot(snapshot: dict[str, Any], path: Path | None = None) -> Path:
    """Write the snapshot to disk as pretty-printed JSON. Returns the path written."""
    config.ensure_dirs()
    path = path or config.SHEET_SNAPSHOT_PATH
    path.write_text(json.dumps(snapshot, indent=2, ensure_ascii=False))
    return path


def load_snapshot(path: Path | None = None) -> dict[str, Any]:
    """Read back the cached snapshot. Raises FileNotFoundError if absent."""
    path = path or config.SHEET_SNAPSHOT_PATH
    return json.loads(path.read_text())
