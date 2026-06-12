"""Wake-word matcher — exact + 'short utterance' fast-path detection."""
from __future__ import annotations

import re

_PUNCT_RE = re.compile(r"[\s,。!?、:;\"\'《》【】()()「」.!?\-_~`]+")

# After stripping wake word, this many normalized chars or fewer = "fast path eligible"
_FAST_PATH_MAX_EXTRA_CHARS = 2


class WakeWordMatcher:
    def __init__(self, wake_words: list[str]):
        # Sort longest first so a longer wake word ("一点点点") matches before "点点"
        self._wake_words = sorted(wake_words, key=len, reverse=True)

    def contains_wake(self, text: str) -> bool:
        norm = _PUNCT_RE.sub("", text).lower()
        for w in self._wake_words:
            if w.lower() in norm:
                return True
        return False

    def match_only(self, text: str) -> bool:
        """True iff text is essentially just a wake word + ≤2 extra chars.

        Case-insensitive, same as contains_wake — ASR lowercases ASCII wake
        words ("Hinata" → "hinata"), and a case-sensitive check here meant
        wake-only pings never took the cached fast path.
        """
        norm = _PUNCT_RE.sub("", text).strip().lower()
        if not norm:
            return False
        for w in self._wake_words:
            w_low = w.lower()
            if w_low in norm:
                idx = norm.find(w_low)
                before = norm[:idx]
                after = norm[idx + len(w_low):]
                extra = len(before) + len(after)
                if extra <= _FAST_PATH_MAX_EXTRA_CHARS:
                    return True
        return False
