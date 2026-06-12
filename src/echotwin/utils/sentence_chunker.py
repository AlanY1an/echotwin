"""Streaming sentence chunker.

First sentence: emits at any of FIRST_PUNCT (more lenient → faster TTS).
Subsequent: emits at PUNCT only.
"""
from __future__ import annotations

import re

PUNCT = set("。!?;.\n")
FIRST_PUNCT = set("。!?,;,~、.\n")

# Speakability: only worth pushing to Fish if content remains after stripping
# [emotion tags]/punctuation/whitespace. Empty chunks (\n, tag-only,
# punctuation-only) make Fish return an "empty audio" error and finish the
# WHOLE stream — all text pushed afterwards goes silent (observed live
# 2026-06-11: nothing after a \n was spoken).
_TAG_RE = re.compile(r"\[[^\]]*\]")
_NON_SPEECH = set("。!?;,、:~…·.!?,;:~ \t\n\r\"'““””‘’()()「」《》〈〉【】-—_*#`")


def speakable(text: str) -> bool:
    rest = _TAG_RE.sub("", text)
    return any(c not in _NON_SPEECH for c in rest)

# Send the first sentence out once this many characters accumulate (TTS first
# audio doesn't wait for the full sentence). Increase if the cut feels abrupt;
# if live A/B testing reveals word-boundary seams, roll back to 24/32.
FIRST_MAX_CHARS = 16


class SentenceChunker:
    def __init__(self) -> None:
        self._buf = ""
        self._is_first = True

    def feed(self, delta: str) -> list[str]:
        self._buf += delta
        out: list[str] = []
        while True:
            puncts = FIRST_PUNCT if self._is_first else PUNCT
            idx = -1
            for i, c in enumerate(self._buf):
                if c in puncts:
                    idx = i
                    break
            # First-sentence char cap: cut at the cap when there's no punctuation
            # or it's too far away — the cap must win, otherwise it fails exactly
            # in the longest-first-sentence case (big delta + distant punctuation).
            # Cost: the cut may land mid-word with a synthesis seam; increase
            # FIRST_MAX_CHARS to roll back.
            if self._is_first and len(self._buf) >= FIRST_MAX_CHARS and (
                idx == -1 or idx >= FIRST_MAX_CHARS
            ):
                sentence, self._buf = (
                    self._buf[:FIRST_MAX_CHARS],
                    self._buf[FIRST_MAX_CHARS:],
                )
                out.append(sentence)
                self._is_first = False
                continue
            if idx == -1:
                break
            sentence, self._buf = self._buf[: idx + 1], self._buf[idx + 1 :]
            out.append(sentence)
            self._is_first = False
        return out

    def flush(self) -> str:
        rem, self._buf = self._buf, ""
        return rem

    def reset(self) -> None:
        self._buf = ""
        self._is_first = True
