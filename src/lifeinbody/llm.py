"""Anthropic API wrapper for classification and draft generation."""
from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

from .config import ANTHROPIC_API_KEY, ANTHROPIC_MODEL, BUSINESS_CONTEXT

if TYPE_CHECKING:  # pragma: no cover
    from .tracker.models import Message

log = logging.getLogger("lifeinbody.llm")


class LLMUnavailable(RuntimeError):
    pass


def _client():
    if not ANTHROPIC_API_KEY:
        raise LLMUnavailable("ANTHROPIC_API_KEY is not set in .env")
    try:
        import anthropic
    except ImportError as exc:  # pragma: no cover
        raise LLMUnavailable("anthropic SDK is not installed") from exc
    return anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)


def classify_followup(subject: str, messages: list["Message"]) -> tuple[bool, str]:
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
    return "".join(b.text for b in resp.content if getattr(b, "type", "") == "text").strip()
