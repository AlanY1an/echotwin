"""SpeculativeLLM — LLM stream pre-opened before endpoint confirmation.

The watchdog opens the stream early with the provisional transcript when
"silence ≥300ms AND the streaming-ASR pipeline has drained"; events are
buffered first, nothing is pushed to TTS. After endpoint confirmation:
  - matches(final text, current dialogue length) → think_speak continues
    the stream via events() (replays the buffer, then live), and writes
    user_payload (what the LLM actually saw) into history;
  - no match → abort(). Anthropic still bills the already-generated tokens;
    not recorded locally (no MessageEnd means no usage available), only a
    [spec-llm] wasted log line is emitted.
"""
from __future__ import annotations

import asyncio

from loguru import logger


class SpeculativeLLM:
    def __init__(
        self,
        llm,
        system: str,
        messages: list[dict],
        *,
        user_text: str,
        user_payload: str,
        tools: list[dict] | None,
        dialogue_len: int,
    ):
        self.user_text = user_text        # transcript at speculation time (wake word already stripped)
        self.user_payload = user_payload  # JSON written into messages — history is based on this
        self.messages = messages          # full messages the speculative stream saw (reused for tool rounds)
        self._dialogue_len = dialogue_len
        self._buf: list = []
        self._live: asyncio.Queue = asyncio.Queue()
        self._attached = False
        self._task = asyncio.create_task(self._pump(llm, system, messages, tools))

    async def _pump(self, llm, system, messages, tools) -> None:
        try:
            async for ev in llm.stream_chat(system, messages, tools=tools):
                if self._attached:
                    self._live.put_nowait(ev)
                else:
                    self._buf.append(ev)
            self._live.put_nowait(None)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning(f"[spec-llm] stream failed: {e!r}")
            self._live.put_nowait(e)

    def matches(self, final_user_text: str, dialogue_len: int) -> bool:
        """The speculation may only be adopted if the text matches AND the
        dialogue history wasn't modified by other turns."""
        return (
            final_user_text.strip() == self.user_text.strip()
            and dialogue_len == self._dialogue_len
        )

    async def events(self):
        """Attach: replay buffered events first, then continue with the live
        stream (single-threaded asyncio — no loss, no duplication)."""
        self._attached = True
        for ev in self._buf:
            yield ev
        if self._task.done() and self._live.empty():
            # The stream already ended during buffering; the sentinel is in the
            # queue. An empty queue means the pump hasn't put it yet.
            pass
        while True:
            ev = await self._live.get()
            if ev is None:
                return
            if isinstance(ev, Exception):
                raise ev
            yield ev

    async def abort(self) -> None:
        if not self._task.done():
            self._task.cancel()
            logger.info(f"[spec-llm] wasted speculation for {self.user_text!r} "
                        f"(billed by Anthropic, not recorded locally)")
        await asyncio.gather(self._task, return_exceptions=True)
