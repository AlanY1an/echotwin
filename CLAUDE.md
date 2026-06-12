# EchoTwin — Claude session brief

Discord voice bot. Cloned-voice TTS (Fish Audio) + Claude Haiku 4.5 LLM with tool calling + local streaming ASR (sherpa-onnx zipformer) + Silero VAD. Multi-party safe via a three-layer addressee pipeline (table-lookup reflexes → LLM arbiter → heuristic fallback). Solo dev, single repo, master branch. Chinese-first product (persona, UI strings, prompts, test data are Chinese); code comments and docs are English.

## Run / test commands

```bash
.venv/bin/python -m echotwin          # start bot (reads .env + config.yaml)
.venv/bin/pytest tests/                  # full suite (~320 tests, ~15s; live-API tests auto-excluded)
.venv/bin/pytest tests/ -m live          # ONLY the live tests (real Anthropic/Fish calls — costs money)
.venv/bin/pytest tests/unit/test_X.py -v # single file
.venv/bin/python -m tests.perf.bench_e2e_latency  # E2E latency benchmark (live API)
.venv/bin/python -m scripts.verify_organic        # multi-party scenario replay (offline)
.venv/bin/python -m scripts.synthesize_fast_responses  # warm wake-word audio cache
bash scripts/download_models.sh          # download Silero VAD + SenseVoice (one-time)
kill -HUP <pid>                          # hot-reload config.yaml + persona file
```

No Dockerfile / CI. Runs locally on macOS arm64. Logs go to stderr AND `data/logs/echotwin_YYYY-MM-DD.log` (20 MB rotation, sanitized).

## Repo layout

```
src/echotwin/
├── __main__.py            # entrypoint: load opus, apply DAVE patch, load .env+yaml, run bot
├── bot.py                 # VoiceAgentBot(discord.Client) — sessions, on_user_audio, consumer loop
├── config.py              # Pydantic Config + env: prefix resolution
├── config_watcher.py      # SIGHUP handler — hot-reload persona + timeouts
├── persona.py             # Persona dataclass + load_persona (frontmatter)
├── session.py             # VoiceSession (per-guild) + SessionState enum
├── audio/
│   ├── dave_patch.py      # CRITICAL — monkey-patch davey decryption (see Gotchas)
│   ├── audio_source.py    # StreamingOpusAudioSource → discord.AudioSource bridge
│   ├── ogg_demux.py       # OGG container → opus packet extractor
│   ├── preroll_buffer.py  # 300ms pre-VAD ring buffer
│   └── resampler.py       # soxr 48k↔16k
├── pipeline/
│   ├── listen.py          # discord-ext-voice-recv callback
│   ├── addressee.py       # AddresseeDetector (legacy mode, used when organic is off)
│   ├── organic.py         # multi-party addressee: hard_verdict reflexes + classify heuristic fallback
│   ├── arbiter.py         # gray-zone LLM arbitration (accept/reject/clarify/open_floor JSON verdict)
│   ├── speculative.py     # SpeculativeLLM — pre-open the LLM stream on stable ASR partials
│   ├── filler.py          # cached filler phrases pre-queued on predicted-slow turns
│   └── think_speak.py     # respond_to_user — LLM tool loop + TTS + playback
├── providers/
│   ├── factory.py         # make_vad/asr/llm/tts/arbiter_llm (provider switch)
│   ├── vad/silero.py      # Silero ONNX
│   ├── asr/sherpa_stream.py # streaming zipformer — partial_text while speaking (default)
│   ├── asr/funasr_local.py # SenseVoiceSmall batch ASR + emotion (class-level model cache)
│   ├── asr/sensevoice_parse.py  # tolerant <|tag|> parser
│   ├── llm/claude_haiku.py # Anthropic SDK → typed event stream
│   ├── llm/groq.py        # Groq OpenAI-compatible (raw aiohttp) — arbiter only
│   └── tts/fish_audio_stream.py  # msgpack WebSocket /v1/tts/live
├── tools/                 # base + registry + get_time / get_date / get_weather
├── wake_word/             # matcher + fast_response cache
├── commands/public.py     # /join /leave /say /sleep /wake /persona
├── commands/owner_dm.py   # /persona-admin /voice-admin /admin (DM only, owner only)
├── cost/{tracker,pricing}.py  # SQLite cost ledger
├── monitoring/health_server.py  # aiohttp :9090 healthz/stats.json
└── utils/{retry,quota,sentence_chunker}.py

prompts/personas/*.md      # YAML frontmatter + body (system prompt)
prompts/base_template.md   # wraps persona body: voice rules, emotion tags, tool discipline, injection defenses
config.yaml                # main config (env: prefix → environment variable)
data/                      # costs.db, logs/, wake_responses/{persona_id}/*.ogg, runtime_config.json
models/{silero_vad,SenseVoiceSmall}/  # downloaded weights (sherpa model auto-downloads from HF)
tests/{unit,integration,perf}/        # pytest, asyncio_mode=auto; fixtures/addressee_golden.jsonl
```

Spec + plan documents live OUTSIDE the repo at `../docs/superpowers/{specs,plans}/`. Working notes in gitignored `dev-docs/` (organized by task folder, see `dev-docs/README.md`).

## Key conventions

- **Config**: pydantic v2 `BaseModel` (NOT dataclass). `env:VAR_NAME` strings in YAML resolve via `_walk` in `config.py:load_config`.
- **Logging**: loguru (NOT stdlib logging). Sensitive-value filter + file sink in `logging_setup.py`.
- **Code comments and docs are English; user-facing strings, prompts, personas, and test data are Chinese.** Commit messages are Chinese.
- **Persona model**: `@dataclass(frozen=True)` Persona with `id, name, voice_id, wake_words, fast_responses, limit_exceeded_text, farewell_text, system_prompt` + per-persona Fish TTS knobs. Loaded by `load_persona(path)` via `python-frontmatter`.
- **bot.persona** is source of truth for `voice_id` and `wake_words`. `config.tts.fish_audio_stream.voice_id` is an OPTIONAL override (`bot.active_voice_id()` returns `config_voice_id or persona.voice_id`).
- **Async everywhere**: discord.py, aiohttp, aiosqlite, anthropic AsyncAnthropic, websockets.
- **Audio path**: Discord opus 48kHz stereo → downmix mono → resample 16k for VAD only → ASR consumes 48k mono. Fish Audio TTS returns OGG/Opus 48kHz mono.
- **TTS protocol**: msgpack over WebSocket. Events: `start` (open) / `text` (push_text) / `flush` / `stop` (end_turn) / `audio` (incoming) / `finish` (incoming, ends stream).
- **LLM event stream**: not raw text — typed events `TextDelta | ToolUseStart | ToolUseInputDelta | ToolUseEnd | MessageEnd` defined in `providers/llm/base.py`. The think_speak loop drives multi-round tool execution (max 4 rounds). `MessageEnd` carries token usage (input/output/cache_write/cache_read) — new LLM providers must populate it or cost tracking goes blind.
- **Cost tracking**: every paid call reports to `bot.cost_tracker.record(kind, amount)` — LLM usage + TTS UTF-8 bytes from think_speak's finally block, `/say` via `_speak_text`, fast-response synth via `_synth_with_persona`, gray-zone arbitration inside `arbitrate()` (model-specific `cost_prefix`). Pricing keys in `cost/pricing.py`. The quota guard reads this DB; a new paid path that doesn't record is invisible to budgets.
- **Listening**: ALWAYS attach via `bot.start_listening(vc, guild_id)`, never `vc.listen(listener)` directly — start_listening registers the `after=` death watch that auto-restarts the reader (capped 5×) when voice_recv's AudioReader dies on a feed_rtp exception.

## Addressee decision (organic.enabled, the default)

Three layers, in order. The design rule: **layer 1 does table lookups only — all semantic judgment belongs to the LLM** (see gotcha 18).

1. **`hard_verdict` reflexes** (organic.py, free, instant): wake word at sentence edge → instant ACCEPT (feeds the fast-response cache + speculation, must stay 0ms); only one human in channel (solo) → ACCEPT; ≤3-char fragments with no meaning (not a question, not particle-ended, not in the backchannel word set) → REJECT. Everything else → gray zone.
2. **LLM arbiter** (arbiter.py): ships the utterance + speaker + last ~6 ambient lines + bot's last reply + last addressee to a small LLM, gets back a one-line JSON verdict `accept|reject|clarify|open_floor` with a reason (logged as `[arbiter]`/`signals=['llm:…']`). Default provider: Groq `qwen/qwen3-32b` (~350ms, `llm.groq` config, needs GROQ_API_KEY; auto-falls back to the conversation Haiku when the key is missing). Few-shot examples in `ARBITER_SYSTEM` are load-bearing — zero-shot prompting on this task is near chance level. Guard rails: `arbiter_timeout_ms` (1.5s) and `arbiter_max_per_min` (20/guild sliding window).
3. **`classify` heuristic fallback** (organic.py): the full scoring ruleset, used only when arbitration fails/times out/rate-limits, or with `gray_zone: heuristic`. Guarded by the golden set (`tests/fixtures/addressee_golden.jsonl`, ~70 labeled real-traffic cases; metrics: miss ≤10%, wrong-accept ≤10%) and `scripts/verify_organic.py`.

Verdict dispatch in `_finalize_utterance`: ACCEPT → enqueue; REJECT → ambient note (rolling transcript, injected into payloads as `recent_room_chat`, max 12 lines / 500 chars / 120s fresh, stripped from history on commit — ambient is context, not memory); CLARIFY → cached "are you talking to me?" audio (per-user cooldown); OPEN_FLOOR → wait `open_floor_wait_ms`, yield if a human takes the floor, else self-select; MENTION → reply with probability `mention_reply_rate` (default 0).

`addressee.py` (wake-word + 4 rules) is the legacy detector, used only when `organic.enabled: false`.

## Pipeline flow (think_speak.py:respond_to_user)

```
audio frame → listen.py write (user=None? → rate-limited WARN + drop)
  → schedule on_user_audio (future logged via done callback — exceptions visible)
  → whitelist gate (per-user log) → DAVE decrypt → opus decode (per-user ok/fail
    counters in session.opus_ok/opus_fail — NEVER session-global)
  → downmix → preroll buffer push → VAD (16k, noise gate only)
  → first real packet: mark in_speech + snapshot per-user counter, drain preroll into ASR
  → streaming ASR partials feed _maybe_spawn_spec_llm: when speculative_llm is on,
    a stable partial that hard_verdict ACCEPTs pre-opens the LLM stream
    (gray-zone partials never speculate — they must wait for arbitration)
  → watchdog endpoint (config.bot.endpoint_silence_ms, default 600ms wallclock silence):
    reset + preroll.clear() → _spawn_finalize (background task, per-user dedup defers
    instead of dropping; snapshot taken synchronously before spawn)
  → _finalize_utterance: ASR final → empty? (logged) → <600ms? (per-user duration, logged)
       → addressee decision (three layers, above) → verdict dispatch
       ACCEPT & wake-only → play random cached fast_response, return
       ACCEPT → strip wake word; PRE-OPEN TTS WS if queue empty (attached to the
             Utterance; dropped utterances MUST go through bot._discard_utterance
             or the Fish WS leaks); attach matched SpeculativeLLM; barge-in check
             (current addressee re-speaking aborts the playing turn); enqueue
  → consumer loop: drain-merge (everything queued during playback folds into ONE
       turn — payload gets queued_speakers; merged-away utterances' TTS/spec released,
       primary's spec invalidated) → respond_to_user(...)
       quota.should_block? → play cached _limit.ogg, return
       filler: slow turns (filler_keywords hit) pre-queue a cached persona phrase
         into frame_queue — plays first, LLM audio appends seamlessly
       TTS WS: reuse the pre-opened one from the Utterance (fallback: fresh);
         open runs CONCURRENTLY with the LLM stream (tts_open_task;
         awaited before first push_text — don't re-serialize it)
       try:
         LLM stream loop (max 4 rounds, per-event 20s timeout):
           TextDelta → SentenceChunker → speakable() gate → tts.push_text + flush
           ToolUseStart/InputDelta/End → accumulate tool_use blocks
           MessageEnd(tool_use) → execute tools, append tool_result, restart stream
           MessageEnd(end_turn) → flush remaining (speakable-gated) + tts.end_turn(), break
           MessageEnd also accumulates token usage across rounds
         wait for play_done OR client_abort OR 30s deadline (poll every 250ms)
       finally:  # runs on EVERY exit incl. exceptions + CancelledError
         drain_task.cancel(), tts.close(), is_audible=False
         cost_tracker.record(LLM tokens + TTS bytes)
         commit history (completed_normally && !client_abort) else roll back user msg
           — recent_room_chat is stripped from the committed user message
         state=IDLE; organic window/last-reply state on normal completion only
```

Per-turn `[latency]` log line: endpoint→asr_done→consumer_start→llm_first_delta→first_audio stage deltas. Real-machine steady state: ~400-1100ms mouth-to-ear; speculation hits show llm_first_delta ≈ 0.

## Gotchas — these will bite you

1. **DAVE end-to-end encryption** (`audio/dave_patch.py`): Discord enforces DAVE E2EE on voice. `discord-ext-voice-recv` only handles the RTP layer; the opus payload stays encrypted → libopus rejects with "corrupted stream". The patch monkey-patches `AudioReader.__init__` and `decryptor.decrypt_rtp` to call `davey.DaveSession.decrypt(user_id, MediaType.audio, ...)` after RTP decrypt. Applied at startup in `__main__.py`. NEVER REMOVE.

2. **FunASR class-level model cache** (`providers/asr/funasr_local.py`): SenseVoice loads in 5-10s. Multiple users in a channel each get a FunASRLocal instance, so the loaded model is shared via `_model_cache: dict[(model_dir, device), model] + _model_cache_lock`. Don't move this back to instance-level.

3. **SenseVoice tag-format drift** (`providers/asr/sensevoice_parse.py`): legacy-style `<|zh|><|SAD|><|EVENT|>content` AND current FunASR-style `<|EMO_UNKNOWN|><|Speech|><|withitn|>content` both exist. Parser is tolerant of any tag order.

4. **VAD pre-roll** (`audio/preroll_buffer.py`): VAD has inherent latency; without buffering the first 200-300ms of each utterance, ASR drops the leading syllable. Buffer holds last 300ms (15 frames × 20ms) of 48k mono PCM, drained into ASR on `vad_result.speech_started == True`.

5. **Playback wait must poll client_abort** (`pipeline/think_speak.py`): a bare `await asyncio.wait_for(play_done.wait(), ...)` will not unblock when barge-in fires. The loop polls `play_done | client_abort | 30s deadline` every 250ms. If `play_done` doesn't fire (TTS WS stalled), we still recover.

6. **TTS finish-event silent failure**: Fish Audio occasionally accepts a turn but never sends `finish` back. drain_tts would hang indefinitely; the 30s playback deadline + `bytes_received` diagnostic log catches this. Don't extend the deadline.

7. **Empty chunks kill the whole Fish stream**: pushing a chunk with no speakable content (`\n`, emotion-tag-only, punctuation-only) makes Fish return an "empty audio" error AND finish the stream — every chunk pushed afterwards goes silent (LLM replies containing `\n\n` lost their second half this way). Both push sites gate on `speakable()` (`utils/sentence_chunker.py`). Keep it that way.

8. **`wake_word_required` config flag**: obsolete (the addressee layers own wake-word logic) but the field must stay for old runtime_config.json compatibility. Don't add new code that reads it.

9. **`config.tts.fish_audio_stream.voice_id` is an override**, NOT the source of truth. Always go through `bot.active_voice_id()`.

10. **runtime_config.json** (`commands/owner_dm.py:save_runtime_config`): owner slash commands persist `active_persona`, `voice_id_override`, `wake_word_required`, `listen_only_users`, `extra_owner_ids` here — only those five fields. Loaded in setup_hook BEFORE `_init_persona_resources()`; keep that order or a runtime persona override silently uses the yaml persona's resources after restart. `listen_only_users` non-empty = the bot IGNORES everyone else (per-user log on first drop) — check this first when "the bot can't hear person X".

11. **wttr.in quirks** (`tools/get_weather.py`): returns JSON as text/plain, so `r.json(content_type=None)` is required. Geocoding is approximate, so the reply always shows the user's input as the city name, not `nearest_area`.

12. **DAVE sometimes fails on the first frames** (`UnencryptedWhenPassthroughDisabled`): davey starts with passthrough disabled by protocol design — ~1-3 frame loss at session start is expected and retrying is useless. The patch enables `set_passthrough_mode(True, 10)` once per session, skips non-opus payload types (≠120), and counts expected vs UNEXPECTED failures separately. If `Unencrypted...` appears MID-session, that's an epoch/transition problem, not startup noise.

13. **trim_history must keep the dialogue user-first** (`session.py`): the Anthropic API 400s on assistant-first message lists, and think_speak appends the user msg BEFORE trimming. The trim drops leading non-user messages after slicing — don't "simplify" it back to a bare slice (that bug permanently bricked the bot after 20 turns).

14. **respond_to_user cleanup lives in a finally block** (`pipeline/think_speak.py`): TTS close, `is_audible=False`, state reset, history commit/rollback, cost recording. New early-exit paths inside the try are fine; new acquisitions must release in that finally. A stuck `is_audible=True` makes the backchannel filter eat every short utterance in the guild.

15. **Per-user opus counters** (`session.opus_ok/opus_fail` dicts): the <600ms utterance filter computes duration from these. A session-global counter gets polluted by concurrent speakers and zeroed by new-user init — both caused real utterances to be dropped. Keep them per-user.

16. **`[stats]` log is the voice-detection debugging entrypoint** (every 5s per guild): `dave_epoch / reader=alive|DEAD / mls_users / per-user att/ok/fail/pass` + per-user packet counts. "Can't hear user X" triage: attempts flat → Discord not delivering; fail rising → DAVE decrypt broken (epoch); reader=DEAD → receive thread died (auto-restart should fire); all normal → app-layer filter dropped it (each filter logs). Also check `[amp]` rms: a speaker on open speakers gets their mic suppressed by their own client's echo cancellation WHILE the bot plays (rms ≈ 0 arrives server-side) — that is client-side physics, not a bug; headphones fix it. For SPEED issues read the per-turn `[latency]` line instead.

17. **Pinned deps** (`pyproject.toml`): discord.py / discord-ext-voice-recv / davey are pinned EXACTLY because dave_patch.py + voice_recv_patch.py monkey-patch their internals. Verify the patches before bumping any of them.

18. **Streaming ASR engine = sherpa-onnx, NOT funasr streaming** (`asr/sherpa_stream.py`): funasr's paraformer-zh-streaming runs slower than realtime on M-series CPU (RTF≈1.5, failed all spike gates) — do not "switch back to the same library for consistency". sherpa zipformer int8: ~15ms/chunk, ~22ms final flush. Evidence: `scripts/spike_streaming_asr.py` vs `scripts/spike_sherpa_streaming.py`. Its `speculate()` deliberately returns (None,-1): the final flush (0.4s silence + input_finished) often emits the last word; adopting the partial would truncate it.

19. **Speculative LLM** (`pipeline/speculative.py`, `bot.speculative_llm`): the watchdog pre-opens the LLM stream on stable streaming-ASR partials; finalize carries it into the Utterance only if final text + dialogue-length snapshot match, else aborts. Every drop path MUST settle it (finalize try/finally + _discard_utterance) — a leaked stream keeps billing at Anthropic. Aborted speculations are NOT in costs.db (no MessageEnd = no usage); count `[spec-llm] wasted` logs instead. The trigger gate uses `hard_verdict` — only hard-accepts (wake word at edge / solo) speculate.

20. **Don't add semantic rules back into the reflex layer** (`pipeline/organic.py` + `arbiter.py`): live multi-party traffic proved that scoring rules for second-person reference, character overlap, responsiveness, backchannel word lists etc. are an endless patch treadmill — every new mis-accept pattern should become an arbiter few-shot example or a golden-set case, not a new `if`. `hard_verdict` stays at three table lookups. `arbitrate()` records its own cost (`cost_prefix` per model; `pricing.py` must have matching price entries).

## Adding new things

- **New LLM tool**: create `tools/xxx.py` with `Tool` subclass (name, description, input_schema, async execute). Register in `bot.py:setup_hook` under the `if "xxx" in enabled` block. Add `xxx` to `config.yaml:tools.enabled`. Add unit test mocking the API call. Note `base_template.md` instructs the persona to call tools only when explicitly asked (spontaneous tool calls add a full LLM round ≈ +1-2s).

- **New persona**: drop `prompts/personas/<id>.md` with frontmatter (`name`, `voice_id` required; `wake_words`, `fast_responses`, `limit_exceeded_text`, `farewell_text`, TTS knobs optional). Switch via `/persona-admin use <id>` or `config.yaml:bot.active_persona`. Persona swap auto-rebuilds wake matcher + addressee detector + fast response cache (re-synthesizes if needed).

- **New LLM provider**: implement `providers/llm/base.py:LLMProvider` (async `stream_chat` yielding the typed events). Add elif branch in `providers/factory.py:make_llm`. Switch via `config.yaml:llm.provider`. Populate MessageEnd usage and add pricing keys.

- **New ASR provider**: implement `providers/asr/base.py:ASRProvider` (the `feed_audio` / `end_utterance` interface). Local providers should use class-level model cache. Add elif branch in `providers/factory.py:make_asr`.

- **New TTS provider**: implement `providers/tts/base.py:TTSProvider` (open / push_text / flush / end_turn / packets / cancel / close). Format MUST be OGG/Opus 48kHz mono for the existing audio_source pipeline.

- **New arbiter model**: add a provider branch in `factory.py:make_arbiter_llm`, matching `<prefix>_input/_output` keys in `cost/pricing.py`, and set `organic.arbiter_provider`.

## Slash commands

Public (everyone): `/join`, `/leave`, `/say <text>` (max 500 chars), `/sleep`, `/wake`, `/persona current|list`.

Owner-only (DM only): `/persona-admin use <name>`, `/persona-admin reload`, `/voice-admin set <id>`, `/voice-admin show`, `/admin cost`, `/admin health`, `/admin wakeword on|off`, `/admin whitelist add|remove|list|clear`, `/admin owner add|remove|list`, `/admin reload-config`, `/admin restart`.

`TEST_GUILD_ID` env var — when set, slash commands sync to that guild instantly (vs ~1h global).

## What works / what's known-flaky

- **Working**: full conversation loop, tool calls, wake-word fast path, organic multi-party addressee pipeline (solo + wake paths fully validated live; gray-zone arbitration validated on limited live samples), barge-in (headphones; see gotcha 16 for the speakers caveat), queue merging, speculation, filler, two-stage idle, hot reload, quota cap, reader-death auto-restart.
- **Known limitation**: a speaker on open speakers cannot barge in — their own Discord client's echo cancellation suppresses their mic while the bot plays (server receives silence). Client-side; headphones fix it.
- **Watch items**: gray-zone arbiter quality at multi-party scale (collect `[arbiter]` logs, backfill golden set); per-user opus decode failures concentrating on specific users (candidate cause of "sometimes can't hear X"); ASR confidence as an arbiter input (strongest addressee signal per the literature; sherpa exposure unverified).
- **Not implemented (deliberately, see v2 spec)**: voiceprint, long-term memory, music playback, Home Assistant, custom-trained wake-word ONNX, backchannel utterances by the bot, local-model arbitration (golden set is the ready-made eval gate if attempted).

## Don't

- Don't introduce stdlib logging — everything uses loguru.
- Don't restructure `dave_patch.py` — it's load-bearing.
- Don't call `vc.listen()` directly — use `bot.start_listening()` (death watch, see Key conventions).
- Don't run `pytest -m live` casually — those tests hit paid APIs.
- Don't remove `wake_word_required` field from config (old runtime_config.json files may reference it).
- Don't add semantic judgment to `hard_verdict` — gray zone belongs to the arbiter (gotcha 20).
- Don't push to remote without asking.
- Don't run `git rebase -i` or `git reset --hard` without explicit instruction.
