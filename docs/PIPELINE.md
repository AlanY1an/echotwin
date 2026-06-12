**English** | [简体中文](PIPELINE.zh.md)

# EchoTwin Realtime Pipeline

A user opens their mouth → the bot's cloned voice replies. Between those two
events, **10 layers (0-9)** process the audio. This document is the
layer-by-layer reference: each section names the entry function (file, and a
line number where it is stable), what comes in, what comes out, what knobs
tune it, and what fails.

If you suspect a bug, the **debug order** at the bottom tells you which layer
to suspect first based on which log line stops appearing.

---

## Data flow

```
                ┌─ Layer 0: Discord ingress ──────────────────────┐
                │   VoiceRecvClient + DAVE decrypt + 3 patches    │
                │   listen.py:VoiceListener.write → bot callback  │
                │   out: per-user opus packets (20ms @ 48k stereo)│
                └────────────────┬────────────────────────────────┘
                                 ▼
┌──── Layer 1: Audio decode + endpoint watchdog ───────────────────────┐
│   bot.py:on_user_audio / _speech_watchdog_loop                       │
│   opuslib_next decode → numpy downmix mono → soxr 48k→16k            │
│   per-user state: in_speech, opus_ok/fail counters, preroll buffer   │
│   watchdog: endpoint_silence_ms wallclock endpoint; fires            │
│   speculative ASR (300ms) and speculative LLM triggers along the way │
│   out: pcm_48k_mono (→ ASR) + pcm_16k_mono (→ VAD)                   │
└────────────────────────────┬─────────────────────────────────────────┘
                             ▼
┌──── Layer 2: VAD (Silero ONNX) ──────────────────────────────────────┐
│   providers/vad/silero.py:feed (L61) — noise gate only, NOT endpoint │
│   512-sample (32ms) chunks; double-threshold hysteresis              │
│   out: VADResult(is_voice, speech_started, utterance_ended)          │
│   support: audio/preroll_buffer.py — 300ms history → ASR head        │
└────────────────────────────┬─────────────────────────────────────────┘
                             ▼
┌──── Layer 3: ASR — sherpa-onnx streaming zipformer (default) ────────┐
│   providers/asr/sherpa_stream.py — partial_text() while speaking;    │
│   final = 0.4s silence flush + input_finished (~20ms)                │
│   batch fallback: funasr_local.py (SenseVoiceSmall) + speculative    │
│   ASR pre-run at 300ms silence; emotion sidecar backfills emotion    │
│   out: ASRResult(text, language, emotion, is_final)                  │
└────────────────────────────┬─────────────────────────────────────────┘
                             ▼
┌──── Layer 4: Addressee verdict (organic, 3 tiers) ───────────────────┐
│   bot.py:_finalize_utterance                                         │
│   600ms guard → pure-punct guard → ack-word guard                    │
│   ① organic.py:hard_verdict (instant table lookup)                   │
│   ② arbiter.py LLM arbitration (gray zone, Groq qwen3-32b)           │
│   ③ organic.py:classify heuristic fallback                           │
│   dispatch: ACCEPT/REJECT/CLARIFY/OPEN_FLOOR/MENTION                 │
│   then: wake fast path → barge-in → TTS WS pre-open → enqueue        │
│   legacy mode (organic.enabled=false): addressee.py 4 rules          │
└────────────────────────────┬─────────────────────────────────────────┘
                             ▼
┌──── Layer 5: LLM stream + tool loop ─────────────────────────────────┐
│   pipeline/think_speak.py:respond_to_user (L33)                      │
│   consumer dequeue: _drain_merge_extras folds backlog into one turn  │
│   filler: cached phrase pre-queued on predicted-slow turns           │
│   speculative LLM stream attaches as round 0 when it matches         │
│   typed events: TextDelta / ToolUseStart / ...Delta / ...End / End   │
│   max 4 tool rounds; per-event 20s timeout; prompt cache             │
│   out: TextDelta stream → Layer 6                                    │
└────────────────────────────┬─────────────────────────────────────────┘
                             ▼
┌──── Layer 6: Sentence chunker ───────────────────────────────────────┐
│   utils/sentence_chunker.py:feed                                     │
│   first sentence: lenient punct + 16-char cap; subsequent: strict    │
│   speakable() guard — never push empty/tag-only/punct-only chunks    │
│   out: complete sentences → tts.push_text + tts.flush                │
└────────────────────────────┬─────────────────────────────────────────┘
                             ▼
┌──── Layer 7: TTS WebSocket (Fish Audio) ─────────────────────────────┐
│   providers/tts/fish_audio_stream.py:_open_with_voice                │
│   msgpack over WSS; 6 persona TTS knobs in start payload             │
│   WS is usually PRE-OPENED at enqueue time (Layer 4)                 │
│   3× retry on connect (async_retry, 0.5/1/2s backoff)                │
│   out: OGG/Opus 48k mono byte stream                                 │
└────────────────────────────┬─────────────────────────────────────────┘
                             ▼
┌──── Layer 8: OGG demux + frame queue ────────────────────────────────┐
│   audio/ogg_demux.py — RFC 3533 page parser; skips OpusHead/OpusTags │
│   audio/audio_source.py — discord.AudioSource bridge, 15ms timeout,  │
│     SILENCE_OPUS on starve, EOF on None sentinel                     │
│   queue: sync_queue.Queue(maxsize=200) — filler packets go in first  │
└────────────────────────────┬─────────────────────────────────────────┘
                             ▼
                ┌─ Layer 9: discord.py player thread ─────────────┐
                │   voice_client.play(source, after=callback)     │
                │   read() called every 20ms; bot.loop callback   │
                │   sets play_done event                          │
                └─────────────────────────────────────────────────┘
```

---

## Configuration cheat-sheet

Defaults come from `src/echotwin/config.py` (pydantic models). Live values
in `config.yaml` may override them. Owner slash commands persist a small
subset to `data/runtime_config.json`.

| Section | Field | Pydantic default | Live `config.yaml` |
|---|---|---|---|
| `bot` | `endpoint_silence_ms` | 600 | 600 |
| | `endpoint_tick_ms` | 100 | 100 |
| | `speculative_asr` | true | true |
| | `speculative_asr_silence_ms` | 300 | 300 |
| | `speculative_llm` | false | **true** |
| | `filler_mode` | `smart` | `smart` |
| | `filler_keywords` | 天气/几点/时间/日期/查/搜 | same |
| `bot.organic` | `enabled` | false | **true** |
| | `gray_zone` | `llm` | `llm` |
| | `arbiter_provider` | `""` (reuse Haiku) | **`groq`** |
| | `arbiter_timeout_ms` | 1500 | 1500 |
| | `arbiter_max_per_min` | 20 | 20 |
| | `ambient_max_age_s` | 120 | 120 |
| | `conversation_window_s` | 45 | 45 |
| | `clarify_cooldown_s` | 60 | 60 |
| | `open_floor_wait_ms` | 1500 | 1500 |
| | `mention_reply_rate` | 0.0 | 0.0 (off) |
| `vad.silero` | `threshold` | 0.5 | 0.5 |
| | `threshold_low` | 0.3 | 0.3 |
| | `min_silence_duration_ms` | 250 | **800** |
| | `frame_window` | 2 | **3** |
| | `preroll_ms` | 300 | 300 |
| `asr` | `provider` | `funasr_local` | **`sherpa_stream`** |
| | `emotion_sidecar` | true | true |
| `asr.funasr_local` | `language` | `auto` | **`zh`** |
| `asr.sherpa_stream` | `repo` | zipformer bilingual zh-en int8 | same |
| `addressee` (legacy) | `continuation_window_seconds` | 15.0 | **0** |
| | `solo_channel_auto` | true | true |
| `llm.claude_haiku` | `max_tokens` | 300 | 300 |
| | `temperature` | 0.7 | 0.7 |
| | `enable_prompt_cache` | true | true |
| `llm.groq` | `model` | `qwen/qwen3-32b` | same |
| | `max_tokens` | 100 | 100 |
| | `temperature` | 0.0 | 0.0 |
| `tts.fish_audio_stream` | `model` | `s2-pro` | `s2-pro` |
| | `latency` | `low` | `low` |

`runtime_config.json` carries: `active_persona`, `voice_id_override`,
`wake_word_required`, `listen_only_users`, `extra_owner_ids`. Loaded on
startup; written by every owner slash command that mutates state.

---

## Layer 0 — Discord ingress

### DAVE end-to-end decryption — `audio/dave_patch.py`

Discord enforced DAVE on **2026-03-02**. After RTP decryption the opus
payload is *still* encrypted with per-user DAVE keys; libopus rejects it
as "corrupted stream". The patch monkey-patches `AudioReader.__init__`
and `decryptor.decrypt_rtp` to call
`davey.DaveSession.decrypt(user_id, MediaType.audio, rtp_payload)` after
RTP-layer decrypt. It also:
- calls `set_passthrough_mode(True, 10)` once per session
- skips non-opus payload types (only PT 120 is decrypted)
- counts expected vs **unexpected** failures separately; unexpected ones
  (e.g. epoch desync) are logged at counts 1/10/every-100

**Failure modes** (all return RTP plaintext as fallback):
- `dave_session` not ready → `dave_passthrough++`
- SSRC → user_id mapping missing → `no_user++`
- Expected `UnencryptedWhenPassthroughDisabled` → `dave_fail_expected++`
  (1-3 frames at session start; protocol design, retrying is useless)
- Other DAVE error → `dave_fail_unexpected++`, logged

If RTP plaintext is fed to opus, Layer 1 will count `opus fail++`. If
`Unencrypted...` appears MID-session, that's an epoch/transition problem,
not startup noise.

### Three defensive patches — `audio/voice_recv_patch.py`

All applied at startup via `apply_voice_recv_patches()` (L98). They work
around alpha-quality bugs in `discord-ext-voice-recv 0.5.2a179`.

**Patch 1 — `_remove_ssrc` safety** (L109). voice_recv's stock
implementation crashes with `AttributeError: '_MissingSentinel' object has
no attribute 'speaking_timer'` when called with `_reader is MISSING`
(happens during shutdown / reconnect). The wrapper checks the sentinel
and no-ops.

**Patch 2 — macOS UDP keepalive** (L57). discord.py calls `connect()`
on the UDP socket; voice_recv's `UDPKeepAlive.run` then calls
`sendto(packet, addr)` which raises `OSError: EISCONN` on macOS, spinning
the CPU. The patch tries `sock.send(packet)` first (works on connected
sockets), falls back to `sendto`, and on double-failure backs off
`max(1.0, delay)` seconds.

**Patch 3 — `stop()` is play-only** (L140). Stock `VoiceRecvClient.stop()`
calls *both* `stop_playing()` and `stop_listening()`. Every barge-in
called `voice_client.stop()`, silently killing the receiver — that was
the "voice connection dies after barge-in" bug. The patch restores
vanilla discord.py semantics: `stop()` only stops playback.

### Listener callback — `pipeline/listen.py`

`VoiceListener(voice_recv.AudioSink).write(user, data)` (L35) runs in
voice_recv's thread. It schedules `bot.on_user_audio(user_id, name, opus)`
on the main loop via `run_coroutine_threadsafe`. Skips bot's own audio and
frames where `data.opus` is None. Always attach via
`bot.start_listening(vc, guild_id)` — it registers the death watch that
auto-restarts the reader (capped 5×) when voice_recv's AudioReader dies.

---

## Layer 1 — Audio decode + endpoint watchdog (`bot.py`)

### `on_user_audio` — packet ingress

`bot.py:529` — `async def on_user_audio(self, guild_id, user_id, user_name, opus_bytes)`

Per-packet flow:

1. Trace packet (off unless `VOICE_AGENT_TRACE=1`).
2. **Whitelist gate.** If `config.bot.listen_only_users` is non-empty and
   `user_id` not in it, drop — logged once **per user**.
3. Drop 3-byte sentinels (`SILENCE_OPUS`, `len(opus_bytes) < 4`).
4. Stamp `last_activity_time` and `_last_real_audio_{user_id}`; cancel any
   pending farewell.
5. Opus decode (per-user `OpusDecoder`, 48kHz stereo, 5760 samples/frame).
   On `OpusError`, increment per-user `opus_fail`, log every 50.
6. numpy downmix to mono (int16 → reshape (N,2) → mean(axis=1) → int16).
7. `[amp]` RMS amplitude diagnostic every 100 frames.
8. Per-user `Resampler` 48k→16k for VAD only.
9. Per-user `PrerollRingBuffer` (`max_frames = preroll_ms // 20` = 15
   frames @ 300ms). Push 48k mono.
10. Per-user `SileroVAD.feed(pcm_16k)` → `VADResult`.
11. First-real-packet bookkeeping: log `[utt] user U START`, set
    `_in_speech_{user_id} = True`, snapshot `_utt_opus_ok/_utt_opus_fail`
    (used by `_finalize_utterance` to compute utterance length), **cancel
    stale speculative ASR and abort a stale speculative LLM stream** (the
    user resumed speaking → any speculation is void).
12. Lazy `await asr.open()`.
13. Feed ASR. Gate by `vad_result.is_voice or in_speech`. On the first frame
    after START, drain the preroll buffer into ASR first.

Note: VAD's own `utterance_ended` is **not** what fires
`_finalize_utterance`. The wallclock watchdog does. VAD only gates the
in-speech flag.

### `_speech_watchdog_loop` — endpoint detection + speculation triggers

`bot.py:708` — `async def _speech_watchdog_loop(self)`

Tick and silence threshold come from `config.bot.endpoint_tick_ms` /
`endpoint_silence_ms` (100ms / 600ms), re-read every tick so SIGHUP config
reload applies live. Per tick, for every user with an active ASR:

- **Speculative ASR** (batch ASR mode, `bot.speculative_asr`): at
  `speculative_asr_silence_ms` (300ms) of silence, pre-run inference on the
  buffered audio as a background task. Gated on ≥25 packets so a noise blip
  doesn't burn an inference the <600ms filter would drop anyway.
- **Speculative LLM trigger** (`bot.speculative_llm`, streaming ASR only):
  same 300ms silence window → `_maybe_spawn_spec_llm` (see Layer 5).
- **Endpoint**: if `_in_speech_{user_id}` and
  `now - _last_real_audio_{user_id} >= endpoint_silence_ms`:
  - clear `_in_speech` and `_preroll_drained` flags, reset VAD state
  - `preroll.clear()` — drop this utterance's silent tail so it isn't
    prepended to the next one (would corrupt the next wake word)
  - `_spawn_finalize` — runs `_finalize_utterance` as a tracked background
    task. Per-(guild,user) dedup: if a finalize for this user is still
    running, the endpoint is **deferred** (in_speech restored, watchdog
    re-fires later), not dropped. The opus-ok snapshot is taken
    synchronously before spawn.

Every 5s, log the `[stats]` block: one guild line
(`dave_epoch / reader=alive|DEAD / mls_users / per-user att/ok/fail/pass`)
plus per-user lines (`real_ok=N fail=M (last 5s) in_speech last_real
is_audible state`). This is the voice-detection debugging entrypoint —
see Debug order below.

### `_finalize_utterance` — endpoint → ASR text → verdict → enqueue

`bot.py:1166`. Sequence (each drop point logs):

1. Pop the user's speculative LLM stream (if any) — every exit path below
   either carries it into the Utterance or aborts it (`finally` block; a
   leaked stream keeps billing at Anthropic).
2. Compute `utt_ms` from per-user opus counters (snapshot from spawn time).
3. ASR final: if a speculative ASR result exists and its fed-marker equals
   the ASR's current buffered bytes (no new audio arrived), adopt it —
   `[asr] speculation HIT`, no re-inference. Otherwise
   `await asr.end_utterance()`.
4. Empty result → logged drop. **600ms guard**: `utt_ms < 600` → drop.
   **Pure-punctuation guard** → drop. **Ack-word filter**: if
   `session.is_audible` (bot speaking) and the content is in `ACK_WORDS`
   (嗯/对/好/哦/啊/呃/是, yeah/yes/ok/okay/uhhuh/mhm + variants), drop
   without aborting the bot.
5. **Addressee verdict** — Layer 4 below. REJECT/CLARIFY/OPEN_FLOOR/MENTION
   paths return here; only ACCEPT falls through.
6. Wake-word fast path: `wake_matcher.match_only(text)` (wake + ≤2 extra
   chars) → play random cached `fast_response.ogg`, return — skips Layers
   5-7. Disabled while PROCESSING (would commit a truncated reply).
7. Strip wake word; default to "嗨" if empty.
8. **Barge-in** (after the verdict, on the stripped text): if session state
   is PROCESSING and the speaker is the current addressee (or
   `barge_in_mode == "anyone"`), `session.abort()` the current turn.
9. **TTS WS pre-open**: if the utterance queue is empty and not PROCESSING,
   `make_tts(...)` + open task are created here so the WS handshake hides
   inside the dispatch gap. The socket is attached to the Utterance —
   dropped utterances MUST go through `bot._discard_utterance` or the Fish
   WS leaks.
10. **Emotion sidecar** (streaming ASR mode): spawn SenseVoice on the saved
    utterance PCM — see Layer 3.
11. **Speculative LLM attach**: carried into the Utterance only if the final
    text and the dialogue-length snapshot both match what the stream was
    opened with (`[spec-llm] speculation MATCHED`); otherwise aborted.
12. Replace this user's previously queued item (`_dequeue_user`) and enqueue
    `Utterance(user_id, user_name, text, emotion, journey, tts,
    tts_open_task, spec_llm)`.

---

## Layer 2 — VAD (Silero)

`providers/vad/silero.py:61` — `def feed(self, pcm_16k_16bit: bytes) -> VADResult`

Per call:

1. Append PCM to internal buffer.
2. While ≥ 1024 bytes (512 samples = 32ms @ 16k), pop a chunk:
   - ONNX inference. Concat 64-sample context (overlap from previous chunk),
     feed `{"input": x, "state": s, "sr": 16000}`, get probability + state.
   - Double-threshold hysteresis. `prob >= threshold (0.5)` → voice,
     `prob <= threshold_low (0.3)` → silence, else hold previous.
   - Push to sliding window (`maxlen = frame_window`); chunk counts as voice
     only if **all** entries are voice.
   - Silence counter. On voice: reset counter, fire `speech_started` on 0→1
     transition. On silence (after speech started): increment; at
     `min_silence_duration_ms / 32` chunks fire `utterance_ended` and reset.
3. Return `VADResult(is_voice, utterance_ended, speech_started)`.

The VAD result is used **only as a noise gate** for feeding ASR. Endpointing
is the Layer 1 watchdog.

`audio/preroll_buffer.py` — `class PrerollRingBuffer(max_frames=15)`.
`push(frame)` appends to a deque; `drain()` concatenates and clears. Used to
prepend ~300ms of pre-VAD audio to the first ASR frame so the leading
syllable isn't clipped.

**Failure modes** (recap):
- Threshold too low → ambient noise wakes ASR.
- Threshold too high → quiet speech missed.
- `min_silence_duration_ms` too short → mid-sentence breath = endpoint.
- Too long → two sentences glued, LLM context confused.

---

## Layer 3 — ASR

### Default engine: sherpa-onnx streaming zipformer — `providers/asr/sherpa_stream.py`

`asr.provider = sherpa_stream`. Bilingual zh-en zipformer, int8
(~100MB, auto-downloaded from HF). Recognizes audio **as it arrives**:

- `feed_audio(pcm_48k_mono)` resamples to 16k and accumulates; every ~100ms
  of samples a serialized decode task refreshes `partial_text()`. The
  partial drives the speculative-LLM trigger (Layer 1 watchdog).
- `end_utterance()` waits for the in-flight decode, feeds the remaining
  samples plus **0.4s of silence**, calls `input_finished()`, and decodes to
  exhaustion — the final flush often emits the last word, and takes ~20ms.
- `speculate()` deliberately returns `(None, -1)`: adopting the partial at
  endpoint would truncate the tail, and the final chunk is cheap anyway.
- Class-level recognizer cache + per-repo inference lock (shared across all
  instances of the same model). Keeps `last_utterance_pcm` (up to 30s of 48k
  PCM) for the emotion sidecar.
- Engine decision: funasr paraformer-zh-streaming spike-tested at RTF≈1.5 on
  M-series CPU (slower than realtime, 3/3 gates failed); sherpa zipformer
  int8 measured chunk 15ms / final flush 22ms. Evidence:
  `scripts/spike_streaming_asr.py` vs `scripts/spike_sherpa_streaming.py`.
  Do not "switch back to the same library for consistency".

Streaming results carry no emotion tags — `emotion` is always `NEUTRAL` at
this layer; see the sidecar below.

### Batch fallback: FunASR SenseVoiceSmall — `providers/asr/funasr_local.py`

`asr.provider = funasr_local`. `feed_audio` only appends to a buffer;
`end_utterance()` resamples 48k→16k, runs `model.generate(...)` in an
executor (blocking), and parses `<|tag|>` output via
`sensevoice_parse.py` → `ASRResult(text, language, emotion)`.

- **Class-level model cache** (`_model_cache` keyed by `(model_dir,
  device)` + lock): SenseVoice loads in 5-10s; multiple users each get a
  `FunASRLocal` instance but share the loaded model. Don't move this back
  to instance level.
- **Speculative ASR** (batch mode only): `speculate()` runs inference on a
  snapshot of the buffer (not cleared) at 300ms of silence; finalize adopts
  the result iff the fed-marker equals the buffer size at endpoint (no new
  audio arrived). Saves the ASR latency from the perceived endpoint wait.
- Tag parser is tolerant of both `<|zh|><|SAD|><|EVENT|>content` and
  `<|EMO_UNKNOWN|><|Speech|><|withitn|>content` orderings; emotion from a
  known set (NEUTRAL, HAPPY, SAD, ANGRY, FEARFUL, SURPRISED, DISGUSTED).

### Emotion sidecar — `bot.py:_spawn_emotion_sidecar`

With streaming ASR active and `asr.emotion_sidecar: true`, each accepted
utterance's saved PCM is re-run through a shared SenseVoice instance **in
the background**. The result is written to `session.last_emotion[user_id]`
and takes effect on the **next** turn — this turn's LLM message is already
sent before the sidecar finishes. It never blocks the reply. The consumer
prefers a real per-turn emotion (batch path) over the sidecar cache.

---

## Layer 4 — Addressee verdict

Two modes. With `bot.organic.enabled: true` (live config) the three-tier
organic detector below runs. With `enabled: false`, the legacy
`pipeline/addressee.py` 4-rule detector runs instead (wake word / explicit
@mention / continuation window / solo channel — `continuation_window_seconds`
is 0 in the live config, so that rule is off).

### Tier 1 — instant table lookup: `pipeline/organic.py:hard_verdict`

Pure lookup-and-count rules, zero semantic judgment, microseconds:

1. **Wake word at sentence start/end** (vocative) → instant ACCEPT.
   Name mid-sentence → gray zone (could be third-person mention).
2. **Solo** (just speaker + bot in channel) → instant ACCEPT.
3. **≤3-char meaningless fragment** → instant REJECT — only ASR debris
   ("你帮你", "喽"): short words in the ACK set ("好的", "哈哈"), questions,
   particle-ending utterances, and anything during a pending clarify all go
   to the gray zone instead.

Everything else returns None = **gray zone**. The two accept rules exist
because the accept path must be zero-latency (fast-response cache +
speculative streams hang off them); the reject rule is pure cost saving.

### Tier 2 — LLM arbitration: `pipeline/arbiter.py:arbitrate`

Gray-zone utterances go to a small LLM judge (when
`organic.gray_zone: llm`). Payload: the utterance + speaker name, the last
6 eavesdropped room lines, the bot's last reply, who the bot last addressed,
in-window flag, clarify-pending flag. Output: one line of JSON —
`{"verdict": "accept|reject|clarify|open_floor", "reason": "..."}`.

- Provider: `organic.arbiter_provider: groq` → standalone Groq client
  (`llm.groq`, default `qwen/qwen3-32b`, `reasoning_effort: none`,
  ~150-350ms, logged as `[arbiter] ... Nms`). With no `GROQ_API_KEY` (or
  `arbiter_provider: ""`) the conversation Haiku is reused.
- Guardrails: `arbiter_timeout_ms` (1.5s) timeout and a per-guild rate fuse
  of `arbiter_max_per_min` (20) calls/minute — on timeout, failure, bad
  JSON, or fuse trip, the caller falls back to Tier 3.
- The few-shot examples in the system prompt are a **necessity, not
  decoration**: zero-shot addressee detection benchmarks near-random, and
  the Groq smoke test failed the "'you' refers to someone else" case
  without them.
- Arbitration is a paid call: usage is recorded into the cost tracker
  (`qwen3 32b` pricing keys for Groq, Haiku keys otherwise). qwen `<think>`
  blocks are stripped before JSON extraction.

### Tier 3 — heuristic fallback: `pipeline/organic.py:classify`

The full scoring rule set (vocative/solo/clarify-continuation/open-floor/
ACK/in-window scoring with second-person, topic-overlap bigrams, skill
imperatives, self-narration and third-person penalties). It is the safety
net when the arbiter fails, and the whole detector when
`gray_zone: heuristic`. Acceptance is driven by the golden set
`tests/fixtures/addressee_golden.jsonl` (missed-accept ≤10%, false-accept
≤10%, gray zone leans accept). This is also the only tier that can return
MENTION.

### Verdict dispatch (in `bot.py:_finalize_utterance`)

Every verdict logs one `[organic]` line (`verdict=... score=... signals=...`).

- **REJECT** → eavesdrop: the line is appended to `session.ambient`
  (deque, maxlen 30) and injected into the next accepted turn's payload as
  `recent_room_chat` — capped at 12 lines / 500 chars, only entries newer
  than the bot's last reply and fresher than `ambient_max_age_s` (120s).
  `recent_room_chat` is **stripped from the message before history commit**
  (it is in-the-moment reference, not memory).
- **CLARIFY** → also eavesdropped; plays a cached clarifying question
  ("诶,是在叫我吗?", synthesized per persona voice at startup) with a
  per-user `clarify_cooldown_s` (60s) rate limit. The speaker then has a
  10s `clarify_pending` window in which a responsive answer is accepted
  (Tier 1/2/3 all see the flag).
- **OPEN_FLOOR** → also eavesdropped; `_arm_open_floor` waits
  `open_floor_wait_ms` (1.5s) — if any other human starts speaking, the bot
  yields; if nobody takes the floor, it self-selects and enqueues the
  utterance (with its own TTS pre-open).
- **MENTION** (named in third person) → picked up with probability
  `mention_reply_rate` (0.0 = off in live config), otherwise eavesdropped.
- **ACCEPT** → the speaker enters the active-conversation window
  (`conversation_window_s`, 45s) and the utterance proceeds to the fast
  path / barge-in / enqueue steps described in Layer 1.

### Wake-word fast path

`wake_word/matcher.py:match_only` (L24) returns True only if a wake word is
present **and** there are at most 2 extra chars around it. Pure wake-word
utterances ("一点点点", "嗨 点点") play a random cached `.ogg` from
`FastResponseCache` (`wake_word/fast_response.py` — SHA1 of `voice_id:text`
→ file; stale files cleaned; re-synthesized on persona/voice change)
instead of a full LLM round-trip.

---

## Layer 5 — LLM stream + tool loop

### Typed events — `providers/llm/base.py`

```python
@dataclass class TextDelta:           text: str
@dataclass class ToolUseStart:        tool_use_id: str; name: str
@dataclass class ToolUseInputDelta:   tool_use_id: str; partial_json: str
@dataclass class ToolUseEnd:          tool_use_id: str
@dataclass class MessageEnd:          stop_reason: str  # + token usage fields
```

`MessageEnd` carries token usage (input/output/cache_write/cache_read) —
a new LLM provider that doesn't populate it makes cost tracking go blind.
`stream_text_only(provider, system, messages)` is a thin adapter that yields
only `TextDelta.text` strings — used by the greeting/farewell paths.

### Providers

`ClaudeHaikuProvider.stream_chat` (`providers/llm/claude_haiku.py:35`) maps
Anthropic SDK events to the typed events. **Prompt cache**: when enabled,
the system block carries `cache_control={"type": "ephemeral"}` and the last
non-final assistant turn in history is also cache-flagged. 5-minute TTL;
TTFT < 200ms within the window.

`GroqProvider` (`providers/llm/groq.py`) is an OpenAI-compatible
non-streaming single shot used only for arbitration (Layer 4); it implements
the same `stream_chat` interface (one TextDelta + MessageEnd), no tool use.

### Speculative LLM — `pipeline/speculative.py` + `bot.py:_maybe_spawn_spec_llm`

With `bot.speculative_llm: true` and streaming ASR: at 300ms of silence the
watchdog checks that the streaming pipeline is drained, takes the partial
transcript, runs it through the **same addressee gate as finalize**
(organic mode: speculate only on hard-verdict ACCEPTs — never burn a paid
stream on the gray zone), strips the wake word, and opens
`SpeculativeLLM` — an LLM stream whose events are buffered, with nothing
pushed to TTS yet.

At endpoint, finalize attaches the stream to the Utterance **only if** the
final text and a dialogue-length snapshot both match; `respond_to_user` then
consumes it as round 0 via `events()` (buffer replay + live), giving
`llm_first_delta ≈ 0` in the `[latency]` line. Any non-match or drop path
aborts it (`[spec-llm] wasted` log). Aborted speculations are still billed
by Anthropic but produce no MessageEnd, so they are NOT in costs.db — count
the wasted logs instead.

### Consumer loop + drain-merge — `bot.py:_consumer_loop`

One consumer per guild, serial dispatch from `session.utterance_queue`.
On dequeue: drop if SLEEPING or no voice client (via `_discard_utterance`,
which releases the pre-opened TTS WS and aborts an attached speculative
stream). Then `_drain_merge_extras` (bot.py:1512) empties everything that
piled up in the queue while the previous turn was playing and folds it into
this turn (`[merge] folding N queued utterance(s)`): the merged items'
pre-opened resources are released one by one, and if anything merged, the
primary's speculative stream is voided too (the payload is no longer what
it pre-ran on). Merged speakers appear in the LLM payload as
`queued_speakers` plus a note asking for one combined reply, and all of
them enter the conversation window after the turn.

### `respond_to_user` — the orchestrator

`pipeline/think_speak.py:33`. Critical integration point.

**Setup**:
- Quota gate (`bot.quota_guard.should_block`); on block, play cached
  `_limit.ogg`, return.
- `session.new_turn()`; set `current_addressee_id`; state PROCESSING.
- Build the user message: JSON `{"speaker", "emotion", "content"}` —
  plus `queued_speakers`/`note` on merge turns and `recent_room_chat`
  (Layer 4 ambient) when organic is on. If a speculative stream is
  attached, **its** payload is used verbatim (that's what the model saw).
  Append to history, trim (`trim_history` keeps the dialogue user-first).
- `SentenceChunker` + `OggDemuxer` + `frame_queue` (maxsize 200).
- **Filler**: `should_play_filler(user_text, filler_mode, filler_keywords)`
  — `smart` mode fills only predicted-slow turns (keyword hit → likely tool
  round-trip), `always`/`off` as named. A cached persona phrase's opus
  packets are pre-fed into this turn's `frame_queue` (`[filler] queued N
  packets`); it plays first and the LLM audio appends seamlessly. No second
  playback path; barge-in/cleanup semantics unchanged.
- **TTS WS**: prefer the connection pre-opened at endpoint time; fall back
  to a fresh one if absent or its open already failed. Either way the
  handshake runs **concurrently** with the LLM stream (`tts_open_task`,
  awaited before the first push_text — don't re-serialize it).

**Producer task `drain_tts`**: async-iterate `tts.packets()` (OGG byte
chunks), demux, push opus packets to the frame queue (5ms sleep on Full);
abort early on `client_abort`. At the end, **reliably** put the `None`
sentinel (up to 250 retries / 5s) — without it the audio source never sees
EOF and `play_done` never fires. If zero bytes arrived and the TTS recorded
a server protocol error, an explicit "user heard silence" error is logged.

**Playback**: `StreamingOpusAudioSource(frame_queue)`; stop any prior
playback; `voice_client.play(source, after=→ play_done.set)`;
`session.is_audible = True` (toggles the ack-word filter in Layer 4).

**LLM streaming loop**, max 4 rounds:
- Round 0 uses the attached speculative stream when present; otherwise
  `bot.llm.stream_chat(system, messages, tools)`. Per-event 20s timeout.
- `TextDelta` → buffer + `SentenceChunker`. Each complete sentence passes
  the `speakable()` guard (Layer 6), then `tts.push_text` + `tts.flush`
  (counting UTF-8 bytes for cost).
- `ToolUseStart/InputDelta/End` → accumulate tool_use blocks.
- `MessageEnd` → save stop_reason, **accumulate token usage across rounds**.
- `stop_reason == "tool_use"` → execute tools, append tool_result blocks,
  re-stream. Otherwise flush the chunker remainder, `tts.end_turn()`, break.

**Playback wait**, 30s deadline, polling every 250ms:
`play_done | client_abort | drain-done + 8s grace | deadline`. A bare
`wait_for(play_done)` would not unblock on barge-in; this loop also recovers
when Fish never sends `finish` (see Layer 7).

**Cleanup — single `finally` block, runs on EVERY exit** including
exceptions and CancelledError:
- settle `tts_open_task`, abort any attached speculative stream (idempotent),
  cancel `drain_task`, `tts.close()`, `session.is_audible = False`.
- **Cost recording**: LLM token usage + TTS UTF-8 bytes →
  `bot.cost_tracker.record(...)`. Every paid path must report or the quota
  guard goes blind.
- `[latency]` journey line: stage deltas
  `endpoint→asr_done→consumer_start→llm_first_delta→first_audio→...`.
- History: on normal completion, strip `recent_room_chat` from the user
  message, then commit the assistant reply; otherwise roll back the user
  message (an aborted turn must not poison history).
- State back to IDLE (compare-and-set — a mid-turn `/sleep` stays SLEEPING);
  timestamps, `last_bot_reply`, and conversation-window entries only on
  normal completion.

New early-exit paths inside the try are fine; new acquisitions must release
in that finally. A stuck `is_audible=True` makes the ACK filter eat every
short utterance in the guild.

---

## Layer 6 — Sentence chunker

`utils/sentence_chunker.py` — `SentenceChunker.feed(delta) -> list[str]`.

Two punctuation sets:
- `FIRST_PUNCT = {"。", "!", "?", ",", ";", ",", "~", "、", ".", "\n"}` — lenient
- `PUNCT = {"。", "!", "?", ";", ".", "\n"}` — strict

The first sentence additionally has a **16-char cap** (`FIRST_MAX_CHARS`):
when no punctuation appears within 16 chars, the chunker cuts there anyway —
the cap must win, otherwise the longest-first-sentence case (big delta,
distant punctuation) defeats the low-TTFT purpose. Cost: the cut may land
mid-word with a synthesis seam; increase the constant to roll back.
`_is_first` flips after the first emit; subsequent sentences stay whole for
natural prosody. `flush()` returns any leftover unterminated text.

### `speakable()` guard

`speakable(text)` strips `[emotion tags]`, punctuation, and whitespace and
returns True only if real content remains. Callers (think_speak) **never
push non-speakable chunks to Fish**: an empty chunk (`\n`, tag-only,
punctuation-only) makes Fish return an "empty audio" error and `finish` the
**whole** stream — every sentence pushed afterwards goes silent.

---

## Layer 7 — TTS WebSocket (Fish Audio)

### `FishConfig` — the 6 persona TTS knobs

`providers/tts/fish_audio_stream.py`:

```python
@dataclass
class FishConfig:
    api_key: str
    voice_id: str
    fallback_voice_id: str = ""
    model: str = "s2-pro"
    latency: str = "low"
    base_url: str = "wss://api.fish.audio"
    connect_timeout: float = 5.0
    first_audio_timeout: float = 8.0
    idle_timeout: float = 5.0
    # Per-persona TTS tuning
    temperature: float = 0.7    # 0-1; voice consistency
    top_p: float = 0.7          # 0-1; sampling diversity
    speed: float = 1.0          # prosody.speed multiplier
    volume_db: float = 0.0      # prosody.volume in dB
    chunk_length: int = 200     # generation chunk size
```

`make_tts(cfg, voice_id, persona)` in `providers/factory.py` pulls
`tts_temperature` / `tts_top_p` / `tts_speed` / `tts_volume_db` /
`tts_latency` / `tts_chunk_length` from the persona frontmatter and
threads them into `FishConfig`.

### Connect

`_open_with_voice(voice_id)`:
1. URL `{base_url}/v1/tts/live`; headers `Authorization: Bearer {api_key}`,
   `Model: {model}`.
2. `websockets.connect(...)` with `connect_timeout`. Wraps
   `OSError`/`asyncio.TimeoutError` into `FishConnectError`. The `make_tts`
   retry decorator retries 3× with 0.5/1/2s backoff.
3. Build the start payload:

```python
request_body = {
    "text": "",
    "reference_id": voice_id,
    "format": "opus",
    "temperature": self._cfg.temperature,
    "top_p": self._cfg.top_p,
    "chunk_length": self._cfg.chunk_length,
    "prosody": {
        "speed": self._cfg.speed,
        "volume": self._cfg.volume_db,
    },
}
if self._cfg.latency != "normal":
    request_body["latency"] = self._cfg.latency
payload = {"event": "start", "request": request_body}
```

4. Send msgpack-packed payload; start the `_read_loop()` task.

In the normal flow this open happens at **enqueue time** (Layer 4 pre-open)
so the handshake overlaps the dispatch gap and the LLM stream.

### Send / receive

| Direction | Method | Payload |
|---|---|---|
| → | `push_text(s)` | `{"event": "text", "text": s}` |
| → | `flush()` | `{"event": "flush"}` |
| → | `end_turn()` | `{"event": "stop"}` |
| ← | `_read_loop` | `{"event": "audio", "audio": bytes}` → queue |
| ← | | `{"event": "finish", "reason": "stop"}` → put None, return |

`packets()` is the async iterator the caller drains. Returns when the None
sentinel arrives. Server-side protocol errors (e.g. invalid reference_id,
empty-audio input) arrive as `finish` with `reason != "stop"` and are saved
to `last_error` for the drain_tts diagnostic.

**Failure mode**: Fish Audio sometimes accepts a turn but never sends
`finish`. `_read_loop` blocks indefinitely → `packets()` never terminates →
`drain_tts` never finishes → `play_done` never fires. The 30s deadline +
`bytes_received` log line in `respond_to_user` are the safety net. Don't
extend the deadline.

---

## Layer 8 — OGG demux + frame queue

### `OggDemuxer` — `audio/ogg_demux.py`

RFC 3533 Ogg page parser. Each page header is 27 bytes (`OggS` magic at
offset 0, segment count at offset 26), followed by an N-byte segment table,
then the body. Packets are sequences of segments where the final segment is
< 255 bytes.

- `feed(data)` (L26) — extend buffer, call `_parse_pages()`.
- `_parse_pages()` — sync to `OggS` magic; peel complete pages into
  `_packet_carry`; on a < 255-byte segment, emit the carried packet.
- `_emit_packet` skips the first 2 emissions (OpusHead, OpusTags metadata).
- `packets()` (L32) — generator over the pending audio packet deque.
- `flush()` (L36) — final parse + drain.

### `StreamingOpusAudioSource` — `audio/audio_source.py`

`discord.py` calls `read()` (L35) every 20ms in its player thread. Returns
whatever bytes you give it directly to UDP via `is_opus() = True`.

```python
SILENCE_OPUS = b"\xf8\xff\xfe"   # L20 — 3-byte Celt 20ms mono silence

def read(self) -> bytes:
    if self._eof:
        return b""
    try:
        item = self.frame_queue.get(timeout=0.015)  # 15ms
    except queue.Empty:
        return SILENCE_OPUS
    if item is None:
        self._eof = True
        return b""
    return item
```

The 15ms timeout is shorter than discord's 20ms cadence so we don't
underrun. Returning `b""` triggers discord's `after` callback → sets
`play_done`. Filler packets (Layer 5) sit at the head of the same queue, so
they play before the first LLM-generated audio with no separate path.

---

## Layer 9 — Discord playback

discord.py owns this. `voice_client.play(source, after=callback)` spawns a
player thread that calls `source.read()` every 20ms, sends via the voice
UDP socket, and on `b""` calls `after(error=None)` from the player thread.
We schedule `play_done.set()` on the main loop via
`bot.loop.call_soon_threadsafe`.

This layer is a black box. If `play_done` never fires, the bug is almost
always Layer 8 (no `None` sentinel reached the queue) or Layer 7 (Fish
Audio never sent `finish`).

---

## Bypass modules

These don't sit *on* the data path but they can drop or redirect packets.
Check them before going deep into a layer.

- **Whitelist** — `config.bot.listen_only_users` + `/admin whitelist`.
  Filter at Layer 1 entry; logged once per skipped user. Non-empty list =
  the bot IGNORES everyone else — check this first when "the bot can't hear
  person X".
- **Co-owners** — `bot.extra_owner_ids` + `/admin owner`. No pipeline
  impact; only authorizes additional users for owner-only slash commands.
- **i18n** — `src/echotwin/i18n/`. Wraps slash command UI text. Nothing
  else.
- **`voice_recv_patch.py`** — see Layer 0.

---

## End-to-end log trail

When everything is working, you'll see these lines for each turn:

```
[utt] user U START (first real packet)
[watchdog] user U: no real audio in 0.6Xs — finalizing utterance
[asr] speculation HIT for user_name              # batch ASR + speculative_asr
[ASR/watchdog] user_name(uid): text  emotion=NEUTRAL
[organic] user_name: verdict=accept score=99 signals=['wake_word'] text='...'
[arbiter] accept (回答她的提问) 312ms for '...'   # gray-zone turns only
[spec-llm] speculative stream opened for '...'   # speculative_llm on
[spec-llm] speculation MATCHED — attaching to turn
[consumer] dequeued: user_name: '...'
[merge] folding N queued utterance(s) into this turn: ...   # if backlog
[respond] start: user=U text='...'
[filler] queued N packets from _filler_xxx.ogg   # slow-turn filler
[respond] starting LLM stream
[respond] first TTS audio chunk received (bytes=N)
[respond] LLM done, total_chars=N text='...'
[respond] drain_tts done, total bytes_received=N
[latency] endpoint→asr_done=Xms asr_done→consumer_start=Xms ... total=Xms
[emotion-sidecar] uid=U emotion=HAPPY took=Xms   # streaming ASR mode
[heartbeat] uptime=Xs guilds=N sessions=M cost_today=$X
```

Every 5s while in-channel:
```
[stats] guild G: dave_epoch=N reader=alive mls_users=[...] u<id> att/ok/fail/pass=...
[stats] user U: real_ok=Δ fail=Δ (last 5s)  in_speech=bool  last_real=Xs ago  is_audible=bool  state=...
```

Plus `[amp] frames=N rms=X peak=Y` every 100 decoded frames — the quickest
way to confirm real audio energy is arriving.

---

## Debug order

Symptom: bot doesn't respond. Walk down the layers, stop at the first
missing line:

| Missing log line | Suspect layer | Common cause |
|---|---|---|
| `[stats] real_ok=0` | 0/1 | Discord not delivering (att flat); whitelist blocking; DAVE decrypt failing (fail rising); reader=DEAD (auto-restart should fire) |
| `[utt] user U START` | 1 | packets all sentinels; whitelist; SLEEPING state |
| `[amp] rms≈0` | physics | **a speaker-on-loudspeaker user gets ducked by their own client's echo cancellation while the bot is talking — the server receives near-zero RMS. Client-side physics; headphones fix it** |
| `[ASR/...] text` | 3 | sherpa/SenseVoice model load failed; <100ms audio |
| `[ASR/...] dropping pure-punct` / `dropping {N}ms utterance` | 4 (filters) | working as intended — below 600ms or no content |
| `[ASR/...] treating ack-word` | 4 (filters) | working as intended — bot was speaking and you said "嗯" |
| `[organic] verdict=reject` | 4 (verdict) | eavesdropped on purpose; check the signals list / `[arbiter]` reason |
| `[arbiter]` slow or `timeout` | 4 (tier 2) | Groq down / key missing → heuristic fallback (look for `arbiter_fallback` in signals) |
| `[addressee] dropping non-addressed` | 4 (legacy) | organic disabled + continuation_window=0 → needs explicit wake word |
| `[respond] start` | 4/5 (queue) | utterance never enqueued, or consumer dropped it (SLEEPING / no voice client) |
| `[spec-llm] wasted` (frequent) | 5 | partials unstable or gray-zone-heavy traffic — wasted paid streams; consider disabling speculative_llm |
| `[respond] LLM done, total_chars=0` | 5 | LLM returned empty — check Anthropic API key |
| `[respond] first TTS audio chunk received` | 7 | Fish never sent audio — check API key, voice_id, model=s2-pro; also `TTS produced NO audio` + last_error |
| `[respond] drain_tts done` | 7 | Fish never sent `finish` — 30s deadline will eventually fire |
| `[respond] play_done` wait times out | 8/9 | None sentinel never reached queue, OR discord.py player thread crashed |

For SPEED issues (bot responds but slowly), read the per-turn `[latency]`
line instead: the stage deltas
(`endpoint→asr_done→consumer_start→llm_first_delta→first_audio`) point at
the slow layer directly. `[merge]` lines explain "it answered three people
at once"; `llm_first_delta≈0` means a speculative stream attached.

If voice connection silently dies after barge-in: that's the Layer 0 patch 3
case. Should be fixed; if it returns, `/leave` + `/join` recovers and
`audio/voice_recv_patch.py` needs revisiting.
