"""Parse a sheet_snapshot.json blob into typed ParsedSheet data.

All tab-specific quirks (currency formatting, header rows, totals rows, blank
columns) are handled here. Downstream code should never touch the raw rows.
"""
from __future__ import annotations

import logging
import re
from datetime import date, datetime
from typing import Any, Iterable

from .client import load_snapshot
from .models import (
    EmailTemplate,
    Family,
    Lesson,
    MonthlyInvoice,
    ParsedSheet,
    YearMonthSummary,
)

log = logging.getLogger("lifeinbody.sheet.parser")

_MONTH_NAMES = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10, "november": 11, "december": 12,
}


# ---------- coercion helpers ----------

def _to_float(cell: str) -> float | None:
    """Parse '$26,061.25', '14.5', '0', '' → float or None."""
    if cell is None:
        return None
    s = str(cell).strip()
    if not s:
        return None
    # Strip currency symbols, thousands separators, surrounding parens (neg accounting).
    neg = s.startswith("(") and s.endswith(")")
    if neg:
        s = s[1:-1]
    s = re.sub(r"[\$,\s]", "", s)
    if s in ("", "-", "—"):
        return None
    try:
        v = float(s)
    except ValueError:
        return None
    return -v if neg else v


def _to_int(cell: str) -> int | None:
    f = _to_float(cell)
    return int(f) if f is not None else None


def _parse_master_log_date(cell: str) -> date | None:
    """Master Log dates are 'M/D/YYYY' or 'MM/DD/YYYY'."""
    s = str(cell).strip()
    if not s:
        return None
    for fmt in ("%m/%d/%Y", "%-m/%-d/%Y"):  # %-m fails on Windows but we're on macOS
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def _month_from_tab_name(name: str) -> int | None:
    """'June', 'April ', 'jan' → 1-12."""
    key = name.strip().lower()
    return _MONTH_NAMES.get(key)


def _normalize_header(cell: str) -> str:
    return cell.strip()


# ---------- Master Log ----------

def _parse_master_log(rows: list[list[str]]) -> list[Lesson]:
    if not rows:
        return []
    # Header is row 0. We rely on positional columns rather than name lookup so the
    # parser keeps working if a column is renamed; we still validate the count.
    if len(rows[0]) < 14:
        log.warning("Master Log header has only %d cols; expected ≥14", len(rows[0]))

    lessons: list[Lesson] = []
    for r in rows[1:]:
        if not r or not r[0].strip():
            continue
        d = _parse_master_log_date(r[0])
        if d is None:
            continue
        rate = _to_float(r[5]) if len(r) > 5 else None
        hours = _to_float(r[6]) if len(r) > 6 else None
        if rate is None or hours is None:
            continue
        lessons.append(
            Lesson(
                date=d,
                student_id=(r[1] if len(r) > 1 else "").strip(),
                student_first=(r[2] if len(r) > 2 else "").strip(),
                student_last=(r[3] if len(r) > 3 else "").strip(),
                family_id=(r[4] if len(r) > 4 else "").strip(),
                rate=rate,
                hours=hours,
                start_time=(r[7] if len(r) > 7 else "").strip(),
                end_time=(r[8] if len(r) > 8 else "").strip(),
                lesson_type=(r[9] if len(r) > 9 else "").strip(),
                tutor=(r[10] if len(r) > 10 else "").strip(),
                source_key=(r[11] if len(r) > 11 else "").strip(),
            )
        )
    return lessons


# ---------- Monthly tabs ----------

_TOTAL_ROW_LABELS = {"total hrs", "total hours", "total"}
_RATE_ROW_LABELS = {"rate", "rates"}
_REVENUE_ROW_LABELS = {"total", "totals", "monthly total"}  # the $-sum row often labelled 'total'


def _find_label_row(rows: list[list[str]], labels: set[str]) -> int | None:
    """Return the row index whose col-0 (lowercased, trimmed) matches any label."""
    for i, r in enumerate(rows):
        if r and r[0].strip().lower() in labels:
            return i
    return None


def _parse_monthly_tab(name: str, rows: list[list[str]], year: int) -> list[MonthlyInvoice]:
    month = _month_from_tab_name(name)
    if month is None:
        return []
    if not rows or not rows[0]:
        return []

    header = rows[0]
    # Student columns are everything in row 0 that has a non-empty label and isn't an
    # aggregate column. The last two header columns are typically 'Daily Hrs' /
    # 'Daily Revenue' — we drop those by name.
    student_cols: list[tuple[int, str]] = []
    for j, h in enumerate(header):
        hl = h.strip().lower()
        if not hl or hl in ("daily hrs", "daily hours", "daily revenue", "weighted avg.", "weighted avg"):
            continue
        student_cols.append((j, h.strip()))

    # Find the canonical rows by label
    hrs_row_idx = _find_label_row(rows, {"total hrs", "total hours"})
    rate_row_idx = _find_label_row(rows, _RATE_ROW_LABELS)
    # The revenue ($) row is labelled 'total' but only AFTER the rate row (since
    # both can match 'total'). Look for 'total' below rate.
    rev_row_idx = None
    start = (rate_row_idx + 1) if rate_row_idx is not None else 0
    for i in range(start, len(rows)):
        if rows[i] and rows[i][0].strip().lower() in ("total", "totals"):
            rev_row_idx = i
            break

    status_row_idx = _find_label_row(rows, {"invoice status"})
    past_row_idx = _find_label_row(rows, {"past amounts", "past amount"})
    grand_row_idx = _find_label_row(rows, {"grand total", "grand totals"})

    def cell(row_idx: int | None, col_idx: int) -> str:
        if row_idx is None:
            return ""
        row = rows[row_idx]
        return row[col_idx] if col_idx < len(row) else ""

    invoices: list[MonthlyInvoice] = []
    for col_idx, student in student_cols:
        hours = _to_float(cell(hrs_row_idx, col_idx)) or 0.0
        revenue = _to_float(cell(rev_row_idx, col_idx)) or 0.0
        status = cell(status_row_idx, col_idx).strip()
        past = _to_float(cell(past_row_idx, col_idx))
        grand = _to_float(cell(grand_row_idx, col_idx))

        # Skip students that have no presence in the month at all.
        if hours == 0.0 and revenue == 0.0 and not status and past is None and grand is None:
            continue

        invoices.append(
            MonthlyInvoice(
                year=year,
                month=month,
                student_id=student,
                hours=hours,
                revenue=revenue,
                invoice_status=status,
                past_amount=past,
                grand_total=grand,
            )
        )
    return invoices


# ---------- Year Overview ----------

_YEAR_OVERVIEW_MONTH_ROW_OFFSETS = list(range(12))  # 12 month rows starting from header+1


def _parse_year_overview(rows: list[list[str]]) -> list[YearMonthSummary]:
    """Walk both stacked tables (Monthly Revenue, then Monthly Tutoring Hours).

    A 'Month' header row defines the year columns for each block. The next 12
    rows are January…December. Some cells are blank (no data yet)."""
    summaries: dict[tuple[int, int, str], float | None] = {}

    blocks: list[tuple[int, str]] = []  # (header_row_idx, 'revenue' | 'hours')
    for i, r in enumerate(rows):
        if not r:
            continue
        c0 = (r[1] if len(r) > 1 else "").strip().lower()
        if c0 == "month" and len(r) > 7:
            # Look up which block we're in by scanning the previous few rows for the
            # section header text.
            preceding = " ".join(
                (rows[k][1] if k < len(rows) and len(rows[k]) > 1 else "")
                for k in range(max(0, i - 3), i)
            ).lower()
            if "tutor" in preceding or "hour" in preceding:
                blocks.append((i, "hours"))
            else:
                blocks.append((i, "revenue"))

    for header_idx, kind in blocks:
        header = rows[header_idx]
        # Year columns sit at columns 2 onwards until we hit a non-year header.
        year_cols: list[tuple[int, int]] = []
        for j in range(2, len(header)):
            yr = _to_int(header[j])
            if yr is None or yr < 2000 or yr > 2100:
                continue
            year_cols.append((j, yr))
        for offset in range(1, 13):
            row_idx = header_idx + offset
            if row_idx >= len(rows):
                break
            row = rows[row_idx]
            if not row or len(row) < 2:
                continue
            month_label = row[1].strip().lower()
            month = _MONTH_NAMES.get(month_label)
            if month is None:
                continue
            for col_idx, yr in year_cols:
                val = _to_float(row[col_idx] if col_idx < len(row) else "")
                key = (yr, month, kind)
                summaries[key] = val

    # Pivot to YearMonthSummary
    pairs: dict[tuple[int, int], dict[str, float | None]] = {}
    for (yr, month, kind), v in summaries.items():
        pairs.setdefault((yr, month), {})[kind] = v
    out: list[YearMonthSummary] = []
    for (yr, month), d in sorted(pairs.items()):
        out.append(YearMonthSummary(year=yr, month=month, revenue=d.get("revenue"), hours=d.get("hours")))
    return out


# ---------- Directory ----------

def _parse_directory(rows: list[list[str]]) -> list[Family]:
    if not rows:
        return []
    # Header row 0 names: Client, Student(s) Name, Parent Name, Invoice Recipient,
    # Required CC, Alternate Email, Payment Type.
    families: list[Family] = []
    for r in rows[1:]:
        if not r or not r[0].strip():
            continue
        cc_raw = (r[4] if len(r) > 4 else "").strip()
        cc_tuple = tuple(part.strip() for part in re.split(r"[,;]", cc_raw) if part.strip()) if cc_raw else ()
        families.append(
            Family(
                family_id=r[0].strip(),
                students=(r[1] if len(r) > 1 else "").strip(),
                parent_name=(r[2] if len(r) > 2 else "").strip(),
                invoice_recipient=(r[3] if len(r) > 3 else "").strip(),
                cc=cc_tuple,
                alternate_email=(r[5] if len(r) > 5 else "").strip(),
                payment_type=(r[6] if len(r) > 6 else "").strip(),
            )
        )
    return families


# ---------- Email Templates ----------

def _parse_email_templates(rows: list[list[str]]) -> list[EmailTemplate]:
    """The tab has a title row, blank row, header row ['Situation','Body','Subject'],
    then template rows. We just scan for rows whose col 0 is non-empty after the header."""
    if not rows:
        return []
    header_idx = None
    for i, r in enumerate(rows):
        if r and r[0].strip().lower() == "situation":
            header_idx = i
            break
    if header_idx is None:
        return []
    out: list[EmailTemplate] = []
    for r in rows[header_idx + 1:]:
        if not r or not r[0].strip():
            continue
        out.append(
            EmailTemplate(
                situation=r[0].strip(),
                body=(r[1] if len(r) > 1 else "").strip(),
                subject=(r[2] if len(r) > 2 else "").strip(),
            )
        )
    return out


# ---------- Top-level entry points ----------

def parse_snapshot(snapshot: dict[str, Any]) -> ParsedSheet:
    """Turn a raw snapshot dict into a typed ParsedSheet."""
    tabs_by_name = {t["name"]: t for t in snapshot.get("tabs", [])}
    workbook_title = snapshot.get("workbook_title", "")
    # The workbook title is the year (e.g. "2026"); fall back to current year if not.
    try:
        year = int(workbook_title)
    except (TypeError, ValueError):
        year = datetime.now().year

    lessons = _parse_master_log(tabs_by_name.get("Master Log", {}).get("rows", []))

    monthly_invoices: list[MonthlyInvoice] = []
    for tab_name, tab in tabs_by_name.items():
        if _month_from_tab_name(tab_name) is None:
            continue
        monthly_invoices.extend(_parse_monthly_tab(tab_name, tab.get("rows", []), year))

    year_summary = _parse_year_overview(tabs_by_name.get("Year Overview", {}).get("rows", []))
    families = _parse_directory(tabs_by_name.get("Directory", {}).get("rows", []))
    templates = _parse_email_templates(tabs_by_name.get("Email Templates", {}).get("rows", []))

    synced_at_raw = snapshot.get("synced_at")
    synced_at = datetime.fromisoformat(synced_at_raw) if synced_at_raw else datetime.utcnow()

    return ParsedSheet(
        workbook_id=snapshot.get("workbook_id", ""),
        workbook_title=workbook_title,
        synced_at=synced_at,
        lessons=tuple(lessons),
        monthly_invoices=tuple(monthly_invoices),
        year_summary=tuple(year_summary),
        families=tuple(families),
        email_templates=tuple(templates),
    )


def parse_cached() -> ParsedSheet:
    """Convenience: load data/sheet_snapshot.json and parse it."""
    return parse_snapshot(load_snapshot())
