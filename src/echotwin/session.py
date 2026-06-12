"""Per-guild VoiceSession state."""
from __future__ import annotations

import asyncio
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional

import opuslib_next
from loguru import logger


class SessionState(Enum):
    IDLE = "IDLE"
    PROCESSING = "PROCESSING"
    SLEEPING = "SLEEPING"


@dataclass
class Utterance:
    user_id: int
    user_name: str
    text: str
    emotion: str = "NEUTRAL"
    asr_final_time: float = field(default_factory=time.time)
    # This turn's latency journey (LatencyJourney); None for untimed paths like fast-path
    journey: object | None = None
    # TTS pre-opened at endpoint enqueue (only when the queue is empty); dropped
    # utterances MUST be closed via bot._discard_utterance — leaking the Fish WS otherwise
    tts: object | None = None
    tts_open_task: object | None = None
    # Matched speculative LLM stream (SpeculativeLLM); likewise MUST be settled via _discard_utterance
    spec_llm: object | None = None


@dataclass
class VoiceSession:
    """Per-guild conversation state."""

    guild_id: int
    bot_id: int
    voice_channel_id: Optional[int] = None
    state: SessionState = SessionState.IDLE
    sentence_id: Optional[str] = None
    current_addressee_id: Optional[int] = None
    client_abort: bool = False
    is_audible: bool = False  # True only when bot is actually playing TTS audio
    # LOOP-clock timestamp (asyncio loop.time()), NOT wallclock — _check_idle
    # compares against loop.time(). Set by get_or_create_session at creation;
    # the 0.0 default alone would make a fresh session look idle-since-boot.
    last_activity_time: float = 0.0
    last_bot_speak_time: float = 0.0  # set when bot finishes a TTS turn (for continuation rule)
    last_addressee_id: int | None = None  # the user the bot was last replying to
    goodbye_pending: bool = False  # set after stage-1 farewell, cleared on activity

    # Conversation history (Claude messages format)
    dialogue: list[dict] = field(default_factory=list)

    # Emotion side-channel (streaming ASR mode): latest emotion backfilled by
    # SenseVoice in the background, lags one turn behind
    last_emotion: dict[int, str] = field(default_factory=dict)

    # --- Organic multi-user conversation state (used when organic.enabled) ---
    # Eavesdropping: also record non-addressed transcripts (injected into LLM context)
    ambient: deque = field(default_factory=lambda: deque(maxlen=30))
    # Active-conversation window: user_id → last participation time (time.time())
    organic_participants: dict[int, float] = field(default_factory=dict)
    # Gray-zone clarification: user_id → last clarify time (shared by the
    # 10s pending continuation + cooldown)
    clarify_pending_at: dict[int, float] = field(default_factory=dict)
    clarify_last_at: dict[int, float] = field(default_factory=dict)
    # The bot's most recent reply (topic-continuity signal) + timestamp + most
    # recent speaker ("bot" or user_id)
    last_bot_reply: str = ""
    last_bot_reply_ts: float = 0.0
    last_voice_event: object = None
    # Arbiter rate fuse: timestamps of recent arbiter calls (time.time())
    arbiter_calls: deque = field(default_factory=lambda: deque(maxlen=64))

    # Per-user opus decode counters. MUST be per-user: a session-global
    # counter gets polluted by concurrent speakers and the <600ms utterance
    # filter then drops real speech.
    opus_ok: dict[int, int] = field(default_factory=dict)
    opus_fail: dict[int, int] = field(default_factory=dict)

    # Per-user resources (created on first audio frame from that user)
    opus_decoders: dict[int, Any] = field(default_factory=dict)  # user_id → opuslib decoder
    resamplers: dict[int, Any] = field(default_factory=dict)
    vads: dict[int, Any] = field(default_factory=dict)
    asrs: dict[int, Any] = field(default_factory=dict)
    user_names: dict[int, str] = field(default_factory=dict)
    preroll_buffers: dict[int, Any] = field(default_factory=dict)  # user_id → PrerollRingBuffer

    # Utterance queue (FIFO of Utterance) shared across all users in this guild
    utterance_queue: asyncio.Queue = field(default_factory=asyncio.Queue)
    consumer_task: Optional[asyncio.Task] = None

    # Tracks empty-channel timeout
    alone_seconds: int = 0

    def new_turn(self) -> str:
        self.sentence_id = uuid.uuid4().hex
        self.client_abort = False
        return self.sentence_id

    async def abort(self) -> None:
        logger.info(f"[guild {self.guild_id}] aborting current turn")
        self.client_abort = True
        # Compare-and-set: only leave PROCESSING. An unconditional IDLE here
        # would silently undo a /sleep issued while a turn was in flight.
        if self.state == SessionState.PROCESSING:
            self.state = SessionState.IDLE
        self.current_addressee_id = None

    def trim_history(self, max_turns: int) -> None:
        """Keep most recent max_turns rounds (each round = user + assistant pair).

        The Anthropic Messages API rejects conversations whose first message
        is not from the user, so after slicing we drop any leading
        non-user messages (e.g. the orphaned assistant half of a split pair).
        """
        max_msgs = max_turns * 2
        if len(self.dialogue) > max_msgs:
            self.dialogue = self.dialogue[-max_msgs:]
        while self.dialogue and self.dialogue[0]["role"] != "user":
            self.dialogue.pop(0)
