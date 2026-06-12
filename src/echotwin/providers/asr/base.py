from __future__ import annotations
from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum
from typing import Awaitable, Callable, Optional


class ASRMode(Enum):
    BATCH = "batch"           # FunASR-like: feed all audio, infer at end_utterance
    STREAMING = "streaming"   # cloud providers


@dataclass
class ASRResult:
    text: str
    language: str = "zh"
    emotion: str = "NEUTRAL"  # NEUTRAL/HAPPY/SAD/ANGRY/FEARFUL/DISGUSTED/SURPRISED
    is_final: bool = True


class ASRProvider(ABC):
    mode: ASRMode

    @abstractmethod
    async def open(self) -> None:
        """Initialize connection / model."""

    @abstractmethod
    async def feed_audio(self, opus_packet: bytes) -> None:
        """Feed an Opus 48kHz mono frame from Discord."""

    @abstractmethod
    async def end_utterance(self) -> Optional[ASRResult]:
        """Signal end-of-speech. Returns final transcription, or None if empty."""

    async def on_partial(self, callback: Callable[[str], Awaitable[None]]) -> None:
        """Streaming providers: register callback for partial results.
        Default no-op for batch providers."""

    # --- Speculative decoding hooks (optional; default no-op) ---------
    # The watchdog pre-runs inference at ~300ms of silence; the result is
    # adopted directly at endpoint confirmation if no new audio arrived.
    def buffered_bytes(self) -> int:
        """Bytes currently buffered for the in-flight utterance."""
        return 0

    async def speculate(self) -> tuple[Optional[ASRResult], int]:
        """Run inference on the CURRENT buffer WITHOUT consuming it.

        Returns (result, fed_marker). Caller validates the speculation by
        comparing fed_marker with buffered_bytes() at endpoint time.
        result=None ⇒ skip speculation regardless of marker (unsupported,
        buffer too short, or inference failed).
        """
        return (None, -1)

    def drop_buffer(self) -> None:
        """Discard the buffered utterance audio (speculation was adopted)."""
        return None

    @abstractmethod
    async def close(self) -> None:
        """Cleanup resources."""

    async def preload(self) -> None:
        """Pre-load models / open connections at bot startup.
        Default no-op for cloud providers."""
