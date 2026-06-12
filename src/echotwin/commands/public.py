"""Public slash commands: /join /leave /say /sleep /wake /persona current|list."""
from __future__ import annotations

import asyncio
import queue as sync_queue
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import voice_recv
from loguru import logger

from echotwin.i18n import prompts as _locale

from echotwin.audio.audio_source import StreamingOpusAudioSource
from echotwin.audio.ogg_demux import OggDemuxer
from echotwin.i18n import t, ls
from echotwin.persona import list_personas, render_system_prompt
from echotwin.providers.factory import make_tts
from echotwin.providers.llm.base import stream_text_only
from echotwin.session import SessionState

if TYPE_CHECKING:
    from echotwin.bot import VoiceAgentBot


def register_public_commands(tree: app_commands.CommandTree, bot: "VoiceAgentBot") -> None:

    @tree.command(name="join", description=ls("cmd.join.desc"))
    async def cmd_join(interaction: discord.Interaction):
        loc = interaction.locale
        if (
            not isinstance(interaction.user, discord.Member)
            or not interaction.user.voice
            or not interaction.user.voice.channel
        ):
            await interaction.response.send_message(t("resp.user_not_in_voice", loc), ephemeral=True)
            return
        channel = interaction.user.voice.channel
        await interaction.response.defer(ephemeral=True)
        try:
            if interaction.guild and interaction.guild.voice_client:
                vc = interaction.guild.voice_client
                if vc.channel != channel:
                    await vc.move_to(channel)
            else:
                vc = await channel.connect(cls=voice_recv.VoiceRecvClient)

            # Start listening
            session = bot.get_or_create_session(interaction.guild.id)
            session.voice_channel_id = channel.id

            if isinstance(vc, voice_recv.VoiceRecvClient):
                bot.start_listening(vc, interaction.guild.id)
                logger.info(f"[guild {interaction.guild.id}] listening to voice channel {channel.name}")
        except Exception as e:
            logger.exception("join failed")
            await interaction.followup.send(t("resp.join_failed", loc, error=e), ephemeral=True)
            return

        await interaction.followup.send(t("resp.joined", loc, channel=channel.name), ephemeral=True)

        # Match nickname to persona — best-effort
        try:
            await bot.sync_nickname_in_guild(interaction.guild)
        except Exception as e:
            logger.warning(f"sync nickname failed: {e}")

        # Greeting
        if bot.config.bot.greeting.enabled:
            try:
                await _do_greeting(bot, vc, interaction.guild)
            except Exception as e:
                logger.warning(f"greeting failed: {e}")

    @tree.command(name="leave", description=ls("cmd.leave.desc"))
    async def cmd_leave(interaction: discord.Interaction):
        loc = interaction.locale
        guild = interaction.guild
        if not guild or not guild.voice_client:
            await interaction.response.send_message(t("resp.bot_not_in_voice", loc), ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        try:
            await _graceful_leave(bot, guild, reason="slash_command")
        except Exception as e:
            logger.warning(f"leave error: {e}")
        await interaction.followup.send(t("resp.leaving", loc), ephemeral=True)

    @tree.command(name="say", description=ls("cmd.say.desc"))
    @app_commands.describe(text=ls("cmd.say.param.text"))
    async def cmd_say(interaction: discord.Interaction, text: str):
        loc = interaction.locale
        if not interaction.guild or not interaction.guild.voice_client:
            await interaction.response.send_message(
                t("resp.bot_not_in_voice_join_first", loc), ephemeral=True
            )
            return
        if len(text) > 500:
            await interaction.response.send_message(t("resp.text_too_long", loc), ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        try:
            await _speak_text(interaction.guild.voice_client, bot, text)
            await interaction.followup.send(
                t("resp.spoken", loc, snippet=text[:30]), ephemeral=True
            )
        except Exception as e:
            logger.exception("/say failed")
            await interaction.followup.send(
                t("resp.generic_failure", loc, error=e), ephemeral=True
            )

    @tree.command(name="sleep", description=ls("cmd.sleep.desc"))
    async def cmd_sleep(interaction: discord.Interaction):
        loc = interaction.locale
        if not interaction.guild:
            return
        session = bot.sessions.get(interaction.guild.id)
        if not session:
            await interaction.response.send_message(t("resp.bot_not_in_voice", loc), ephemeral=True)
            return
        session.state = SessionState.SLEEPING
        await interaction.response.send_message(t("resp.sleeping_now", loc), ephemeral=True)

    @tree.command(name="wake", description=ls("cmd.wake.desc"))
    async def cmd_wake(interaction: discord.Interaction):
        loc = interaction.locale
        if not interaction.guild:
            return
        session = bot.sessions.get(interaction.guild.id)
        if not session:
            await interaction.response.send_message(t("resp.bot_not_in_voice", loc), ephemeral=True)
            return
        if session.state == SessionState.SLEEPING:
            session.state = SessionState.IDLE
            await interaction.response.send_message(t("resp.waking_up", loc), ephemeral=True)
        else:
            await interaction.response.send_message(t("resp.not_sleeping", loc), ephemeral=True)

    @tree.command(name="persona", description=ls("cmd.persona.desc"))
    @app_commands.describe(action=ls("cmd.persona.param.action"))
    @app_commands.choices(
        action=[
            app_commands.Choice(name=ls("choice.persona.current"), value="current"),
            app_commands.Choice(name=ls("choice.persona.list"), value="list"),
        ]
    )
    async def cmd_persona(interaction: discord.Interaction, action: app_commands.Choice[str]):
        loc = interaction.locale
        if action.value == "current":
            await interaction.response.send_message(
                t("resp.persona_current", loc, id=bot.persona.id, name=bot.persona.name),
                ephemeral=True,
            )
        else:
            from echotwin.persona import load_persona
            from pathlib import Path
            personas = list_personas("prompts")
            cur = bot.persona.id
            lines = []
            for pid in personas:
                marker = "⭐" if pid == cur else "  "
                # Read each persona's display name (best-effort; show id if load fails)
                try:
                    p = load_persona(Path("prompts") / "personas" / f"{pid}.md")
                    lines.append(f"{marker} {p.name}  (id: {pid})")
                except Exception:
                    lines.append(f"{marker} ?  (id: {pid})")
            text = "\n".join(lines)
            await interaction.response.send_message(f"```\n{text}\n```", ephemeral=True)


async def _do_greeting(bot: "VoiceAgentBot", voice_client: discord.VoiceClient, guild: discord.Guild) -> None:
    """Synthesize a greeting and play it via TTS."""
    text = bot.config.bot.greeting.text
    if text is None:
        # LLM-generated greeting
        channel_name = voice_client.channel.name if voice_client.channel else ""
        members = len(voice_client.channel.members) - 1 if voice_client.channel else 0
        sys_prompt = render_system_prompt(
            bot.persona,
            bot.persona.name,
            channel_name=channel_name,
            members_online=members,
        )
        prompt = _locale.GREETING_PROMPT[bot.persona.language].format(
            channel=channel_name, members=members
        )
        text = ""
        async for delta in stream_text_only(
            bot.llm, sys_prompt, [{"role": "user", "content": prompt}]
        ):
            text += delta
    if not text.strip():
        return
    await _speak_text(voice_client, bot, text)


async def _graceful_leave(bot: "VoiceAgentBot", guild: discord.Guild, reason: str) -> None:
    """Say farewell, then disconnect from voice."""
    vc = guild.voice_client
    if vc is None:
        return

    if bot.config.bot.farewell.enabled:
        text = bot.config.bot.farewell.text
        if text is None:
            try:
                channel_name = vc.channel.name if vc.channel else ""
                sys_prompt = render_system_prompt(
                    bot.persona,
                    bot.persona.name,
                    channel_name=channel_name,
                    members_online=1,
                )
                prompt = _locale.FAREWELL_PROMPT[bot.persona.language].format(
                    channel=channel_name, reason=reason
                )
                text = ""
                async for delta in stream_text_only(
                    bot.llm, sys_prompt, [{"role": "user", "content": prompt}]
                ):
                    text += delta
            except Exception as e:
                logger.warning(f"farewell LLM failed: {e}")
                # Per-persona farewell (correct language/voice) instead of a hardcoded phrase
                text = bot.persona.farewell_text
        if text and text.strip():
            try:
                await _speak_text(vc, bot, text)
            except Exception as e:
                logger.warning(f"farewell TTS failed: {e}")

    try:
        await vc.disconnect(force=False)
    except Exception:
        pass
    # Reset server nickname so the bot shows under its default app name when idle
    try:
        await bot.clear_nickname_in_guild(guild)
    except Exception as e:
        logger.warning(f"clear nickname failed: {e}")
    await bot.cleanup_session(guild.id)


async def _speak_text(voice_client: discord.VoiceClient, bot: "VoiceAgentBot", text: str) -> None:
    """Synthesize text via Fish Audio TTS and play in Discord voice channel.

    If the voice client is already playing, stop it first (interrupt previous).
    """
    if voice_client.is_playing():
        voice_client.stop()
        await asyncio.sleep(0.05)  # let stop propagate

    tts = make_tts(bot.config, voice_id=bot.active_voice_id(), persona=bot.persona)
    await tts.open()
    await tts.push_text(text)
    await tts.end_turn()

    tracker = getattr(bot, "cost_tracker", None)
    if tracker is not None:
        try:
            await tracker.record("fishaudio_tts", len(text.encode("utf-8")))
        except Exception as e:
            logger.warning(f"[cost] record failed: {e}")

    demux = OggDemuxer()
    frame_queue: sync_queue.Queue = sync_queue.Queue(maxsize=200)

    async def drain_packets():
        try:
            async for chunk in tts.packets():
                demux.feed(chunk)
                for opus_pkt in demux.packets():
                    while True:
                        try:
                            frame_queue.put_nowait(opus_pkt)
                            break
                        except sync_queue.Full:
                            await asyncio.sleep(0.005)
            for opus_pkt in demux.flush():
                try:
                    frame_queue.put_nowait(opus_pkt)
                except sync_queue.Full:
                    pass
        finally:
            try:
                frame_queue.put_nowait(None)
            except sync_queue.Full:
                pass

    drain_task = asyncio.create_task(drain_packets())

    source = StreamingOpusAudioSource(frame_queue)
    play_done = asyncio.Event()

    def after(error):
        if error:
            logger.warning(f"voice play error: {error}")
        bot.loop.call_soon_threadsafe(play_done.set)

    voice_client.play(source, after=after)
    try:
        await asyncio.wait_for(play_done.wait(), timeout=120.0)
    except asyncio.TimeoutError:
        if voice_client.is_playing():
            voice_client.stop()
    finally:
        drain_task.cancel()
        try:
            await drain_task
        except (asyncio.CancelledError, Exception):
            pass
        await tts.close()
