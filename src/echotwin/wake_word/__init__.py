"""Wake-word fast path: detect short addresses ('一点点点?') and play cached TTS."""
from .matcher import WakeWordMatcher
from .fast_response import FastResponseCache

__all__ = ["WakeWordMatcher", "FastResponseCache"]
