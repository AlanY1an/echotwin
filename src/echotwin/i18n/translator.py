"""Discord app_commands translator — wires our STRINGS table into Discord UI.

Use `ls("key")` (alias for `app_commands.locale_str`) anywhere a description,
parameter description, or choice name is set. Discord will call our translator
at command-render time with the user's client locale.
"""
from __future__ import annotations

import discord
from discord import app_commands
from discord.app_commands import locale_str as ls

from .strings import STRINGS, _normalize_locale


class VoiceAgentTranslator(app_commands.Translator):
    async def translate(
        self,
        string: app_commands.locale_str,
        locale: discord.Locale,
        context: app_commands.TranslationContext,
    ) -> str | None:
        key = string.message
        table = STRINGS.get(key)
        if table is None:
            return None
        code = _normalize_locale(locale)
        return table.get(code)


__all__ = ["VoiceAgentTranslator", "ls"]
