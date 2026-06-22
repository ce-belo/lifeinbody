"""Anthropic API wrapper — classification today; draft generation in Step 4."""
from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

from .config import ANTHROPIC_API_KEY, ANTHROPIC_MODEL

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
