"""Dataclasses used by the sync pipeline. Persisted via raw SQL in tracker.db."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class Message:
    message_id: str
    thread_id: str
    sent_at: datetime
    from_email: str
    from_name: str
    to_emails: str         # comma-joined
    snippet: str
    body_plain: str
    is_from_us: bool       # derived from SENT label or from_email match


@dataclass
class Thread:
    thread_id: str
    sender_email: str
    sender_name: str
    subject: str
    last_message_at: datetime
    last_message_from: str          # 'us' | 'them'
    status: str                     # 'new' | 'replied' | 'waiting' | 'closed'
    needs_followup: bool
    followup_reason: str
    message_count: int
    last_synced_at: datetime
    label_ids: list[str] = field(default_factory=list)


@dataclass
class InvoiceMention:
    thread_id: str
    detected_amount: float | None
    detected_currency: str | None
    detected_client: str | None
    status: str
    notes: str
    detected_at: datetime
