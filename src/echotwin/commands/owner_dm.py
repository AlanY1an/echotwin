"""Owner-only slash commands (usable in DMs or guild channels; replies are ephemeral)."""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from loguru import logger

from echotwin.i18n import t, ls
from echotwin.persona import list_personas, load_persona

if TYPE_CHECKING:
    from echotwin.bot import VoiceAgentBot


RUNTIME_CONFIG_PATH = Path("data/runtime_config.json")


def save_runtime_config(bot: "VoiceAgentBot") -> None:
    RUNTIME_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "active_persona": bot.config.bot.active_persona,
        "voice_id_override": bot.config.tts.fish_audio_stream.voice_id,
        "wake_word_required": bot.config.bot.wake_word_required,
        "listen_only_users": list(bot.config.bot.listen_only_users),
        "extra_owner_ids": sorted(bot.extra_owner_ids),
    }
    RUNTIME_CONFIG_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2))


def load_runtime_config(bot: "VoiceAgentBot") -> None:
    if not RUNTIME_CONFIG_PATH.exists():
        return
    try:
        data = json.loads(RUNTIME_CONFIG_PATH.read_text())
        if "active_persona" in data:
            bot.config.bot.active_persona = data["active_persona"]
            # Reload Persona instance to match
            bot.persona = load_persona(
                Path("prompts") / "personas" / f"{bot.config.bot.active_persona}.md"
            )
        # Backward-compat: old key was "voice_id"
        for key in ("voice_id_override", "voice_id"):
            if key in data:
                bot.config.tts.fish_audio_stream.voice_id = data[key]
                break
        if "wake_word_required" in data:
            bot.config.bot.wake_word_required = bool(data["wake_word_required"])
        if "listen_only_users" in data and isinstance(data["listen_only_users"], list):
            bot.config.bot.listen_only_users = [int(u) for u in data["listen_only_users"]]
        if "extra_owner_ids" in data and isinstance(data["extra_owner_ids"], list):
            bot.extra_owner_ids = {int(u) for u in data["extra_owner_ids"]}
        logger.info(f"Loaded runtime config from {RUNTIME_CONFIG_PATH}")
    except Exception as e:
        logger.warning(f"Failed to load runtime config: {e}")


def _is_owner(bot: "VoiceAgentBot", user_id: int) -> bool:
    if bot.app_owner_id is not None and user_id == bot.app_owner_id:
        return True
    return user_id in bot.extra_owner_ids


def _is_primary_owner(bot: "VoiceAgentBot", user_id: int) -> bool:
    """Only the Discord application owner — co-owners cannot manage the owner list."""
    return bot.app_owner_id is not None and user_id == bot.app_owner_id


def register_owner_commands(tree: app_commands.CommandTree, bot: "VoiceAgentBot") -> None:

    persona_grp = app_commands.Group(
        name="persona-admin",
        description=ls("grp.persona_admin.desc"),
    )

    @persona_grp.command(name="use", description=ls("cmd.persona_admin.use.desc"))
    @app_commands.describe(name=ls("cmd.persona_admin.use.param.name"))
    async def persona_use(interaction: discord.Interaction, name: str):
        if not _is_owner(bot, interaction.user.id):
            return
        loc = interaction.locale
        # Defer immediately — persona swap re-synthesizes fast-response audios
        # via Fish Audio (5 × ~1s) and the limit message, which blows the 3s
        # interaction window. Use followup at the end instead.
        await interaction.response.defer(ephemeral=True)
        personas = list_personas("prompts")
        if name not in personas:
            await interaction.followup.send(
                t("resp.persona_unknown", loc, name=name, available=", ".join(personas)),
                ephemeral=True,
            )
            return
        try:
            new_persona = load_persona(Path("prompts") / "personas" / f"{name}.md")
        except Exception as e:
            await interaction.followup.send(
                t("resp.persona_load_failed", loc, error=e), ephemeral=True
            )
            return
        bot.config.bot.active_persona = name
        bot.persona = new_persona
        for sess in bot.sessions.values():
            sess.dialogue.clear()
        save_runtime_config(bot)
        # Rebuild persona-bound resources so wake words / addressee detector /
        # fast-response cache match the new persona (otherwise we'd still be
        # checking against the previous persona's wake words!)
        try:
            from echotwin.pipeline.addressee import AddresseeDetector
            from echotwin.wake_word.matcher import WakeWordMatcher
            from echotwin.wake_word.fast_response import FastResponseCache
            bot.wake_matcher = WakeWordMatcher(wake_words=new_persona.wake_words)
            bot.fast_cache = FastResponseCache(
                persona_id=new_persona.id,
                voice_id=new_persona.voice_id,
                responses=new_persona.fast_responses,
                data_dir=Path("data"),
            )
            if bot.user is not None:
                bot.addressee_detector = AddresseeDetector(
                    persona=new_persona,
                    bot_user_id=bot.user.id,
                    continuation_window_seconds=bot.config.addressee.continuation_window_seconds,
                    solo_channel_auto=bot.config.addressee.solo_channel_auto,
                )
            # Refresh fast-response audio cache + limit-message audio for new persona
            await bot.fast_cache.ensure_synthesized(bot._synth_with_persona)
            limit_dir = Path("data") / "wake_responses" / new_persona.id
            limit_dir.mkdir(parents=True, exist_ok=True)
            limit_path = limit_dir / "_limit.ogg"
            if not limit_path.exists() or limit_path.stat().st_size == 0:
                audio = await bot._synth_with_persona(new_persona.limit_exceeded_text)
                if audio:
                    limit_path.write_bytes(audio)
            bot.limit_audio_path = limit_path
            await bot._ensure_filler_audio()
        except Exception as e:
            logger.warning(f"persona resource refresh failed: {e}")
        # Update Discord server nickname only in guilds where bot is currently
        # in a voice channel (idle guilds keep the default app name)
        try:
            await bot.sync_nickname_in_active_guilds()
        except Exception as e:
            logger.warning(f"sync nickname failed: {e}")
        await interaction.followup.send(
            t("resp.persona_switched", loc, name=name, display=new_persona.name),
            ephemeral=True,
        )

    @persona_grp.command(name="reload", description=ls("cmd.persona_admin.reload.desc"))
    async def persona_reload(interaction: discord.Interaction):
        if not _is_owner(bot, interaction.user.id):
            return
        loc = interaction.locale
        try:
            bot.persona = load_persona(
                Path("prompts") / "personas" / f"{bot.config.bot.active_persona}.md"
            )
        except Exception as e:
            await interaction.response.send_message(
                t("resp.persona_reload_failed", loc, error=e), ephemeral=True
            )
            return
        await interaction.response.send_message(
            t("resp.persona_reloaded", loc, id=bot.persona.id), ephemeral=True
        )

    voice_grp = app_commands.Group(
        name="voice-admin",
        description=ls("grp.voice_admin.desc"),
    )

    @voice_grp.command(name="set", description=ls("cmd.voice_admin.set.desc"))
    @app_commands.describe(voice_id=ls("cmd.voice_admin.set.param.voice_id"))
    async def voice_set(interaction: discord.Interaction, voice_id: str):
        if not _is_owner(bot, interaction.user.id):
            return
        loc = interaction.locale
        bot.config.tts.fish_audio_stream.voice_id = voice_id
        save_runtime_config(bot)
        await interaction.response.send_message(
            t("resp.voice_set", loc, voice_id=voice_id), ephemeral=True
        )

    @voice_grp.command(name="show", description=ls("cmd.voice_admin.show.desc"))
    async def voice_show(interaction: discord.Interaction):
        if not _is_owner(bot, interaction.user.id):
            return
        loc = interaction.locale
        override = bot.config.tts.fish_audio_stream.voice_id
        eff = bot.active_voice_id()
        if override:
            msg = t("resp.voice_show_override", loc, eff=eff, persona=bot.persona.voice_id)
        else:
            msg = t("resp.voice_show_persona", loc, eff=eff)
        await interaction.response.send_message(msg, ephemeral=True)

    admin_grp = app_commands.Group(
        name="admin",
        description=ls("grp.admin.desc"),
    )

    @admin_grp.command(name="cost", description=ls("cmd.admin.cost.desc"))
    async def admin_cost(interaction: discord.Interaction):
        if not _is_owner(bot, interaction.user.id):
            return
        loc = interaction.locale
        if bot.cost_tracker is None:
            await interaction.response.send_message(t("resp.cost_disabled", loc), ephemeral=True)
            return
        now = time.time()
        day_ago = now - 86400
        month_ago = now - 30 * 86400
        today = await bot.cost_tracker.summary(day_ago)
        month = await bot.cost_tracker.summary(month_ago)
        today_total = sum(today.values())
        month_total = sum(month.values())
        lines = [t("resp.cost_header", loc), "", t("resp.cost_today", loc, total=today_total)]
        for k, v in today.items():
            lines.append(f"  • {k}: ${v:.4f}")
        lines.append("")
        lines.append(t("resp.cost_month", loc, total=month_total))
        for k, v in month.items():
            lines.append(f"  • {k}: ${v:.4f}")
        await interaction.response.send_message("\n".join(lines), ephemeral=True)

    @admin_grp.command(name="health", description=ls("cmd.admin.health.desc"))
    async def admin_health(interaction: discord.Interaction):
        if not _is_owner(bot, interaction.user.id):
            return
        loc = interaction.locale
        uptime = int(time.time() - bot.start_time)
        h, rem = divmod(uptime, 3600)
        m = rem // 60
        eff_voice = bot.active_voice_id()
        msg = t(
            "resp.health",
            loc,
            h=h, m=m,
            guilds=len(bot.guilds),
            sessions=len(bot.sessions),
            persona_id=bot.persona.id,
            persona_name=bot.persona.name,
            voice_short=eff_voice[:16],
        )
        await interaction.response.send_message(msg, ephemeral=True)

    @admin_grp.command(name="wakeword", description=ls("cmd.admin.wakeword.desc"))
    @app_commands.describe(state=ls("cmd.admin.wakeword.param.state"))
    @app_commands.choices(
        state=[
            app_commands.Choice(name=ls("choice.state.on"), value="on"),
            app_commands.Choice(name=ls("choice.state.off"), value="off"),
        ]
    )
    async def admin_wakeword(interaction: discord.Interaction, state: app_commands.Choice[str]):
        if not _is_owner(bot, interaction.user.id):
            return
        loc = interaction.locale
        bot.config.bot.wake_word_required = state.value == "on"
        save_runtime_config(bot)
        await interaction.response.send_message(
            t("resp.wakeword_set", loc, state=bot.config.bot.wake_word_required),
            ephemeral=True,
        )

    @admin_grp.command(name="reload-config", description=ls("cmd.admin.reload_config.desc"))
    async def admin_reload(interaction: discord.Interaction):
        if not _is_owner(bot, interaction.user.id):
            return
        loc = interaction.locale
        watcher = getattr(bot, "config_watcher", None)
        if watcher is None:
            await interaction.response.send_message(
                t("resp.config_watcher_missing", loc), ephemeral=True
            )
            return
        await interaction.response.defer(ephemeral=True)
        diff = await watcher.reload()
        if diff:
            lines = "\n".join(f"- {k}: {v[0]} → {v[1]}" for k, v in diff.items())
            msg = t("resp.reload_done", loc, diff=lines)
        else:
            msg = t("resp.reload_done_no_changes", loc)
        await interaction.followup.send(msg, ephemeral=True)

    @admin_grp.command(name="restart", description=ls("cmd.admin.restart.desc"))
    async def admin_restart(interaction: discord.Interaction):
        if not _is_owner(bot, interaction.user.id):
            return
        loc = interaction.locale
        guild_ids = list(bot.sessions.keys())
        for gid in guild_ids:
            await bot.cleanup_session(gid)
        await interaction.response.send_message(
            t("resp.sessions_reset", loc, count=len(guild_ids)), ephemeral=True
        )

    @admin_grp.command(
        name="whitelist",
        description=ls("cmd.admin.whitelist.desc"),
    )
    @app_commands.describe(
        action=ls("cmd.admin.whitelist.param.action"),
        user=ls("cmd.admin.whitelist.param.user"),
    )
    @app_commands.choices(
        action=[
            app_commands.Choice(name=ls("choice.action.add"), value="add"),
            app_commands.Choice(name=ls("choice.action.remove"), value="remove"),
            app_commands.Choice(name=ls("choice.action.list"), value="list"),
            app_commands.Choice(name=ls("choice.action.clear"), value="clear"),
        ]
    )
    async def admin_whitelist(
        interaction: discord.Interaction,
        action: app_commands.Choice[str],
        user: discord.User | None = None,
    ):
        if not _is_owner(bot, interaction.user.id):
            return
        loc = interaction.locale
        wl: list[int] = list(bot.config.bot.listen_only_users)
        act = action.value
        if act == "list":
            if not wl:
                msg = t("resp.whitelist_empty", loc)
            else:
                names = []
                for uid in wl:
                    u = bot.get_user(uid)
                    names.append(f"  - {uid} ({u.display_name if u else '?'})")
                msg = t("resp.whitelist_header", loc) + "\n" + "\n".join(names)
            await interaction.response.send_message(msg, ephemeral=True)
            return
        if act == "clear":
            bot.config.bot.listen_only_users = []
            bot._wl_skip_logged = False
            save_runtime_config(bot)
            await interaction.response.send_message(
                t("resp.whitelist_cleared", loc), ephemeral=True
            )
            return
        # add / remove require user
        if user is None:
            await interaction.response.send_message(
                t("resp.action_needs_user", loc, action=act), ephemeral=True
            )
            return
        if act == "add":
            if user.id in wl:
                msg = t("resp.whitelist_user_already", loc, name=user.display_name, id=user.id)
            else:
                wl.append(user.id)
                bot.config.bot.listen_only_users = wl
                bot._wl_skip_logged = False
                save_runtime_config(bot)
                msg = t("resp.whitelist_added", loc, name=user.display_name, id=user.id)
        else:  # remove
            if user.id not in wl:
                msg = t("resp.whitelist_user_not_present", loc, name=user.display_name)
            else:
                wl.remove(user.id)
                bot.config.bot.listen_only_users = wl
                bot._wl_skip_logged = False
                save_runtime_config(bot)
                msg = t("resp.whitelist_removed", loc, name=user.display_name)
        await interaction.response.send_message(msg, ephemeral=True)

    @admin_grp.command(
        name="owner",
        description=ls("cmd.admin.owner.desc"),
    )
    @app_commands.describe(
        action=ls("cmd.admin.owner.param.action"),
        user=ls("cmd.admin.owner.param.user"),
    )
    @app_commands.choices(
        action=[
            app_commands.Choice(name=ls("choice.action.add"), value="add"),
            app_commands.Choice(name=ls("choice.action.remove"), value="remove"),
            app_commands.Choice(name=ls("choice.action.list"), value="list"),
        ]
    )
    async def admin_owner(
        interaction: discord.Interaction,
        action: app_commands.Choice[str],
        user: discord.User | None = None,
    ):
        loc = interaction.locale
        if not _is_primary_owner(bot, interaction.user.id):
            await interaction.response.send_message(
                t("resp.owner_primary_only", loc), ephemeral=True
            )
            return
        act = action.value
        if act == "list":
            primary = bot.app_owner_id
            primary_u = bot.get_user(primary) if primary else None
            lines = [
                t(
                    "resp.owner_primary_label",
                    loc,
                    id=primary,
                    name=primary_u.display_name if primary_u else "?",
                )
            ]
            if not bot.extra_owner_ids:
                lines.append(t("resp.owner_co_empty", loc))
            else:
                lines.append(t("resp.owner_co_header", loc))
                for uid in sorted(bot.extra_owner_ids):
                    u = bot.get_user(uid)
                    lines.append(f"  - {uid} ({u.display_name if u else '?'})")
            await interaction.response.send_message("\n".join(lines), ephemeral=True)
            return
        if user is None:
            await interaction.response.send_message(
                t("resp.action_needs_user", loc, action=act), ephemeral=True
            )
            return
        if user.id == bot.app_owner_id:
            await interaction.response.send_message(
                t("resp.owner_cant_self_add", loc), ephemeral=True
            )
            return
        if act == "add":
            if user.id in bot.extra_owner_ids:
                msg = t("resp.owner_already_co", loc, name=user.display_name, id=user.id)
            else:
                bot.extra_owner_ids.add(user.id)
                save_runtime_config(bot)
                msg = t("resp.owner_added", loc, name=user.display_name, id=user.id)
        else:  # remove
            if user.id not in bot.extra_owner_ids:
                msg = t("resp.owner_not_co", loc, name=user.display_name)
            else:
                bot.extra_owner_ids.discard(user.id)
                save_runtime_config(bot)
                msg = t("resp.owner_removed", loc, name=user.display_name)
        await interaction.response.send_message(msg, ephemeral=True)

    # Admin groups work in DMs and guild channels alike: every command
    # gates on _is_owner and replies ephemerally, so the context restriction
    # adds no security — it only forced owners to switch to a DM window.
    # NOTE: the attribute is `allowed_contexts` — assigning to `.contexts`
    # is silently ignored by discord.py.
    for grp in (persona_grp, voice_grp, admin_grp):
        grp.allowed_contexts = discord.app_commands.AppCommandContext(
            guild=True, dm_channel=True, private_channel=False
        )
        tree.add_command(grp)
