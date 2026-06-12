"""Parse SenseVoice ASR output tags (tolerant to multiple format variants).

SenseVoice may emit tags in various orders:
  <|zh|><|SAD|><|EVENT|>content                  (legacy tag order)
  <|EMO_UNKNOWN|><|Speech|><|withitn|>content    (newer FunASR)

We strip ALL `<|tag|>` markers and extract emotion/language opportunistically.
"""
from __future__ import annotations
import re

TAG_PATTERN = re.compile(r"<\|([^|]+)\|>")

EMOTIONS = {
    "HAPPY", "SAD", "ANGRY", "NEUTRAL", "FEARFUL", "DISGUSTED", "SURPRISED",
}


def parse_sensevoice_output(raw: str) -> dict:
    tags = TAG_PATTERN.findall(raw)
    content = TAG_PATTERN.sub("", raw).strip()

    emotion = "NEUTRAL"
    language = "zh"

    for t in tags:
        # Emotion: 'EMO_HAPPY' or bare 'HAPPY'
        upper = t.upper()
        if upper.startswith("EMO_"):
            stripped = upper[4:]
            if stripped in EMOTIONS:
                emotion = stripped
            continue
        if upper in EMOTIONS:
            emotion = upper
            continue
        # Language: 2-letter lowercase
        if len(t) == 2 and t.islower():
            language = t

    return {
        "language": language,
        "emotion": emotion,
        "event": "",
        "content": content,
    }
