"""Thread classification rules + invoice regex pass.

Pure functions — no Gmail API or DB access. Inputs are parsed messages.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Iterable

from ..tracker.models import InvoiceMention, Message

FOLLOWUP_KEYWORDS = (
    "invoice",
    "quote",
    "estimate",
    "follow up",
    "follow-up",
    "haven't heard",
    "havent heard",
    "still waiting",
)

# Match "$1,234.56", "$ 50", "€1.000,00" loosely. Currency captured separately.
_AMOUNT_RE = re.compile(
    r"(?P<currency>[$€£])\s*(?P<amount>\d{1,3}(?:[,\s]\d{3})*(?:\.\d{1,2})?|\d+(?:\.\d{1,2})?)"
)
_INVOICE_NUM_RE = re.compile(r"invoice\s*#?\s*(\d{1,8})", re.IGNORECASE)
_CURRENCY_NAMES = {"$": "USD", "€": "EUR", "£": "GBP"}


def business_days_since(then: datetime, now: datetime | None = None) -> int:
    """Number of weekdays strictly between `then` and `now`."""
    now = now or datetime.now(timezone.utc)
    if then.tzinfo is None:
        then = then.replace(tzinfo=timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    d1, d2 = then.date(), now.date()
    if d2 <= d1:
        return 0
    days = 0
    cur = d1
    while cur < d2:
        cur = cur + timedelta(days=1)
        if cur.weekday() < 5:
            days += 1
    return days


def classify_status(
    messages: list[Message],
    thread_label_names: Iterable[str],
    waiting_threshold_business_days: int = 3,
) -> tuple[str, str]:
    """Return (status, last_message_from) where status ∈ {new, replied, waiting, closed}."""
    if any(name.lower() == "closed" for name in thread_label_names):
        latest = messages[-1] if messages else None
        return "closed", ("us" if latest and latest.is_from_us else "them")

    latest = messages[-1]
    if latest.is_from_us:
        if business_days_since(latest.sent_at) > waiting_threshold_business_days:
            return "waiting", "us"
        return "replied", "us"
    return "new", "them"


def classify_followup(messages: list[Message]) -> tuple[bool, str]:
    """Rule-based follow-up detection. Returns (needs_followup, reason)."""
    latest = messages[-1]
    body_lower = (latest.body_plain or latest.snippet or "").lower()

    hits = [k for k in FOLLOWUP_KEYWORDS if k in body_lower]
    if hits:
        return True, f"keyword: {hits[0]}"

    stripped = (latest.body_plain or latest.snippet or "").strip()
    if stripped.endswith("?") and not latest.is_from_us:
        return True, "open question"

    return False, ""


def detect_invoice_mentions(thread_id: str, messages: list[Message]) -> list[InvoiceMention]:
    """Scan every message body for currency amounts and invoice numbers."""
    out: list[InvoiceMention] = []
    seen: set[tuple] = set()
    now = datetime.now(timezone.utc)

    for msg in messages:
        text = msg.body_plain or msg.snippet or ""
        if not text:
            continue

        for m in _AMOUNT_RE.finditer(text):
            symbol = m.group("currency")
            raw = m.group("amount").replace(",", "").replace(" ", "")
            try:
                amount = float(raw)
            except ValueError:
                continue
            currency = _CURRENCY_NAMES.get(symbol, symbol)
            key = (round(amount, 2), currency)
            if key in seen:
                continue
            seen.add(key)
            out.append(
                InvoiceMention(
                    thread_id=thread_id,
                    detected_amount=amount,
                    detected_currency=currency,
                    detected_client=None,
                    status="mentioned",
                    notes=f"amount match in message {msg.message_id}",
                    detected_at=now,
                )
            )

        for m in _INVOICE_NUM_RE.finditer(text):
            num = m.group(1)
            key = ("invoice#", num)
            if key in seen:
                continue
            seen.add(key)
            out.append(
                InvoiceMention(
                    thread_id=thread_id,
                    detected_amount=None,
                    detected_currency=None,
                    detected_client=None,
                    status="mentioned",
                    notes=f"invoice #{num} mentioned in message {msg.message_id}",
                    detected_at=now,
                )
            )

    return out
