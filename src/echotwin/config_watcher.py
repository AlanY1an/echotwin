"""Hot-reload watcher — re-reads config + persona on SIGHUP.

Reloadable fields (whitelist):
- bot.active_persona (also swaps persona instance + side effects)
- bot.inactivity_timeout_seconds, hard_timeout_seconds, empty_channel_timeout_seconds
- bot.endpoint_silence_ms, endpoint_tick_ms (watchdog reads them each tick)
- All persona file contents (wake_words, voice_id, fast_responses, system prompt)

Non-reloadable: provider choice, API keys, Discord token, logging.
"""
from __future__ import annotations

import signal
from pathlib import Path
from typing import TYPE_CHECKING

from loguru import logger

from echotwin.config import Config, load_config
from echotwin.persona import PersonaError, load_persona

if TYPE_CHECKING:
    from echotwin.bot import VoiceAgentBot


_RELOADABLE_FIELDS = [
    ("bot", "active_persona"),
    ("bot", "inactivity_timeout_seconds"),
    ("bot", "hard_timeout_seconds"),
    ("bot", "empty_channel_timeout_seconds"),
    ("bot", "endpoint_silence_ms"),
    ("bot", "endpoint_tick_ms"),
]


def _diff_reloadable(old: Config, new: Config) -> dict[str, tuple]:
    diff: dict[str, tuple] = {}
    for section, field in _RELOADABLE_FIELDS:
        old_v = getattr(getattr(old, section), field)
        new_v = getattr(getattr(new, section), field)
        if old_v != new_v:
            diff[f"{section}.{field}"] = (old_v, new_v)
    return diff


class ConfigWatcher:
    def __init__(self, bot: "VoiceAgentBot", config_path: str | Path):
        self._bot = bot
        self._path = str(config_path)

    def install(self) -> None:
        """Register SIGHUP handler that schedules a reload on the bot loop."""
        try:
            self._bot.loop.add_signal_handler(
                signal.SIGHUP,
                lambda: self._bot.loop.create_task(self.reload()),
            )
            logger.info("[config-watcher] SIGHUP handler installed")
        except (ValueError, NotImplementedError):
            logger.warning(
                "[config-watcher] platform does not support SIGHUP — slash command only"
            )

    async def reload(self) -> dict[str, tuple]:
        """Reload config.yaml + persona; apply whitelisted diff. Returns the diff."""
        logger.info("[config-watcher] reload triggered")
        try:
            new_cfg = load_config(self._path)
        except Exception as e:
            logger.error(f"[config-watcher] config reload failed, keeping old: {e}")
            return {}

        try:
            new_persona_path = (
                Path("prompts") / "personas" / f"{new_cfg.bot.active_persona}.md"
            )
            new_persona = load_persona(new_persona_path)
        except PersonaError as e:
            logger.error(f"[config-watcher] persona reload failed, keeping old: {e}")
            return {}

        diff = _diff_reloadable(self._bot.config, new_cfg)
        old_persona_id = self._bot.persona.id

        # Apply atomically
        self._bot.config = new_cfg
        self._bot.persona = new_persona

        # Runtime overrides (persona / voice / whitelist / co-owners set via
        # owner slash commands) take precedence over yaml — without this a
        # SIGHUP silently reverted them all to config.yaml values.
        from echotwin.commands.owner_dm import load_runtime_config
        load_runtime_config(self._bot)

        # Side effects on persona swap (compare the FINAL persona, which may
        # have been restored from runtime config)
        if self._bot.persona.id != old_persona_id:
            await self._refresh_persona_resources(self._bot.config, self._bot.persona)

        logger.info(
            f"[config-watcher] applied diff: {diff or '{}'}; persona={self._bot.persona.id}"
        )
        return diff

    async def _refresh_persona_resources(self, cfg: Config, persona) -> None:
        """Rebuild matcher, addressee detector, fast cache when persona changes."""
        from echotwin.pipeline.addressee import AddresseeDetector
        from echotwin.wake_word.fast_response import FastResponseCache
        from echotwin.wake_word.matcher import WakeWordMatcher

        self._bot.wake_matcher = WakeWordMatcher(wake_words=persona.wake_words)
        self._bot.fast_cache = FastResponseCache(
            persona_id=persona.id,
            voice_id=persona.voice_id,
            responses=persona.fast_responses,
            data_dir=Path("data"),
        )
        if self._bot.user is not None:
            self._bot.addressee_detector = AddresseeDetector(
                persona=persona,
                bot_user_id=self._bot.user.id,
                continuation_window_seconds=cfg.addressee.continuation_window_seconds,
                solo_channel_auto=cfg.addressee.solo_channel_auto,
            )

        # Refresh fast-response cache (fire-and-forget)
        try:
            await self._bot.fast_cache.ensure_synthesized(self._bot._synth_with_persona)
        except Exception as e:
            logger.warning(f"[config-watcher] fast-response refresh failed: {e}")

        # Refresh filler audio for the new persona
        try:
            await self._bot._ensure_filler_audio()
        except Exception as e:
            logger.warning(f"[config-watcher] filler refresh failed: {e}")

        # Re-synthesize quota-limit announcement
        try:
            limit_dir = Path("data") / "wake_responses" / persona.id
            limit_dir.mkdir(parents=True, exist_ok=True)
            limit_path = limit_dir / "_limit.ogg"
            if not limit_path.exists() or limit_path.stat().st_size == 0:
                audio = await self._bot._synth_with_persona(persona.limit_exceeded_text)
                if audio:
                    limit_path.write_bytes(audio)
            self._bot.limit_audio_path = limit_path
        except Exception as e:
            logger.warning(f"[config-watcher] limit audio refresh failed: {e}")

        # Sync server nickname to new persona — only in guilds where bot is
        # currently in a voice channel (idle guilds revert to default app name)
        try:
            await self._bot.sync_nickname_in_active_guilds()
        except Exception as e:
            logger.warning(f"[config-watcher] nickname sync failed: {e}")
