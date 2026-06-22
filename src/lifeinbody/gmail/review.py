"""Interactive review loop: walk every thread needing follow-up, draft, accept or regenerate."""
from __future__ import annotations

import sqlite3
import textwrap
from typing import Callable

from rich.console import Console
from rich.panel import Panel

from ..llm import LLMUnavailable, generate_draft
from . import drafts as gmail_drafts
from .draft_runner import (
    DraftError,
    _load_thread_messages,
    _load_thread_meta,
    _pick_recipient,
)

QUIT_TOKENS = {"q", "quit", "exit"}


def _queue(conn: sqlite3.Connection) -> list[str]:
    """Threads that still need a human pass: needs_followup=1, status='new',
    and either no draft yet or a draft that hasn't been approved."""
    rows = conn.execute(
        """SELECT t.thread_id
            FROM threads t
            LEFT JOIN drafts d ON d.thread_id = t.thread_id
            WHERE t.status = 'new'
              AND t.needs_followup = 1
              AND (d.draft_id IS NULL OR d.approved = 0)
            ORDER BY t.last_message_at DESC"""
    ).fetchall()
    return [r["thread_id"] for r in rows]


def _print_thread_context(console: Console, meta: sqlite3.Row, messages: list, recipient: str) -> None:
    latest = messages[-1]
    body = (latest.body_plain or latest.snippet or "").strip()
    if len(body) > 1200:
        body = body[:1200] + "\n[…truncated…]"
    console.print(
        Panel(
            f"[bold]{meta['subject'] or '(no subject)'}[/bold]\n"
            f"[dim]reply to:[/dim] {recipient}   "
            f"[dim]messages:[/dim] {len(messages)}   "
            f"[dim]last from:[/dim] {meta['last_message_from']}",
            border_style="cyan",
        )
    )
    console.print(Panel(body, title=f"last message ({latest.sent_at:%Y-%m-%d %H:%M})", border_style="dim"))


def _print_full_thread(console: Console, messages: list) -> None:
    for m in messages:
        who = "us" if m.is_from_us else (m.from_email or "them")
        body = (m.body_plain or m.snippet or "").strip()
        if len(body) > 2000:
            body = body[:2000] + "\n[…truncated…]"
        console.print(
            Panel(body, title=f"[{m.sent_at:%Y-%m-%d %H:%M}]  {who}", border_style="dim")
        )


def _push_or_update_draft(
    conn: sqlite3.Connection,
    service,
    thread_id: str,
    recipient: str,
    subject: str,
    body: str,
) -> str:
    """Create the Gmail draft if it doesn't exist yet, otherwise update it. Returns the draft_id."""
    row = gmail_drafts.get_draft_for_thread(conn, thread_id)
    if row:
        gmail_drafts.update_reply_draft(
            service,
            draft_id=row["draft_id"],
            thread_id=thread_id,
            to_address=recipient,
            subject=subject,
            body=body,
        )
        return row["draft_id"]
    resp = gmail_drafts.create_reply_draft(
        service,
        thread_id=thread_id,
        to_address=recipient,
        subject=subject,
        body=body,
    )
    draft_id = resp.get("id", "")
    gmail_drafts.record_draft(conn, draft_id, thread_id)
    return draft_id


def _review_one(
    conn: sqlite3.Connection,
    service,
    console: Console,
    thread_id: str,
    *,
    prompt: Callable[[str], str],
    position: tuple[int, int],
) -> str:
    """Show one thread, prompt the user, perform the chosen action.
    Returns 'next' to advance, 'quit' to break out of the queue."""
    i, total = position
    console.rule(f"[bold cyan]{i}/{total}[/bold cyan]  thread {thread_id}")

    meta = _load_thread_meta(conn, thread_id)
    messages = _load_thread_messages(conn, thread_id)
    if not messages:
        console.print("[yellow]No messages for this thread — skipping.[/yellow]")
        return "next"

    recipient = _pick_recipient(messages, fallback_to_emails=messages[-1].to_emails)
    subject = meta["subject"] or "(no subject)"
    _print_thread_context(console, meta, messages, recipient)

    body: str | None = None
    tone_hint = ""

    while True:
        if body is None:
            with console.status("Drafting with Claude…"):
                body = generate_draft(messages, tone_hint=tone_hint)
            if body.strip() == "NO_REPLY_NEEDED":
                console.print("[yellow]Claude says this thread doesn't need a reply — skipping.[/yellow]")
                return "next"
            draft_id = _push_or_update_draft(conn, service, thread_id, recipient, subject, body)
            console.print(Panel(body, title=f"draft → {recipient}  ({draft_id})", border_style="green"))

        choice = prompt("[a]ccept / [r]egenerate / [v]iew thread / [s]kip / [q]uit  > ").strip().lower()

        if choice in QUIT_TOKENS:
            return "quit"
        if choice in ("", "s", "skip"):
            console.print("[dim]skipped.[/dim]")
            return "next"
        if choice in ("a", "accept", "y", "yes"):
            row = gmail_drafts.get_draft_for_thread(conn, thread_id)
            if not row:
                console.print("[red]No draft on file — cannot mark approved.[/red]")
                return "next"
            gmail_drafts.mark_approved(conn, row["draft_id"])
            console.print(f"[green]✓ approved[/green]  draft [bold]{row['draft_id']}[/bold] ready to send from Gmail.")
            return "next"
        if choice in ("v", "view"):
            _print_full_thread(console, messages)
            continue
        if choice in ("r", "regenerate"):
            tone_hint = prompt("Tone hint (blank = just regenerate): ").strip()
            body = None  # force re-draft on next loop iteration
            continue
        console.print(f"[yellow]Unknown choice {choice!r}. Try a/r/v/s/q.[/yellow]")


def review_pending(
    conn: sqlite3.Connection,
    service,
    *,
    console: Console | None = None,
    prompt: Callable[[str], str] | None = None,
) -> None:
    console = console or Console()
    prompt = prompt or input

    queue = _queue(conn)
    if not queue:
        console.print("[green]Inbox zero — no threads need review.[/green]")
        return

    console.print(f"[cyan]{len(queue)} thread(s) to review.[/cyan]\n")
    for i, thread_id in enumerate(queue, 1):
        try:
            outcome = _review_one(
                conn, service, console, thread_id,
                prompt=prompt, position=(i, len(queue)),
            )
        except DraftError as exc:
            console.print(f"[red]✗ {exc}[/red]")
            continue
        except LLMUnavailable as exc:
            console.print(f"[red]LLM error: {exc}[/red]")
            return
        if outcome == "quit":
            console.print("[dim]quit — bye.[/dim]")
            return
    console.print("\n[green]Reviewed every pending thread.[/green]")
