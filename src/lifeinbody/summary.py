"""Daily summary: inbox state + business KPIs.

`build_report` gathers everything from the tracker DB and the cached sheet
snapshot into a typed object. Three renderers turn that into a terminal panel,
a plain-text email body, and a JSON blob.
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict, dataclass
from datetime import date, datetime, timezone
from pathlib import Path

from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from . import config
from .sheet.metrics import DashboardMetrics, compute_metrics
from .sheet.models import YearMonthSummary
from .sheet.parser import parse_snapshot
from .sheet.client import load_snapshot


@dataclass(frozen=True)
class InboxState:
    last_synced_at: datetime | None
    total_threads: int
    total_messages: int
    needs_review: int          # status='new' AND needs_followup=1 AND no approved draft
    drafts_unapproved: int     # in `drafts` table with approved=0
    drafts_approved: int       # approved=1 — ready to send manually in Gmail
    invoice_mentions: int


@dataclass(frozen=True)
class SummaryReport:
    generated_at: datetime
    as_of: date
    inbox: InboxState
    metrics: DashboardMetrics | None
    year_summary: tuple[YearMonthSummary, ...] = ()  # for the multi-year dashboard chart
    snapshot_age_minutes: int | None = None          # how stale is the sheet snapshot?


# ---------- data collection ----------

def _gather_inbox(conn: sqlite3.Connection) -> InboxState:
    total_threads = conn.execute("SELECT COUNT(*) FROM threads").fetchone()[0]
    total_messages = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    needs_review = conn.execute("""
        SELECT COUNT(*) FROM threads t
        LEFT JOIN drafts d ON d.thread_id = t.thread_id AND d.approved = 1
        WHERE t.status = 'new' AND t.needs_followup = 1 AND d.draft_id IS NULL
    """).fetchone()[0]
    drafts_unapproved = conn.execute(
        "SELECT COUNT(*) FROM drafts WHERE approved = 0"
    ).fetchone()[0]
    drafts_approved = conn.execute(
        "SELECT COUNT(*) FROM drafts WHERE approved = 1"
    ).fetchone()[0]
    invoice_mentions = conn.execute(
        "SELECT COUNT(*) FROM invoices WHERE status = 'mentioned'"
    ).fetchone()[0]
    last_synced_raw = conn.execute(
        "SELECT MAX(last_synced_at) FROM threads"
    ).fetchone()[0]
    last_synced = datetime.fromisoformat(last_synced_raw) if last_synced_raw else None

    return InboxState(
        last_synced_at=last_synced,
        total_threads=total_threads,
        total_messages=total_messages,
        needs_review=needs_review,
        drafts_unapproved=drafts_unapproved,
        drafts_approved=drafts_approved,
        invoice_mentions=invoice_mentions,
    )


def _load_metrics_safe(
    as_of: date,
) -> tuple[DashboardMetrics | None, tuple[YearMonthSummary, ...], int | None]:
    """Load the sheet snapshot and compute metrics. Returns (None, (), None) if missing."""
    if not config.SHEET_SNAPSHOT_PATH.exists():
        return None, (), None
    snapshot = load_snapshot()
    parsed = parse_snapshot(snapshot)
    synced_raw = snapshot.get("synced_at")
    if synced_raw:
        synced_at = datetime.fromisoformat(synced_raw)
        age_minutes = int((datetime.now(synced_at.tzinfo) - synced_at).total_seconds() / 60)
    else:
        age_minutes = None
    metrics = compute_metrics(parsed, as_of=as_of)
    return metrics, parsed.year_summary, age_minutes


def build_report(conn: sqlite3.Connection, as_of: date | None = None) -> SummaryReport:
    as_of = as_of or date.today()
    inbox = _gather_inbox(conn)
    metrics, year_summary, snap_age = _load_metrics_safe(as_of)
    return SummaryReport(
        generated_at=datetime.now(timezone.utc),
        as_of=as_of,
        inbox=inbox,
        metrics=metrics,
        year_summary=year_summary,
        snapshot_age_minutes=snap_age,
    )


# ---------- formatting helpers ----------

def _fmt_money(v: float | None) -> str:
    if v is None:
        return "—"
    return f"${v:,.0f}" if abs(v) >= 100 else f"${v:,.2f}"


def _fmt_delta(pct: float | None) -> str:
    if pct is None:
        return ""
    return f"{pct:+.1f}%"


def _delta_color(pct: float | None) -> str:
    if pct is None:
        return "dim"
    if pct >= 0:
        return "green"
    if pct >= -10:
        return "yellow"
    return "red"


_MONTHS = ["", "Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


# ---------- terminal renderer ----------

def render_terminal(report: SummaryReport, console: Console) -> None:
    inbox = report.inbox

    inbox_table = Table.grid(padding=(0, 2))
    inbox_table.add_column(style="dim")
    inbox_table.add_column(justify="right")
    inbox_table.add_row("Threads in INBOX", f"{inbox.total_threads:,}")
    inbox_table.add_row("Messages indexed", f"{inbox.total_messages:,}")
    inbox_table.add_row(
        "Need review (no approved draft)",
        f"[bold yellow]{inbox.needs_review}[/bold yellow]" if inbox.needs_review else "0",
    )
    inbox_table.add_row(
        "Drafts awaiting approval",
        f"[bold]{inbox.drafts_unapproved}[/bold]" if inbox.drafts_unapproved else "0",
    )
    inbox_table.add_row(
        "Drafts approved, ready to send",
        f"[bold green]{inbox.drafts_approved}[/bold green]" if inbox.drafts_approved else "0",
    )
    inbox_table.add_row("Invoice mentions tracked", f"{inbox.invoice_mentions}")
    if inbox.last_synced_at:
        inbox_table.add_row("Last email sync", inbox.last_synced_at.astimezone().strftime("%Y-%m-%d %H:%M"))
    console.print(Panel(inbox_table, title="📬  Inbox", border_style="cyan"))

    if report.metrics is None:
        console.print(
            Panel(
                "[yellow]No sheet snapshot found. Run `lifeinbody sync sheet` to pull the operations workbook.[/yellow]",
                title="📈  Business KPIs",
                border_style="yellow",
            )
        )
        return

    m = report.metrics
    metrics_table = Table.grid(padding=(0, 2))
    metrics_table.add_column(style="dim")
    metrics_table.add_column(justify="right")
    metrics_table.add_column()

    r = m.mtd_revenue
    metrics_table.add_row(
        f"MTD revenue ({_MONTHS[r.month]} {r.year})",
        f"[bold]{_fmt_money(r.revenue)}[/bold]",
        f"[{_delta_color(r.yoy_delta_pct)}]{_fmt_delta(r.yoy_delta_pct)} YoY[/{_delta_color(r.yoy_delta_pct)}]",
    )
    h = m.mtd_hours
    metrics_table.add_row(
        f"MTD hours ({_MONTHS[h.month]} {h.year})",
        f"[bold]{h.hours:,.2f}[/bold]",
        f"[{_delta_color(h.yoy_delta_pct)}]{_fmt_delta(h.yoy_delta_pct)} YoY[/{_delta_color(h.yoy_delta_pct)}]",
    )
    y = m.ytd_revenue
    metrics_table.add_row(
        f"YTD revenue (thru {_MONTHS[y.closed_months_through]} {y.year})",
        f"[bold]{_fmt_money(y.revenue)}[/bold]",
        f"[{_delta_color(y.yoy_delta_pct)}]{_fmt_delta(y.yoy_delta_pct)} YoY[/{_delta_color(y.yoy_delta_pct)}]",
    )
    a = m.avg_rate
    metrics_table.add_row(
        f"Avg $/hr this month",
        f"[bold]{_fmt_money(a.current_month_avg)}[/bold]",
        f"[{_delta_color(a.delta_pct)}]{_fmt_delta(a.delta_pct)} vs {a.prior_full_year}[/{_delta_color(a.delta_pct)}]",
    )
    console.print(Panel(metrics_table, title="📈  Business KPIs", border_style="cyan"))

    # Concentration panel
    fc = m.family_concentration
    conc = Table.grid(padding=(0, 2))
    conc.add_column(style="dim")
    conc.add_column(justify="right")
    conc.add_column(justify="right")
    for s in fc.top_families:
        conc.add_row(s.family_id, _fmt_money(s.revenue), f"{s.share_pct:.1f}%")
    conc.add_row("[bold]Top 5 combined[/bold]", "", f"[bold]{fc.top_n_share_pct:.1f}%[/bold]")
    console.print(
        Panel(conc, title=f"👨‍👩‍👧 Top {fc.top_n} family revenue ({fc.year} YTD)", border_style="cyan")
    )

    # Outstanding
    o = m.outstanding
    if o.items:
        table = Table(show_header=True, header_style="bold")
        table.add_column("Family")
        table.add_column("Student")
        table.add_column("Month", justify="center")
        table.add_column("Amount", justify="right")
        table.add_column("Weeks", justify="right")
        table.add_column("Status")
        for it in o.items:
            stale = "[red]" if it.is_stale else ""
            stale_end = "[/red]" if it.is_stale else ""
            table.add_row(
                it.family_id or "—",
                it.student_id,
                f"{_MONTHS[it.month]} {it.year}",
                _fmt_money(it.amount_owed),
                f"{stale}{it.weeks_since_month_close}{stale_end}",
                f"{stale}{it.status}{stale_end}",
            )
        subtitle = (
            f"total {_fmt_money(o.total)}  ·  "
            f"[red]stale {_fmt_money(o.stale_total)}[/red] "
            f"(≥{o.stale_threshold_weeks} Mondays since month close)"
        )
        console.print(Panel(table, title="💰  Outstanding receivables", subtitle=subtitle, border_style="cyan"))
    else:
        console.print(Panel("[green]All caught up.[/green]", title="💰  Outstanding receivables", border_style="green"))

    if report.snapshot_age_minutes is not None and report.snapshot_age_minutes > 24 * 60:
        console.print(
            f"[yellow]⚠ Sheet snapshot is {report.snapshot_age_minutes // 60} hours old — "
            "consider `lifeinbody sync sheet`.[/yellow]"
        )


# ---------- plain-text renderer (email body) ----------

def render_text(report: SummaryReport) -> str:
    lines: list[str] = []
    lines.append(f"Life in Body — daily summary  ({report.as_of:%Y-%m-%d})")
    lines.append("=" * 60)
    lines.append("")
    lines.append("📬  INBOX")
    inbox = report.inbox
    lines.append(f"  Threads in INBOX:            {inbox.total_threads:,}")
    lines.append(f"  Need review (no approved):   {inbox.needs_review}")
    lines.append(f"  Drafts awaiting approval:    {inbox.drafts_unapproved}")
    lines.append(f"  Drafts approved (ready):     {inbox.drafts_approved}")
    lines.append(f"  Invoice mentions tracked:    {inbox.invoice_mentions}")
    if inbox.last_synced_at:
        lines.append(f"  Last email sync:             {inbox.last_synced_at.astimezone():%Y-%m-%d %H:%M}")
    lines.append("")

    m = report.metrics
    if m is None:
        lines.append("📈  BUSINESS KPIs — sheet snapshot missing. Run `lifeinbody sync sheet`.")
        return "\n".join(lines)

    lines.append("📈  BUSINESS KPIs")
    r = m.mtd_revenue
    lines.append(
        f"  MTD revenue ({_MONTHS[r.month]} {r.year}):   {_fmt_money(r.revenue):>12s}  "
        f"({_fmt_delta(r.yoy_delta_pct)} YoY)"
    )
    h = m.mtd_hours
    lines.append(
        f"  MTD hours ({_MONTHS[h.month]} {h.year}):     {h.hours:>9.2f}     "
        f"({_fmt_delta(h.yoy_delta_pct)} YoY)"
    )
    y = m.ytd_revenue
    lines.append(
        f"  YTD revenue (thru {_MONTHS[y.closed_months_through]}):  "
        f"{_fmt_money(y.revenue):>12s}  ({_fmt_delta(y.yoy_delta_pct)} YoY)"
    )
    a = m.avg_rate
    if a.current_month_avg is not None:
        lines.append(
            f"  Avg $/hr this month:    {_fmt_money(a.current_month_avg):>12s}  "
            f"({_fmt_delta(a.delta_pct)} vs {a.prior_full_year})"
        )
    lines.append("")

    fc = m.family_concentration
    lines.append(f"👨‍👩‍👧  TOP {fc.top_n} FAMILIES  ({fc.year} YTD)")
    for s in fc.top_families:
        lines.append(f"  {s.family_id:<16s} {_fmt_money(s.revenue):>10s}   {s.share_pct:.1f}%")
    lines.append(f"  Top {fc.top_n} combined share: {fc.top_n_share_pct:.1f}%")
    lines.append("")

    o = m.outstanding
    lines.append(f"💰  OUTSTANDING RECEIVABLES  total {_fmt_money(o.total)} / stale {_fmt_money(o.stale_total)}")
    if not o.items:
        lines.append("  All caught up.")
    else:
        for it in o.items:
            flag = "  ⚠ STALE" if it.is_stale else ""
            lines.append(
                f"  {(it.family_id or '?'):<14s} {it.student_id:<14s} "
                f"{_MONTHS[it.month]:>3s} {it.year}  "
                f"{_fmt_money(it.amount_owed):>9s}  {it.weeks_since_month_close} wk  [{it.status}]{flag}"
            )
    return "\n".join(lines)


# ---------- JSON renderer ----------

def render_json(report: SummaryReport) -> dict:
    def _to_jsonable(obj):
        if isinstance(obj, datetime):
            return obj.isoformat()
        if isinstance(obj, date):
            return obj.isoformat()
        if hasattr(obj, "__dataclass_fields__"):
            return {k: _to_jsonable(v) for k, v in asdict(obj).items()}
        if isinstance(obj, (list, tuple)):
            return [_to_jsonable(v) for v in obj]
        if isinstance(obj, dict):
            return {k: _to_jsonable(v) for k, v in obj.items()}
        return obj

    return _to_jsonable(report)


def render_json_str(report: SummaryReport) -> str:
    return json.dumps(render_json(report), indent=2, ensure_ascii=False, default=str)
