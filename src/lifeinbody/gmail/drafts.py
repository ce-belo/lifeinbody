"""Gmail draft creation — builds MIME, calls users.drafts.create, records to DB."""
from __future__ import annotations

import base64
import logging
import sqlite3
from datetime import datetime, timezone
from email.message import EmailMessage

log = logging.getLogger("lifeinbody.gmail.drafts")


def _build_reply_mime(*, to_address: str, subject: str, body: str) -> EmailMessage:
    msg = EmailMessage()
    msg["To"] = to_address
    if not subject.lower().startswith("re:"):
        subject = f"Re: {subject}"
    msg["Subject"] = subject
    msg.set_content(body)
    return msg


def create_reply_draft(
    service,
    *,
    thread_id: str,
    to_address: str,
    subject: str,
    body: str,
) -> dict:
    """Create a Gmail draft as a reply on the given thread. Returns the API response."""
    mime = _build_reply_mime(to_address=to_address, subject=subject, body=body)
    raw = base64.urlsafe_b64encode(mime.as_bytes()).decode("ascii")
    resp = (
        service.users()
        .drafts()
        .create(userId="me", body={"message": {"raw": raw, "threadId": thread_id}})
        .execute()
    )
    log.info("created draft %s for thread %s", resp.get("id"), thread_id)
    return resp


def create_standalone_draft(
    service,
    *,
    to_address: str,
    subject: str,
    body: str,
) -> dict:
    """Create a Gmail draft that isn't a reply to anything (e.g. the daily summary)."""
    msg = EmailMessage()
    msg["To"] = to_address
    msg["Subject"] = subject
    msg.set_content(body)
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode("ascii")
    resp = (
        service.users()
        .drafts()
        .create(userId="me", body={"message": {"raw": raw}})
        .execute()
    )
    log.info("created standalone draft %s", resp.get("id"))
    return resp


def update_reply_draft(
    service,
    *,
    draft_id: str,
    thread_id: str,
    to_address: str,
    subject: str,
    body: str,
) -> dict:
    """Replace the body of an existing draft, keeping the same draft id."""
    mime = _build_reply_mime(to_address=to_address, subject=subject, body=body)
    raw = base64.urlsafe_b64encode(mime.as_bytes()).decode("ascii")
    resp = (
        service.users()
        .drafts()
        .update(
            userId="me",
            id=draft_id,
            body={"message": {"raw": raw, "threadId": thread_id}},
        )
        .execute()
    )
    log.info("updated draft %s for thread %s", draft_id, thread_id)
    return resp


def record_draft(conn: sqlite3.Connection, draft_id: str, thread_id: str) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO drafts (draft_id, thread_id, created_at, approved) VALUES (?, ?, ?, 0)",
        (draft_id, thread_id, datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()


def thread_has_draft(conn: sqlite3.Connection, thread_id: str) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM drafts WHERE thread_id = ? LIMIT 1", (thread_id,)
        ).fetchone()
        is not None
    )


def get_draft_for_thread(conn: sqlite3.Connection, thread_id: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT draft_id, thread_id, created_at, approved FROM drafts WHERE thread_id = ?",
        (thread_id,),
    ).fetchone()


def mark_approved(conn: sqlite3.Connection, draft_id: str) -> None:
    conn.execute("UPDATE drafts SET approved = 1 WHERE draft_id = ?", (draft_id,))
    conn.commit()


def list_gmail_draft_ids(service) -> set[str]:
    """Return every draft id currently in the user's Gmail. Paginated."""
    ids: set[str] = set()
    page_token: str | None = None
    while True:
        kwargs = {"userId": "me", "maxResults": 500}
        if page_token:
            kwargs["pageToken"] = page_token
        resp = service.users().drafts().list(**kwargs).execute()
        for d in resp.get("drafts", []) or []:
            ids.add(d["id"])
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    return ids


def reconcile_with_gmail(conn: sqlite3.Connection, service) -> int:
    """Drop local draft rows whose Gmail draft no longer exists.

    Covers two cases that diverge our DB from reality: the user sent the draft
    from Gmail (Gmail consumes the draft id), or deleted it manually.
    Returns the number of rows removed.
    """
    live = list_gmail_draft_ids(service)
    local = [r[0] for r in conn.execute("SELECT draft_id FROM drafts").fetchall()]
    stale = [d for d in local if d not in live]
    if not stale:
        return 0
    conn.executemany("DELETE FROM drafts WHERE draft_id = ?", [(d,) for d in stale])
    conn.commit()
    log.info("pruned %d stale draft row(s)", len(stale))
    return len(stale)
