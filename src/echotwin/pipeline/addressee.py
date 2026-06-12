"""Addressee detector — decides whether an ASR utterance is meant for the bot."""
from __future__ import annotations

import re
import time
from typing import Protocol

from echotwin.persona import Persona


class _SessionLike(Protocol):
    last_bot_speak_time: float
    last_addressee_id: int | None


# Treat these as fluff to strip when comparing wake-word against utterance text.
_PUNCT_RE = re.compile(
    r"[\s,。!?、:;\"\'《》【】()()「」.!?\-_~`]+",
)


def _normalize(text: str) -> str:
    return _PUNCT_RE.sub("", text).lower()


class AddresseeDetector:
    def __init__(
        self,
        persona: Persona,
        bot_user_id: int,
        continuation_window_seconds: float = 15.0,
        solo_channel_auto: bool = True,
    ):
        self._wake_words_orig = list(persona.wake_words)
        # Normalize once for cheap comparison; fall back to originals when stripping
        self._wake_words_norm = sorted(
            (w for w in (_normalize(w) for w in self._wake_words_orig) if w),
            key=len,
            reverse=True,
        )
        self._bot_mention = f"<@{bot_user_id}>"
        # Discord also formats mentions as <@!id>
        self._bot_mention_alt = f"<@!{bot_user_id}>"
        self._continuation = continuation_window_seconds
        self._solo_auto = solo_channel_auto

    def _matches_wake(self, text: str) -> bool:
        norm = _normalize(text)
        if not norm:
            return False
        return any(w in norm for w in self._wake_words_norm)

    def is_addressed(
        self,
        text: str,
        *,
        speaker_id: int,
        session: _SessionLike,
        channel_member_count: int,
    ) -> bool:
        if not text or not text.strip():
            return False
        # Rule 1: wake word
        if self._matches_wake(text):
            return True
        # Rule 2: explicit @bot mention
        if self._bot_mention in text or self._bot_mention_alt in text:
            return True
        # Rule 3: continuation — same speaker, recent bot speak
        if (
            session.last_addressee_id == speaker_id
            and time.time() - session.last_bot_speak_time < self._continuation
        ):
            return True
        # Rule 4: solo channel (just bot + 1 user)
        if self._solo_auto and channel_member_count == 2:
            return True
        return False

    def strip_wake_word(self, text: str) -> str:
        """Remove wake-word occurrence so the LLM sees clean input.

        Also cleans up surrounding bracket/punctuation that becomes orphaned
        (e.g. 【一点点点】查天气 → 查天气, not 【】查天气).
        """
        result = text
        # try original wake words first (longest first), case-insensitive
        for w in sorted(self._wake_words_orig, key=len, reverse=True):
            idx = result.lower().find(w.lower())
            if idx != -1:
                result = (result[:idx] + result[idx + len(w):])
                # Strip leading non-word chars (handles 【】, leading spaces,
                # punctuation orphaned by wake-word removal)
                result = re.sub(r"^\W+", "", result, flags=re.UNICODE)
                # Also strip trailing non-word chars at the END of original head
                # if it's now adjacent to the new start (rare but seen with 【...】 pattern)
                return result.strip()
        return result
