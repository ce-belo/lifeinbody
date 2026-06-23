"""Typed model for the parsed operations workbook."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime


@dataclass(frozen=True)
class Lesson:
    """One row of Master Log = one lesson delivered."""
    date: date
    student_id: str
    student_first: str
    student_last: str
    family_id: str
    rate: float
    hours: float
    tutor: str
    start_time: str = ""
    end_time: str = ""
    lesson_type: str = ""
    source_key: str = ""

    @property
    def revenue(self) -> float:
        return self.rate * self.hours


@dataclass(frozen=True)
class MonthlyInvoice:
    """One student × one month from a monthly tab."""
    year: int
    month: int                       # 1-12
    student_id: str
    hours: float
    revenue: float
    invoice_status: str              # 'Paid' | 'Follow-Up' | 'No Invoice' | ''
    past_amount: float | None = None
    grand_total: float | None = None


@dataclass(frozen=True)
class YearMonthSummary:
    """One cell in the Year Overview YoY tables."""
    year: int
    month: int                       # 1-12
    revenue: float | None
    hours: float | None


@dataclass(frozen=True)
class Family:
    """One row of the Directory tab."""
    family_id: str
    students: str
    parent_name: str
    invoice_recipient: str
    cc: tuple[str, ...] = ()
    alternate_email: str = ""
    payment_type: str = ""


@dataclass(frozen=True)
class EmailTemplate:
    """One row of the Email Templates tab."""
    situation: str
    subject: str
    body: str


@dataclass(frozen=True)
class ParsedSheet:
    workbook_id: str
    workbook_title: str              # e.g. "2026"
    synced_at: datetime
    lessons: tuple[Lesson, ...] = field(default_factory=tuple)
    monthly_invoices: tuple[MonthlyInvoice, ...] = field(default_factory=tuple)
    year_summary: tuple[YearMonthSummary, ...] = field(default_factory=tuple)
    families: tuple[Family, ...] = field(default_factory=tuple)
    email_templates: tuple[EmailTemplate, ...] = field(default_factory=tuple)
