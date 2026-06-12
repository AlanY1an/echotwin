"""Gray-zone addressee arbitration — a small LLM judges whether the utterance was said to the bot.

hard_verdict (organic.py) handles instant verdicts and triggering; all semantic
judgment (coreference resolution / topic continuity / responsiveness to a
clarifying question / open question vs rhetorical exclamation) lives here.
On failure/timeout returns None and the caller falls back to classify()'s
heuristic scoring — the full rule set is kept as the safety net.
"""
from __future__ import annotations

import asyncio
import json
import re

from loguru import logger

from echotwin.i18n.prompts import ARBITER_PAYLOAD_KEYS, ARBITER_SYSTEM
from echotwin.pipeline.organic import Verdict
from echotwin.providers.llm.base import MessageEnd, TextDelta

_JSON_RE = re.compile(r"\{.*\}", re.DOTALL)
_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL)



async def arbitrate(
    llm,
    *,
    bot_name: str,
    speaker: str,
    utterance: str,
    room_lines: list[str],
    last_bot_reply: str,
    last_addressee: str | None,
    in_window: bool,
    clarify_pending: bool,
    language: str = "zh",
    timeout: float = 1.5,
    cost_tracker=None,
    ids: dict | None = None,
    cost_prefix: str = "claude_haiku_4_5",
) -> tuple[Verdict, str] | None:
    import time as _t

    t0 = _t.monotonic()
    keys = ARBITER_PAYLOAD_KEYS.get(language, ARBITER_PAYLOAD_KEYS["zh"])
    payload = json.dumps(
        {
            keys["utterance"]: f"{speaker}: {utterance}",
            keys["room"]: room_lines[-6:],
            keys["last_reply"].format(bot_name=bot_name): last_bot_reply or keys["none_reply"],
            keys["last_addressee"].format(bot_name=bot_name): last_addressee or keys["none_addressee"],
            keys["in_window"]: in_window,
            keys["clarify_pending"].format(bot_name=bot_name): clarify_pending,
        },
        ensure_ascii=False,
    )
    system = ARBITER_SYSTEM.get(language, ARBITER_SYSTEM["zh"]).format(bot_name=bot_name)

    async def _run():
        text = ""
        usage = None
        async for ev in llm.stream_chat(system, [{"role": "user", "content": payload}]):
            if isinstance(ev, TextDelta):
                text += ev.text
            elif isinstance(ev, MessageEnd):
                usage = ev
        return text, usage

    try:
        text, usage = await asyncio.wait_for(_run(), timeout=timeout)
    except asyncio.TimeoutError:
        logger.warning(f"[arbiter] timeout ({timeout}s) for {utterance!r}")
        return None
    except Exception as e:
        logger.warning(f"[arbiter] failed for {utterance!r}: {e!r}")
        return None

    # Arbitration is a paid call too — MUST be recorded (the quota guard reads this DB)
    if cost_tracker is not None and usage is not None:
        _ids = ids or {}
        try:
            if usage.input_tokens:
                await cost_tracker.record(
                    f"{cost_prefix}_input", usage.input_tokens, **_ids
                )
            if usage.output_tokens:
                await cost_tracker.record(
                    f"{cost_prefix}_output", usage.output_tokens, **_ids
                )
            if usage.cache_creation_input_tokens:
                await cost_tracker.record(
                    f"{cost_prefix}_cache_write",
                    usage.cache_creation_input_tokens,
                    **_ids,
                )
            if usage.cache_read_input_tokens:
                await cost_tracker.record(
                    f"{cost_prefix}_cache_read",
                    usage.cache_read_input_tokens,
                    **_ids,
                )
        except Exception as e:
            logger.warning(f"[arbiter] cost record failed: {e}")

    # Thinking models like qwen3 may emit <think>…</think> (often containing braces); strip first, then find JSON
    m = _JSON_RE.search(_THINK_RE.sub("", text))
    if m is None:
        logger.warning(f"[arbiter] no JSON in reply: {text!r}")
        return None
    try:
        obj = json.loads(m.group(0))
        verdict = Verdict(obj["verdict"])
    except (ValueError, KeyError) as e:
        logger.warning(f"[arbiter] bad verdict JSON {text!r}: {e}")
        return None
    reason = str(obj.get("reason", ""))
    logger.info(
        f"[arbiter] {verdict.value} ({reason}) "
        f"{(_t.monotonic() - t0) * 1000:.0f}ms for {utterance!r}"
    )
    return verdict, reason
