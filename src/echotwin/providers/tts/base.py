from __future__ import annotations
from abc import ABC, abstractmethod
from typing import AsyncIterator


class TTSProvider(ABC):
    """Streaming TTS provider interface.

    Lifecycle:
        open() → push_text() multiple times → flush() / end_turn() → packets() drained → close()
    For barge-in, call cancel() to abort mid-utterance.
    """

    @abstractmethod
    async def open(self) -> None:
        """Open WS / connection, send start event."""

    @abstractmethod
    async def push_text(self, text: str) -> None:
        """Push text chunk for synthesis."""

    @abstractmethod
    async def flush(self) -> None:
        """Force synthesize buffered text now."""

    @abstractmethod
    async def end_turn(self) -> None:
        """Signal end of bot's turn (e.g. send 'stop')."""

    @abstractmethod
    def packets(self) -> AsyncIterator[bytes]:
        """Yield audio chunks (e.g. Ogg/Opus bytes) as they arrive."""

    @abstractmethod
    async def cancel(self) -> None:
        """Abort current synthesis (for barge-in)."""

    @abstractmethod
    async def close(self) -> None:
        """Cleanup all resources."""
