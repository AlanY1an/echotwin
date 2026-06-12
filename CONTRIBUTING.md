# Contributing to EchoTwin

Thanks for stopping by! This is a young project — contributions of all sizes are welcome.

## Dev setup

```bash
python3.11 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
.venv/bin/pytest tests/        # ~320 tests, no API keys needed
```

`bash scripts/download_models.sh` gets the local VAD/ASR models if you want to run the bot itself (see [docs/SETUP.md](docs/SETUP.md)).

## Before you open a PR

- `pytest tests/` must be green (live tests are auto-excluded; don't run `-m live` casually — they cost money).
- Read [CLAUDE.md](CLAUDE.md) — it's the internals guide (architecture, gotchas, conventions). The gotchas section will save you hours.
- If you touch README content, keep `README.md` and `README.zh.md` in sync.
- Code comments and docs in English; user-facing strings/prompts/test data stay in the bot's language.

## Where help is most wanted

- **English support for the heuristic addressee fallback** — the LLM arbiter is bilingual, but the regex/wordlist fallback layer is Chinese-tuned (`pipeline/organic.py`). Needs an English rule pack + an English golden set (`tests/fixtures/addressee_golden.jsonl` shows the format).
- **Localized tool outputs** — `get_time/get_date/get_weather` return Chinese-formatted strings regardless of persona language.
- **Multi-party mode hardening** — the organic addressee pipeline is experimental; real-traffic logs and golden-set cases are the most valuable contribution.
- **New TTS/LLM providers** — clean provider interfaces in `providers/` (see "Adding new things" in CLAUDE.md).

One rule above all: **don't add semantic judgment as regex rules** — ambiguity belongs to the LLM arbiter (CLAUDE.md gotcha #20 explains why, learned the hard way).
