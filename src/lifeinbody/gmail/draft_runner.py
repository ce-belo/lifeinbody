"""Compose a single draft end-to-end: DB → LLM → Gmail → DB → terminal.

Lives separately from cli.py so the REPL in Step 5 can reuse it.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime
from typing import Iterable

from email.utils import getaddresses
from rich.console import Console
from rich.panel import Panel

from ..tracker.models import Message
from ..llm import generate_draft
from . import drafts as gmail_drafts


class DraftError(RuntimeError):
    pass


def _load_thread_messages(conn: sqlite3.Connection, thread_id: str) -> list[Message]:
    rows = conn.execute(
        """SELECT message_id, thread_id, sent_at, from_email, to_emails,
                   snippet, body_plain
            FROM messages WHERE thread_id = ?
            ORDER BY sent_at ASC""",
        (thread_id,),
    ).fetchall()
    if not rows:
        raise DraftError(f"no messages stored for thread {thread_id}. Run `lifeinbody sync emails` first.")

    # We also need is_from_us, which we re-derive using the same domain check.
    from .. import config

    our_domains = {d.lower() for d in config.OUR_DOMAINS}
    our_addrs = {a.lower() for a in (config.OUR_ADDRESSES + [config.GMAIL_ADDRESS])}

    def is_us(email_: str) -> bool:
        if not email_:
            return False
        addr = email_.lower()
        if addr in our_addrs:
            return True
        return addr.rsplit("@", 1)[-1] in our_domains

    msgs: list[Message] = []
    for r in rows:
        msgs.append(
            Message(
                message_id=r["message_id"],
                thread_id=r["thread_id"],
                sent_at=datetime.fromisoformat(r["sent_at"]),
                from_email=r["from_email"] or "",
                from_name="",  # not needed for draft generation
                to_emails=r["to_emails"] or "",
                snippet=r["snippet"] or "",
                body_plain=r["body_plain"] or "",
                is_from_us=is_us(r["from_email"] or ""),
            )
        )
    return msgs


def _load_thread_meta(conn: sqlite3.Connection, thread_id: str) -> sqlite3.Row:
    row = conn.execute(
        "SELECT thread_id, subject, sender_email, sender_name, status, last_message_from FROM threads WHERE thread_id = ?",
        (thread_id,),
    ).fetchone()
    if not row:
        raise DraftError(f"thread {thread_id} not in DB. Run `lifeinbody sync emails`.")
    return row


def _pick_recipient(messages: list[Message], fallback_to_emails: str) -> str:
    """Recipient = sender of the latest non-us message, else first To: from our last outbound."""
    for m in reversed(messages):
        if not m.is_from_us and m.from_email:
            return m.from_email
    # Fall back to the original To: of the most recent message (whoever we last wrote to).
    if fallback_to_emails:
        addrs = [addr for _, addr in getaddresses([fallback_to_emails]) if addr]
        if addrs:
            return addrs[0]
    raise DraftError("could not determine recipient for this thread.")


def list_pending_thread_ids(conn: sqlite3.Connection) -> list[str]:
    """Threads where status='new' AND needs_followup=1 AND no draft yet."""
    rows = conn.execute(
        """SELECT t.thread_id FROM threads t
            WHERE t.status = 'new'
              AND t.needs_followup = 1
              AND NOT EXISTS (SELECT 1 FROM drafts d WHERE d.thread_id = t.thread_id)
            ORDER BY t.last_message_at DESC"""
    ).fetchall()
    return [r["thread_id"] for r in rows]


def draft_for_thread(
    conn: sqlite3.Connection,
    service,
    thread_id: str,
    *,
    console: Console | None = None,
    verbose: bool = True,
    tone_hint: str = "",
) -> dict:
    """End-to-end: pull messages, call LLM, create Gmail draft, record in DB."""
    console = console or Console()
    meta = _load_thread_meta(conn, thread_id)
    messages = _load_thread_messages(conn, thread_id)
    if not messages:
        raise DraftError(f"thread {thread_id} has no messages.")

    latest = messages[-1]
    recipient = _pick_recipient(messages, fallback_to_emails=latest.to_emails)
    subject = meta["subject"] or "(no subject)"

    if verbose:
        console.print(f"[cyan]Thread:[/cyan] {subject}")
        console.print(f"[cyan]To:[/cyan]     {recipient}")
        console.print(f"[cyan]Status:[/cyan] {meta['status']} (last msg from {meta['last_message_from']}, {len(messages)} messages)")
        console.print("[dim]Asking Claude to draft…[/dim]")

    body = generate_draft(messages, tone_hint=tone_hint)

    if body.strip() == "NO_REPLY_NEEDED":
        console.print("[yellow]✗ LLM said this thread doesn't need a reply.[/yellow]")
        return {"skipped": True, "reason": "NO_REPLY_NEEDED"}

    resp = gmail_drafts.create_reply_draft(
        service,
        thread_id=thread_id,
        to_address=recipient,
        subject=subject,
        body=body,
    )
    draft_id = resp.get("id", "")
    msg_id = (resp.get("message") or {}).get("id", "")
    gmail_drafts.record_draft(conn, draft_id, thread_id)

    thread_url = f"https://mail.google.com/mail/u/0/#inbox/{thread_id}"
    draft_url = f"https://mail.google.com/mail/u/0/#drafts/{msg_id}" if msg_id else None

    if verbose:
        console.print(Panel(body, title=f"draft reply → {recipient}", border_style="green"))
        console.print(f"[green]✓ Saved as Gmail draft[/green]  id=[bold]{draft_id}[/bold]")
        console.print(f"  Open thread: [link={thread_url}]{thread_url}[/link]")
        if draft_url:
            console.print(f"  Open draft:  [link={draft_url}]{draft_url}[/link]")
    else:
        console.print(f"  [green]✓ draft {draft_id}[/green]  → {recipient}  ({len(body)} chars)")

    return {
        "draft_id": draft_id,
        "message_id": msg_id,
        "thread_id": thread_id,
        "recipient": recipient,
        "body": body,
        "thread_url": thread_url,
        "draft_url": draft_url,
    }
