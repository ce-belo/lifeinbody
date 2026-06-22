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
        f"[bold]{result['needs_followup_total']}[/bold] need follow-up, "
        f"[bold]{result['invoice_mentions_this_run']}[/bold] invoice mentions this run "
        f"([bold]{result['invoice_mentions_total']}[/bold] total)."
    )


@sync_app.command("sheet")
def sync_sheet() -> None:
    """Pull the operations workbook into a cached snapshot."""
    _todo(6, "lifeinbody sync sheet")


@app.command()
def summary(
    json_out: bool = typer.Option(False, "--json", help="Emit machine-readable JSON."),
    email: bool = typer.Option(False, "--email", help="Also send the summary to DAILY_SUMMARY_RECIPIENT."),
) -> None:
    """Daily CLI summary — inbox state + business KPIs."""
    _todo(8, "lifeinbody summary")


@app.command()
def draft(
    thread_id: str = typer.Argument(None, help="Gmail thread ID to draft for."),
    all_pending: bool = typer.Option(False, "--all-pending", help="Draft for every new+followup thread without a draft."),
) -> None:
    """Generate a Gmail draft reply for one thread (or every pending thread)."""
    _todo(4, "lifeinbody draft")


@app.command()
def review() -> None:
    """Interactive REPL: paste a thread, get a classified draft."""
    _todo(5, "lifeinbody review")


@app.command()
def dashboard(
    no_open: bool = typer.Option(False, "--no-open", help="Render the HTML but don't open it in a browser."),
) -> None:
    """Build (and open) the HTML business dashboard."""
    _todo(9, "lifeinbody dashboard")


@app.command("run-daily")
def run_daily() -> None:
    """Sync emails, sync sheet, then summary --email. Designed for cron/launchd."""
    _todo(10, "lifeinbody run-daily")


if __name__ == "__main__":
    app()
