**English** | [简体中文](SETUP.zh.md)

# Setup Guide — from zero to a talking bot

Everything you need to get the bot into your Discord server and talking. Takes ~15 minutes if you already have the API accounts.

## Prerequisites

- **macOS (arm64) or Linux**, Python 3.11+
- **libopus** — `brew install opus` (macOS) / `apt install libopus0` (Debian/Ubuntu)
- A Discord account with a server you manage
- API keys (see step 3): Fish Audio (TTS), Anthropic (LLM), optionally Groq (multi-party arbitration)

## 1. Create the Discord application

1. Go to the [Discord Developer Portal](https://discord.com/developers/applications)→ **New Application** → name it.
2. **Bot** tab:
   - Click **Reset Token**, copy it — this is your `DISCORD_TOKEN`.
   - Under **Privileged Gateway Intents**, enable **Server Members Intent**
     (the bot needs voice-channel member lists and display names).
3. **Installation** tab (or OAuth2 → URL Generator):
   - Scopes: `bot`, `applications.commands`
   - Bot permissions: **View Channels, Send Messages, Connect, Speak**
   - Open the generated URL in a browser and invite the bot to your server.

## 2. Clone a voice on Fish Audio

1. Sign up at [fish.audio](https://fish.audio), create an API key(`FISH_AUDIO_API_KEY`).
2. Upload a voice sample to create a voice model — the model ID is your persona's `voice_id`. (Any public model ID on fish.audio works too.)Clone only voices you have permission to use.

## 3. API keys → `.env`

```bash
cp .env.example .env
```

| Variable | Required | Where to get it |
|---|---|---|
| `DISCORD_TOKEN` | yes | step 1 |
| `FISH_AUDIO_API_KEY` | yes | fish.audio dashboard |
| `ANTHROPIC_API_KEY` | yes | [console.anthropic.com](https://console.anthropic.com) |
| `GROQ_API_KEY` | no | [console.groq.com](https://console.groq.com) — powers the fast multi-party addressee arbiter (~350ms); without it arbitration falls back to the (slower) conversation LLM |
| `TEST_GUILD_ID` | dev only | your server ID — makes slash commands sync instantly instead of ~1h |

## 4. Install & download models

```bash
python3.11 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
bash scripts/download_models.sh   # Silero VAD (~2MB) + SenseVoiceSmall (~234MB)
# the streaming ASR model (~100MB) auto-downloads from Hugging Face on first run
```

## 5. Configure

```bash
cp config.example.yaml config.yaml
```

Two ready-made personas ship with the repo: `ariana_en` (English, default — works out of the box) and `ouyang_zh` (Chinese — open it and replace `voice_id` with your model ID from step 2). To build your own, copy `prompts/personas/_template.md` (English) or `_template.zh.md` (Chinese), fill in `name`, `voice_id`, `language` (zh|en — switches every LLM prompt, default voice lines, AND the streaming-ASR model; the first run with a new language downloads ~100 MB), wake words, and the personality prompt (the body of the file IS the system prompt). Then set `bot.active_persona: my_persona` in `config.yaml`.

The defaults are sane for a first run: streaming ASR, organic multi-party mode on, daily budget capped at $5.

## 6. Run

```bash
.venv/bin/python -m echotwin
```

In Discord: join a voice channel, type `/join`, and talk. Solo with the bot,everything you say gets a reply; with multiple people, call its wake word once and just keep talking — the addressee pipeline figures out the rest.

First-run sanity checklist:

- `Bot logged in as …` and `Slash commands synced` in the logs
- `/join` makes the bot appear in your voice channel
- Speaking produces `[ASR/watchdog] you(…): <text>` log lines
- The reply plays in the cloned voice within ~1s

## 7. Useful next steps

- `/say 你好` — quick TTS smoke test without the LLM
- `/admin cost` (owner only, DM or any channel) — spend so far
- `kill -HUP <pid>` — hot-reload config.yaml + persona edits, no restart
- Wake-word fast path: cache instant audio replies with `.venv/bin/python -m scripts.synthesize_fast_responses`
- Per-layer pipeline reference and debugging guide: [`PIPELINE.md`](PIPELINE.md)

## Troubleshooting setup

| Symptom | Fix |
|---|---|
| `PrivilegedIntentsRequired` on startup | Enable **Server Members Intent** in the Developer Portal (step 1.2) |
| Slash commands don't appear | Global sync takes up to 1h — set `TEST_GUILD_ID` in `.env` for instant sync |
| Bot joins but never speaks | Check `FISH_AUDIO_API_KEY` and your persona's `voice_id`; grep logs for `Fish` |
| Bot can't hear anyone | It must be `/join`-ed (not dragged) into the channel; check `[stats]` log lines |
| `corrupted stream` spam in logs | A few at session start are normal (Discord E2EE handshake); see PIPELINE.md if continuous |
| You can't interrupt the bot | Use headphones — on open speakers, your own Discord client's echo cancellation mutes your mic while the bot plays |
