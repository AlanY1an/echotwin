from __future__ import annotations

import asyncio
import os
import time as _time
from pathlib import Path

import discord
import opuslib_next
from discord import app_commands
from loguru import logger

from .audio.preroll_buffer import PrerollRingBuffer
from .audio.resampler import Resampler
from .config import Config
from .cost.tracker import CostTracker
from .persona import Persona, load_persona, render_system_prompt
from .pipeline.think_speak import respond_to_user
from .providers.factory import make_asr, make_llm, make_tts, make_vad
from .session import SessionState, Utterance, VoiceSession


# Default filler texts (used when the persona has no fillers configured; still synthesized in the persona voice)
from echotwin.i18n import prompts as _locale

# Back-compat aliases — the per-language tables live in i18n/prompts.py
DEFAULT_FILLERS = _locale.DEFAULT_FILLERS["zh"]


def _dave_stats_line(vc, user_ids: list[int]) -> str:
    """One diagnostic string for the [stats] log: DAVE epoch, MLS membership,
    per-user decryption counters and reader liveness.

    Designed to answer, after the fact: was the user's audio never delivered
    (attempts flat), delivered but undecryptable (failures rising), or did
    the whole receive thread die (reader=DEAD)? Never raises.
    """
    try:
        if vc is None:
            return "dave=no-voice-client"
        reader = getattr(vc, "_reader", None)
        reader_state = (
            "alive" if (reader is not None and getattr(reader, "active", False)) else "DEAD"
        )
        sess = getattr(getattr(vc, "_connection", None), "dave_session", None)
        if sess is None or not getattr(sess, "ready", False):
            return f"dave=not-ready reader={reader_state}"
        parts = [f"dave_epoch={getattr(sess, 'epoch', '?')}", f"reader={reader_state}"]
        try:
            parts.append(f"mls_users={len(list(sess.get_user_ids()))}")
        except Exception:
            pass
        for uid in user_ids:
            st = None
            for key in (uid, str(uid)):
                try:
                    st = sess.get_decryption_stats(key)
                    break
                except Exception:
                    continue
            if st is not None:
                parts.append(
                    f"u{uid}[att={getattr(st, 'attempts', '?')} "
                    f"ok={getattr(st, 'successes', '?')} "
                    f"fail={getattr(st, 'failures', '?')} "
                    f"pass={getattr(st, 'passthroughs', '?')}]"
                )
        return " ".join(parts)
    except Exception as e:  # noqa: BLE001 — diagnostics must never break the watchdog
        return f"dave=stats-error:{e!r}"


class VoiceAgentBot(discord.Client):
    def __init__(self, config: Config):
        intents = discord.Intents.default()
        intents.voice_states = True
        # message_content not needed - we only use slash commands
        intents.members = True   # needed for member.display_name, voice channel members list
        super().__init__(intents=intents)
        self.config = config
        self.tree = app_commands.CommandTree(self)
        self.app_owner_id: int | None = None
        self.extra_owner_ids: set[int] = set()
        self.sessions: dict[int, VoiceSession] = {}  # guild_id → session
        self.llm = None  # initialized in setup_hook
        self.cost_tracker: CostTracker | None = None
        self.start_time: float = _time.time()
        self._health_runner = None
        self._idle_watcher_task: asyncio.Task | None = None
        self._speech_watchdog_task: asyncio.Task | None = None
        # Persona is the source of truth for voice_id / wake_words
        self.persona: Persona = load_persona(
            Path("prompts") / "personas" / f"{config.bot.active_persona}.md"
        )
        # Addressee detector — instantiated in on_ready once we know our own user id
        self.addressee_detector = None
        # Tool registry — built in setup_hook
        self.tool_registry = None
        # Wake-word fast-path — built in setup_hook
        self.wake_matcher = None
        self.fast_cache = None
        # Quota guard — built in setup_hook
        self.quota_guard = None
        # Pre-synthesized "limit exceeded" audio path; populated lazily
        self.limit_audio_path = None

    def active_voice_id(self) -> str:
        """Effective voice id: config override (if set) wins over persona's."""
        return self.config.tts.fish_audio_stream.voice_id or self.persona.voice_id

    # Max automatic reader restarts per guild before giving up (reset on /join)
    _READER_RESTART_CAP = 5

    def start_listening(self, vc, guild_id: int, *, _is_restart: bool = False) -> None:
        """Attach a VoiceListener to the voice client WITH a death watch.

        voice_recv's AudioReader stops itself permanently on any feed_rtp
        exception; without an `after=` callback nobody notices and the bot
        sits in the channel deaf. On abnormal death we log loudly and
        re-listen (capped, to avoid a restart storm when the cause persists).
        """
        from .pipeline.listen import VoiceListener

        if not hasattr(self, "_reader_restarts"):
            self._reader_restarts = {}
        if not _is_restart:
            self._reader_restarts[guild_id] = 0

        listener = VoiceListener(
            bot_id=self.user.id,
            loop=self.loop,
            on_user_audio=lambda uid, uname, opus: self.on_user_audio(
                guild_id, uid, uname, opus
            ),
        )

        def on_reader_stop(error):
            # Called from the reader's stopper thread. error is None on a
            # clean stop (/leave, re-listen) — only restart on real death.
            if error is None:
                return
            logger.error(
                f"[listen] audio reader DIED for guild {guild_id}: {error!r}"
            )

            def _restart():
                cap = getattr(self, "_READER_RESTART_CAP", 5)
                count = self._reader_restarts.get(guild_id, 0)
                if count >= cap:
                    logger.error(
                        f"[listen] guild {guild_id}: reader died {count} times — "
                        f"giving up auto-restart; use /leave + /join to recover"
                    )
                    return
                self._reader_restarts[guild_id] = count + 1
                if not vc.is_connected():
                    logger.warning(
                        f"[listen] guild {guild_id}: reader died but voice client "
                        f"is disconnected; not restarting"
                    )
                    return
                try:
                    self.start_listening(vc, guild_id, _is_restart=True)
                    logger.info(
                        f"[listen] guild {guild_id}: reader restarted "
                        f"(attempt {count + 1}/{self._READER_RESTART_CAP})"
                    )
                except Exception as e:
                    logger.error(f"[listen] guild {guild_id}: reader restart failed: {e!r}")

            self.loop.call_soon_threadsafe(_restart)

        vc.listen(listener, after=on_reader_stop)

    async def _synth_with_persona(self, text: str) -> bytes:
        """Synthesize one short text via Fish Audio, return raw OGG/Opus bytes."""
        tts = make_tts(self.config, voice_id=self.active_voice_id(), persona=self.persona)
        await tts.open()
        await tts.push_text(text)
        await tts.flush()
        await tts.end_turn()
        chunks: list[bytes] = []
        async for c in tts.packets():
            chunks.append(c)
        await tts.close()
        if self.cost_tracker is not None:
            try:
                await self.cost_tracker.record("fishaudio_tts", len(text.encode("utf-8")))
            except Exception as e:
                logger.warning(f"[cost] record failed: {e}")
        return b"".join(chunks)

    async def _fast_path_play(self, voice_client, session, user_id, cached_path) -> None:
        """Play a cached short response and update last-bot-spoke timestamps."""
        import time as _t
        try:
            await self._play_cached_opus(voice_client, cached_path)
        except Exception as e:
            logger.warning(f"[fast-response] playback failed: {e}")
        session.last_bot_speak_time = _t.time()
        session.last_addressee_id = user_id
        session.last_activity_time = asyncio.get_event_loop().time()

    async def _play_cached_opus(self, voice_client, path) -> None:
        """Play a single ogg-opus file via the streaming audio source."""
        import queue as sq
        from .audio.audio_source import StreamingOpusAudioSource
        from .audio.ogg_demux import OggDemuxer

        if voice_client.is_playing():
            voice_client.stop()
            await asyncio.sleep(0.05)

        fq: sq.Queue = sq.Queue()
        demux = OggDemuxer()
        demux.feed(path.read_bytes())
        for pkt in demux.packets():
            fq.put_nowait(pkt)
        for pkt in demux.flush():
            fq.put_nowait(pkt)
        fq.put_nowait(None)

        source = StreamingOpusAudioSource(fq)
        done = asyncio.Event()
        voice_client.play(
            source,
            after=lambda e: self.loop.call_soon_threadsafe(done.set),
        )
        try:
            await asyncio.wait_for(done.wait(), timeout=10.0)
        except asyncio.TimeoutError:
            logger.warning("[fast-response] playback timeout")
            if voice_client.is_playing():
                voice_client.stop()

    async def sync_nickname_in_guild(self, guild: discord.Guild) -> None:
        """Set bot's nickname in this guild to match the active persona.

        Best-effort: if bot lacks 'Change Nickname' permission, log and move on.
        """
        if guild.me is None:
            return
        target = self.persona.name
        if guild.me.nick == target:
            return
        try:
            await guild.me.edit(nick=target)
            logger.info(f"[nickname] guild {guild.id}: set nick to {target!r}")
        except discord.Forbidden:
            logger.warning(
                f"[nickname] guild {guild.id}: bot lacks 'Change Nickname' permission"
            )
        except Exception as e:
            logger.warning(f"[nickname] guild {guild.id}: failed: {e}")

    async def clear_nickname_in_guild(self, guild: discord.Guild) -> None:
        """Reset bot's nickname in this guild back to its default app name."""
        if guild.me is None or guild.me.nick is None:
            return
        try:
            await guild.me.edit(nick=None)
            logger.info(f"[nickname] guild {guild.id}: reset nick to default")
        except discord.Forbidden:
            logger.warning(
                f"[nickname] guild {guild.id}: bot lacks 'Change Nickname' permission"
            )
        except Exception as e:
            logger.warning(f"[nickname] guild {guild.id}: reset failed: {e}")

    async def sync_nickname_in_active_guilds(self) -> None:
        """Update nick only in guilds where the bot is currently in a voice channel."""
        for g in self.guilds:
            if g.voice_client is not None:
                await self.sync_nickname_in_guild(g)

    async def setup_hook(self) -> None:
        # Cost tracker
        self.cost_tracker = CostTracker(self.config.cost.store_path)
        await self.cost_tracker.init()

        # Quota guard — blocks new turns when daily/monthly cap is hit (in shutdown mode)
        from .utils.quota import QuotaGuard
        self.quota_guard = QuotaGuard(
            self.cost_tracker,
            daily_usd=self.config.cost.daily_budget_usd,
            monthly_usd=self.config.cost.monthly_budget_usd,
            on_exceed=self.config.cost.on_exceed,
        )

        # Initialize LLM
        self.llm = make_llm(self.config)

        # Dedicated LLM for organic gray-zone arbitration (optional; missing key auto-falls back to the conversation LLM)
        from .providers.factory import make_arbiter_llm
        self.arbiter_llm = None
        self.arbiter_cost_prefix = "claude_haiku_4_5"
        arb = make_arbiter_llm(self.config)
        if arb is not None:
            self.arbiter_llm, self.arbiter_cost_prefix = arb
            logger.info(f"[arbiter] using {self.arbiter_llm.model} (Groq)")
        elif getattr(self.config.bot.organic, "arbiter_provider", ""):
            logger.warning(
                "[arbiter] arbiter_provider 已配置但缺 GROQ_API_KEY — 复用对话 LLM"
            )

        # Tool registry — built once at startup based on config.tools.enabled
        from .tools.registry import ToolRegistry
        from .tools.get_time import GetTime
        from .tools.get_date import GetDate
        from .tools.get_weather import GetWeather

        self.tool_registry = ToolRegistry()
        enabled = set(self.config.tools.enabled)
        tz = self.config.tools.default_timezone
        # Tool output language follows the active persona so an English persona
        # gets English weather/time strings, not the zh defaults.
        tool_lang = getattr(self.persona, "language", "zh") if getattr(self, "persona", None) else "zh"
        if "get_time" in enabled:
            self.tool_registry.register(GetTime(default_timezone=tz, lang=tool_lang))
        if "get_date" in enabled:
            self.tool_registry.register(GetDate(default_timezone=tz, lang=tool_lang))
        if "get_weather" in enabled:
            # wttr.in is keyless; no env var needed
            self.tool_registry.register(
                GetWeather(default_city=self.config.tools.get_weather.default_city, lang=tool_lang)
            )

        # Load runtime config FIRST (overrides config.yaml for persona/voice/
        # wakeword) — persona-derived resources below must be built from the
        # runtime-selected persona, not the yaml one.
        from .commands.owner_dm import load_runtime_config
        load_runtime_config(self)

        # Wake matcher + fast-response cache + quota-limit audio for the
        # (possibly runtime-overridden) active persona.
        await self._init_persona_resources()

        # Pre-load ASR model if local (FunASR loads ~5-10s on CPU)
        asr_test = make_asr(self.config, language=self.persona.language)
        if hasattr(asr_test, "preload"):
            try:
                await asr_test.preload()
            except Exception as e:
                logger.warning(f"ASR preload failed (will retry on first use): {e}")

        # Register slash commands
        from .commands.public import register_public_commands
        from .commands.owner_dm import register_owner_commands
        register_public_commands(self.tree, self)
        register_owner_commands(self.tree, self)

        # Health server
        from .monitoring.health_server import start_health_server
        try:
            self._health_runner = await start_health_server(self, self.config.monitoring.http_port)
        except Exception as e:
            logger.warning(f"health server failed to start: {e}")

        from .i18n import VoiceAgentTranslator
        await self.tree.set_translator(VoiceAgentTranslator())

        import os
        test_guild_id = os.environ.get("TEST_GUILD_ID")
        if test_guild_id:
            guild = discord.Object(id=int(test_guild_id))
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
            logger.info(f"Slash commands synced to test guild {test_guild_id}")
        else:
            await self.tree.sync()
            logger.info("Slash commands synced globally (may take up to 1 hour)")

    def _filler_paths(self) -> list:
        """Expected filler file paths for the CURRENT persona + effective voice.

        Computed from texts (never globbed): stale files from old fillers or a
        cleared voice override must not be picked up — the `_` prefix lets them
        survive warm-up cleanup, so a glob would keep randomly picking them forever.
        """
        import hashlib
        texts = self.persona.fillers or _locale.DEFAULT_FILLERS[self.persona.language]
        d = Path("data") / "wake_responses" / self.persona.id
        voice = self.active_voice_id()
        return [
            d / f"_filler_{hashlib.sha1(f'{voice}:{t}'.encode('utf-8')).hexdigest()[:12]}.ogg"
            for t in texts
        ]

    async def _ensure_filler_audio(self) -> None:
        """Synthesize missing filler audio for the current persona (best-effort)."""
        texts = self.persona.fillers or _locale.DEFAULT_FILLERS[self.persona.language]
        for text, p in zip(texts, self._filler_paths()):
            if p.exists() and p.stat().st_size > 0:
                continue
            p.parent.mkdir(parents=True, exist_ok=True)
            try:
                audio = await self._synth_with_persona(text)
                if audio:
                    p.write_bytes(audio)
                    logger.info(f"[filler] synthesized {text!r} -> {p.name}")
            except Exception as e:
                logger.warning(f"[filler] synth failed for {text!r}: {e}")

    def pick_filler_path(self):
        import random
        cands = [p for p in self._filler_paths() if p.exists() and p.stat().st_size > 0]
        return random.choice(cands) if cands else None

    async def _init_persona_resources(self) -> None:
        """Build wake matcher, fast-response cache and quota-limit audio
        from the CURRENT self.persona. Must run after load_runtime_config
        so a runtime persona override is reflected in these resources."""
        from .wake_word import WakeWordMatcher, FastResponseCache

        # Per-user ASR instances are language-specific (the streaming model is
        # auto-selected from persona.language) — drop them so the next audio
        # frame recreates them with the right model after a persona switch.
        for session in getattr(self, "sessions", {}).values():
            session.asrs.clear()

        self.wake_matcher = WakeWordMatcher(wake_words=self.persona.wake_words)
        self.fast_cache = FastResponseCache(
            persona_id=self.persona.id,
            voice_id=self.persona.voice_id,
            responses=self.persona.fast_responses,
            data_dir=Path("data"),
        )
        # Synthesize missing fast-response audio (best-effort; bot still works without)
        try:
            await self.fast_cache.ensure_synthesized(self._synth_with_persona)
        except Exception as e:
            logger.warning(f"[fast-response] cache warm-up failed: {e}")

        # Pre-synthesize quota-limit announcement (one-shot; only re-synth if persona changes)
        try:
            limit_dir = Path("data") / "wake_responses" / self.persona.id
            limit_dir.mkdir(parents=True, exist_ok=True)
            self.limit_audio_path = limit_dir / "_limit.ogg"
            if not self.limit_audio_path.exists() or self.limit_audio_path.stat().st_size == 0:
                audio = await self._synth_with_persona(self.persona.limit_exceeded_text)
                if audio:
                    self.limit_audio_path.write_bytes(audio)
                    logger.info(f"[quota] limit message cached at {self.limit_audio_path}")
        except Exception as e:
            logger.warning(f"[quota] limit-message cache failed: {e}")

        # Pre-synthesize filler phrases (best-effort)
        try:
            await self._ensure_filler_audio()
        except Exception as e:
            logger.warning(f"[filler] cache warm-up failed: {e}")

        # Pre-synthesize organic clarify phrases (best-effort)
        try:
            await self._ensure_clarify_audio()
        except Exception as e:
            logger.warning(f"[organic] clarify warm-up failed: {e}")

    async def on_ready(self):
        assert self.user is not None
        logger.info(f"Bot logged in as {self.user} (id={self.user.id})")
        logger.info(f"In {len(self.guilds)} guild(s)")
        try:
            app_info = await self.application_info()
            self.app_owner_id = app_info.owner.id if app_info.owner else None
            logger.info(f"App owner id: {self.app_owner_id}")
        except Exception as e:
            logger.warning(f"Could not fetch app owner: {e}")

        # Build addressee detector now that bot's own user id is known
        from .pipeline.addressee import AddresseeDetector
        self.addressee_detector = AddresseeDetector(
            persona=self.persona,
            bot_user_id=self.user.id,
            continuation_window_seconds=self.config.addressee.continuation_window_seconds,
            solo_channel_auto=self.config.addressee.solo_channel_auto,
        )

        # Start idle watcher (auto-leave detection) + heartbeat
        if self._idle_watcher_task is None or self._idle_watcher_task.done():
            self._idle_watcher_task = self.loop.create_task(self._idle_watcher_loop())
        self.loop.create_task(self._heartbeat_loop())
        # Speech endpoint watchdog (the only endpoint mechanism)
        if self._speech_watchdog_task is None or self._speech_watchdog_task.done():
            self._speech_watchdog_task = self.loop.create_task(self._speech_watchdog_loop())

        # SIGHUP-driven hot reload of config + persona
        from .config_watcher import ConfigWatcher
        self.config_watcher = ConfigWatcher(self, "config.yaml")
        self.config_watcher.install()
        # Note: nickname is set on /join and reset on /leave — not on startup,
        # since the bot isn't in any voice channel yet.

    async def on_disconnect(self):
        logger.warning("Bot disconnected from Discord")

    # ------------------------------------------------------------
    # Voice session management
    # ------------------------------------------------------------

    def get_or_create_session(self, guild_id: int) -> VoiceSession:
        if guild_id not in self.sessions:
            assert self.user is not None
            session = VoiceSession(guild_id=guild_id, bot_id=self.user.id)
            # Loop clock, NOT time.time() — _check_idle compares loop.time().
            # Mixing clocks made idle timeouts never fire for silent sessions.
            session.last_activity_time = self.loop.time()
            self.sessions[guild_id] = session
            # Start consumer task
            session.consumer_task = self.loop.create_task(
                self._consumer_loop(guild_id), name=f"consumer-{guild_id}"
            )
        return self.sessions[guild_id]

    async def cleanup_session(self, guild_id: int) -> None:
        session = self.sessions.pop(guild_id, None)
        if not session:
            return
        if session.consumer_task and not session.consumer_task.done():
            session.consumer_task.cancel()
        # Cancel pending speculative ASR tasks
        for user_id in list(session.asrs.keys()):
            self._cancel_spec_asr(session, user_id)
        # Drain queued utterances — their pre-opened TTS sockets must close
        try:
            while True:
                self._discard_utterance(session.utterance_queue.get_nowait())
        except asyncio.QueueEmpty:
            pass
        # Close ASR sessions
        for asr in session.asrs.values():
            try:
                await asr.close()
            except Exception:
                pass

    # ------------------------------------------------------------
    # Audio receive callback (called from VoiceListener via run_coroutine_threadsafe)
    # ------------------------------------------------------------

    async def on_user_audio(self, guild_id: int, user_id: int, user_name: str, opus_bytes: bytes) -> None:
        """Decode Discord's stereo opus → mono PCM, then feed VAD + ASR.

        Whitelist gate: when bot.config.bot.listen_only_users is non-empty,
        ignore every other user. Empty list = listen to everyone.

        Endpoint detection lives entirely in _speech_watchdog_loop (wallclock
        timer). This method only:
          - drops 3-byte OPUS_SILENCE sentinels (useless for endpointing AND
            for content — they carry no audio info)
          - on real packets: stamps last_real_audio_time, decodes, feeds VAD
            for noise gating, feeds ASR
          - marks user as 'in speech' on the FIRST real packet of an utterance
            (we don't trust VAD speech_started — it misses transitions when
            Discord stops sending packets entirely between bursts).
        """
        self._trace_packet(user_id, "real" if len(opus_bytes) >= 4 else "sentinel", len(opus_bytes))
        # Whitelist gate: when listen_only_users is non-empty, drop everything
        # from other users (cheap — runs before any decoding work).
        wl = self.config.bot.listen_only_users
        if wl and user_id not in wl:
            # Log once PER USER — a single process-wide log made "the bot
            # can't hear person X" nearly undiagnosable when the whitelist
            # was the cause.
            logged: set = getattr(self, "_wl_skip_logged_users", set())
            if user_id not in logged:
                logger.info(
                    f"[whitelist] ignoring user {user_id} ({user_name}) — "
                    f"only listening to {wl}"
                )
                logged.add(user_id)
                self._wl_skip_logged_users = logged
            return
        session = self.get_or_create_session(guild_id)
        if session.state == SessionState.SLEEPING:
            return

        # Sentinels: nothing to do. They don't tell us anything useful —
        # Discord sends them sporadically (~3/sec) at the START or END of a
        # silence period, but during sustained silence Discord may send
        # NOTHING at all (verified empirically: 17s gaps observed). Endpoint
        # detection runs on a wallclock timer in _speech_watchdog_loop.
        if len(opus_bytes) < 4:
            return

        session.user_names[user_id] = user_name
        session.last_activity_time = asyncio.get_event_loop().time()
        # Stamp wallclock of last REAL audio packet — read by watchdog
        setattr(session, f"_last_real_audio_{user_id}", _time.time())
        # Any audio activity cancels a pending goodbye
        if session.goodbye_pending:
            logger.info(f"[idle] activity resumed during goodbye grace — staying")
            session.goodbye_pending = False

        # Stereo opus decoder, generous frame buffer (handles 60ms frames)
        if user_id not in session.opus_decoders:
            session.opus_decoders[user_id] = opuslib_next.Decoder(48000, 2)
        try:
            pcm_48k_stereo = session.opus_decoders[user_id].decode(opus_bytes, 5760)
            session.opus_ok[user_id] = session.opus_ok.get(user_id, 0) + 1
            if session.opus_ok[user_id] % 100 == 0:
                logger.info(
                    f"[diag] opus user={user_id} ok={session.opus_ok[user_id]} "
                    f"fail={session.opus_fail.get(user_id, 0)}"
                )
        except opuslib_next.OpusError as e:
            session.opus_fail[user_id] = session.opus_fail.get(user_id, 0) + 1
            if session.opus_fail[user_id] % 50 == 1:
                logger.warning(
                    f"[diag] opus decode fail user={user_id} "
                    f"(count={session.opus_fail[user_id]}): {e} (len={len(opus_bytes)})"
                )
            return

        import numpy as np
        stereo = np.frombuffer(pcm_48k_stereo, dtype=np.int16)
        if stereo.size == 0 or stereo.size % 2 != 0:
            return
        mono_arr = stereo.reshape(-1, 2).mean(axis=1).astype(np.int16)
        pcm_48k_mono = mono_arr.tobytes()

        # Amplitude diagnostic: log RMS every 100 frames
        session._amp_count = getattr(session, "_amp_count", 0) + 1
        if session._amp_count % 100 == 0:
            rms = float(np.sqrt(np.mean(mono_arr.astype(np.float32) ** 2)))
            peak = int(np.abs(mono_arr).max())
            logger.info(f"[amp] frames={session._amp_count} rms={rms:.0f} peak={peak}")

        # Per-user resampler 48k→16k
        if user_id not in session.resamplers:
            session.resamplers[user_id] = Resampler(48000, 16000)
        pcm_16k = session.resamplers[user_id].feed(pcm_48k_mono)

        # Per-user pre-roll buffer (~300ms of 48k mono frames before VAD trigger).
        # Push BEFORE VAD inference so the buffer always contains the most recent
        # frames including the current one.
        if user_id not in session.preroll_buffers:
            preroll_ms = self.config.vad.silero.preroll_ms
            # Discord frames are ~20ms; one entry per 20ms frame
            max_frames = max(1, preroll_ms // 20)
            session.preroll_buffers[user_id] = PrerollRingBuffer(max_frames=max_frames)
        session.preroll_buffers[user_id].push(pcm_48k_mono)

        # Per-user VAD — used ONLY for noise gating (decide whether to feed ASR),
        # NOT for endpointing. Endpoint = wallclock watchdog.
        if user_id not in session.vads:
            session.vads[user_id] = make_vad(self.config)
        vad_result = session.vads[user_id].feed(pcm_16k)

        # First real packet of a new utterance → mark in_speech, snapshot
        # counters, drain pre-roll into ASR head. We use packet-arrival, not
        # vad.speech_started, because VAD misses START transitions when
        # Discord stops sending packets entirely between speech bursts.
        in_speech = getattr(session, f"_in_speech_{user_id}", False)
        if not in_speech:
            logger.info(f"[utt] user {user_id} START (first real packet)")
            self._trace_event(user_id, "utt_start")
            self._cancel_spec_asr(session, user_id)  # stale speculation from last utterance
            stale_spec_llm = getattr(session, f"_spec_llm_{user_id}", None)
            if stale_spec_llm is not None:
                setattr(session, f"_spec_llm_{user_id}", None)
                self._abort_spec_llm(stale_spec_llm)  # user resumed → speculation void
            setattr(session, f"_in_speech_{user_id}", True)
            setattr(session, f"_utt_opus_ok_{user_id}", session.opus_ok.get(user_id, 0))
            setattr(session, f"_utt_opus_fail_{user_id}", session.opus_fail.get(user_id, 0))

        # Per-user ASR (lazy init on first voice frame)
        if user_id not in session.asrs:
            asr = make_asr(self.config, language=self.persona.language)
            await asr.open()
            session.asrs[user_id] = asr

        # Feed ASR. On the first frame after START, drain pre-roll first
        # so the leading ~300ms of audio isn't lost. Gate by VAD.is_voice
        # to filter background noise (still useful even though VAD doesn't
        # drive endpointing).
        if vad_result.is_voice or in_speech:
            # Always drain preroll once at start of utterance
            preroll_bytes = getattr(session, f"_preroll_drained_{user_id}", False)
            if not preroll_bytes:
                head = session.preroll_buffers[user_id].drain()
                if head:
                    await session.asrs[user_id].feed_audio(head)
                else:
                    await session.asrs[user_id].feed_audio(pcm_48k_mono)
                setattr(session, f"_preroll_drained_{user_id}", True)
            else:
                await session.asrs[user_id].feed_audio(pcm_48k_mono)

    # --- Diagnostic trace (toggle via env VOICE_AGENT_TRACE=1) -----------

    _TRACE_ENABLED = bool(os.environ.get("VOICE_AGENT_TRACE", ""))
    _TRACE_PATH = Path("data") / "packet_trace.log"

    def _trace_packet(self, user_id: int, kind: str, size: int) -> None:
        if not VoiceAgentBot._TRACE_ENABLED:
            return
        try:
            self._TRACE_PATH.parent.mkdir(parents=True, exist_ok=True)
            with open(self._TRACE_PATH, "a") as f:
                f.write(f"{_time.time():.4f}\t{user_id}\tpacket\t{kind}\t{size}\n")
        except Exception:
            pass

    def _trace_event(self, user_id: int, event: str, info: str = "") -> None:
        if not VoiceAgentBot._TRACE_ENABLED:
            return
        try:
            self._TRACE_PATH.parent.mkdir(parents=True, exist_ok=True)
            with open(self._TRACE_PATH, "a") as f:
                f.write(f"{_time.time():.4f}\t{user_id}\tevent\t{event}\t{info}\n")
        except Exception:
            pass

    # NOTE: on_user_speaking_stop removed — voice_recv's
    # voice_member_speaking_stop event proved unreliable in our setup
    # (verified empirically: did not fire even after 17s of zero packets).
    # Endpointing now lives in _speech_watchdog_loop only.

    async def _speech_watchdog_loop(self) -> None:
        """Wallclock-based endpoint detector + periodic per-user stat log.

        Tick / silence threshold come from config.bot.endpoint_tick_ms /
        endpoint_silence_ms (defaults 100ms / 600ms), read each tick so a
        SIGHUP config reload applies live. Also: every 5s, log per-user
        packet stats to make "Discord stopped sending" vs "our code
        dropped" diagnosable without VOICE_AGENT_TRACE.
        """
        STATS_INTERVAL_S = 5.0
        last_stats_at = _time.time()

        while not self.is_closed():
            try:
                silence_threshold_s = self.config.bot.endpoint_silence_ms / 1000.0
                tick_s = self.config.bot.endpoint_tick_ms / 1000.0
                await asyncio.sleep(tick_s)
                now = _time.time()

                for guild_id, session in list(self.sessions.items()):
                    candidate_uids = list(session.asrs.keys())
                    for user_id in candidate_uids:
                        in_speech = getattr(session, f"_in_speech_{user_id}", False)
                        if not in_speech:
                            continue
                        last_real = getattr(session, f"_last_real_audio_{user_id}", 0.0)
                        if last_real == 0:
                            continue
                        since_real = now - last_real
                        # Speculative ASR: at ~300ms of silence (well before the
                        # endpoint) pre-run inference on the buffered audio. The
                        # finalize path adopts the result iff no new audio
                        # arrived (fed-marker check). Gate on ≥25 packets so a
                        # noise blip doesn't burn an inference the <600ms filter
                        # would drop anyway.
                        if (
                            self.config.bot.speculative_asr
                            and since_real >= self.config.bot.speculative_asr_silence_ms / 1000.0
                            and getattr(session, f"_spec_task_{user_id}", None) is None
                            and user_id in session.asrs
                            and session.opus_ok.get(user_id, 0)
                            - getattr(session, f"_utt_opus_ok_{user_id}", 0) >= 25
                        ):
                            self._spawn_spec_asr(session, user_id)
                        # Speculative LLM (Phase 2, default off): same silence
                        # window, requires a streaming ASR with drained pipeline
                        if (
                            self.config.bot.speculative_llm
                            and since_real >= self.config.bot.speculative_asr_silence_ms / 1000.0
                            and getattr(session, f"_spec_llm_{user_id}", None) is None
                            and user_id in session.asrs
                        ):
                            self._maybe_spawn_spec_llm(session, guild_id, user_id)
                        if since_real >= silence_threshold_s:
                            user_name = session.user_names.get(user_id, str(user_id))
                            logger.info(
                                f"[watchdog] user {user_id}: no real audio "
                                f"in {since_real:.2f}s — finalizing utterance"
                            )
                            self._trace_event(user_id, "watchdog_endpoint", f"silent_for={since_real:.2f}s")
                            setattr(session, f"_in_speech_{user_id}", False)
                            setattr(session, f"_preroll_drained_{user_id}", False)
                            if user_id in session.vads:
                                session.vads[user_id].reset()
                            # Drop the ~300ms tail of THIS utterance so it isn't
                            # prepended to the next one (corrupts the wake word
                            # at the head of the next transcript).
                            if user_id in session.preroll_buffers:
                                session.preroll_buffers[user_id].clear()
                            # Must run AFTER the in_speech/preroll reset above —
                            # _spawn_finalize relies on that ordering for its
                            # snapshot and deferral logic.
                            self._spawn_finalize(session, guild_id, user_id, user_name)

                # Periodic stats: per-user packet counts + in_speech state
                if now - last_stats_at >= STATS_INTERVAL_S:
                    last_stats_at = now
                    for guild_id, session in list(self.sessions.items()):
                        guild_obj = self.get_guild(guild_id)
                        vc = guild_obj.voice_client if guild_obj else None
                        active_uids = [
                            u for u in session.user_names
                            if not (self.user and u == self.user.id)
                        ]
                        logger.info(
                            f"[stats] guild {guild_id}: "
                            f"{_dave_stats_line(vc, active_uids)}"
                        )
                        for user_id in list(session.user_names.keys()):
                            if user_id == self.user.id if self.user else False:
                                continue
                            last_real = getattr(session, f"_last_real_audio_{user_id}", 0.0)
                            in_speech = getattr(session, f"_in_speech_{user_id}", False)
                            since_real = (now - last_real) if last_real else float("inf")
                            ok = session.opus_ok.get(user_id, 0)
                            fail = session.opus_fail.get(user_id, 0)
                            prev_ok = getattr(session, f"_stats_prev_ok_{user_id}", 0)
                            prev_fail = getattr(session, f"_stats_prev_fail_{user_id}", 0)
                            ok_5s = ok - prev_ok
                            fail_5s = fail - prev_fail
                            setattr(session, f"_stats_prev_ok_{user_id}", ok)
                            setattr(session, f"_stats_prev_fail_{user_id}", fail)
                            logger.info(
                                f"[stats] user {user_id}: real_ok={ok_5s} fail={fail_5s} "
                                f"(last 5s)  in_speech={in_speech}  "
                                f"last_real={since_real:.1f}s ago  "
                                f"is_audible={session.is_audible}  state={session.state.value}"
                            )
            except asyncio.CancelledError:
                return
            except Exception as e:
                logger.warning(f"speech watchdog error: {e}")

    def _spawn_spec_asr(self, session, user_id: int) -> None:
        """Fire a speculative ASR inference as a tracked task (strong ref +
        exception retrieval — see the GC note in _spawn_finalize)."""
        if not hasattr(self, "_spec_tasks"):
            self._spec_tasks = set()
        task = self.loop.create_task(session.asrs[user_id].speculate())
        setattr(session, f"_spec_task_{user_id}", task)
        self._spec_tasks.add(task)

        def _done(t: asyncio.Task) -> None:
            self._spec_tasks.discard(t)
            if not t.cancelled() and t.exception() is not None:
                logger.warning(f"[asr] speculation task failed: {t.exception()!r}")

        task.add_done_callback(_done)

    def _ambient_note(self, session, user_id: int, user_name: str, text: str) -> None:
        """Eavesdrop: non-addressed utterances are also recorded into scene context (organic infrastructure)."""
        session.ambient.append({"speaker": user_name, "text": text, "ts": _time.time()})
        session.last_voice_event = user_id

    def _arm_open_floor(
        self, session, guild_id: int, user_id: int, user_name: str,
        text: str, emotion: str, wait_ms: int,
    ) -> None:
        """Open-floor self-selection: wait wait_ms, yielding if any other real
        user speaks meanwhile; if nobody takes the floor, pick it up."""

        async def _wait():
            deadline = self.loop.time() + wait_ms / 1000.0
            while self.loop.time() < deadline:
                for uid in list(session.user_names.keys()):
                    if uid != user_id and not (self.user and uid == self.user.id):
                        if getattr(session, f"_in_speech_{uid}", False):
                            logger.info("[organic] open floor — a human took it, yielding")
                            return
                await asyncio.sleep(0.1)
            logger.info(f"[organic] open floor unclaimed — self-selecting: {text!r}")
            from echotwin.utils.latency import LatencyJourney
            journey = LatencyJourney("open_floor")
            now = _time.time()
            session.organic_participants[user_id] = now
            pre_tts = None
            pre_open = None
            if session.utterance_queue.empty() and session.state != SessionState.PROCESSING:
                try:
                    pre_tts = make_tts(
                        self.config, voice_id=self.active_voice_id(), persona=self.persona
                    )
                    pre_open = self.loop.create_task(pre_tts.open())
                except Exception as e:
                    logger.warning(f"[organic] open-floor pre-open failed: {e}")
                    pre_tts, pre_open = None, None
            self._dequeue_user(session, user_id)
            await session.utterance_queue.put(
                Utterance(
                    user_id=user_id, user_name=user_name, text=text,
                    emotion=emotion, journey=journey,
                    tts=pre_tts, tts_open_task=pre_open,
                )
            )

        if not hasattr(self, "_open_floor_tasks"):
            self._open_floor_tasks = set()
        t = self.loop.create_task(_wait())
        self._open_floor_tasks.add(t)
        t.add_done_callback(self._open_floor_tasks.discard)

    def _clarify_via_llm(
        self, session, guild_id: int, user_id: int, user_name: str,
        text: str, emotion: str,
    ) -> None:
        """Phase-2 hook (default off): for gray-zone utterances, ask the LLM once
        whether it was addressed to the bot; if yes, pick it up belatedly."""

        async def _ask():
            try:
                lang = self.persona.language
                prompt = _locale.CLARIFY_LLM_PROMPT[lang].format(
                    user=user_name, text=text, bot=self.persona.name,
                    last=session.last_bot_reply[:60],
                )
                answer = ""
                from .providers.llm.base import TextDelta
                async for ev in self.llm.stream_chat(
                    _locale.CLARIFY_LLM_SYSTEM[lang],
                    [{"role": "user", "content": prompt}],
                ):
                    if isinstance(ev, TextDelta):
                        answer += ev.text
                        if len(answer) >= 3:
                            break
                logger.info(f"[organic] clarify_llm verdict={answer.strip()!r} for {text!r}")
                # Language-neutral parse: models may answer 是/yes regardless of prompt language
                if answer.strip().lower().startswith(("是", "yes")):
                    self._arm_open_floor(
                        session, guild_id, user_id, user_name, text, emotion, 0
                    )
            except Exception as e:
                logger.warning(f"[organic] clarify_llm failed: {e!r}")

        if not hasattr(self, "_clarify_llm_tasks"):
            self._clarify_llm_tasks = set()
        t = self.loop.create_task(_ask())
        self._clarify_llm_tasks.add(t)
        t.add_done_callback(self._clarify_llm_tasks.discard)

    # Default gray-zone clarification texts (when organic.clarify_texts is empty)
    DEFAULT_CLARIFY = _locale.DEFAULT_CLARIFY["zh"]

    def _clarify_paths(self) -> list:
        import hashlib
        cfg = getattr(self.config.bot, "organic", None)
        texts = (cfg.clarify_texts if cfg and cfg.clarify_texts else None) or list(
            _locale.DEFAULT_CLARIFY[self.persona.language]
        )
        d = Path("data") / "wake_responses" / self.persona.id
        voice = self.active_voice_id()
        return [
            (t, d / f"_clarify_{hashlib.sha1(f'{voice}:{t}'.encode()).hexdigest()[:12]}.ogg")
            for t in texts
        ]

    async def _ensure_clarify_audio(self) -> None:
        for text, p in self._clarify_paths():
            if p.exists() and p.stat().st_size > 0:
                continue
            p.parent.mkdir(parents=True, exist_ok=True)
            try:
                audio = await self._synth_with_persona(text)
                if audio:
                    p.write_bytes(audio)
                    logger.info(f"[organic] clarify synthesized {text!r}")
            except Exception as e:
                logger.warning(f"[organic] clarify synth failed: {e}")

    def pick_clarify_path(self):
        import random
        cands = [p for _, p in self._clarify_paths() if p.exists() and p.stat().st_size > 0]
        return random.choice(cands) if cands else None

    def _spawn_emotion_sidecar(self, session, user_id: int, pcm_48k: bytes) -> None:
        """SenseVoice side-channel emotion backfill (streaming ASR has no emotion tags).

        Doesn't block the reply; the result is written to session.last_emotion
        and only takes effect on the **next** turn — this turn's LLM message is
        already sent before the side-channel finishes. The class-level inference
        lock keeps it serialized with other generate calls."""
        if not pcm_48k:
            return
        if not hasattr(self, "_sidecar_asr"):
            from .providers.asr.funasr_local import FunASRLocal
            f = self.config.asr.funasr_local
            self._sidecar_asr = FunASRLocal(
                model_dir=f.model_dir, device=f.device, language=f.language
            )

        async def _run():
            import time as _t
            t0 = _t.monotonic()
            try:
                await self._sidecar_asr.preload()
                await self._sidecar_asr.feed_audio(pcm_48k)
                result = await self._sidecar_asr.end_utterance()
                if result is not None:
                    session.last_emotion[user_id] = result.emotion
                    logger.info(
                        f"[emotion-sidecar] uid={user_id} emotion={result.emotion} "
                        f"took={int((_t.monotonic() - t0) * 1000)}ms"
                    )
            except Exception as e:
                logger.warning(f"[emotion-sidecar] failed: {e!r}")

        if not hasattr(self, "_sidecar_tasks"):
            self._sidecar_tasks = set()
        t = self.loop.create_task(_run())
        self._sidecar_tasks.add(t)
        t.add_done_callback(self._sidecar_tasks.discard)

    def _build_organic_ctx(self, session, user_id: int, channel_member_count: int):
        """Build the organic classification context (shared by finalize and the
        speculative trigger, kept consistent)."""
        from .pipeline.organic import OrganicContext

        organic_cfg = self.config.bot.organic
        now = _time.time()
        return OrganicContext(
            wake_words=list(self.persona.wake_words),
            in_window=(
                now - session.last_bot_reply_ts < organic_cfg.conversation_window_s
                or now - session.organic_participants.get(user_id, 0)
                < organic_cfg.conversation_window_s
            ),
            solo=(channel_member_count == 2),
            last_bot_text=session.last_bot_reply,
            last_speaker_was_bot=(session.last_voice_event == "bot"),
            others_present=[
                n for uid, n in session.user_names.items()
                if uid != user_id and not (self.user and uid == self.user.id)
            ],
            clarify_pending=(now - session.clarify_pending_at.get(user_id, 0) < 10),
        )

    def _maybe_spawn_spec_llm(self, session, guild_id: int, user_id: int) -> None:
        """Speculative LLM trigger (streaming ASR only, config-gated).

        Review constraints: the partial must come from a drained chunk pipeline
        (the tick-stability criterion is near-always-true, unreliable);
        user_text goes through strip_wake_word first; the dialogue length goes
        into the snapshot; tools must be passed through. At most once per utterance."""
        try:
            asr = session.asrs.get(user_id)
            if asr is None or not hasattr(asr, "partial_text"):
                return
            if not getattr(asr, "pipeline_drained", lambda: False)():
                return
            raw = asr.partial_text()
            if not raw or not raw.strip():
                return
            raw = raw.strip()
            # Addressee pre-check gate (live logs showed every line of
            # person-to-person chatter wastefully opening a paid stream — even
            # a bare "是") — the partial goes through the same addressee check
            # as finalize; no speculation for what won't be picked up.
            if len(raw) < 4:
                return
            guild0 = self.get_guild(guild_id)
            vc0 = guild0.voice_client if guild0 else None
            members0 = len(vc0.channel.members) if vc0 and vc0.channel else 1
            organic_cfg = getattr(self.config.bot, "organic", None)
            if organic_cfg is not None and organic_cfg.enabled:
                # Speculate only on hard accepts (wake word / solo) — the gray
                # zone waits for LLM arbitration, it's uncertain by nature, so
                # don't waste a paid stream on it
                from .pipeline.organic import Verdict, hard_verdict
                hard = hard_verdict(
                    raw, self._build_organic_ctx(session, user_id, members0)
                )
                if hard is None or hard[0] != Verdict.ACCEPT:
                    return
            elif self.addressee_detector is not None:
                if not self.addressee_detector.is_addressed(
                    raw, speaker_id=user_id, session=session,
                    channel_member_count=members0,
                ):
                    return
            text = raw
            if self.addressee_detector is not None:
                text = self.addressee_detector.strip_wake_word(text) or _locale.WAKE_FALLBACK[self.persona.language]

            import json as _json
            emotion = "NEUTRAL"
            if hasattr(session, "last_emotion"):
                emotion = session.last_emotion.get(user_id, "NEUTRAL")
            user_name = session.user_names.get(user_id, str(user_id))
            payload = _json.dumps(
                {"speaker": user_name, "emotion": emotion, "content": text},
                ensure_ascii=False,
            )
            guild = self.get_guild(guild_id)
            vc = guild.voice_client if guild else None
            channel_name = vc.channel.name if vc and vc.channel else ""
            members = len(vc.channel.members) if vc and vc.channel else 1
            system_prompt = render_system_prompt(
                self.persona, self.persona.name,
                channel_name=channel_name, members_online=members,
            )
            tools = None
            if self.tool_registry is not None and not self.tool_registry.is_empty():
                tools = self.tool_registry.to_anthropic_tools()

            from .pipeline.speculative import SpeculativeLLM
            dialogue_len = len(session.dialogue)
            messages = list(session.dialogue) + [{"role": "user", "content": payload}]
            spec = SpeculativeLLM(
                self.llm, system_prompt, messages,
                user_text=text, user_payload=payload,
                tools=tools, dialogue_len=dialogue_len,
            )
            setattr(session, f"_spec_llm_{user_id}", spec)
            logger.info(f"[spec-llm] speculative stream opened for {text!r}")
        except Exception as e:
            logger.warning(f"[spec-llm] trigger failed: {e!r}")

    def _abort_spec_llm(self, spec) -> None:
        """Abort a speculative LLM stream (fire-and-forget, strong-ref'd)."""
        if not hasattr(self, "_discard_tasks"):
            self._discard_tasks = set()
        t = self.loop.create_task(spec.abort())
        self._discard_tasks.add(t)
        t.add_done_callback(self._discard_tasks.discard)

    def _cancel_spec_asr(self, session, user_id: int) -> None:
        old = getattr(session, f"_spec_task_{user_id}", None)
        if old is not None and not old.done():
            old.cancel()
        setattr(session, f"_spec_task_{user_id}", None)

    def _spawn_finalize(
        self, session, guild_id: int, user_id: int, user_name: str
    ) -> None:
        """Run _finalize_utterance as a tracked task.

        Inline-awaiting it in the watchdog froze endpoint detection for ALL
        users/guilds during one user's ASR inference (hundreds of ms).

        Dedup per (guild, user): the user's NEXT utterance can reach its own
        endpoint while this finalize's ASR inference is still in flight
        (long utterance on CPU); two concurrent generate() calls on the
        shared FunASR model must not happen. We keep in_speech=True for the
        blocked user so the watchdog re-fires after the running task ends —
        the second utterance is delayed, not dropped.
        """
        if not hasattr(self, "_finalize_in_flight"):
            self._finalize_in_flight = set()
            self._finalize_tasks = set()
        key = (guild_id, user_id)
        if key in self._finalize_in_flight:
            logger.warning(
                f"[watchdog] finalize for user {user_id} still running — "
                f"deferring this endpoint to the next watchdog pass"
            )
            # Undo the watchdog's in_speech reset so it re-fires for us later
            setattr(session, f"_in_speech_{user_id}", True)
            return
        # Snapshot SYNCHRONOUSLY — see _finalize_utterance(ok_at_start=...)
        ok_at_start = getattr(session, f"_utt_opus_ok_{user_id}", 0)
        self._finalize_in_flight.add(key)
        task = self.loop.create_task(
            self._finalize_utterance(
                session, guild_id, user_id, user_name,
                source="watchdog", ok_at_start=ok_at_start,
            )
        )
        self._finalize_tasks.add(task)  # strong ref — unreferenced tasks can be GC'd

        def _done(t: asyncio.Task) -> None:
            self._finalize_in_flight.discard(key)
            self._finalize_tasks.discard(t)
            if not t.cancelled() and t.exception() is not None:
                logger.opt(exception=t.exception()).error(
                    f"[watchdog] finalize failed for user {user_id}"
                )

        task.add_done_callback(_done)

    async def _finalize_utterance(
        self,
        session: VoiceSession,
        guild_id: int,
        user_id: int,
        user_name: str,
        *,
        source: str,
        ok_at_start: int | None = None,
    ) -> None:
        """Run ASR end_utterance and route the result through addressee →
        fast-path → LLM enqueue. Shared between VAD and speaking_stop signals."""
        # Speculative LLM: pop unconditionally at the top — EVERY exit path
        # below must either carry it into the Utterance or abort it (a leaked
        # stream keeps billing at Anthropic and could attach to a later turn).
        spec_llm = getattr(session, f"_spec_llm_{user_id}", None)
        setattr(session, f"_spec_llm_{user_id}", None)
        spec_llm_carried = False
        try:
            # Audio-duration filter — drop utterances with too little actual audio.
            # < 300ms of real packets cannot physically contain a meaningful word;
            # any ASR text from such a short window is noise / DAVE crypto fragment
            # / partial transmission. This catches the false barge-ins ("对"
            # "可" "嗯") without penalizing real short interrupts (a deliberate
            # "停" lasts 400-500ms = 20+ packets).
            from echotwin.utils.latency import LatencyJourney
            journey = LatencyJourney("endpoint")

            # ok_at_start is pre-snapshotted by _spawn_finalize on the watchdog's
            # synchronous path — between create_task and the task actually running,
            # the user's next utterance may have overwritten the snapshot attribute
            # on the session; a direct getattr would score the previous utterance
            # as 0ms and wrongly drop it.
            if ok_at_start is None:
                ok_at_start = getattr(session, f"_utt_opus_ok_{user_id}", 0)
            ok_now = session.opus_ok.get(user_id, 0)
            utt_packets = max(0, ok_now - ok_at_start)
            utt_ms = utt_packets * 20  # each opus packet ≈ 20ms

            asr = session.asrs[user_id]
            spec_task = getattr(session, f"_spec_task_{user_id}", None)
            setattr(session, f"_spec_task_{user_id}", None)
            asr_result = None
            if spec_task is not None:
                try:
                    spec_result, spec_fed = await spec_task
                except (asyncio.CancelledError, Exception) as e:
                    logger.warning(f"[asr] speculation failed: {e!r}")
                    spec_result, spec_fed = None, -1
                if spec_result is not None and spec_fed == asr.buffered_bytes():
                    asr.drop_buffer()
                    asr_result = spec_result
                    logger.info(f"[asr] speculation HIT for {user_name}")
                elif spec_result is not None:
                    logger.info("[asr] speculation stale (user resumed) — rerunning")
            if asr_result is None:
                asr_result = await asr.end_utterance()
            journey.mark("asr_done")
            if asr_result is None or not asr_result.text.strip():
                # Noise / pure-tag output. Log it — this used to be the only
                # silent drop point in the whole detection chain.
                logger.info(
                    f"[ASR/{source}] empty ASR result from {user_name} "
                    f"({utt_ms}ms of audio) — dropped"
                )
                return
            text = asr_result.text.strip()

            if utt_ms < 600:
                logger.info(
                    f"[ASR/{source}] dropping {utt_ms}ms utterance from {user_name}: "
                    f"{text!r} (too short to be real speech)"
                )
                return

            # Pure-punctuation guard: even with enough audio, if ASR produced no
            # content chars at all, it's still garbage.
            import re as _re
            content_chars = _re.sub(r"[\s\W_]+", "", text, flags=_re.UNICODE)
            if len(content_chars) < 1:
                logger.info(f"[ASR/{source}] dropping pure-punct from {user_name}: {text!r}")
                return

            # Anti-interruption acknowledgement filter: while bot is audibly
            # speaking, treat short backchannel utterances ("嗯", "对", "好",
            # "嗯嗯", "对对" ...) as listener acknowledgements rather than
            # barge-ins. Drop them so the bot's reply isn't truncated mid-sentence.
            # Real interrupts are longer ("等一下" "停" "别说了" "我想问个事").
            ACK_WORDS = {
                "嗯", "嗯嗯", "嗯哼", "嗯啊", "唔", "唔嗯",
                "对", "对对", "对呀", "对啊", "对哦",
                "好", "好的", "好啊", "好哒", "好哦",
                "哦", "哦哦", "噢", "诶", "欸",
                "啊", "啊啊", "呃", "呃嗯",
                "是", "是的", "是啊", "是哦",
                "yeah", "yes", "yep", "ok", "okay", "uhhuh", "mhm",
            }
            if session.is_audible and content_chars.lower() in ACK_WORDS:
                logger.info(
                    f"[ASR/{source}] treating {text!r} as listener ack "
                    f"(bot speaking) — not interrupting"
                )
                return

            logger.info(
                f"[ASR/{source}] {user_name}({user_id}): {text}  emotion={asr_result.emotion}"
            )

            # Addressee filter — drop utterances not directed at the bot
            guild_obj = self.get_guild(guild_id)
            voice_client = guild_obj.voice_client if guild_obj else None
            channel = voice_client.channel if voice_client else None
            channel_member_count = len(channel.members) if channel else 1
            organic_cfg = getattr(self.config.bot, "organic", None)
            if organic_cfg is not None and organic_cfg.enabled:
                # Organic multi-user detection (spec: dev-docs/2026-06-11-organic-multiparty)
                # Layered: hard_verdict instant verdict (zero cost) → gray-zone
                # LLM arbitration (semantic) → on arbiter failure fall back to
                # classify() heuristic scoring (the full rule set kept as safety net)
                from .pipeline.organic import Verdict, classify, hard_verdict

                now = _time.time()
                ctx = self._build_organic_ctx(session, user_id, channel_member_count)
                hard = hard_verdict(text, ctx)
                if hard is not None:
                    verdict, score, sigs = hard
                elif organic_cfg.gray_zone == "llm" and (
                    len([t for t in session.arbiter_calls if now - t < 60])
                    < organic_cfg.arbiter_max_per_min
                ):
                    from .pipeline.arbiter import arbitrate

                    session.arbiter_calls.append(now)
                    res = await arbitrate(
                        getattr(self, "arbiter_llm", None) or self.llm,
                        bot_name=self.persona.name,
                        language=self.persona.language,
                        speaker=user_name,
                        utterance=text,
                        room_lines=[
                            f"{e['speaker']}: {e['text']}" for e in session.ambient
                        ],
                        last_bot_reply=session.last_bot_reply,
                        last_addressee=session.user_names.get(
                            session.last_addressee_id or 0
                        ),
                        in_window=ctx.in_window,
                        clarify_pending=ctx.clarify_pending,
                        timeout=organic_cfg.arbiter_timeout_ms / 1000,
                        cost_tracker=getattr(self, "cost_tracker", None),
                        ids=dict(guild_id=str(guild_id), user_id=str(user_id)),
                        cost_prefix=getattr(
                            self, "arbiter_cost_prefix", "claude_haiku_4_5"
                        ),
                    )
                    if res is not None:
                        verdict, score, sigs = res[0], 0, [f"llm:{res[1]}"]
                    else:
                        verdict, score, sigs = classify(text, ctx)
                        sigs.append("arbiter_fallback")
                else:
                    verdict, score, sigs = classify(text, ctx)
                logger.info(
                    f"[organic] {user_name}: verdict={verdict.value} score={score} "
                    f"signals={sigs} text={text!r}"
                )
                session.clarify_pending_at.pop(user_id, None)
                if verdict == Verdict.MENTION:
                    # Phase 2: mentioned in third person → low-rate pickup (rate=0 means off)
                    import random as _random
                    if _random.random() < organic_cfg.mention_reply_rate:
                        logger.info(f"[organic] mention reply fires for {user_name}")
                        verdict = Verdict.ACCEPT
                    else:
                        self._ambient_note(session, user_id, user_name, text)
                        return
                if verdict == Verdict.REJECT:
                    self._ambient_note(session, user_id, user_name, text)
                    return
                if verdict == Verdict.CLARIFY:
                    self._ambient_note(session, user_id, user_name, text)
                    if organic_cfg.clarify_llm:
                        # Phase 2: gray zone escalates to asking the LLM (default off) — async verdict, pick up belatedly on "是"
                        self._clarify_via_llm(
                            session, guild_id, user_id, user_name, text,
                            asr_result.emotion,
                        )
                        return
                    if (
                        voice_client is not None
                        and now - session.clarify_last_at.get(user_id, 0)
                        > organic_cfg.clarify_cooldown_s
                    ):
                        clarify = self.pick_clarify_path()
                        if clarify is not None:
                            session.clarify_pending_at[user_id] = now
                            session.clarify_last_at[user_id] = now
                            asyncio.create_task(
                                self._play_cached_opus(voice_client, clarify)
                            )
                            logger.info(f"[organic] clarifying to {user_name}")
                    return
                if verdict == Verdict.OPEN_FLOOR:
                    self._ambient_note(session, user_id, user_name, text)
                    if organic_cfg.open_floor:
                        self._arm_open_floor(
                            session, guild_id, user_id, user_name, text,
                            asr_result.emotion, organic_cfg.open_floor_wait_ms,
                        )
                    return
                # ACCEPT → enters the window, falls through to the normal path
                session.organic_participants[user_id] = now
                session.last_voice_event = user_id
            elif self.addressee_detector is not None:
                addressed = self.addressee_detector.is_addressed(
                    text,
                    speaker_id=user_id,
                    session=session,
                    channel_member_count=channel_member_count,
                )
                if not addressed:
                    logger.info(
                        f"[addressee] dropping non-addressed utterance from {user_name}: {text!r}"
                    )
                    return

            # Wake-word fast path: short addresses ("一点点点?", "点点啊") → cached TTS,
            # skip LLM. Saves ~800ms vs. running LLM + TTS for trivial pings.
            # NOT while a reply is playing (PROCESSING): the cached playback would
            # stop() the live reply, fire play_done as if it completed normally,
            # and commit the truncated reply to history — let it barge-in instead.
            if (
                session.state != SessionState.PROCESSING
                and self.wake_matcher is not None
                and self.fast_cache is not None
                and voice_client is not None
                and self.wake_matcher.match_only(text)
            ):
                cached = await self.fast_cache.get_random()
                if cached is not None:
                    logger.info(f"[fast-response] wake-only match — playing cached {cached.name}")
                    asyncio.create_task(self._fast_path_play(voice_client, session, user_id, cached))
                    return
                # Fall through to LLM if cache empty

            if self.addressee_detector is not None:
                text = self.addressee_detector.strip_wake_word(text)
                if not text:
                    text = _locale.WAKE_FALLBACK[self.persona.language]  # wake-word only fall-through

            # Barge-in check
            if session.state == SessionState.PROCESSING:
                addressee = session.current_addressee_id
                if user_id == addressee:
                    if self.config.bot.barge_in_mode in ("addressee_only", "anyone"):
                        await session.abort()
                elif self.config.bot.barge_in_mode == "anyone":
                    await session.abort()

            # Replace this user's existing queued item (if any) and enqueue new
            # Pre-open the TTS WS so the handshake hides inside the dispatch gap
            # (and is ready before a speculative LLM fires). Only when this turn
            # will dispatch immediately — a socket queued behind a long-playing
            # turn goes stale (Fish closes idle connections).
            pre_tts = None
            pre_tts_open = None
            if session.utterance_queue.empty() and session.state != SessionState.PROCESSING:
                try:
                    pre_tts = make_tts(
                        self.config, voice_id=self.active_voice_id(), persona=self.persona
                    )
                    pre_tts_open = self.loop.create_task(pre_tts.open())
                except Exception as e:
                    logger.warning(f"[tts] pre-open failed: {e}")
                    pre_tts, pre_tts_open = None, None

            # Emotion sidecar (streaming ASR has no emotion labels)
            if (
                self.config.asr.emotion_sidecar
                and getattr(asr, "last_utterance_pcm", b"")
            ):
                self._spawn_emotion_sidecar(session, user_id, asr.last_utterance_pcm)

            # Speculative LLM: only carry it if the FINAL text (and dialogue
            # snapshot) match what the stream was opened with.
            carried_spec = None
            if spec_llm is not None and spec_llm.matches(text, len(session.dialogue)):
                carried_spec = spec_llm
                spec_llm_carried = True
                logger.info("[spec-llm] speculation MATCHED — attaching to turn")

            self._dequeue_user(session, user_id)
            await session.utterance_queue.put(
                Utterance(
                    user_id=user_id,
                    user_name=user_name,
                    text=text,
                    emotion=asr_result.emotion,
                    journey=journey,
                    tts=pre_tts,
                    tts_open_task=pre_tts_open,
                    spec_llm=carried_spec,
                )
            )
        finally:
            if spec_llm is not None and not spec_llm_carried:
                self._abort_spec_llm(spec_llm)

    def _detect_wake(self, text: str) -> str | None:
        low = text.lower()
        for w in self.persona.wake_words:
            if w.lower() in low:
                return w
        return None

    def _strip_wake(self, text: str, wake_word: str) -> str:
        idx = text.lower().find(wake_word.lower())
        if idx == 0:
            return text[len(wake_word):].lstrip(",。!?, ").strip()
        return text

    def _discard_utterance(self, utt) -> None:
        """Release a dropped utterance's pre-opened TTS + speculative LLM."""
        spec = getattr(utt, "spec_llm", None)
        if spec is not None:
            self._abort_spec_llm(spec)
        task = getattr(utt, "tts_open_task", None)
        tts = getattr(utt, "tts", None)
        if task is not None and not task.done():
            task.cancel()
        if tts is None:
            return

        async def _close():
            if task is not None:
                await asyncio.gather(task, return_exceptions=True)
            try:
                await tts.close()
            except Exception:
                pass

        if not hasattr(self, "_discard_tasks"):
            self._discard_tasks = set()
        t = self.loop.create_task(_close())
        self._discard_tasks.add(t)
        t.add_done_callback(self._discard_tasks.discard)

    def _drain_merge_extras(
        self, session: VoiceSession, primary: Utterance
    ) -> list[tuple[int, str, str]]:
        """Dequeue utterances backlogged during playback and merge them into one
        turn: returns (user_id, name, text). Pre-opened TTS / speculative streams
        of merged utterances are released one by one; if anything was merged, the
        primary utterance's speculative stream is also invalidated — the merged
        payload is no longer what it pre-ran on."""
        extras: list[tuple[int, str, str]] = []
        try:
            while True:
                item = session.utterance_queue.get_nowait()
                extras.append((item.user_id, item.user_name, item.text))
                self._discard_utterance(item)
        except asyncio.QueueEmpty:
            pass
        if extras and primary.spec_llm is not None:
            self._abort_spec_llm(primary.spec_llm)
            primary.spec_llm = None
        return extras

    def _dequeue_user(self, session: VoiceSession, user_id: int) -> None:
        """Remove any queued utterance from the same user (so newer one replaces)."""
        kept: list[Utterance] = []
        try:
            while True:
                item = session.utterance_queue.get_nowait()
                if item.user_id != user_id:
                    kept.append(item)
                else:
                    self._discard_utterance(item)
        except asyncio.QueueEmpty:
            pass
        for item in kept:
            session.utterance_queue.put_nowait(item)

    # ------------------------------------------------------------
    # Consumer loop: serial dispatch from utterance_queue → think_speak
    # ------------------------------------------------------------

    # ------------------------------------------------------------
    # Idle watcher (auto-leave) + Heartbeat
    # ------------------------------------------------------------

    async def _idle_watcher_loop(self) -> None:
        """Periodically check each session: empty channel or no activity → leave."""
        while not self.is_closed():
            try:
                await asyncio.sleep(10)
                for guild_id in list(self.sessions.keys()):
                    await self._check_idle(guild_id)
            except asyncio.CancelledError:
                return
            except Exception as e:
                logger.warning(f"idle watcher error: {e}")

    async def _check_idle(self, guild_id: int) -> None:
        guild = self.get_guild(guild_id)
        if guild is None or guild.voice_client is None or guild.voice_client.channel is None:
            return
        session = self.sessions.get(guild_id)
        if session is None:
            return

        members = [m for m in guild.voice_client.channel.members if not m.bot]
        if not members:
            session.alone_seconds += 10
            if session.alone_seconds >= self.config.bot.empty_channel_timeout_seconds:
                logger.info(f"[guild {guild_id}] empty channel timeout, leaving")
                from .commands.public import _graceful_leave
                # No humans left to hear a goodbye — disconnect silently, skipping
                # a pointless LLM/TTS farewell to an empty room.
                await _graceful_leave(self, guild, reason="empty_channel", say_farewell=False)
                return
            return

        session.alone_seconds = 0
        elapsed = self.loop.time() - session.last_activity_time
        inactivity = self.config.bot.inactivity_timeout_seconds
        hard = self.config.bot.hard_timeout_seconds

        # Stage 2: hard cutoff regardless of goodbye state
        if elapsed > hard:
            logger.info(
                f"[guild {guild_id}] hard timeout ({elapsed:.0f}s > {hard}s), force-leaving"
            )
            from .commands.public import _graceful_leave
            session.goodbye_pending = False
            await _graceful_leave(self, guild, reason="hard_timeout")
            return

        # Stage 1: speak the single LLM farewell then wait grace for activity.
        # The post-goodbye disconnect below is silent so we don't say it twice.
        if not session.goodbye_pending and elapsed > inactivity:
            logger.info(
                f"[guild {guild_id}] inactivity timeout ({elapsed:.0f}s) — sending goodbye"
            )
            session.goodbye_pending = True
            try:
                from .commands.public import _generate_farewell, _speak_text
                if self.config.bot.farewell.enabled:
                    text = await _generate_farewell(self, guild.voice_client, "inactivity")
                    if text and text.strip():
                        await _speak_text(guild.voice_client, self, text)
            except Exception as e:
                logger.warning(f"[idle] goodbye TTS failed: {e}")
            # Reset clock so we don't immediately re-trigger; allow ~5s grace
            session.last_activity_time = self.loop.time() - inactivity + 5
            return

        # Goodbye was already spoken in Stage 1; grace expired → disconnect
        # silently (say_farewell=False) so we don't repeat a second goodbye.
        if session.goodbye_pending and elapsed > inactivity + 1:
            logger.info(f"[guild {guild_id}] post-goodbye grace expired, leaving")
            from .commands.public import _graceful_leave
            session.goodbye_pending = False
            await _graceful_leave(
                self, guild, reason="inactivity_after_goodbye", say_farewell=False
            )

    async def _heartbeat_loop(self) -> None:
        while not self.is_closed():
            try:
                await asyncio.sleep(self.config.monitoring.heartbeat_interval_seconds)
                uptime = int(_time.time() - self.start_time)
                cost_today = 0.0
                if self.cost_tracker:
                    try:
                        cost_today = await self.cost_tracker.total(_time.time() - 86400)
                    except Exception:
                        pass
                logger.info(
                    f"[heartbeat] uptime={uptime}s guilds={len(self.guilds)} "
                    f"sessions={len(self.sessions)} cost_today=${cost_today:.4f}"
                )
            except asyncio.CancelledError:
                return
            except Exception as e:
                logger.warning(f"heartbeat error: {e}")

    async def dm_owner(self, message: str) -> None:
        if self.app_owner_id is None:
            return
        try:
            user = await self.fetch_user(self.app_owner_id)
            await user.send(message)
        except Exception as e:
            logger.warning(f"Could not DM owner: {e}")

    # ------------------------------------------------------------
    # Consumer loop
    # ------------------------------------------------------------

    async def _consumer_loop(self, guild_id: int) -> None:
        session = self.sessions[guild_id]
        logger.info(f"[consumer] loop started for guild {guild_id}")
        while True:
            try:
                utterance: Utterance = await session.utterance_queue.get()
                logger.info(f"[consumer] dequeued: {utterance.user_name}: {utterance.text!r}")
            except asyncio.CancelledError:
                logger.info(f"[consumer] cancelled for guild {guild_id}")
                return
            if session.state == SessionState.SLEEPING:
                logger.info("[consumer] sleeping, dropping")
                self._discard_utterance(utterance)
                continue

            guild = self.get_guild(guild_id)
            if guild is None or guild.voice_client is None:
                logger.warning(f"[guild {guild_id}] no voice client, dropping utterance")
                self._discard_utterance(utterance)
                continue
            logger.info(f"[consumer] dispatching respond_to_user")

            channel_name = guild.voice_client.channel.name if guild.voice_client.channel else ""
            members_online = len(guild.voice_client.channel.members) if guild.voice_client.channel else 1
            try:
                system_prompt = render_system_prompt(
                    self.persona,
                    self.persona.name,
                    channel_name=channel_name,
                    members_online=members_online,
                )
            except Exception as e:
                logger.error(f"persona render failed: {e}")
                self._discard_utterance(utterance)
                continue

            # Queued merge: fetch all utterances backlogged during playback and
            # fold them into this turn for a unified reply (live P3: one-by-one
            # queuing waited up to 7s worst case). Release/invalidation all
            # happens inside drain.
            merged = self._drain_merge_extras(session, utterance)
            if merged:
                logger.info(
                    "[merge] folding %d queued utterance(s) into this turn: %s"
                    % (len(merged), "; ".join(f"{n}:{t[:20]}" for _, n, t in merged))
                )

            try:
                # Emotion priority: this turn's real emotion (batch SenseVoice)
                # > side-channel cache (lags one turn) > NEUTRAL — don't let
                # the batch path regress
                effective_emotion = (
                    utterance.emotion
                    if utterance.emotion != "NEUTRAL"
                    else session.last_emotion.get(utterance.user_id, "NEUTRAL")
                )
                await respond_to_user(
                    self,
                    session,
                    guild.voice_client,
                    user_id=utterance.user_id,
                    user_name=utterance.user_name,
                    user_text=utterance.text,
                    emotion=effective_emotion,
                    system_prompt=system_prompt,
                    journey=utterance.journey,
                    pre_tts=utterance.tts,
                    pre_tts_open_task=utterance.tts_open_task,
                    spec_llm=utterance.spec_llm,
                    merged=merged or None,
                )
            except Exception as e:
                logger.exception(f"respond_to_user failed: {e}")
