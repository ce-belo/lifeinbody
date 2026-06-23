"""Six dashboard headline metrics computed from a ParsedSheet.

Pure library — no I/O, no CLI rendering. Step 8's summary command and step 9's
HTML dashboard will both consume DashboardMetrics.
"""
from __future__ import annotations

from calendar import monthrange
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Iterable

from .models import Lesson, MonthlyInvoice, ParsedSheet


# ---------- metric containers ----------

@dataclass(frozen=True)
class MtdRevenue:
    year: int
    month: int
    revenue: float
    prior_year_revenue: float | None
    yoy_delta_pct: float | None


@dataclass(frozen=True)
class MtdHours:
    year: int
    month: int
    hours: float
    prior_year_hours: float | None
    yoy_delta_pct: float | None


@dataclass(frozen=True)
class YtdRevenue:
    year: int
    closed_months_through: int            # last fully-closed month (0 if Jan and not closed)
    revenue: float
    prior_year_revenue: float | None      # same Jan..closed_months_through last year
    yoy_delta_pct: float | None


@dataclass(frozen=True)
class FamilyShare:
    family_id: str
    revenue: float
    share_pct: float


@dataclass(frozen=True)
class FamilyConcentration:
    year: int
    top_n: int
    total_ytd_revenue: float
    top_families: tuple[FamilyShare, ...]
    top_n_share_pct: float


@dataclass(frozen=True)
class OutstandingItem:
    family_id: str | None                  # filled from Directory join when possible
    student_id: str
    year: int
    month: int
    amount_owed: float
    weeks_since_month_close: int           # 0 = month hasn't closed yet
    is_stale: bool
    status: str                            # 'Follow-Up' or '' (arrears-only)


@dataclass(frozen=True)
class OutstandingReceivables:
    total: float
    stale_total: float
    stale_threshold_weeks: int
    items: tuple[OutstandingItem, ...]


@dataclass(frozen=True)
class AvgRate:
    current_month_avg: float | None
    prior_full_year: int | None
    prior_full_year_avg: float | None
    delta_pct: float | None


@dataclass(frozen=True)
class DashboardMetrics:
    as_of: date
    workbook_year: int
    mtd_revenue: MtdRevenue
    mtd_hours: MtdHours
    ytd_revenue: YtdRevenue
    family_concentration: FamilyConcentration
    outstanding: OutstandingReceivables
    avg_rate: AvgRate


# ---------- helpers ----------

def _safe_pct_change(current: float, prior: float | None) -> float | None:
    if prior is None or prior == 0:
        return None
    return (current - prior) / prior * 100


def _weeks_since_month_close(year: int, month: int, today: date) -> int:
    """Number of Mondays elapsed since the first Monday strictly after the month's
    last day. 0 means the invoice send-Monday hasn't happened yet."""
    last_day = date(year, month, monthrange(year, month)[1])
    if today <= last_day:
        return 0
    day_after_close = last_day + timedelta(days=1)
    days_to_monday = (-day_after_close.weekday()) % 7
    first_send_monday = day_after_close + timedelta(days=days_to_monday)
    if today < first_send_monday:
        return 0
    return (today - first_send_monday).days // 7 + 1


def _build_family_lookup(lessons: Iterable[Lesson]) -> dict[str, str]:
    """Map student_id (the col-1 'Student Identifier' from Master Log, which is
    also the monthly-tab column header) → family_id. First occurrence wins."""
    lookup: dict[str, str] = {}
    for l in lessons:
        if l.student_id and l.family_id and l.student_id not in lookup:
            lookup[l.student_id] = l.family_id
    return lookup


UNPAID_STATUSES = {"follow-up", "sent"}
STALE_STATUSES = {"follow-up"}


# ---------- per-metric computation ----------

def _mtd_revenue(parsed: ParsedSheet, as_of: date, year: int) -> MtdRevenue:
    month = as_of.month
    cur = sum(l.revenue for l in parsed.lessons if l.date.year == year and l.date.month == month)
    prior = next(
        (y.revenue for y in parsed.year_summary if y.year == year - 1 and y.month == month),
        None,
    )
    return MtdRevenue(
        year=year, month=month, revenue=cur,
        prior_year_revenue=prior,
        yoy_delta_pct=_safe_pct_change(cur, prior),
    )


def _mtd_hours(parsed: ParsedSheet, as_of: date, year: int) -> MtdHours:
    month = as_of.month
    cur = sum(l.hours for l in parsed.lessons if l.date.year == year and l.date.month == month)
    prior = next(
        (y.hours for y in parsed.year_summary if y.year == year - 1 and y.month == month),
        None,
    )
    return MtdHours(
        year=year, month=month, hours=cur,
        prior_year_hours=prior,
        yoy_delta_pct=_safe_pct_change(cur, prior),
    )


def _ytd_revenue(parsed: ParsedSheet, as_of: date, year: int) -> YtdRevenue:
    closed_through = as_of.month - 1
    if closed_through < 1:
        return YtdRevenue(year=year, closed_months_through=0, revenue=0.0,
                          prior_year_revenue=None, yoy_delta_pct=None)
    cur = sum(
        l.revenue for l in parsed.lessons
        if l.date.year == year and l.date.month <= closed_through
    )
    prior = sum(
        (y.revenue or 0.0) for y in parsed.year_summary
        if y.year == year - 1 and y.month <= closed_through
    )
    return YtdRevenue(
        year=year, closed_months_through=closed_through, revenue=cur,
        prior_year_revenue=prior if prior else None,
        yoy_delta_pct=_safe_pct_change(cur, prior),
    )


def _family_concentration(lessons: Iterable[Lesson], year: int, top_n: int) -> FamilyConcentration:
    totals: dict[str, float] = {}
    for l in lessons:
        if l.date.year != year:
            continue
        if not l.family_id:
            continue
        totals[l.family_id] = totals.get(l.family_id, 0.0) + l.revenue
    total = sum(totals.values())
    ranked = sorted(totals.items(), key=lambda kv: kv[1], reverse=True)[:top_n]
    shares = tuple(
        FamilyShare(family_id=f, revenue=r, share_pct=(r / total * 100) if total else 0.0)
        for f, r in ranked
    )
    top_share = sum(s.share_pct for s in shares)
    return FamilyConcentration(
        year=year, top_n=top_n, total_ytd_revenue=total,
        top_families=shares, top_n_share_pct=top_share,
    )


def _outstanding(
    parsed: ParsedSheet,
    as_of: date,
    stale_threshold_weeks: int,
) -> OutstandingReceivables:
    family_lookup = _build_family_lookup(parsed.lessons)

    by_student: dict[str, list[MonthlyInvoice]] = {}
    for inv in parsed.monthly_invoices:
        by_student.setdefault(inv.student_id, []).append(inv)
    for invs in by_student.values():
        invs.sort(key=lambda i: (i.year, i.month))

    items: list[OutstandingItem] = []
    for student, invs in by_student.items():
        # 1. One item per unpaid month (Follow-Up or Sent).
        for inv in invs:
            status_l = inv.invoice_status.strip().lower()
            if status_l not in UNPAID_STATUSES:
                continue
            if inv.revenue <= 0:
                continue
            weeks = _weeks_since_month_close(inv.year, inv.month, as_of)
            is_stale = status_l in STALE_STATUSES and weeks >= stale_threshold_weeks
            items.append(
                OutstandingItem(
                    family_id=family_lookup.get(student),
                    student_id=student,
                    year=inv.year,
                    month=inv.month,
                    amount_owed=inv.revenue,
                    weeks_since_month_close=weeks,
                    is_stale=is_stale,
                    status=inv.invoice_status,
                )
            )
        # 2. Arrears: most recent non-Paid row with past_amount > 0. Counted once.
        for inv in reversed(invs):
            if (
                inv.past_amount and inv.past_amount > 0
                and inv.invoice_status.strip().lower() != "paid"
            ):
                weeks = _weeks_since_month_close(inv.year, inv.month, as_of)
                items.append(
                    OutstandingItem(
                        family_id=family_lookup.get(student),
                        student_id=student,
                        year=inv.year,
                        month=inv.month,
                        amount_owed=inv.past_amount,
                        weeks_since_month_close=weeks,
                        is_stale=weeks >= stale_threshold_weeks,
                        status="Arrears",
                    )
                )
                break

    items.sort(key=lambda x: (-x.weeks_since_month_close, -x.amount_owed))
    total = sum(i.amount_owed for i in items)
    stale_total = sum(i.amount_owed for i in items if i.is_stale)
    return OutstandingReceivables(
        total=total, stale_total=stale_total,
        stale_threshold_weeks=stale_threshold_weeks,
        items=tuple(items),
    )


def _avg_rate(parsed: ParsedSheet, as_of: date, year: int) -> AvgRate:
    cur_lessons = [l for l in parsed.lessons if l.date.year == year and l.date.month == as_of.month]
    cur_hours = sum(l.hours for l in cur_lessons)
    cur_rev = sum(l.revenue for l in cur_lessons)
    cur_avg = (cur_rev / cur_hours) if cur_hours > 0 else None

    prior_full_year = year - 1
    prior_rows = [y for y in parsed.year_summary if y.year == prior_full_year]
    prior_rev = sum((y.revenue or 0) for y in prior_rows)
    prior_hrs = sum((y.hours or 0) for y in prior_rows)
    prior_avg = (prior_rev / prior_hrs) if prior_hrs > 0 else None

    return AvgRate(
        current_month_avg=cur_avg,
        prior_full_year=prior_full_year if prior_avg is not None else None,
        prior_full_year_avg=prior_avg,
        delta_pct=_safe_pct_change(cur_avg, prior_avg) if cur_avg is not None else None,
    )


# ---------- entry point ----------

def compute_metrics(
    parsed: ParsedSheet,
    as_of: date | None = None,
    *,
    top_n: int = 5,
    stale_threshold_weeks: int = 2,
) -> DashboardMetrics:
    as_of = as_of or date.today()
    try:
        year = int(parsed.workbook_title)
    except (TypeError, ValueError):
        year = as_of.year
    return DashboardMetrics(
        as_of=as_of,
        workbook_year=year,
        mtd_revenue=_mtd_revenue(parsed, as_of, year),
        mtd_hours=_mtd_hours(parsed, as_of, year),
        ytd_revenue=_ytd_revenue(parsed, as_of, year),
        family_concentration=_family_concentration(parsed.lessons, year, top_n),
        outstanding=_outstanding(parsed, as_of, stale_threshold_weeks),
        avg_rate=_avg_rate(parsed, as_of, year),
    )
