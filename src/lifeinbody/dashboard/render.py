"""Render a SummaryReport into a self-contained dashboard.html file."""
from __future__ import annotations

import base64
from datetime import datetime
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

from .. import config
from ..summary import SummaryReport

_TEMPLATE_DIR = Path(__file__).parent / "templates"
_LOGO_PATH = config.REPO_ROOT / "Assets" / "LIB_Logo_Green.png"


def _logo_data_uri() -> str:
    if not _LOGO_PATH.exists():
        return ""
    b64 = base64.b64encode(_LOGO_PATH.read_bytes()).decode("ascii")
    return f"data:image/png;base64,{b64}"
_MONTHS = ["", "Jan", "Feb", "Mar", "Apr", "May", "Jun",
           "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def _fmt_money(v: float | None, *, cents: bool = False) -> str:
    if v is None:
        return "—"
    if cents or abs(v) < 100:
        return f"${v:,.2f}"
    return f"${v:,.0f}"


def _fmt_delta(pct: float | None, suffix: str = "") -> tuple[str, str]:
    if pct is None:
        return "", "dim"
    cls = "pos" if pct >= 0 else ("warn" if pct >= -10 else "neg")
    return f"{pct:+.1f}%{suffix}", cls


def _pill_cls(status: str) -> str:
    s = status.lower()
    if "stale" in s:
        return "stale"
    if "follow" in s:
        return "stale"
    if "sent" in s:
        return "sent"
    if "arrears" in s:
        return "arrears"
    return ""


# ---------- SVG line chart ----------

def _render_yoy_chart(year_summary, current_year: int) -> tuple[str, list[dict]]:
    """Return (svg_markup, legend_data) for monthly revenue across recent years.

    Shows 3 most recent years that have any data: typically year-2, year-1, current.
    """
    if not year_summary:
        return "", []

    # Collate revenues by year/month
    by_year: dict[int, dict[int, float]] = {}
    for s in year_summary:
        if s.revenue is None:
            continue
        by_year.setdefault(s.year, {})[s.month] = s.revenue

    candidate_years = sorted(by_year.keys())
    # Pick up to 3 most recent years that have data
    years_to_show = candidate_years[-3:]
    if not years_to_show:
        return "", []

    width, height = 700, 280
    pad_l, pad_r, pad_t, pad_b = 56, 16, 16, 36
    plot_w = width - pad_l - pad_r
    plot_h = height - pad_t - pad_b

    max_val = max(
        (v for yr in years_to_show for v in by_year.get(yr, {}).values()),
        default=0,
    )
    if max_val <= 0:
        return "", []
    # Round max up to a nice y-axis tick
    nice_max = (int(max_val / 5000) + 1) * 5000

    def x_for(month: int) -> float:
        return pad_l + (month - 1) / 11 * plot_w

    def y_for(val: float) -> float:
        return pad_t + plot_h - (val / nice_max) * plot_h

    palette = ["#cbd5e1", "#94a3b8", "#3b82f6"]  # older years muted, current bold
    colors = {yr: palette[i + (3 - len(years_to_show))] for i, yr in enumerate(years_to_show)}

    # Grid lines + y labels
    grid_lines: list[str] = []
    n_ticks = 5
    for i in range(n_ticks + 1):
        val = nice_max * (n_ticks - i) / n_ticks
        y = pad_t + (plot_h * i / n_ticks)
        grid_lines.append(
            f'<line x1="{pad_l}" y1="{y:.1f}" x2="{pad_l + plot_w}" y2="{y:.1f}" '
            f'stroke="#f3f4f6" stroke-width="1"/>'
        )
        grid_lines.append(
            f'<text x="{pad_l - 8}" y="{y + 4:.1f}" text-anchor="end" font-size="10" fill="#9ca3af">'
            f'${val/1000:.0f}k</text>'
        )

    # X-axis month labels
    month_labels: list[str] = []
    for m in range(1, 13):
        month_labels.append(
            f'<text x="{x_for(m):.1f}" y="{height - pad_b + 18:.0f}" text-anchor="middle" '
            f'font-size="10" fill="#9ca3af">{_MONTHS[m]}</text>'
        )

    # Per-year polylines
    lines_svg: list[str] = []
    legend: list[dict] = []
    for yr in years_to_show:
        pts = []
        for m in range(1, 13):
            v = by_year.get(yr, {}).get(m)
            if v is None:
                continue
            pts.append((x_for(m), y_for(v)))
        if len(pts) < 2:
            # Single point: render as a dot
            for x, y in pts:
                lines_svg.append(
                    f'<circle cx="{x:.1f}" cy="{y:.1f}" r="3" fill="{colors[yr]}"/>'
                )
        else:
            polyline = " ".join(f"{x:.1f},{y:.1f}" for x, y in pts)
            width_px = 2.5 if yr == current_year else 1.5
            lines_svg.append(
                f'<polyline points="{polyline}" fill="none" stroke="{colors[yr]}" '
                f'stroke-width="{width_px}" stroke-linejoin="round" stroke-linecap="round"/>'
            )
            for x, y in pts:
                lines_svg.append(
                    f'<circle cx="{x:.1f}" cy="{y:.1f}" r="2.5" fill="{colors[yr]}"/>'
                )
        legend.append({"label": str(yr), "color": colors[yr]})

    svg = (
        f'<svg viewBox="0 0 {width} {height}" xmlns="http://www.w3.org/2000/svg" '
        f'role="img" aria-label="Monthly revenue by year">'
        + "".join(grid_lines)
        + "".join(month_labels)
        + "".join(lines_svg)
        + "</svg>"
    )
    return svg, legend


# ---------- top-level renderer ----------

def _build_context(report: SummaryReport) -> dict:
    m = report.metrics
    workbook_year = m.workbook_year if m else report.as_of.year

    kpis: list[dict] = []
    if m:
        r = m.mtd_revenue
        d, cls = _fmt_delta(r.yoy_delta_pct, " YoY")
        kpis.append({"label": f"MTD revenue ({_MONTHS[r.month]} {r.year})",
                     "value": _fmt_money(r.revenue), "delta": d, "delta_cls": cls})
        h = m.mtd_hours
        d, cls = _fmt_delta(h.yoy_delta_pct, " YoY")
        kpis.append({"label": f"MTD hours ({_MONTHS[h.month]} {h.year})",
                     "value": f"{h.hours:,.2f}", "delta": d, "delta_cls": cls})
        y = m.ytd_revenue
        d, cls = _fmt_delta(y.yoy_delta_pct, " YoY")
        kpis.append({"label": f"YTD revenue (thru {_MONTHS[y.closed_months_through]})",
                     "value": _fmt_money(y.revenue), "delta": d, "delta_cls": cls})
        a = m.avg_rate
        d, cls = _fmt_delta(a.delta_pct, f" vs {a.prior_full_year}")
        kpis.append({"label": "Avg $/hr this month",
                     "value": _fmt_money(a.current_month_avg, cents=True), "delta": d, "delta_cls": cls})

    top_families = []
    top_n = 0
    top_combined = ""
    if m and m.family_concentration.top_families:
        fc = m.family_concentration
        top_n = fc.top_n
        # Bars relative to the leader, so the visual ranks are clear
        max_share = max(s.share_pct for s in fc.top_families)
        for s in fc.top_families:
            top_families.append({
                "name": s.family_id,
                "amount": _fmt_money(s.revenue),
                "share": f"{s.share_pct:.1f}%",
                "width": f"{s.share_pct / max_share * 100:.1f}",
            })
        top_combined = f"{fc.top_n_share_pct:.1f}%"

    outstanding_items = []
    outstanding_total = "—"
    outstanding_stale = "—"
    stale_weeks = 2
    if m:
        o = m.outstanding
        outstanding_total = _fmt_money(o.total)
        outstanding_stale = _fmt_money(o.stale_total)
        stale_weeks = o.stale_threshold_weeks
        for it in o.items:
            refs = report.threads_by_family.get(it.family_id or "", ())
            threads_payload = [
                {
                    "subject": ref.subject,
                    "sender_name": ref.sender_name or ref.sender_email or "(unknown)",
                    "status": ref.status,
                    "url": f"https://mail.google.com/mail/u/0/#inbox/{ref.thread_id}",
                    "last": ref.last_message_at.astimezone().strftime("%Y-%m-%d"),
                }
                for ref in refs
            ]
            outstanding_items.append({
                "family": it.family_id,
                "student": it.student_id,
                "month": f"{_MONTHS[it.month]} {it.year}",
                "amount": _fmt_money(it.amount_owed),
                "weeks": it.weeks_since_month_close,
                "status": it.status,
                "pill_cls": "stale" if it.is_stale else _pill_cls(it.status),
                "is_stale": it.is_stale,
                "thread_count": len(threads_payload),
                "threads": threads_payload,
            })

    chart_svg, chart_legend = _render_yoy_chart(report.year_summary, workbook_year) if m else ("", [])

    waiting_detail = [
        {
            "name": w.sender_name or w.sender_email or "(unknown)",
            "email": w.sender_email,
            "subject": w.subject,
            "last_contact": w.last_message_at.astimezone().strftime("%Y-%m-%d"),
            "days": w.business_days_waiting,
        }
        for w in report.inbox.waiting_threads
    ]
    inbox_ctx = {
        "total_threads": f"{report.inbox.total_threads:,}",
        "closed_threads": f"{report.inbox.closed_threads:,}",
        "needs_review": report.inbox.needs_review,
        "waiting_on_them": report.inbox.waiting_on_them,
        "waiting_detail": waiting_detail,
        "drafts_unapproved": report.inbox.drafts_unapproved,
        "drafts_approved": report.inbox.drafts_approved,
        "last_synced": report.inbox.last_synced_at.astimezone().strftime("%Y-%m-%d %H:%M")
        if report.inbox.last_synced_at else None,
    }

    snapshot_warning = None
    if m is None:
        snapshot_warning = "No sheet snapshot found. Run `lifeinbody sync sheet`."
    elif report.snapshot_age_minutes and report.snapshot_age_minutes > 24 * 60:
        snapshot_warning = (
            f"Sheet snapshot is {report.snapshot_age_minutes // 60} hours old — "
            "consider `lifeinbody sync sheet`."
        )

    return {
        "as_of": report.as_of.strftime("%A, %B %d, %Y"),
        "generated_at": report.generated_at.astimezone().strftime("%Y-%m-%d %H:%M %Z"),
        "workbook_year": workbook_year,
        "logo_data_uri": _logo_data_uri(),
        "kpis": kpis,
        "top_families": top_families,
        "top_n": top_n,
        "top_combined": top_combined,
        "outstanding_items": outstanding_items,
        "outstanding_total": outstanding_total,
        "outstanding_stale": outstanding_stale,
        "outstanding_count": len(outstanding_items),
        "stale_weeks": stale_weeks,
        "chart_svg": chart_svg,
        "chart_legend": chart_legend,
        "inbox": inbox_ctx,
        "snapshot_warning": snapshot_warning,
        "llm_cost": report.llm_cost,
    }


def render_dashboard(report: SummaryReport, output_path: Path | None = None) -> Path:
    """Render the dashboard HTML and write it. Returns the path."""
    config.ensure_dirs()
    output_path = output_path or config.DASHBOARD_HTML_PATH

    env = Environment(
        loader=FileSystemLoader(str(_TEMPLATE_DIR)),
        autoescape=select_autoescape(["html", "xml"]),
        trim_blocks=True,
        lstrip_blocks=True,
    )
    template = env.get_template("dashboard.html.j2")
    html = template.render(**_build_context(report))
    output_path.write_text(html, encoding="utf-8")
    return output_path
