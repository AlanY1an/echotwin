**English** | [简体中文](README.zh.md)

# EchoTwin

*Give a voice an echo twin — a Discord companion that talks back in a cloned voice.*

Full-duplex realtime Discord voice companion. Fish Audio cloned-voice TTS + Claude Haiku 4.5 LLM (with tool calling) + local streaming ASR (sherpa-onnx zipformer, partials while you speak) + Silero VAD.

**What it does best today: one-on-one voice conversation.** Join a channel, talk naturally, get cloned-voice replies in well under a second (~400-1100ms mouth-to-ear, measured live) — speculative ASR/LLM execution, pre-opened TTS sockets, and cached fillers do the work. Barge-in, tool calls (time/date/weather), hot-swappable personas, per-turn cost tracking with budget caps.

**Experimental: organic multi-party mode.** In group channels, a three-layer addressee pipeline decides whether each utterance is directed at the bot — table-lookup reflexes settle the obvious cases instantly, ambiguous ones go to a fast LLM arbiter (Groq qwen3-32b, ~350ms, reading the room's recent transcript), and a golden-set-tested heuristic ruleset backstops failures. Rejected chatter feeds a rolling ambient transcript so accepted replies land in context; open questions yield the floor to humans first; utterances queued during playback merge into one reply. It works and ships on by default, but it's under active development — expect occasional wrong calls about when to speak.

The product surface is Chinese-first (personas, prompts, test data); code and docs are English.

> New here? **[`docs/SETUP.md`](docs/SETUP.md)** walks from zero (Discord app, voice cloning, API keys) to a talking bot in ~15 minutes. Layer-by-layer pipeline + debug guide: [`docs/PIPELINE.md`](docs/PIPELINE.md)

## Quick start (macOS dev)

```bash
# 1. Install
python3.11 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

# 2. Download local models (Silero VAD ~2 MB + SenseVoiceSmall ~234 MB)
bash scripts/download_models.sh

# 3. Configure
cp .env.example .env       # fill in DISCORD_TOKEN / FISH_AUDIO_API_KEY / ANTHROPIC_API_KEY
                           # optional: GROQ_API_KEY (multi-party gray-zone arbiter;
                           # falls back to the conversation Haiku without it)
cp config.example.yaml config.yaml   # default persona / voice id / thresholds prefilled

# 4. Run
python -m echotwin
```

Faster slash-command sync during development:

```bash
TEST_GUILD_ID=<your test guild id> python -m echotwin
```

## Discord commands

All slash command labels are localized — Discord shows them in your client language (English / Simplified Chinese supported, Traditional Chinese aliases to Simplified, anything else falls back to English). Add a new locale by extending `src/echotwin/i18n/strings.py`.

### Public (run in any channel)

| Command | What it does |
|---|---|
| `/join` | Bot joins your current voice channel |
| `/leave` | Bot leaves (with farewell) |
| `/say <text>` | Speak text in the cloned voice (debug) |
| `/sleep` | Stay in channel but go quiet (`/wake` to resume) |
| `/wake` | Wake from sleep |
| `/persona current\|list` | Show active persona / list all personas |

### Owner-only (DM the bot)

| Command | What it does |
|---|---|
| `/persona-admin use <name>` | Switch persona (clears all guild histories, refreshes wake words + addressee detector + fast-response audio cache) |
| `/persona-admin reload` | Re-read the current persona file (no restart) |
| `/voice-admin set <id>` | Override the Fish Audio voice ID |
| `/voice-admin show` | Show the effective voice ID |
| `/admin cost` | Today / this month spending |
| `/admin health` | Bot internal status |
| `/admin wakeword on\|off` | Toggle wake-word required mode |
| `/admin reload-config` | Hot-reload `config.yaml` + persona files |
| `/admin restart` | Soft-restart all sessions |
| `/admin whitelist add\|remove\|list\|clear <user>` | Restrict who the bot listens to (cross-server) |
| `/admin owner add\|remove\|list <user>` | Manage co-owners (primary owner only — co-owners can run all admin commands except this one) |

### Voice conversation

After `/join`, just talk — the bot pipelines VAD → streaming ASR → addressee decision → LLM (with tools) → cloned-voice TTS automatically.

- **Organic multi-party mode** (`bot.organic.enabled`, default on): no wake word needed once a conversation is going. Calling the bot's name (sentence edge) or being alone with it always gets an instant reply; ambiguous utterances are judged by an LLM arbiter that reads the last few lines of room chatter; rejected speech is eavesdropped into context instead of discarded. Open questions to the room ("有人知道…吗") wait ~1.5s and self-select only if no human answers. Utterances queued while the bot is talking merge into one combined reply.
- **Multi-user safe**: Discord delivers one track per speaker; per-user VAD/ASR; a shared queue serializes replies.
- **Barge-in**: speak again while the bot is replying → it stops mid-sentence(default `addressee_only` — only the current addressee interrupts). Note: speakers on open loudspeakers usually CAN'T barge in — their own Discord client's echo cancellation mutes their mic while bot audio plays (server receives silence). Headphones fix it.
- **Backchannel filter**: short acknowledgements ("嗯", "ok", "yeah", "对") and utterances under 600 ms are dropped while the bot is speaking, so casual nods don't truncate the reply.
- **Wake word (optional)**: `/admin wakeword on` requires the persona's wake word before any reply (legacy mode; organic mode makes this unnecessary).
- **Whitelist (optional)**: `/admin whitelist add @user` makes the bot ignore everyone else's voice until cleared.

## Hot configuration

Most settings can be hot-reloaded — no restart needed:

- `kill -HUP <pid>` or `/admin reload-config` re-reads `config.yaml` and the active persona file.
- `/voice-admin set <id>`, `/admin wakeword`, `/admin whitelist`, `/admin owner` all persist to `data/runtime_config.json` and survive restart.
- Switching ASR provider (streaming ↔ batch) needs a restart.

```yaml
# Override the Fish voice (or do it via /voice-admin set at runtime)
tts:
  fish_audio_stream:
    voice_id: <new_id>
    fallback_voice_id: <backup_id>
```

## Health endpoints

Listening on `:9090` by default:

```
GET /healthz       → 200 ok / 503 not_ready
GET /readyz        → 200 ok / 503
GET /stats.json    → {uptime_seconds, guilds, active_sessions}
```

## Authoring a new persona

Drop a `.md` file in `prompts/personas/`. Frontmatter is YAML; only `name` and `voice_id` are required. Set `language: zh|en` (default `zh`) to switch every LLM-facing prompt — base template, arbiter few-shots, default fillers/clarify lines, greeting/farewell — to that language; the file body becomes the system prompt, so write it in the same language. Per-persona Fish Audio TTS knobs (temperature, speed, volume, etc.) are optional — see `prompts/personas/_template.md` for the full scaffold with comments. The base template (`prompts/base_template.md`) supplies voice rules, emotion tags, and prompt-injection defenses to every persona.

Switch via `/persona-admin use <id>` (DM, owner only) or `config.yaml:bot.active_persona`. Persona swap auto-rebuilds the wake-word matcher, addressee detector, and fast-response audio cache.

## Testing

```bash
# Full suite (no API keys required) — ~320 tests, ~15s; live tests auto-excluded
.venv/bin/pytest tests/

# Live tests only (real Anthropic/Fish calls — costs money)
.venv/bin/pytest tests/ -m live

# Multi-party addressee scenario replay (offline, 13-line scripted conversation)
.venv/bin/python -m scripts.verify_organic

# E2E latency benchmark (live API calls)
.venv/bin/python -m tests.perf.bench_e2e_latency
```

The addressee heuristics are acceptance-tested against a golden set of ~70 labeled real-traffic utterances (`tests/fixtures/addressee_golden.jsonl`, metrics: missed-accept ≤10%, false-accept ≤10%). Test data is intentionally Chinese — that's the language the bot operates in.

## Troubleshooting

| Symptom | Where to look |
|---|---|
| Bot online but `/join` stays silent | Fish Audio API quota / `voice_id` invalid? Search logs for `Fish Audio` |
| User talks, bot doesn't react | 1) Whitelist set? Check `/admin whitelist list`. 2) `/sleep` mode? 3) Look for `[ASR]` log lines to confirm transcription |
| Bot keeps getting interrupted by short "嗯/ok" | The backchannel filter exists; if it's still too sensitive, raise `utt_ms` threshold in `bot.py:_finalize_utterance` |
| LLM slow | Anthropic prompt-cache miss? It only hits within a 5-min window |
| Model load fails on startup | Re-run `bash scripts/download_models.sh` |
| Slash commands not appearing | Global sync can take up to an hour; set `TEST_GUILD_ID` for instant per-guild sync |
| Voice connection dies silently after barge-in | Should be fixed by `audio/voice_recv_patch.py`; if it returns, `/leave` + `/join` recovers |

## Latency

Measured on live Discord traffic (macOS M-series, local ASR, `[latency]` log line per turn):

| Stage | Typical |
|---|---|
| End-of-speech detection → ASR final text | 15-30 ms (streaming ASR already consumed the audio) |
| Dispatch / queue | ~40 ms |
| LLM first token | **~0 ms on speculation hit** (stream pre-opened while you were still talking) / 100-700 ms otherwise |
| First TTS audio chunk | 230-550 ms |
| **Total mouth-to-ear** | **best 361 ms, typically 0.6-1.1 s** |

How it gets there: streaming ASR partials trigger a speculative LLM stream before you finish the sentence; the TTS WebSocket is pre-opened at enqueue time; predicted-slow turns (tool calls) play a cached filler phrase immediately so the wait never feels dead. Reproduce with `.venv/bin/python -m tests.perf.bench_e2e_latency` or read the per-turn `[latency]` log line.

## Language support

The bot is **production-ready in Chinese** today. English deployment status,honestly:

- ✅ Slash-command UI is fully localized (en/zh, per-user Discord locale).
- ✅ The streaming ASR model is bilingual (zh-en); personas, wake words,fast-responses, fillers, and farewell lines are all per-persona text — write them in English and they synthesize in English.
- ✅ Personas carry a `language: zh|en` field that switches every LLM-facing prompt: the base template (`base_template.md` / `base_template.en.md`), the arbiter prompt with language-native few-shot examples, default fillers/clarify lines, greeting/farewell generation, and filler keywords. Tool outputs (time/date/weather strings) are still Chinese-formatted — minor, on the roadmap.
- ⚠️ The heuristic addressee fallback (regexes, word lists, length thresholds)is Chinese-tuned and golden-set-validated **for Chinese only**. In English,rely on the LLM arbiter (`gray_zone: llm`) and expect the fallback to be conservative.

Contributions welcome — the golden-set format makes adding a new language a data problem, not an architecture problem.

## Design notes

1. **Fish Audio uses msgpack** over WebSocket — JSON does not work (verified empirically).
2. **Discord audio frames are 20 ms Opus @ 48 kHz**; Silero VAD downsamples to 16 kHz internally.
3. **Per-user VAD/ASR instances** (Discord delivers a track per speaker); a shared utterance queue serializes LLM/TTS, merging anything queued during playback into one reply.
4. **Streaming ASR + speculation**: sherpa-onnx zipformer emits partials while the user speaks; a stable partial that the reflex layer would accept pre-opens the LLM stream, so the reply often starts generating before the user finishes (llm_first_delta ≈ 0 on a hit).
5. **Three-layer addressee decision**: table-lookup reflexes (zero cost, ~80% of traffic) → LLM arbiter with room context for the gray zone (few-shot prompting is load-bearing; zero-shot is near chance) → heuristic scoring fallback gated by the golden set. Semantic judgment is never encoded as regex rules.
6. **Ambient transcript is context, not memory**: rejected speech feeds a rolling room transcript injected into the next accepted turn (fresh ≤120s), then stripped before committing to history.
7. **Speakability gate**: chunks with no synthesizable content (`\n`, emotion-tag-only, punctuation-only) are never pushed to Fish — an empty chunk makes Fish terminate the whole stream and silences the rest of the reply.
8. **Anthropic prompt cache**: system prompt + last assistant turn are cache-flagged → TTFT < 200 ms within a 5-min window.
9. **Voice fallback**: when the primary `voice_id` fails, the bot falls back to `fallback_voice_id` and DMs the owner.
10. **Cost tracking**: SQLite ledger covering every paid path (LLM turns, TTS bytes, arbitration calls); `/admin cost` queries it; a quota guard blocks new turns over budget.
11. **Error reporting**: fatal errors → DM owner; user-triggered errors → ephemeral reply (so the channel stays clean).
12. **DAVE end-to-end encryption**: Discord enforces E2EE on voice. `audio/dave_patch.py` monkey-patches `discord-ext-voice-recv` to decrypt the opus payload before libopus sees it. Do not remove.

For per-layer details and debug walkthrough, see [`docs/PIPELINE.md`](docs/PIPELINE.md).
