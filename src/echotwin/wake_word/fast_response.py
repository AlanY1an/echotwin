"""On-disk cache of pre-synthesized 'wake-only' TTS responses."""
from __future__ import annotations

import hashlib
import random
from pathlib import Path
from typing import Awaitable, Callable

from loguru import logger


class FastResponseCache:
    def __init__(
        self,
        persona_id: str,
        voice_id: str,
        responses: list[str],
        data_dir: Path | str,
    ):
        self._persona_id = persona_id
        self._voice_id = voice_id
        self._responses = list(responses)
        self._dir = Path(data_dir) / "wake_responses" / persona_id

    @property
    def dir(self) -> Path:
        return self._dir

    def _path_for(self, text: str) -> Path:
        h = hashlib.sha1(f"{self._voice_id}:{text}".encode("utf-8")).hexdigest()[:12]
        return self._dir / f"{h}.ogg"

    async def ensure_synthesized(
        self, synth_fn: Callable[[str], Awaitable[bytes]]
    ) -> None:
        """Synthesize any missing response audio; remove stale files."""
        self._dir.mkdir(parents=True, exist_ok=True)
        for text in self._responses:
            p = self._path_for(text)
            if p.exists() and p.stat().st_size > 0:
                continue
            try:
                audio = await synth_fn(text)
                if audio:
                    p.write_bytes(audio)
                    logger.info(f"[fast-response] synthesized {text!r} -> {p.name}")
                else:
                    logger.warning(f"[fast-response] empty audio for {text!r}")
            except Exception as e:
                logger.warning(f"[fast-response] synth failed for {text!r}: {e}")
        # Cleanup stale files. Underscore prefix = reserved (e.g. _limit.ogg,
        # the quota announcement) — deleting those forces a paid re-synthesis
        # on every startup / SIGHUP / persona switch.
        wanted = {self._path_for(t).name for t in self._responses}
        if self._dir.exists():
            for f in self._dir.glob("*.ogg"):
                if f.name not in wanted and not f.name.startswith("_"):
                    try:
                        f.unlink()
                        logger.info(f"[fast-response] removed stale {f.name}")
                    except OSError:
                        pass

    async def get_random(self) -> Path | None:
        existing = [
            self._path_for(t)
            for t in self._responses
            if self._path_for(t).exists() and self._path_for(t).stat().st_size > 0
        ]
        if not existing:
            return None
        return random.choice(existing)
