from __future__ import annotations
from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass
class VADResult:
    is_voice: bool                  # whether the current frame contains speech
    utterance_ended: bool           # utterance just ended (silence threshold reached)
    speech_started: bool = False    # rising edge: silent → speaking this frame


class VADProvider(ABC):
    @abstractmethod
    def feed(self, pcm_16k_16bit: bytes) -> VADResult:
        """Feed 16kHz int16 mono PCM bytes, get VAD result."""

    def reset(self) -> None:
        """Clear internal state."""
