**English** | [简体中文](LATENCY.zh.md)

# EchoTwin Latency Report

Where every millisecond of a voice turn goes: measured, decomposed, and
separated into **deliberate conversation-design waits** vs **pipeline cost**.

- Measured 2026-07-10, macOS M-series, residential network (the actual
  deployment condition — a datacenter deploy would shave LLM round-trips).
- Every number below is reproducible: `scripts/bench_latency.py` and
  `scripts/bench_llm_models.py`.

## TL;DR

| | p50 |
|---|---|
| First voice (cached filler) | **~0.6 s** after end of speech |
| Full reply, pipeline only | **~1.2 s** (ASR 19 ms / LLM 971 ms / Fish TTS 174 ms / playout 40 ms) |
| Full reply, mouth-to-ear | **~1.75 s** (adds the deliberate 550 ms end-of-turn wait) |
| Fastest logged production turn | **361 ms** endpoint→first-audio (speculative LLM hit) |

Fish Audio TTS is **10 % of the pipeline** — never the bottleneck. The two
dominant terms are the LLM first sentence (55 %) and the deliberate VAD
end-of-turn wait (31 %).

## Why production logs mislead

The per-turn `[latency]` log line is honest about what it measures, but its
aggregates over-state pipeline cost. Over 88 logged production turns:

| Stage (log naming) | p50 | p90 | What pollutes it |
|---|---|---|---|
| `endpoint→asr_done` | 19 ms | 30 ms | Nothing — local compute, tight spread (9–33 ms). Trustworthy. |
| `asr_done→consumer_start` | 39 ms | **2144 ms** | Multi-party: queue wait while the bot is speaking, addressee arbitration. Design, not pipeline. |
| `consumer_start→llm_first_delta` | 519 ms | 1088 ms | Speculation hits (~0 ms) mixed with misses; network variance. |
| `llm_first_delta→first_audio` | 435 ms | 549 ms | **Not a TTS number**: it silently includes the LLM still composing the rest of the first sentence, because TTS receives complete sentences from the chunker. |
| total | 1032 ms | 3331 ms | All of the above. |

Two things the logs *structurally* cannot see:

1. **The VAD end-of-turn wait.** The journey starts at `endpoint` — but the
   user stopped speaking `endpoint_silence_ms` (500 ms) + up to one poll tick
   (100 ms) earlier. That ~550 ms is real, felt latency that never appears in
   any log line.
2. **Time-to-first-sentence vs TTFT.** TTS starts on the first complete
   *sentence*, not the first token. The gap (~280 ms on Haiku) hides inside
   `llm_first_delta→first_audio` and gets misattributed to TTS.

## Controlled benchmark methodology

`scripts/bench_latency.py` measures each network stage in isolation, one turn
at a time — no queueing, no arbitration — under production configuration:

- Real persona system prompt (base template + active persona body)
- Tools schema attached (production always sends it)
- Prompt caching on (system block + last assistant message, as in production)
- Fish `s2-pro`, `latency: low`, voice bound at socket-open, socket pre-opened
  before the timed section (the production hot path)
- Rolling 4-message history + rotating conversational questions
- N = 12 runs per stage, p50/p90 reported; run 1 (prompt-cache write) is
  excluded from the cache-hit aggregate

## Results

Stage stats (12 runs, 2026-07-10):

| Stage | p50 | p90 | min | max |
|---|---|---|---|---|
| `llm_ttft` (request → first delta) | 687 ms | 908 ms | 560 ms | 1031 ms |
| `llm_ttfs` (request → first sentence) | 971 ms | 1052 ms | 808 ms | 1114 ms |
| `tts_open` (WS handshake + voice bind) | 181 ms | 228 ms | 136 ms | 236 ms |
| `tts_ttfa` (sentence push → first Opus packet) | 174 ms | 416 ms | 130 ms | 1232 ms |

Honest mouth-to-ear composition (p50, single user):

| Component | Time | Share | Nature |
|---|---|---|---|
| VAD end-of-turn silence | 550 ms | 31 % | **Design** (config: 500 ms + tick/2) |
| Streaming ASR tail | 19 ms | 1 % | Pipeline (local compute, from logs) |
| LLM first sentence | 971 ms | 55 % | Pipeline (measured, cache-hit) |
| Fish TTS first audio | 174 ms | 10 % | Pipeline (measured, pre-opened WS) |
| Discord playout | ~40 ms | 2 % | Transport (estimate) |
| **Total** | **~1754 ms** | | |

Cross-validation: the ex-endpoint sum (19 + 971 + 174 + 40 ≈ 1204 ms) is
consistent with the log-side single-user turns (p50 980 ms + playout);
production runs slightly faster than the bench because fillers/speculation
sometimes shortcut the path and real replies often open with shorter
sentences than the bench questions elicit.

## Fast first-sentence model experiment

The LLM first sentence dominates the pipeline, so we measured whether a small
fast model could speak it instead (`scripts/bench_llm_models.py` — same
persona prompt, same history, streaming, no tools, 6 runs):

| Model | TTFT p50 | First sentence p50 | First-sentence quality |
|---|---|---|---|
| claude-haiku-4-5 (current) | 857 ms | 1055 ms | ✅ In-persona, uses emotion tags |
| **qwen/qwen3-32b** (Groq, `reasoning_effort: none`) | **360 ms** | **369 ms** | ✅ Surprisingly good: short natural openers ("哎嘿嘿,", "晴天呀!"), even emits base-template emotion tags like `[laughing]` |
| llama-3.1-8b-instant (Groq) | 389 ms | 389 ms | ❌ Fast but derails: invents memes and persona facts |
| openai/gpt-oss-20b (Groq) | — | — | Hit free-tier TPM limit (8000/min) before completing |

**qwen3-32b reaches the first sentence ~680 ms sooner than Haiku, and it is
already in the stack** (it powers the multi-party arbiter). A two-stage
scheme — fast model speaks sentence 1, Haiku continues from it — projects
mouth-to-ear at **~1.1 s**. Open problems before shipping it:

1. **The seam**: sentence 1 must be fed to Haiku as an assistant prefix so
   the continuation matches tone and doesn't repeat.
2. **Persona drift**: qwen occasionally invents details (one run
   volunteered a boyfriend). Sentence-1 scope must be constrained to
   phatic openers.
3. **Groq free tier**: 8000 TPM vs ~1.2 k prompt tokens per turn means a paid
   tier or a trimmed fast-path prompt.

## How low can it go? Three tiers

**Tier 1 — config only (~1.55 s):** drop `endpoint_silence_ms` 500 → 300.
Saves 200 ms; risks splitting utterances that contain natural pauses. The
value has already been walked down 800 → 600 → 500; validate against the
harness fixtures before going lower.

**Tier 2 — current architecture (~1.3–1.4 s actual, ~0.4–0.6 s perceived):**

- *Hide* the VAD wait instead of shrinking it: `speculative_llm` opens the
  LLM stream on stable ASR partials, so on a hit the 550 ms wait fully
  overlaps LLM work → ~1.4 s mouth-to-ear. Current hit rate over the logged
  turns is only **8 %** — the highest-leverage optimization in the project.
- Perceived latency: fillers are pre-synthesized local OGG (zero network),
  so first voice ≈ VAD 550 + ASR 19 + disk ~10 ≈ **0.6 s** (0.35 s with a
  300 ms VAD window — inside the human turn-taking gap). `filler_mode:
  always` extends this to every turn.
- Floors that won't move: ASR tail 19 ms, Fish TTS 174 ms (s2-pro low-latency
  service floor; fastest observed 130 ms), Haiku TTFT ~690 ms (API floor from
  a residential network).

**Tier 3 — paradigm change (~0.5 s):** two-stage fast-first-sentence (above),
or a native speech-to-speech model replacing the ASR→LLM→TTS cascade
entirely. Both out of scope for now.

## Reproducing

```bash
# Stage-by-stage pipeline benchmark (needs ANTHROPIC_API_KEY + FISH_AUDIO_API_KEY)
.venv/bin/python scripts/bench_latency.py --runs 12

# First-sentence model comparison (adds GROQ_API_KEY)
.venv/bin/python scripts/bench_llm_models.py --runs 6
```

Both read the active persona from `config.yaml` (`--persona` to override; the
persona supplies the voice id, or set `TEST_VOICE_ID`). API cost per full run
is a few cents (short cached prompts, one short TTS sentence per run).
Live-traffic equivalents: `tests/perf/` and the per-turn `[latency]` log line.
