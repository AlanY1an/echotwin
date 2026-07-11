"""Stage 3+4 pipeline: LLM stream → sentence chunker → TTS WS → Discord playback.

The core 'respond' function takes a user utterance and runs the full
think→speak path, blocking until audio finishes (or aborted).
"""
from __future__ import annotations

import asyncio
import json
import queue as sync_queue
from typing import TYPE_CHECKING

import discord
from loguru import logger

from echotwin.audio.audio_source import StreamingOpusAudioSource
from echotwin.audio.ogg_demux import OggDemuxer
from echotwin.providers.factory import make_tts
from echotwin.providers.llm.base import (
    MessageEnd,
    TextDelta,
    ToolUseEnd,
    ToolUseInputDelta,
    ToolUseStart,
)
from echotwin.utils.sentence_chunker import SentenceChunker, speakable

if TYPE_CHECKING:
    from echotwin.bot import VoiceAgentBot
    from echotwin.session import VoiceSession


async def respond_to_user(
    bot: "VoiceAgentBot",
    session: "VoiceSession",
    voice_client: discord.VoiceClient,
    *,
    user_id: int,
    user_name: str,
    user_text: str,
    emotion: str,
    system_prompt: str,
    journey=None,
    pre_tts=None,
    pre_tts_open_task=None,
    spec_llm=None,
    merged=None,
) -> None:
    """One full turn: build prompt, stream LLM, push sentences to TTS, play audio."""
    from echotwin.session import SessionState

    logger.info(f"[respond] start: user={user_name} text={user_text!r}")
    if journey is not None:
        journey.mark("consumer_start")

    # Quota gate — refuse new turn with cached announcement if cap hit
    quota_guard = getattr(bot, "quota_guard", None)
    if quota_guard is not None and await quota_guard.should_block():
        limit_path = getattr(bot, "limit_audio_path", None)
        if limit_path is not None and limit_path.exists():
            logger.warning(f"[quota] blocked turn for {user_name}; playing cached limit message")
            try:
                await bot._play_cached_opus(voice_client, limit_path)
            except Exception as e:
                logger.warning(f"[quota] limit playback failed: {e}")
        else:
            logger.warning("[quota] blocked turn but no cached limit audio")
        return

    sentence_id = session.new_turn()
    session.current_addressee_id = user_id
    session.state = SessionState.PROCESSING

    # Build user message: JSON wrapped with speaker + emotion. When a
    # speculative LLM stream is attached, record ITS payload — that is what
    # the model actually saw (emotion may differ from the late sidecar value).
    if spec_llm is not None and not merged:
        user_payload = spec_llm.user_payload
    else:
        payload_obj = {"speaker": user_name, "emotion": emotion, "content": user_text}
        if merged:
            # Queued merge turn: hand the backlogged utterances to the model together
            # for a unified reply (the consumer already invalidated the speculative stream)
            payload_obj["queued_speakers"] = [f"{n}: {t}" for _, n, t in merged]
            payload_obj["note"] = (
                "你说话/思考期间这几个人也先后发言了;请综合所有人一次性"
                "简短回应,需要的话点名分别答,不要逐条重复"
            )
        organic_cfg = getattr(bot.config.bot, "organic", None)
        if organic_cfg is not None and organic_cfg.enabled and organic_cfg.ambient_context:
            # Freshness window: right after joining a channel last_bot_reply_ts=0;
            # without this, minutes of backlogged chatter would all be carried into
            # the first turn (the "dredging up old chat" issue observed live 2026-06-11)
            import time as _t
            freshness_floor = _t.time() - organic_cfg.ambient_max_age_s
            room = [
                f"{e['speaker']}: {e['text']}"
                for e in session.ambient
                if e["ts"] > session.last_bot_reply_ts and e["ts"] > freshness_floor
            ][-12:]
            # Budget guardrail: total ≤500 chars, trim starting from the oldest
            while room and sum(len(r) for r in room) > 500:
                room.pop(0)
            if room:
                payload_obj["recent_room_chat"] = room
        user_payload = json.dumps(payload_obj, ensure_ascii=False)
    session.dialogue.append({"role": "user", "content": user_payload})
    session.trim_history(bot.config.bot.history_window)

    chunker = SentenceChunker()
    demux = OggDemuxer()
    frame_queue: sync_queue.Queue = sync_queue.Queue(maxsize=200)

    # Filler: for predicted-slow turns, pre-queue a cached short phrase
    # into THIS turn's frame_queue — it plays first, the LLM reply appends
    # seamlessly. Best-effort; no second playback path.
    from echotwin.pipeline.filler import enqueue_filler_packets, should_play_filler
    if should_play_filler(user_text, bot.config.bot.filler_mode, bot.config.bot.filler_keywords):
        filler_path = bot.pick_filler_path() if hasattr(bot, "pick_filler_path") else None
        if filler_path is not None:
            n_pkts = enqueue_filler_packets(filler_path, frame_queue)
            if n_pkts:
                logger.info(f"[filler] queued {n_pkts} packets from {filler_path.name}")
                if journey is not None:
                    journey.mark("filler_queued")

    # TTS WS for this turn: prefer the connection pre-opened at endpoint time
    # (bot._finalize_utterance); fall back to a fresh one if none was attached
    # or the pre-open already failed/went stale. Either way the handshake runs
    # CONCURRENTLY with the LLM stream below — we await tts_open_task before
    # the first push_text, where open failures surface into the existing
    # abort/cleanup paths.
    tts = pre_tts
    tts_open_task = pre_tts_open_task
    if (
        tts is not None
        and tts_open_task is not None
        and tts_open_task.done()
        and (tts_open_task.cancelled() or tts_open_task.exception() is not None)
    ):
        logger.warning("[respond] pre-opened TTS unusable — opening fresh")
        try:
            await tts.close()
        except Exception:
            pass
        tts, tts_open_task = None, None
    if tts is None:
        tts = make_tts(bot.config, voice_id=bot.active_voice_id(), persona=bot.persona)
        tts_open_task = asyncio.create_task(tts.open())
    elif tts_open_task is None:
        tts_open_task = asyncio.create_task(tts.open())

    # Producer: drain TTS audio chunks → demux → frame_queue
    bytes_received = 0
    first_audio_logged = False

    async def drain_tts() -> None:
        nonlocal bytes_received, first_audio_logged
        try:
            async for ogg_chunk in tts.packets():
                if session.client_abort:
                    break
                bytes_received += len(ogg_chunk)
                if not first_audio_logged:
                    logger.info(f"[respond] first TTS audio chunk received ({len(ogg_chunk)}B)")
                    first_audio_logged = True
                    if journey is not None:
                        journey.mark("first_audio")
                demux.feed(ogg_chunk)
                for opus_pkt in demux.packets():
                    while True:
                        if session.client_abort:
                            return
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
        except Exception as e:
            logger.warning(f"drain_tts error: {e}")
        finally:
            logger.info(f"[respond] drain_tts done, total bytes_received={bytes_received}")
            if bytes_received == 0 and getattr(tts, "last_error", None):
                logger.error(
                    f"[respond] TTS produced NO audio — server protocol error: "
                    f"{tts.last_error} (the user heard silence this turn)"
                )
            # RELIABLY put end-of-stream sentinel — silently dropping it leaves
            # audio_source emitting SILENCE_OPUS forever and after() never fires.
            # Discord drains ~50 packets/sec so a full 200-cap queue clears in 4s.
            for _ in range(250):  # up to 5s of retries
                try:
                    frame_queue.put_nowait(None)
                    break
                except sync_queue.Full:
                    await asyncio.sleep(0.02)

    # Everything below acquires state that MUST be released on every exit
    # path — including exceptions (e.g. voice_client.play raising) and task
    # cancellation (cleanup_session cancelling the consumer). The finally
    # block at the bottom is the single cleanup point; without it,
    # session.is_audible sticks True (ACK filter then drops every short
    # utterance in this guild) and the Fish WS leaks.
    full_response = ""
    aborted_during_llm = False
    drain_task: asyncio.Task | None = None
    completed_normally = False
    # Cost accounting for this turn (recorded in the finally block)
    usage_input = usage_output = usage_cache_write = usage_cache_read = 0
    tts_bytes_sent = 0
    try:
        drain_task = asyncio.create_task(drain_tts())

        # Start playback immediately (frames appear as TTS produces them)
        audio_source = StreamingOpusAudioSource(frame_queue)
        play_done = asyncio.Event()

        def after(error):
            if error:
                logger.warning(f"play after-error: {error}")
            bot.loop.call_soon_threadsafe(play_done.set)

        # If the voice client is still playing something (greeting / previous turn /
        # cached fast response), stop it first — user has clearly initiated a new
        # turn and shouldn't have to wait through the prior audio.
        if voice_client.is_playing():
            logger.info("[respond] interrupting prior playback before starting new turn")
            try:
                voice_client.stop()
            except Exception as e:
                logger.warning(f"voice_client.stop() failed: {e}")
            await asyncio.sleep(0.05)  # let stop propagate to player thread

        voice_client.play(audio_source, after=after)
        # Mark bot as audibly producing audio — used by _finalize_utterance to
        # drop short ASR garbage that would otherwise trigger spurious barge-in
        session.is_audible = True

        # Stream LLM with optional tool-use round-trips, chunk into sentences, push to TTS.
        # When the model decides to call a tool we execute it, append the tool_result, and
        # re-stream the LLM until it produces a final text-only message.
        logger.info(f"[respond] starting LLM stream")
        tools_schema = None
        if getattr(bot, "tool_registry", None) is not None and not bot.tool_registry.is_empty():
            tools_schema = bot.tool_registry.to_anthropic_tools()
        # Speculative round 0 reuses the EXACT messages the spec stream saw
        # (tool rounds build on top of them).
        cur_messages = spec_llm.messages if spec_llm is not None else list(session.dialogue)
        spec_consumed = False

        try:
            max_rounds = 4  # cap tool round-trips per turn (safety)
            for round_idx in range(max_rounds):
                if session.client_abort:
                    aborted_during_llm = True
                    break

                cur_tool_uses: list[dict] = []  # {"id":..., "name":..., "partial_json":...}
                text_this_round = ""
                stop_reason = "end_turn"

                # Wrap stream iterator in a per-event timeout — 20s of silence from
                # the LLM means it's stuck (Anthropic's stream normally yields tokens
                # within 1-2s)
                if spec_llm is not None and not spec_consumed:
                    # Round 0: attach the pre-opened speculative stream
                    spec_consumed = True
                    stream_iter = spec_llm.events().__aiter__()
                else:
                    stream_iter = bot.llm.stream_chat(
                        system_prompt, cur_messages, tools=tools_schema
                    ).__aiter__()
                stalled = False
                while True:
                    try:
                        ev = await asyncio.wait_for(stream_iter.__anext__(), timeout=20.0)
                    except StopAsyncIteration:
                        break
                    except asyncio.TimeoutError:
                        logger.warning("[respond] LLM stream stalled >20s; aborting turn")
                        aborted_during_llm = True
                        stalled = True
                        break
                    if session.client_abort:
                        aborted_during_llm = True
                        break
                    if isinstance(ev, TextDelta):
                        if journey is not None and not getattr(journey, "_llm_marked", False):
                            journey.mark("llm_first_delta")
                            journey._llm_marked = True
                        full_response += ev.text
                        text_this_round += ev.text
                        for sentence in chunker.feed(ev.text):
                            if session.client_abort:
                                aborted_during_llm = True
                                break
                            if not speakable(sentence):
                                continue  # an empty chunk makes Fish finish the WHOLE stream — everything after goes silent
                            await tts_open_task  # WS ready (or raises the open failure)
                            await tts.push_text(sentence)
                            await tts.flush()
                            tts_bytes_sent += len(sentence.encode("utf-8"))
                    elif isinstance(ev, ToolUseStart):
                        cur_tool_uses.append(
                            {"id": ev.tool_use_id, "name": ev.name, "partial_json": ""}
                        )
                    elif isinstance(ev, ToolUseInputDelta):
                        if cur_tool_uses:
                            # Match by id, but in practice the active one is the last
                            for tu in cur_tool_uses:
                                if tu["id"] == ev.tool_use_id:
                                    tu["partial_json"] += ev.partial_json
                                    break
                            else:
                                cur_tool_uses[-1]["partial_json"] += ev.partial_json
                    elif isinstance(ev, ToolUseEnd):
                        pass
                    elif isinstance(ev, MessageEnd):
                        stop_reason = ev.stop_reason
                        # Sum usage across tool rounds (one MessageEnd per round)
                        usage_input += ev.input_tokens
                        usage_output += ev.output_tokens
                        usage_cache_write += ev.cache_creation_input_tokens
                        usage_cache_read += ev.cache_read_input_tokens

                # Close the iterator (cleans up Anthropic stream context)
                try:
                    await stream_iter.aclose()
                except Exception:
                    pass

                if aborted_during_llm:
                    break

                # If the model wants to call tools, run them and feed tool_result back
                if stop_reason == "tool_use" and cur_tool_uses:
                    # Save the assistant turn (text + tool_use blocks) to history
                    assistant_blocks: list[dict] = []
                    if text_this_round:
                        assistant_blocks.append({"type": "text", "text": text_this_round})
                    for tu in cur_tool_uses:
                        try:
                            tool_args = json.loads(tu["partial_json"]) if tu["partial_json"] else {}
                        except json.JSONDecodeError:
                            tool_args = {}
                        assistant_blocks.append(
                            {
                                "type": "tool_use",
                                "id": tu["id"],
                                "name": tu["name"],
                                "input": tool_args,
                            }
                        )
                    cur_messages = cur_messages + [
                        {"role": "assistant", "content": assistant_blocks}
                    ]
                    # Execute each tool and collect tool_result blocks
                    tool_result_blocks: list[dict] = []
                    for tu in cur_tool_uses:
                        try:
                            tool_args = json.loads(tu["partial_json"]) if tu["partial_json"] else {}
                        except json.JSONDecodeError:
                            tool_args = {}
                        result_text = await bot.tool_registry.execute(tu["name"], tool_args)
                        tool_result_blocks.append(
                            {
                                "type": "tool_result",
                                "tool_use_id": tu["id"],
                                "content": result_text,
                            }
                        )
                    cur_messages = cur_messages + [
                        {"role": "user", "content": tool_result_blocks}
                    ]
                    # Continue outer loop to let LLM emit final reply
                    continue

                # No tool call → done with this turn
                break

            if not aborted_during_llm and not session.client_abort:
                remaining = chunker.flush()
                if remaining and speakable(remaining):
                    await tts_open_task
                    await tts.push_text(remaining)
                    tts_bytes_sent += len(remaining.encode("utf-8"))
                await tts_open_task
                await tts.end_turn()
                logger.info(
                    f"[respond] LLM done, total_chars={len(full_response)} "
                    f"text={full_response!r}"
                )
        except Exception as e:
            logger.exception(f"LLM stream error: {e}")
            aborted_during_llm = True
            if (
                tts_open_task.done()
                and not tts_open_task.cancelled()
                and tts_open_task.exception() is not None
            ):
                # TTS never opened — the user heard NOTHING. Clear the
                # accumulated text so the finally block rolls back the user
                # message instead of committing a reply nobody heard.
                full_response = ""

        # If aborted, stop playback + cancel TTS immediately (don't hang on play_done)
        if aborted_during_llm or session.client_abort:
            logger.info("[respond] aborting playback + tts")
            try:
                if voice_client.is_playing():
                    voice_client.stop()
            except Exception:
                pass
            try:
                await tts.cancel()
            except Exception:
                pass
        else:
            # Wait for playback to finish, but stay responsive to barge-in (client_abort)
            # and bound the wait so a stuck TTS WS can't lock the consumer forever.
            # Most replies are < 30s; if we're past that something is wrong.
            deadline = asyncio.get_event_loop().time() + 30.0
            # Once drain_task finishes, audio_source has at most ~200 packets buffered
            # = 4s of audio. Allow extra 8s grace for playback + after() callback.
            # If play_done still isn't set by then, force-cleanup.
            drain_done_at: float | None = None
            while True:
                if play_done.is_set():
                    break
                if session.client_abort:
                    logger.info("[respond] client_abort during playback — stopping")
                    break
                if drain_task.done() and drain_done_at is None:
                    drain_done_at = asyncio.get_event_loop().time()
                if drain_done_at is not None and asyncio.get_event_loop().time() - drain_done_at > 8.0:
                    logger.warning(
                        f"[respond] play_done never fired 8s after drain ended; "
                        f"forcing cleanup (bytes_received={bytes_received})"
                    )
                    break
                if asyncio.get_event_loop().time() > deadline:
                    logger.warning(f"[respond] playback timeout (bytes_received={bytes_received})")
                    break
                try:
                    await asyncio.wait_for(play_done.wait(), timeout=0.25)
                    break
                except asyncio.TimeoutError:
                    continue
            if not play_done.is_set():
                try:
                    if voice_client.is_playing():
                        voice_client.stop()
                except Exception:
                    pass
                try:
                    await tts.cancel()
                except Exception:
                    pass

        completed_normally = True
    finally:
        # Cleanup — runs on EVERY exit path, including exceptions raised by
        # voice_client.play() and CancelledError from cleanup_session.
        # Settle the open task BEFORE tts.close() so teardown ordering is
        # deterministic (a cancel landing mid-handshake would otherwise let
        # close() run before the socket even exists, leaking the WS).
        if not tts_open_task.done():
            tts_open_task.cancel()
        await asyncio.gather(tts_open_task, return_exceptions=True)
        if spec_llm is not None:
            # Settle the speculative task on every exit (abort is idempotent;
            # no-op if the stream was fully consumed).
            try:
                await spec_llm.abort()
            except Exception:
                pass
        if drain_task is not None:
            drain_task.cancel()
            try:
                await drain_task
            except (asyncio.CancelledError, Exception):
                pass
        try:
            await tts.close()
        except Exception:
            pass
        # Bot no longer producing audio
        session.is_audible = False

        # Record this turn's spend (best-effort) — the quota guard and
        # /admin cost read from the cost tracker, so every turn must report.
        tracker = getattr(bot, "cost_tracker", None)
        if tracker is not None:
            ids = dict(
                guild_id=str(session.guild_id),
                user_id=str(user_id),
                sentence_id=sentence_id,
            )
            # Cost kind follows the active provider (claude_haiku_4_5 /
            # groq_qwen3_32b / …); unknown kinds price to 0 in the tracker.
            _kind = getattr(bot.llm, "cost_prefix", "claude_haiku_4_5")
            try:
                if usage_input:
                    await tracker.record(f"{_kind}_input", usage_input, **ids)
                if usage_output:
                    await tracker.record(f"{_kind}_output", usage_output, **ids)
                if usage_cache_write:
                    await tracker.record(f"{_kind}_cache_write", usage_cache_write, **ids)
                if usage_cache_read:
                    await tracker.record(f"{_kind}_cache_read", usage_cache_read, **ids)
                if tts_bytes_sent:
                    await tracker.record("fishaudio_tts", tts_bytes_sent, **ids)
            except Exception as e:
                logger.warning(f"[cost] record failed: {e}")

        if journey is not None:
            logger.info(journey.line())

        # Update history if response was completed (not aborted). Abnormal
        # exits (exception / cancellation) always roll back the user message.
        if completed_normally and not session.client_abort and full_response.strip():
            # Eavesdropping is in-the-moment reference, not memory: strip
            # recent_room_chat before committing history, otherwise stale chatter
            # snapshots recur across the 20-turn history and the model keeps
            # dragging old topics along
            if session.dialogue and session.dialogue[-1]["role"] == "user":
                try:
                    obj = json.loads(session.dialogue[-1]["content"])
                    if obj.pop("recent_room_chat", None) is not None:
                        session.dialogue[-1] = {
                            "role": "user",
                            "content": json.dumps(obj, ensure_ascii=False),
                        }
                except (ValueError, TypeError):
                    pass
            session.dialogue.append({"role": "assistant", "content": full_response})
        else:
            # Roll back the user message we just appended (we never finished responding)
            if session.dialogue and session.dialogue[-1]["role"] == "user":
                session.dialogue.pop()

        # Compare-and-set: a /sleep issued mid-turn sets SLEEPING — an
        # unconditional IDLE here would silently wake the bot back up.
        if session.state == SessionState.PROCESSING:
            session.state = SessionState.IDLE
        session.current_addressee_id = None
        if completed_normally:
            session.last_activity_time = asyncio.get_event_loop().time()
            # Record bot-spoke timestamp + addressee for addressee continuation rule
            import time as _t
            session.last_bot_speak_time = _t.time()
            session.last_addressee_id = user_id
            # Organic: topic-continuity signal + addressee enters the active-conversation window
            if full_response.strip():
                session.last_bot_reply = full_response
                session.last_bot_reply_ts = _t.time()
                session.last_voice_event = "bot"
            session.organic_participants[user_id] = _t.time()
            # Users answered together in a merge turn also enter the active-conversation window
            for _mid, _, _ in merged or []:
                session.organic_participants[_mid] = _t.time()
