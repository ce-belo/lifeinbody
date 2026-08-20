"""Anthropic API wrapper for classification and draft generation."""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from .config import ANTHROPIC_API_KEY, ANTHROPIC_MODEL, BUSINESS_CONTEXT

if TYPE_CHECKING:  # pragma: no cover
    from .tracker.models import Message

log = logging.getLogger("lifeinbody.llm")


class LLMUnavailable(RuntimeError):
    pass


# Per-million-token pricing in USD. Extend when adding new models.
_MODEL_RATES: dict[str, dict[str, float]] = {
    "claude-sonnet-4-6": {"input": 3.0, "output": 15.0, "cache_read": 0.30, "cache_write_5m": 3.75},
}


def _compute_cost(model: str, usage: Any) -> float:
    rates = _MODEL_RATES.get(model)
    if rates is None:
        return 0.0
    input_tokens = getattr(usage, "input_tokens", 0) or 0
    output_tokens = getattr(usage, "output_tokens", 0) or 0
    cache_read = getattr(usage, "cache_read_input_tokens", 0) or 0
    cache_write = getattr(usage, "cache_creation_input_tokens", 0) or 0
    return (
        input_tokens * rates["input"]
        + output_tokens * rates["output"]
        + cache_read * rates["cache_read"]
        + cache_write * rates["cache_write_5m"]
    ) / 1_000_000


def _record_usage(model: str, operation: str, usage: Any, thread_id: str | None) -> None:
    """Insert one row into llm_usage. Never raises — logging is best-effort."""
    try:
        from .tracker.db import connect
        cost = _compute_cost(model, usage)
        with connect() as conn:
            conn.execute(
                """
                INSERT INTO llm_usage
                    (occurred_at, model, operation, input_tokens, output_tokens,
                     cache_read_input_tokens, cache_creation_input_tokens, cost_usd, thread_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    datetime.now(timezone.utc).isoformat(),
                    model,
                    operation,
                    getattr(usage, "input_tokens", 0) or 0,
                    getattr(usage, "output_tokens", 0) or 0,
                    getattr(usage, "cache_read_input_tokens", 0) or 0,
                    getattr(usage, "cache_creation_input_tokens", 0) or 0,
                    cost,
                    thread_id,
                ),
            )
            conn.commit()
    except Exception:
        log.exception("failed to record llm_usage row")


def _client():
    if not ANTHROPIC_API_KEY:
        raise LLMUnavailable("ANTHROPIC_API_KEY is not set in .env")
    try:
        import anthropic
    except ImportError as exc:  # pragma: no cover
        raise LLMUnavailable("anthropic SDK is not installed") from exc
    return anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)


def classify_followup(
    subject: str,
    messages: list["Message"],
    thread_id: str | None = None,
) -> tuple[bool, str]:
    """LLM second-pass follow-up classifier. Returns (needs_followup, reason)."""
    client = _client()

    transcript_lines = []
    for m in messages[-6:]:
        who = "US" if m.is_from_us else "THEM"
        body = (m.body_plain or m.snippet or "").strip()
        transcript_lines.append(f"[{who} @ {m.sent_at.isoformat()}]\n{body[:1500]}")
    transcript = "\n\n---\n\n".join(transcript_lines)

    prompt = f"""You triage email threads for a small tutoring business (Life in Body).
Decide whether THIS thread needs a follow-up from us right now.

Subject: {subject}

Latest messages (oldest first):
{transcript}

Reply with a single JSON object: {{"needs_followup": true|false, "reason": "short phrase"}}.
needs_followup should be true only if there is a clear, pending action on our side:
unanswered question, unpaid invoice, scheduling that requires our reply, a client
chasing us, etc. Auto-receipts, newsletters, or threads already resolved should be false."""

    msg = client.messages.create(
        model=ANTHROPIC_MODEL,
        max_tokens=200,
        messages=[{"role": "user", "content": prompt}],
    )
    _record_usage(ANTHROPIC_MODEL, "classify", msg.usage, thread_id)
    text = "".join(block.text for block in msg.content if getattr(block, "type", "") == "text").strip()

    try:
        start = text.index("{")
        end = text.rindex("}") + 1
        data = json.loads(text[start:end])
        return bool(data.get("needs_followup")), str(data.get("reason", ""))[:200]
    except (ValueError, json.JSONDecodeError):
        log.warning("LLM returned unparseable output: %r", text[:200])
        return False, ""


def _format_transcript(messages: list["Message"], max_chars: int = 2000, last_n: int = 10) -> str:
    lines = []
    for m in messages[-last_n:]:
        who = "us" if m.is_from_us else f"{m.from_name or m.from_email} <{m.from_email}>"
        body = (m.body_plain or m.snippet or "").strip()
        if len(body) > max_chars:
            body = body[:max_chars] + " […truncated…]"
        lines.append(f"[{m.sent_at.strftime('%Y-%m-%d %H:%M')} from {who}]\n{body}")
    return "\n\n---\n\n".join(lines)


def generate_draft(
    messages: list["Message"],
    business_context: str = BUSINESS_CONTEXT,
    tone_hint: str = "",
    thread_id: str | None = None,
) -> str:
    """Generate a reply draft body for the most recent message in the thread."""
    client = _client()
    transcript = _format_transcript(messages)

    tone_line = f"\nTone hint for this draft: {tone_hint}" if tone_hint else ""
    system = f"""You draft reply emails for Life in Body.

{business_context.strip()}{tone_line}

Rules:
- Reply to the most recent message in the conversation. You are writing from "us" to the other party.
- Greet the recipient by first name when it's clear who that is.
- Use the sign-off described above.
- Do NOT include a subject line, quoted previous text, or any wrapper. Output the reply body only, plain text, no markdown.
- Never invent prices, payment amounts, or specific dates/times that aren't already present in the conversation. If a number must be filled in, leave a placeholder like "[amount]" or "[date]".
- Keep it concise. If the latest message is an auto-receipt, a calendar invite, or otherwise needs no reply, output the single token NO_REPLY_NEEDED."""

    user = f"Conversation (oldest first):\n\n{transcript}\n\nDraft the reply now."

    resp = client.messages.create(
        model=ANTHROPIC_MODEL,
        max_tokens=1024,
        system=system,
        messages=[{"role": "user", "content": user}],
    )
    _record_usage(ANTHROPIC_MODEL, "draft", resp.usage, thread_id)
    return "".join(b.text for b in resp.content if getattr(b, "type", "") == "text").strip()


def generate_nudge(
    messages: list["Message"],
    business_context: str = BUSINESS_CONTEXT,
    tone_hint: str = "",
    thread_id: str | None = None,
) -> str:
    """Generate a polite check-in for a thread where we wrote last and they haven't replied."""
    client = _client()
    transcript = _format_transcript(messages)

    tone_line = f"\nTone hint for this nudge: {tone_hint}" if tone_hint else ""
    system = f"""You draft short follow-up nudges for Life in Body.

{business_context.strip()}{tone_line}

Context: we sent the last message in this thread and have not heard back. Write a
brief, warm check-in that re-surfaces what we were waiting on, without sounding
pushy. Reference the previous note ("just circling back on…", "wanted to follow
up on my last message about…") so it doesn't read like a cold message.

Rules:
- Two to four sentences. Friendly, not apologetic, never guilt-tripping.
- Greet by first name when it's clear who you're writing to.
- Use the sign-off described above.
- Do NOT include a subject line, quoted previous text, or any wrapper. Output the body only, plain text, no markdown.
- Never invent prices, dates, or amounts not present in the thread — use placeholders like "[amount]" or "[date]" if needed.
- If the latest message we sent was itself an auto-receipt, calendar invite, or otherwise something that wouldn't warrant a nudge, output the single token NO_NUDGE_NEEDED."""

    user = f"Conversation (oldest first):\n\n{transcript}\n\nDraft the check-in now."

    resp = client.messages.create(
        model=ANTHROPIC_MODEL,
        max_tokens=512,
        system=system,
        messages=[{"role": "user", "content": user}],
    )
    _record_usage(ANTHROPIC_MODEL, "draft", resp.usage, thread_id)
    return "".join(b.text for b in resp.content if getattr(b, "type", "") == "text").strip()
