"""Internationalization for slash command UI (descriptions + responses).

Two parts:
- discord.app_commands `locale_str` for command/parameter descriptions and choice
  names — Discord client picks the right language based on user's app language.
- Manual `t(key, locale, **fmt)` for `interaction.response.send_message` text —
  call sites pass `interaction.locale` so the user sees their preferred language.

Persona content (system prompts, wake words, voice replies) is intentionally NOT
covered here — those stay per-persona, owner-controlled.
"""
from .strings import t, STRINGS, DEFAULT_LOCALE
from .translator import VoiceAgentTranslator, ls

__all__ = ["t", "STRINGS", "DEFAULT_LOCALE", "VoiceAgentTranslator", "ls"]
