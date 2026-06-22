"""Gmail → SQLite sync pipeline.

Pulls threads matching configured labels, parses messages, classifies each
thread, extracts invoice mentions, and upserts everything into the tracker DB.
"""
from __future__ import annotations

import base64
import logging
import sqlite3
from dataclasses import asdict
from datetime import datetime, timezone
from email.utils import parseaddr
from typing import Iterable

from googleapiclient.errors import HttpError

from .. import config
from ..tracker import db as tracker_db
from ..tracker.models import InvoiceMention, Message, Thread
from . import classifier

log = logging.getLogger("lifeinbody.gmail.sync")


# ---------- Gmail API helpers ----------

def _resolve_label_ids(service, label_names: Iterable[str]) -> tuple[dict[str, str], dict[str, str]]:
    """Return (name->id, id->name) maps for the requested labels (and system labels)."""
    resp = service.users().labels().list(userId="me").execute()
    by_name_lower = {lbl["name"].lower(): lbl for lbl in resp.get("labels", [])}
    name_to_id: dict[str, str] = {}
    id_to_name: dict[str, str] = {}
    for n in label_names:
        lbl = by_name_lower.get(n.lower())
        if not lbl:
            log.warning("label %r not found in Gmail account; skipping", n)
            continue
        name_to_id[lbl["name"]] = lbl["id"]
        id_to_name[lbl["id"]] = lbl["name"]
    for lbl in resp.get("labels", []):
        id_to_name.setdefault(lbl["id"], lbl["name"])
    return name_to_id, id_to_name


def _list_thread_ids(service, label_id: str) -> list[str]:
    ids: list[str] = []
    page_token: str | None = None
    while True:
        kwargs = {"userId": "me", "labelIds": [label_id], "maxResults": 100}
        if page_token:
            kwargs["pageToken"] = page_token
        resp = service.users().threads().list(**kwargs).execute()
        ids.extend(t["id"] for t in resp.get("threads", []))
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    return ids


def _fetch_thread(service, thread_id: str) -> dict:
    return (
        service.users()
        .threads()
        .get(userId="me", id=thread_id, format="full")
        .execute()
    )


# ---------- Message parsing ----------

def _header(headers: list[dict], name: str) -> str:
    target = name.lower()
    for h in headers:
        if h.get("name", "").lower() == target:
            return h.get("value", "")
    return ""


def _decode_body(part: dict) -> str:
    data = part.get("body", {}).get("data")
    if not data:
        return ""
    try:
        return base64.urlsafe_b64decode(data + "==").decode("utf-8", errors="replace")
    except Exception:  # pragma: no cover
        return ""


def _extract_plain_text(payload: dict) -> str:
    """Walk a Gmail payload tree and return the best text/plain body found."""
    if not payload:
        return ""
    mime = payload.get("mimeType", "")
    if mime == "text/plain":
        return _decode_body(payload)
    if mime.startswith("multipart/") or payload.get("parts"):
        # Prefer text/plain anywhere in the tree before falling back to html.
        plain = ""
        html = ""
        for part in payload.get("parts", []):
            sub = _extract_plain_text(part)
            if part.get("mimeType") == "text/plain" and sub:
                return sub
            if part.get("mimeType") == "text/html" and not html:
                html = _decode_body(part)
            if sub and not plain:
                plain = sub
        if plain:
            return plain
        if html:
            import re as _re
            return _re.sub(r"<[^>]+>", " ", html)
    if mime == "text/html":
        import re as _re
        return _re.sub(r"<[^>]+>", " ", _decode_body(payload))
    return ""


def _is_from_us(email_: str, label_ids: list[str]) -> bool:
    if "SENT" in label_ids:
        return True
    if not email_:
        return False
    addr = email_.lower()
    if addr in {a.lower() for a in (config.OUR_ADDRESSES + [config.GMAIL_ADDRESS])}:
        return True
    domain = addr.rsplit("@", 1)[-1] if "@" in addr else ""
    return domain in {d.lower() for d in config.OUR_DOMAINS}


def _parse_message(raw: dict, our_address: str) -> Message:
    payload = raw.get("payload", {})
    headers = payload.get("headers", [])
    from_raw = _header(headers, "From")
    to_raw = _header(headers, "To")
    name, email_ = parseaddr(from_raw)
    label_ids = raw.get("labelIds", []) or []
    is_us = _is_from_us(email_, label_ids)
    ms = int(raw.get("internalDate", "0"))
    sent_at = datetime.fromtimestamp(ms / 1000, tz=timezone.utc)
    body = _extract_plain_text(payload)
    return Message(
        message_id=raw["id"],
        thread_id=raw["threadId"],
        sent_at=sent_at,
        from_email=email_ or "",
        from_name=name or "",
        to_emails=to_raw,
        snippet=raw.get("snippet", ""),
        body_plain=body,
        is_from_us=bool(is_us),
    )


# ---------- Upserts ----------

_THREAD_UPSERT = """
INSERT INTO threads (
  thread_id, sender_email, sender_name, subject, last_message_at,
  last_message_from, status, needs_followup, followup_reason,
  message_count, last_synced_at
) VALUES (
  :thread_id, :sender_email, :sender_name, :subject, :last_message_at,
  :last_message_from, :status, :needs_followup, :followup_reason,
  :message_count, :last_synced_at
)
ON CONFLICT(thread_id) DO UPDATE SET
  sender_email      = excluded.sender_email,
  sender_name       = excluded.sender_name,
  subject           = excluded.subject,
  last_message_at   = excluded.last_message_at,
  last_message_from = excluded.last_message_from,
  status            = excluded.status,
  needs_followup    = excluded.needs_followup,
  followup_reason   = excluded.followup_reason,
  message_count     = excluded.message_count,
  last_synced_at    = excluded.last_synced_at
"""

_MESSAGE_UPSERT = """
INSERT INTO messages (
  message_id, thread_id, sent_at, from_email, to_emails, snippet, body_plain
) VALUES (
  :message_id, :thread_id, :sent_at, :from_email, :to_emails, :snippet, :body_plain
)
ON CONFLICT(message_id) DO UPDATE SET
  thread_id  = excluded.thread_id,
  sent_at    = excluded.sent_at,
  from_email = excluded.from_email,
  to_emails  = excluded.to_emails,
  snippet    = excluded.snippet,
  body_plain = excluded.body_plain
"""


def _upsert_thread(conn: sqlite3.Connection, t: Thread) -> None:
    conn.execute(
        _THREAD_UPSERT,
        {
            "thread_id": t.thread_id,
            "sender_email": t.sender_email,
            "sender_name": t.sender_name,
            "subject": t.subject,
            "last_message_at": t.last_message_at.isoformat(),
            "last_message_from": t.last_message_from,
            "status": t.status,
            "needs_followup": int(t.needs_followup),
            "followup_reason": t.followup_reason,
            "message_count": t.message_count,
            "last_synced_at": t.last_synced_at.isoformat(),
        },
    )


def _upsert_messages(conn: sqlite3.Connection, messages: list[Message]) -> None:
    conn.executemany(
        _MESSAGE_UPSERT,
        [
            {
                "message_id": m.message_id,
                "thread_id": m.thread_id,
                "sent_at": m.sent_at.isoformat(),
                "from_email": m.from_email,
                "to_emails": m.to_emails,
                "snippet": m.snippet,
                "body_plain": m.body_plain,
            }
            for m in messages
        ],
    )


def _replace_invoice_mentions(conn: sqlite3.Connection, thread_id: str, invoices: list[InvoiceMention]) -> int:
    """Replace status='mentioned' invoice rows for a thread. Returns rows inserted."""
    conn.execute("DELETE FROM invoices WHERE thread_id = ? AND status = 'mentioned'", (thread_id,))
    if not invoices:
        return 0
    conn.executemany(
        """
        INSERT INTO invoices (thread_id, detected_amount, detected_currency,
                              detected_client, status, notes, detected_at)
        VALUES (:thread_id, :detected_amount, :detected_currency,
                :detected_client, :status, :notes, :detected_at)
        """,
        [
            {
                **{k: v for k, v in asdict(inv).items() if k != "detected_at"},
                "detected_at": inv.detected_at.isoformat(),
            }
            for inv in invoices
        ],
    )
    return len(invoices)


# ---------- Top-level sync ----------

def sync_emails(
    service,
    conn: sqlite3.Connection,
    *,
    labels: list[str] | None = None,
    llm_classify: bool = False,
    debug: bool = False,
) -> dict:
    """Pull threads for configured labels, parse, classify, persist. Idempotent."""
    label_names = list(dict.fromkeys((labels or ["INBOX"]) + list(config.EXTRA_GMAIL_LABELS)))
    name_to_id, id_to_name = _resolve_label_ids(service, label_names)
    if not name_to_id:
        raise RuntimeError(f"None of the requested labels exist in Gmail: {label_names}")

    our_address = config.GMAIL_ADDRESS

    # Collect unique thread IDs across all requested labels.
    thread_ids: set[str] = set()
    for name, lbl_id in name_to_id.items():
        ids = _list_thread_ids(service, lbl_id)
        log.info("label %s (%s): %d threads", name, lbl_id, len(ids))
        thread_ids.update(ids)

    before_thread_count = tracker_db.thread_count(conn)
    new_invoice_rows = 0
    now = datetime.now(timezone.utc)

    if config.ANTHROPIC_API_KEY:
        llm_available = True
    else:
        llm_available = False
        if llm_classify:
            log.warning("--llm-classify requested but ANTHROPIC_API_KEY is not set; using rule pass only")

    for i, tid in enumerate(sorted(thread_ids), 1):
        try:
            raw_thread = _fetch_thread(service, tid)
        except HttpError as exc:
            log.warning("could not fetch thread %s: %s", tid, exc)
            continue

        raw_msgs = raw_thread.get("messages", []) or []
        if not raw_msgs:
            continue

        messages = [_parse_message(m, our_address) for m in raw_msgs]
        messages.sort(key=lambda m: m.sent_at)
        latest = messages[-1]

        thread_label_ids = set()
        for m in raw_msgs:
            thread_label_ids.update(m.get("labelIds", []) or [])
        thread_label_names = [id_to_name.get(lid, lid) for lid in thread_label_ids]

        status, last_from = classifier.classify_status(messages, thread_label_names)
        needs_fu, reason = classifier.classify_followup(messages)

        if llm_classify and llm_available and not needs_fu:
            try:
                from ..llm import classify_followup as llm_followup, LLMUnavailable
                llm_needs, llm_reason = llm_followup(_header(raw_msgs[-1].get("payload", {}).get("headers", []), "Subject"), messages)
                if llm_needs:
                    needs_fu, reason = True, f"llm: {llm_reason}"
            except LLMUnavailable as exc:
                log.warning("LLM classifier unavailable: %s", exc)
            except Exception as exc:  # pragma: no cover
                log.warning("LLM classifier failed for thread %s: %s", tid, exc)

        # Pick the first non-us message as the "sender" of the thread.
        first_them = next((m for m in messages if not m.is_from_us), messages[0])
        subject = _header(raw_msgs[0].get("payload", {}).get("headers", []), "Subject") or "(no subject)"

        thread = Thread(
            thread_id=tid,
            sender_email=first_them.from_email,
            sender_name=first_them.from_name,
            subject=subject,
            last_message_at=latest.sent_at,
            last_message_from=last_from,
            status=status,
            needs_followup=needs_fu,
            followup_reason=reason,
            message_count=len(messages),
            last_synced_at=now,
            label_ids=list(thread_label_ids),
        )

        _upsert_thread(conn, thread)
        _upsert_messages(conn, messages)

        invoices = classifier.detect_invoice_mentions(tid, messages)
        new_invoice_rows += _replace_invoice_mentions(conn, tid, invoices)

        if debug:
            log.debug("thread %s (%s): status=%s needs_fu=%s msgs=%d invoices=%d",
                      tid, subject[:60], status, needs_fu, len(messages), len(invoices))

        if i % 25 == 0:
            conn.commit()

    conn.commit()
    after_thread_count = tracker_db.thread_count(conn)

    return {
        "threads_synced": len(thread_ids),
        "new_threads": max(0, after_thread_count - before_thread_count),
        "needs_followup_total": tracker_db.followup_count(conn),
        "invoice_mentions_this_run": new_invoice_rows,
        "invoice_mentions_total": tracker_db.invoice_mention_count(conn),
        "labels_used": list(name_to_id.keys()),
    }
