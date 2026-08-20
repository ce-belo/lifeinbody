"""Life in Body CLI entry point."""
from __future__ import annotations

import logging

import typer
from rich.console import Console

app = typer.Typer(
    name="lifeinbody",
    help="Life in Body — Gmail secretary + business dashboard.",
    no_args_is_help=True,
    add_completion=False,
)

sync_app = typer.Typer(help="Pull data from Gmail or the Google Sheet into local storage.")
app.add_typer(sync_app, name="sync")

console = Console()


def _setup_logging(debug: bool) -> None:
    from lifeinbody import config

    config.ensure_dirs()
    root = logging.getLogger("lifeinbody")
    root.handlers.clear()
    root.setLevel(logging.DEBUG if debug else logging.INFO)
    handler = logging.FileHandler(config.LOG_PATH)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s :: %(message)s"))
    root.addHandler(handler)
    # Quiet the noisy google libraries unless we're debugging.
    logging.getLogger("googleapiclient.discovery_cache").setLevel(logging.ERROR)


def _todo(step: int, command: str) -> None:
    console.print(f"[yellow]TODO (Step {step}):[/yellow] `{command}` not implemented yet.")


@app.command()
def auth(
    force: bool = typer.Option(False, "--force", help="Delete the saved token and re-run the browser flow."),
) -> None:
    """One-time Gmail OAuth — authorize team@lifeinbody.com."""
    from googleapiclient.discovery import build

    from lifeinbody import config
    from lifeinbody.gmail.auth import OAuthClientMissing, authenticate

    config.ensure_dirs()

    if not config.OAUTH_CLIENT_PATH.exists():
        console.print(f"[red]Missing OAuth client JSON at {config.OAUTH_CLIENT_PATH}.[/red]")
        console.print("Follow the Step 2 walkthrough (README → 'Setting up Gmail OAuth') to create one, then re-run this command.")
        raise typer.Exit(1)

    console.print("[cyan]Opening browser for Google sign-in…[/cyan]")
    if config.GMAIL_ADDRESS:
        console.print(f"[dim]Expected account: {config.GMAIL_ADDRESS}[/dim]")

    try:
        creds = authenticate(force=force)
    except OAuthClientMissing as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1)

    service = build("gmail", "v1", credentials=creds, cache_discovery=False)
    profile = service.users().getProfile(userId="me").execute()
    address = profile.get("emailAddress", "(unknown)")
    total = profile.get("messagesTotal", 0)
    threads_total = profile.get("threadsTotal", 0)

    inbox = service.users().labels().get(userId="me", id="INBOX").execute()
    inbox_unread = inbox.get("messagesUnread", 0)
    inbox_threads = inbox.get("threadsTotal", 0)

    console.print(f"[green]✓ Authenticated as[/green] [bold]{address}[/bold]")
    console.print(f"  Token saved to [dim]{config.OAUTH_TOKEN_PATH}[/dim]")
    console.print(f"  Total messages in mailbox: [bold]{total:,}[/bold]")
    console.print(f"  Total threads in mailbox:  [bold]{threads_total:,}[/bold]")
    console.print(f"  INBOX threads / unread:    [bold]{inbox_threads:,}[/bold] / [bold]{inbox_unread:,}[/bold]")

    if config.GMAIL_ADDRESS and address.lower() != config.GMAIL_ADDRESS.lower():
        console.print(
            f"\n[yellow]⚠ Authenticated account ({address}) doesn't match "
            f"LIFEINBODY_GMAIL_ADDRESS ({config.GMAIL_ADDRESS}).[/yellow]\n"
            "[yellow]  If intentional, update .env. Otherwise run `lifeinbody auth --force` "
            "and pick the right account.[/yellow]"
        )


@sync_app.command("emails")
def sync_emails(
    labels: str = typer.Option("INBOX", "--labels", help="Comma-separated Gmail labels to sync."),
    llm_classify: bool = typer.Option(False, "--llm-classify", help="Run LLM second-pass classifier."),
    debug: bool = typer.Option(False, "--debug", help="Log message bodies + extra detail to data/lifeinbody.log."),
) -> None:
    """Pull new Gmail threads into the local SQLite tracker."""
    from lifeinbody.gmail.auth import get_service
    from lifeinbody.gmail.sync import sync_emails as run_sync
    from lifeinbody.tracker.db import connect

    _setup_logging(debug)
    label_list = [s.strip() for s in labels.split(",") if s.strip()]

    console.print(f"[cyan]Syncing labels:[/cyan] {', '.join(label_list)}")
    service = get_service()
    conn = connect()
    try:
        with console.status("Pulling threads from Gmail…"):
            result = run_sync(
                service,
                conn,
                labels=label_list,
                llm_classify=llm_classify,
                debug=debug,
            )
    finally:
        conn.close()

    console.print(
        f"[green]✓ {result['threads_synced']} threads synced[/green] "
        f"({result['new_threads']} new), "
        f"[bold]{result['needs_followup_total']}[/bold] need follow-up."
    )


@sync_app.command("sheet")
def sync_sheet(
    debug: bool = typer.Option(False, "--debug", help="Verbose logs to data/lifeinbody.log."),
) -> None:
    """Pull the operations workbook into a cached snapshot."""
    from lifeinbody import config
    from lifeinbody.gmail.auth import OAuthClientMissing
    from lifeinbody.sheet.client import SheetIdMissing, fetch_snapshot, write_snapshot

    _setup_logging(debug)

    try:
        with console.status(f"Pulling workbook {config.SHEET_ID[:12]}…"):
            snapshot = fetch_snapshot()
    except OAuthClientMissing as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1)
    except SheetIdMissing as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1)

    path = write_snapshot(snapshot)

    total_rows = sum(t["row_count"] for t in snapshot["tabs"])
    console.print(
        f"[green]✓ {snapshot['tab_count']} tabs[/green] "
        f"([bold]{total_rows}[/bold] rows) from "
        f"[bold]{snapshot['workbook_title']}[/bold] → "
        f"[dim]{path}[/dim]"
    )
    for tab in snapshot["tabs"]:
        console.print(f"  {tab['name']:<30s} {tab['row_count']:>6,} rows × {tab['col_count']} cols")


@app.command()
def summary(
    json_out: bool = typer.Option(False, "--json", help="Emit machine-readable JSON."),
    email: bool = typer.Option(False, "--email", help="Also drop a Gmail draft for DAILY_SUMMARY_RECIPIENT."),
    debug: bool = typer.Option(False, "--debug", help="Verbose logs to data/lifeinbody.log."),
) -> None:
    """Daily summary — inbox state + business KPIs."""
    from lifeinbody import config
    from lifeinbody.summary import build_report, render_terminal, render_text, render_json_str
    from lifeinbody.tracker.db import connect

    _setup_logging(debug)
    conn = connect()
    try:
        report = build_report(conn)
    finally:
        conn.close()

    if json_out:
        # Plain stdout — no Rich coloring, easy to pipe.
        print(render_json_str(report))
        return

    render_terminal(report, console)

    if email:
        recipient = config.DAILY_SUMMARY_RECIPIENT
        if not recipient:
            console.print(
                "[red]--email requested but DAILY_SUMMARY_RECIPIENT is not set in .env.[/red]"
            )
            raise typer.Exit(1)

        from lifeinbody.gmail.auth import get_service
        from lifeinbody.gmail.drafts import create_standalone_draft

        body = render_text(report)
        subject = f"Life in Body daily summary — {report.as_of:%Y-%m-%d}"
        service = get_service()
        resp = create_standalone_draft(service, to_address=recipient, subject=subject, body=body)
        draft_id = resp.get("id", "")
        msg_id = (resp.get("message") or {}).get("id", "")
        console.print(
            f"\n[green]✓ Draft email saved[/green]  id=[bold]{draft_id}[/bold] → {recipient}\n"
            f"  Open: https://mail.google.com/mail/u/0/#drafts/{msg_id}"
        )


@app.command()
def draft(
    thread_id: str = typer.Argument(None, help="Gmail thread ID to draft for."),
    all_pending: bool = typer.Option(False, "--all-pending", help="Draft for every new thread without a draft."),
    nudge_waiting: bool = typer.Option(False, "--nudge-waiting", help="Draft a polite check-in for every status='waiting' thread without a draft."),
    nudge: bool = typer.Option(False, "--nudge", help="Treat the given thread_id as a nudge (we wrote last)."),
    debug: bool = typer.Option(False, "--debug", help="Verbose logs to data/lifeinbody.log."),
) -> None:
    """Generate a Gmail draft for one thread, every pending thread, or every waiting thread."""
    from lifeinbody import config
    from lifeinbody.gmail.auth import get_service
    from lifeinbody.gmail.draft_runner import (
        draft_for_thread, list_nudge_thread_ids, list_pending_thread_ids,
    )
    from lifeinbody.tracker.db import connect

    _setup_logging(debug)

    if not config.ANTHROPIC_API_KEY:
        console.print("[red]ANTHROPIC_API_KEY is not set in .env — required for draft generation.[/red]")
        raise typer.Exit(1)
    mode_flags = sum(1 for f in (all_pending, nudge_waiting) if f)
    if mode_flags > 1:
        console.print("[red]Pass at most one of --all-pending / --nudge-waiting.[/red]")
        raise typer.Exit(1)
    if mode_flags and thread_id:
        console.print("[red]Pass a thread_id OR --all-pending / --nudge-waiting, not both.[/red]")
        raise typer.Exit(1)
    if not mode_flags and not thread_id:
        console.print("[red]Pass a thread_id, --all-pending, or --nudge-waiting.[/red]")
        raise typer.Exit(1)
    if nudge and not thread_id:
        console.print("[red]--nudge only applies when drafting for a single thread_id; use --nudge-waiting for the bulk flow.[/red]")
        raise typer.Exit(1)

    service = get_service()
    conn = connect()
    try:
        if all_pending:
            ids = list_pending_thread_ids(conn)
            console.print(f"Drafting replies for [bold]{len(ids)}[/bold] pending threads…")
            for i, tid in enumerate(ids, 1):
                console.print(f"\n[dim]({i}/{len(ids)})[/dim] thread {tid}")
                try:
                    draft_for_thread(conn, service, tid, console=console, verbose=False, mode="reply")
                except Exception as exc:
                    console.print(f"  [red]✗ failed: {exc}[/red]")
        elif nudge_waiting:
            ids = list_nudge_thread_ids(conn)
            console.print(f"Drafting nudges for [bold]{len(ids)}[/bold] waiting threads…")
            for i, tid in enumerate(ids, 1):
                console.print(f"\n[dim]({i}/{len(ids)})[/dim] thread {tid}")
                try:
                    draft_for_thread(conn, service, tid, console=console, verbose=False, mode="nudge")
                except Exception as exc:
                    console.print(f"  [red]✗ failed: {exc}[/red]")
        else:
            draft_for_thread(
                conn, service, thread_id,
                console=console, verbose=True,
                mode="nudge" if nudge else "reply",
            )
    finally:
        conn.close()


@app.command()
def review(
    debug: bool = typer.Option(False, "--debug", help="Verbose logs to data/lifeinbody.log."),
) -> None:
    """Walk every thread needing follow-up; accept, regenerate, or skip each draft."""
    from lifeinbody import config
    from lifeinbody.gmail.auth import get_service
    from lifeinbody.gmail.review import review_pending
    from lifeinbody.tracker.db import connect

    _setup_logging(debug)

    if not config.ANTHROPIC_API_KEY:
        console.print("[red]ANTHROPIC_API_KEY is not set in .env — required for draft generation.[/red]")
        raise typer.Exit(1)

    service = get_service()
    conn = connect()
    try:
        review_pending(conn, service, console=console)
    finally:
        conn.close()


@app.command()
def dashboard(
    no_sync: bool = typer.Option(False, "--no-sync", help="Skip the sheet + email sync; just re-render from cached data."),
    no_open: bool = typer.Option(False, "--no-open", help="Render the HTML but don't open it in a browser."),
    debug: bool = typer.Option(False, "--debug", help="Verbose logs to data/lifeinbody.log."),
) -> None:
    """Sync sheet + emails, then build (and open) the HTML business dashboard.

    Sync errors are reported but don't abort the render — a partial dashboard
    beats no dashboard. LLM classification is intentionally skipped here; run
    `lifeinbody sync emails --llm-classify` or `lifeinbody draft` on demand.
    """
    import webbrowser

    from lifeinbody.dashboard.render import render_dashboard
    from lifeinbody.summary import build_report
    from lifeinbody.tracker.db import connect

    _setup_logging(debug)
    log = logging.getLogger("lifeinbody.dashboard")

    if not no_sync:
        from lifeinbody.gmail.auth import get_service
        from lifeinbody.gmail.sync import sync_emails as run_sync_emails
        from lifeinbody.sheet.client import fetch_snapshot, write_snapshot

        try:
            with console.status("Pulling operations workbook…"):
                snapshot = fetch_snapshot()
                write_snapshot(snapshot)
            total_rows = sum(t["row_count"] for t in snapshot["tabs"])
            console.print(
                f"[green]✓ Sheet synced[/green]  {snapshot['tab_count']} tabs / {total_rows:,} rows."
            )
        except Exception as exc:
            log.exception("sheet sync failed")
            console.print(f"[yellow]⚠ sheet sync failed:[/yellow] {exc} [dim](using cached snapshot)[/dim]")

        try:
            with console.status("Syncing Gmail INBOX…"):
                service = get_service()
                conn = connect()
                try:
                    result = run_sync_emails(service, conn, labels=["INBOX"], debug=debug)
                finally:
                    conn.close()
            console.print(
                f"[green]✓ Emails synced[/green]  {result['threads_synced']} threads "
                f"({result['new_threads']} new), {result['needs_followup_total']} need follow-up."
            )
        except Exception as exc:
            log.exception("email sync failed")
            console.print(f"[yellow]⚠ email sync failed:[/yellow] {exc} [dim](using cached DB)[/dim]")

    conn = connect()
    try:
        report = build_report(conn)
    finally:
        conn.close()

    path = render_dashboard(report)
    console.print(f"[green]✓ Dashboard written[/green] → [dim]{path}[/dim]")

    if not no_open:
        webbrowser.open(f"file://{path}")
        console.print("  Opened in default browser.")
    else:
        console.print(f"  Open it manually: file://{path}")


@app.command("run-daily")
def run_daily(
    debug: bool = typer.Option(False, "--debug", help="Verbose logs to data/lifeinbody.log."),
) -> None:
    """Sync emails → sync sheet → refresh dashboard → email summary draft.

    Designed for cron / launchd. Each step is independent: a failure on any
    one is logged and reported in the email body but does not abort the rest.
    """
    from lifeinbody import config
    from lifeinbody.dashboard.render import render_dashboard
    from lifeinbody.gmail.auth import get_service
    from lifeinbody.gmail.drafts import create_standalone_draft
    from lifeinbody.gmail.sync import sync_emails as run_sync_emails
    from lifeinbody.sheet.client import fetch_snapshot, write_snapshot
    from lifeinbody.summary import build_report, render_text
    from lifeinbody.tracker.db import connect

    _setup_logging(debug)
    log = logging.getLogger("lifeinbody.run_daily")
    log.info("=== run-daily start ===")

    errors: list[str] = []
    started = __import__("datetime").datetime.now()

    # 1. Sync Gmail INBOX
    try:
        with console.status("Syncing Gmail INBOX…"):
            service = get_service()
            conn = connect()
            try:
                result = run_sync_emails(service, conn, labels=["INBOX"], debug=debug)
            finally:
                conn.close()
        console.print(
            f"[green]✓ Emails synced[/green]  {result['threads_synced']} threads "
            f"({result['new_threads']} new), {result['needs_followup_total']} need follow-up."
        )
    except Exception as exc:
        log.exception("email sync failed")
        errors.append(f"email sync: {exc}")
        console.print(f"[red]✗ email sync failed:[/red] {exc}")

    # 2. Sync Sheets snapshot
    try:
        with console.status("Pulling operations workbook…"):
            snapshot = fetch_snapshot()
            path = write_snapshot(snapshot)
        total_rows = sum(t["row_count"] for t in snapshot["tabs"])
        console.print(
            f"[green]✓ Sheet synced[/green]  {snapshot['tab_count']} tabs / {total_rows:,} rows "
            f"from [bold]{snapshot['workbook_title']}[/bold]."
        )
    except Exception as exc:
        log.exception("sheet sync failed")
        errors.append(f"sheet sync: {exc}")
        console.print(f"[red]✗ sheet sync failed:[/red] {exc}")

    # 3. Build report (always — even partial data is useful)
    conn = connect()
    try:
        report = build_report(conn)
    finally:
        conn.close()

    # 4. Refresh the HTML dashboard
    try:
        dash_path = render_dashboard(report)
        console.print(f"[green]✓ Dashboard refreshed[/green]  [dim]{dash_path}[/dim]")
    except Exception as exc:
        log.exception("dashboard render failed")
        errors.append(f"dashboard: {exc}")
        console.print(f"[red]✗ dashboard failed:[/red] {exc}")

    # 5. Email summary draft
    recipient = config.DAILY_SUMMARY_RECIPIENT
    if not recipient:
        console.print(
            "[yellow]⚠ DAILY_SUMMARY_RECIPIENT not set in .env — skipping email step.[/yellow]"
        )
    else:
        try:
            service = get_service()
            body = render_text(report)
            if errors:
                body = (
                    "⚠ run-daily encountered errors — data may be stale:\n"
                    + "\n".join(f"  - {e}" for e in errors)
                    + "\n\n" + body
                )
            subject = f"Life in Body daily summary — {report.as_of:%Y-%m-%d}"
            resp = create_standalone_draft(
                service, to_address=recipient, subject=subject, body=body,
            )
            msg_id = (resp.get("message") or {}).get("id", "")
            console.print(
                f"[green]✓ Summary draft created[/green]  → {recipient}\n"
                f"  Open: https://mail.google.com/mail/u/0/#drafts/{msg_id}"
            )
        except Exception as exc:
            log.exception("email summary failed")
            errors.append(f"email summary: {exc}")
            console.print(f"[red]✗ email summary failed:[/red] {exc}")

    elapsed = (__import__("datetime").datetime.now() - started).total_seconds()
    if errors:
        log.warning("run-daily completed with %d error(s) in %.1fs", len(errors), elapsed)
        console.print(
            f"\n[yellow]Completed with {len(errors)} error(s) in {elapsed:.1f}s.[/yellow]"
        )
        raise typer.Exit(1)
    log.info("run-daily completed cleanly in %.1fs", elapsed)
    console.print(f"\n[green]✓ All daily tasks complete.[/green] [dim]({elapsed:.1f}s)[/dim]")


if __name__ == "__main__":
    app()
