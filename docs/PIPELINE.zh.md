[English](PIPELINE.md) | **简体中文**

# EchoTwin 实时管道

用户开口 → bot 用克隆声回。中间过的 **10 层(Layer 0-9)**,每一层都在这里
写清楚:入口函数(文件名,行号仅在稳定处标注)、输入、输出、调节参数、
失败模式。

出 bug 时按文末"Debug 顺序"表对照:第一条不见了的 log,就是问题所在那层。

---

## 数据流

```
                ┌─ Layer 0: Discord 入口 ─────────────────────────┐
                │   VoiceRecvClient + DAVE 解密 + 3 个 patch     │
                │   listen.py:VoiceListener.write → bot 回调      │
                │   输出: per-user opus 包(20ms @ 48k stereo)    │
                └────────────────┬────────────────────────────────┘
                                 ▼
┌──── Layer 1: 音频解码 + 端点 watchdog ──────────────────────────────┐
│   bot.py:on_user_audio / _speech_watchdog_loop                       │
│   opuslib_next 解码 → numpy downmix mono → soxr 48k→16k             │
│   per-user 状态: in_speech, opus_ok/fail 计数, preroll buffer       │
│   watchdog: endpoint_silence_ms 墙钟端点;途中触发投机 ASR(300ms)  │
│   和投机 LLM                                                         │
│   输出: pcm_48k_mono (→ ASR) + pcm_16k_mono (→ VAD)                 │
└────────────────────────────┬─────────────────────────────────────────┘
                             ▼
┌──── Layer 2: VAD (Silero ONNX) ──────────────────────────────────────┐
│   providers/vad/silero.py:feed (L61) — 只做噪声门控,不做端点       │
│   512 sample (32ms) 每块;双阈值滞回                                 │
│   输出: VADResult(is_voice, speech_started, utterance_ended)         │
│   配套: audio/preroll_buffer.py — 300ms 历史 → ASR 头              │
└────────────────────────────┬─────────────────────────────────────────┘
                             ▼
┌──── Layer 3: ASR — sherpa-onnx 流式 zipformer(默认)────────────────┐
│   providers/asr/sherpa_stream.py — 边说边出 partial_text();         │
│   final = 0.4s 静音 flush + input_finished(~20ms)                   │
│   批式回退: funasr_local.py (SenseVoiceSmall) + 300ms 静默投机 ASR; │
│   emotion sidecar 旁路补情绪                                         │
│   输出: ASRResult(text, language, emotion, is_final)                 │
└────────────────────────────┬─────────────────────────────────────────┘
                             ▼
┌──── Layer 4: 受话判定(organic 三层)────────────────────────────────┐
│   bot.py:_finalize_utterance                                         │
│   600ms 闸 → 纯标点闸 → ack-word 闸                                 │
│   ① organic.py:hard_verdict(查表秒判)                              │
│   ② arbiter.py LLM 仲裁(灰区,Groq qwen3-32b)                     │
│   ③ organic.py:classify 启发式兜底                                   │
│   分发: ACCEPT/REJECT/CLARIFY/OPEN_FLOOR/MENTION                     │
│   随后: 唤醒快通道 → barge-in → TTS WS 预开 → 入队                 │
│   legacy 模式(organic.enabled=false): addressee.py 4 条规则        │
└────────────────────────────┬─────────────────────────────────────────┘
                             ▼
┌──── Layer 5: LLM 流 + 工具循环 ──────────────────────────────────────┐
│   pipeline/think_speak.py:respond_to_user (L33)                      │
│   consumer 出队: _drain_merge_extras 把积压话语合并成一轮           │
│   filler 垫话: 预判慢轮先排缓存短语                                 │
│   投机 LLM 流匹配时作为第 0 轮直接接上                               │
│   typed events: TextDelta / ToolUseStart / ...Delta / ...End / End  │
│   max 4 轮 tool;per-event 20s 超时;prompt cache                    │
│   输出: TextDelta 流 → Layer 6                                       │
└────────────────────────────┬─────────────────────────────────────────┘
                             ▼
┌──── Layer 6: 句子切分 ───────────────────────────────────────────────┐
│   utils/sentence_chunker.py:feed                                     │
│   首句宽松标点 + 16 字上限;后续句严格标点                           │
│   speakable() 护栏 — 空块/纯标签/纯标点绝不推 Fish                  │
│   输出: 完整句子 → tts.push_text + tts.flush                        │
└────────────────────────────┬─────────────────────────────────────────┘
                             ▼
┌──── Layer 7: TTS WebSocket (Fish Audio) ─────────────────────────────┐
│   providers/tts/fish_audio_stream.py:_open_with_voice                │
│   msgpack over WSS;6 个 persona TTS 调音参数在 start payload        │
│   WS 通常在入队时已被 Layer 4 预开                                   │
│   连接 3× 重试 (async_retry, 0.5/1/2s 退避)                         │
│   输出: OGG/Opus 48k mono 字节流                                     │
└────────────────────────────┬─────────────────────────────────────────┘
                             ▼
┌──── Layer 8: OGG demux + 帧队列 ─────────────────────────────────────┐
│   audio/ogg_demux.py — RFC 3533 page parser;跳过 OpusHead/OpusTags  │
│   audio/audio_source.py — discord.AudioSource 桥;15ms 超时;       │
│     饥饿时 SILENCE_OPUS;None sentinel 触发 EOF                      │
│   queue: sync_queue.Queue(maxsize=200) — filler 包先进队             │
└────────────────────────────┬─────────────────────────────────────────┘
                             ▼
                ┌─ Layer 9: discord.py player thread ─────────────┐
                │   voice_client.play(source, after=callback)     │
                │   read() 每 20ms 调一次;bot.loop 回调          │
                │   set play_done event                           │
                └─────────────────────────────────────────────────┘
```

---

## 配置速查

默认值来自 `src/echotwin/config.py`(pydantic 模型),`config.yaml` 可覆盖。
Owner slash 命令把一小部分持久化到 `data/runtime_config.json`。

| 段落 | 字段 | Pydantic 默认 | `config.yaml` 实际 |
|---|---|---|---|
| `bot` | `endpoint_silence_ms` | 600 | 600 |
| | `endpoint_tick_ms` | 100 | 100 |
| | `speculative_asr` | true | true |
| | `speculative_asr_silence_ms` | 300 | 300 |
| | `speculative_llm` | false | **true** |
| | `filler_mode` | `smart` | `smart` |
| | `filler_keywords` | 天气/几点/时间/日期/查/搜 | 同左 |
| `bot.organic` | `enabled` | false | **true** |
| | `gray_zone` | `llm` | `llm` |
| | `arbiter_provider` | `""`(复用 Haiku) | **`groq`** |
| | `arbiter_timeout_ms` | 1500 | 1500 |
| | `arbiter_max_per_min` | 20 | 20 |
| | `ambient_max_age_s` | 120 | 120 |
| | `conversation_window_s` | 45 | 45 |
| | `clarify_cooldown_s` | 60 | 60 |
| | `open_floor_wait_ms` | 1500 | 1500 |
| | `mention_reply_rate` | 0.0 | 0.0(关) |
| `vad.silero` | `threshold` | 0.5 | 0.5 |
| | `threshold_low` | 0.3 | 0.3 |
| | `min_silence_duration_ms` | 250 | **800** |
| | `frame_window` | 2 | **3** |
| | `preroll_ms` | 300 | 300 |
| `asr` | `provider` | `funasr_local` | **`sherpa_stream`** |
| | `emotion_sidecar` | true | true |
| `asr.funasr_local` | `language` | `auto` | **`zh`** |
| `asr.sherpa_stream` | `repo` | zipformer 双语 zh-en int8 | 同左 |
| `addressee`(legacy) | `continuation_window_seconds` | 15.0 | **0** |
| | `solo_channel_auto` | true | true |
| `llm.claude_haiku` | `max_tokens` | 300 | 300 |
| | `temperature` | 0.7 | 0.7 |
| | `enable_prompt_cache` | true | true |
| `llm.groq` | `model` | `qwen/qwen3-32b` | 同左 |
| | `max_tokens` | 100 | 100 |
| | `temperature` | 0.0 | 0.0 |
| `tts.fish_audio_stream` | `model` | `s2-pro` | `s2-pro` |
| | `latency` | `low` | `low` |

`runtime_config.json` 字段:`active_persona`、`voice_id_override`、
`wake_word_required`、`listen_only_users`、`extra_owner_ids`。开机加载,
任何修改这些值的 owner slash 命令都会立即写盘。

---

## Layer 0 — Discord 入口

### DAVE 端到端解密 — `audio/dave_patch.py`

Discord 在 **2026-03-02** 强制启用 DAVE。RTP 解密之后 opus 内容**仍然**用
per-user DAVE 密钥加密,libopus 会拒绝(报 "corrupted stream")。Patch
对 `AudioReader.__init__` 和 `decryptor.decrypt_rtp` 打 monkey-patch,
RTP 解完后调 `davey.DaveSession.decrypt(user_id, MediaType.audio, rtp_payload)`
再吐给 libopus。另外它还:
- 每会话调一次 `set_passthrough_mode(True, 10)`
- 跳过非 opus payload type(只解 PT 120)
- 把预期失败与**非预期失败**分开计数;非预期的(如 epoch 失步)在
  1/10/每 100 次时打 log

**失败回退**(都退回 RTP 明文):
- `dave_session` 没准备好 → `dave_passthrough++`
- SSRC → user_id 映射缺 → `no_user++`
- 预期的 `UnencryptedWhenPassthroughDisabled` → `dave_fail_expected++`
  (会话开头丢 1-3 帧;协议设计如此,重试没用)
- 其他 DAVE 错 → `dave_fail_unexpected++`,log

退回 RTP 明文后,libopus 大概率失败,Layer 1 那头 `opus fail++`。如果
`Unencrypted...` 出现在会话**中段**,那是 epoch/transition 问题,不是
开场噪声。

### 三个防御性 patch — `audio/voice_recv_patch.py`

启动时 `apply_voice_recv_patches()` (L98) 全部应用。修
`discord-ext-voice-recv 0.5.2a179` 的 alpha 期 bug。

**Patch 1 — `_remove_ssrc` 安全化** (L109)。原版在 `_reader is MISSING`
时(关闭/重连过程中)调,会崩 `AttributeError: '_MissingSentinel' object
has no attribute 'speaking_timer'`。包装后检测到 sentinel 直接 no-op。

**Patch 2 — macOS UDP keepalive** (L57)。discord.py 给 UDP socket
`connect()` 过,voice_recv 又用 `sendto(packet, addr)` → macOS 报
`OSError: EISCONN`,死循环烧 CPU。Patch 改成先 `sock.send(packet)`(已连接
socket 直接收),失败再回退 `sendto`,两次都失败的话 `max(1.0, delay)`
退避避免烧 CPU。

**Patch 3 — `stop()` 只停播放** (L140)。原版 `VoiceRecvClient.stop()`
**同时**调 `stop_playing()` 和 `stop_listening()`。每次 barge-in 调
`voice_client.stop()` 都会静默杀死接收器 — 就是"打断后语音连接断"那个
bug。Patch 恢复 vanilla discord.py 语义:`stop()` 只停播放。

### 监听回调 — `pipeline/listen.py`

`VoiceListener(voice_recv.AudioSink).write(user, data)` (L35) 在 voice_recv
线程里跑。通过 `run_coroutine_threadsafe` 把
`bot.on_user_audio(user_id, name, opus)` 调度到主 loop。跳过 bot 自己的
音频和 `data.opus is None` 的帧。挂监听必须走
`bot.start_listening(vc, guild_id)` — 它注册了 death watch,voice_recv 的
AudioReader 挂掉时自动重启(上限 5 次)。

---

## Layer 1 — 音频解码 + 端点 watchdog (`bot.py`)

### `on_user_audio` — 包入口

`bot.py:529` — `async def on_user_audio(self, guild_id, user_id, user_name, opus_bytes)`

每包流程:

1. trace 包(默认关,需要设 `VOICE_AGENT_TRACE=1`)。
2. **白名单闸**。`config.bot.listen_only_users` 非空且 `user_id` 不在里面
   → drop,**每个被跳过的用户各 log 一次**。
3. drop 3 字节 sentinel(`SILENCE_OPUS`,`len < 4`)。
4. 戳 `last_activity_time` 和 `_last_real_audio_{user_id}`;取消 pending
   farewell。
5. opus 解码(per-user `OpusDecoder`,48kHz stereo,5760 samples/帧)。
   `OpusError` 时 per-user `opus_fail++`,每 50 个 fail log 一次。
6. numpy downmix mono(int16 → reshape (N,2) → mean(axis=1) → int16)。
7. 每 100 帧打一次 `[amp]` RMS 振幅诊断。
8. per-user `Resampler` 48k→16k(只给 VAD 用)。
9. per-user `PrerollRingBuffer`(`max_frames = preroll_ms // 20` = 15 帧
   @ 300ms)。push 48k mono。
10. per-user `SileroVAD.feed(pcm_16k)` → `VADResult`。
11. 收到第一个真实包时记账:log `[utt] user U START`,设
    `_in_speech_{user_id} = True`,快照 `_utt_opus_ok/_utt_opus_fail`
    (`_finalize_utterance` 用来算 utterance 长度),并**取消过期的投机
    ASR、abort 过期的投机 LLM 流**(用户又开口了 → 之前的投机全部作废)。
12. 懒加载 `await asr.open()`。
13. 喂 ASR。门控 `vad_result.is_voice or in_speech`。speech START 后第一
    帧先把 preroll buffer 倒进 ASR。

注意:**触发 `_finalize_utterance` 不是 VAD 自己的 `utterance_ended`**,
而是墙钟 watchdog。VAD 的作用只是给 in_speech 标志当门控。

### `_speech_watchdog_loop` — 端点检测 + 投机触发

`bot.py:708` — `async def _speech_watchdog_loop(self)`

tick 与静默阈值来自 `config.bot.endpoint_tick_ms` / `endpoint_silence_ms`
(100ms / 600ms),每个 tick 重新读取,SIGHUP 热加载即时生效。每 tick,
对每个有活跃 ASR 的 user:

- **投机 ASR**(批式 ASR 模式,`bot.speculative_asr`):静默达到
  `speculative_asr_silence_ms`(300ms)就把已缓冲的音频丢去后台预跑推理。
  门槛 ≥25 个包 — 噪声毛刺反正会被 <600ms 闸丢掉,不值得烧一次推理。
- **投机 LLM 触发**(`bot.speculative_llm`,仅流式 ASR):同样的 300ms
  静默窗口 → `_maybe_spawn_spec_llm`(见 Layer 5)。
- **端点**:`_in_speech_{user_id}` 为 True 且
  `now - _last_real_audio_{user_id} >= endpoint_silence_ms`:
  - 清 `_in_speech` 和 `_preroll_drained` 标志,reset VAD
  - `preroll.clear()` — 丢掉本句的静音尾巴,不然会拼到下一句开头
    (污染下一句句首的唤醒词)
  - `_spawn_finalize` — 把 `_finalize_utterance` 作为追踪后台任务跑。
    per-(guild,user) 去重:上一个 finalize 还在跑就**推迟**这次端点
    (恢复 in_speech,watchdog 下个 pass 重触发),不是丢弃。opus-ok
    快照在 spawn 前同步取好。

每 5s 打 `[stats]` 块:每 guild 一行
(`dave_epoch / reader=alive|DEAD / mls_users / per-user att/ok/fail/pass`)
加每 user 一行(`real_ok=N fail=M (last 5s) in_speech last_real
is_audible state`)。这是语音检测排障的入口 — 见文末 Debug 顺序。

### `_finalize_utterance` — 端点 → ASR 文本 → 判定 → 入队

`bot.py:1166`。顺序(每个丢弃点都打 log):

1. 先把该用户的投机 LLM 流 pop 出来 — 下面每条退出路径要么把它带进
   Utterance,要么 abort 掉(`finally` 兜底;泄漏的流在 Anthropic 持续
   计费)。
2. 用 per-user opus 计数(spawn 时的快照)算 `utt_ms`。
3. ASR 定稿:如果有投机 ASR 结果且其 fed 标记等于 ASR 当前缓冲字节数
   (没有新音频进来),直接采纳 — `[asr] speculation HIT`,免一次重推理;
   否则 `await asr.end_utterance()`。
4. 空结果 → log 后丢。**600ms 闸**:`utt_ms < 600` → 丢。**纯标点闸**
   → 丢。**ack-word 闸**:`session.is_audible`(bot 在说话)且内容在
   `ACK_WORDS`(嗯/对/好/哦/啊/呃/是,yeah/yes/ok/okay/uhhuh/mhm 等
   变体)→ 丢,不打断 bot。
5. **受话判定** — 见下方 Layer 4。REJECT/CLARIFY/OPEN_FLOOR/MENTION 各
   路径在此 return;只有 ACCEPT 继续往下。
6. 唤醒词快通道:`wake_matcher.match_only(text)`(wake + ≤2 额外字)→
   播随机缓存 `fast_response.ogg` 然后 return,跳过 Layer 5-7。
   PROCESSING 期间禁用(否则会把被截断的回复提交进历史)。
7. 去掉 wake word;空了 → 默认 "嗨"。
8. **barge-in**(在判定之后、对去除唤醒词后的文本):session 在
   PROCESSING 且说话人是当前受话人(或 `barge_in_mode == "anyone"`)→
   `session.abort()` 掉当前轮。
9. **TTS WS 预开**:utterance 队列为空且不在 PROCESSING 时,在这里
   `make_tts(...)` + 开 open task,让 WS 握手藏进调度间隙。socket 挂在
   Utterance 上 — 被丢弃的 utterance 必须走 `bot._discard_utterance`,
   否则 Fish WS 泄漏。
10. **emotion sidecar**(流式 ASR 模式):用留存的本句 PCM 起 SenseVoice
    旁路 — 见 Layer 3。
11. **投机 LLM 附着**:仅当最终文本与对话长度快照都和开流时一致才带进
    Utterance(`[spec-llm] speculation MATCHED`);否则 abort。
12. 替换该用户已排队的旧条目(`_dequeue_user`),入队
    `Utterance(user_id, user_name, text, emotion, journey, tts,
    tts_open_task, spec_llm)`。

---

## Layer 2 — VAD (Silero)

`providers/vad/silero.py:61` — `def feed(self, pcm_16k_16bit: bytes) -> VADResult`

每次调用:

1. append PCM 到内部 buffer。
2. 当 buffer ≥ 1024 字节(512 samples = 32ms @ 16k),弹一块:
   - ONNX 推理。拼上 64-sample 上下文(从上一块来的 overlap),喂
     `{"input": x, "state": s, "sr": 16000}`,拿到概率 + 新 state。
   - 双阈值滞回。`prob >= threshold (0.5)` → voice,
     `prob <= threshold_low (0.3)` → silent,中间区段保持上一刻状态。
   - 推入滑窗(`maxlen = frame_window`);本块算 voice 仅当窗内**全是**
     voice。
   - 静默计数器。voice 时:重置计数,0→1 转换发 `speech_started`。
     silent 时(已有 speech 之后):计数 ++;达到
     `min_silence_duration_ms / 32` 块发 `utterance_ended` 并重置。
3. 返回 `VADResult(is_voice, utterance_ended, speech_started)`。

VAD 结果**只用作喂 ASR 的噪声门控**,端点检测在 Layer 1 的 watchdog。

`audio/preroll_buffer.py` — `class PrerollRingBuffer(max_frames=15)`。
`push(frame)` append 到 deque,`drain()` concat + 清空。speech 起点之前的
~300ms 拼到 ASR 头帧前面,防止首音节被切。

**失败模式**:
- 阈值太低 → 环境噪声唤醒 ASR。
- 阈值太高 → 轻声说话听不到。
- `min_silence_duration_ms` 太短 → 句中呼吸停顿被当端点,句子切碎。
- 太长 → 两句粘一起,LLM 上下文乱。

---

## Layer 3 — ASR

### 默认引擎:sherpa-onnx 流式 zipformer — `providers/asr/sherpa_stream.py`

`asr.provider = sherpa_stream`。双语 zh-en zipformer,int8(~100MB,HF
自动下载)。音频**边到边识别**:

- `feed_audio(pcm_48k_mono)` 重采样到 16k 后累积;每攒够 ~100ms 由串行化
  decode 任务刷新 `partial_text()`。partial 驱动投机 LLM 触发(Layer 1
  watchdog)。
- `end_utterance()` 等在飞的 decode 收尾,把剩余 samples 加 **0.4s 静音**
  喂进去,调 `input_finished()`,解到尽头 — 最后这次 flush 经常吐出最后
  一个词,耗时 ~20ms。
- `speculate()` 故意返回 `(None, -1)`:端点处采纳 partial 会截掉尾词,
  而 final chunk 本身就便宜,不需要投机。
- 类级 recognizer 缓存 + per-repo 推理锁(同模型所有实例共享)。保留
  `last_utterance_pcm`(至多 30s 的 48k PCM)给 emotion sidecar 用。
- 引擎决策:funasr paraformer-zh-streaming 在 M 系 CPU 上 spike 实测
  RTF≈1.5(比实时还慢,3/3 门槛全挂);sherpa zipformer int8 实测 chunk
  15ms / final flush 22ms。证据:`scripts/spike_streaming_asr.py` vs
  `scripts/spike_sherpa_streaming.py`。别"为了一致性换回同一个库"。

流式结果不带情绪标签 — 这一层的 `emotion` 恒为 `NEUTRAL`,见下面的
sidecar。

### 批式回退:FunASR SenseVoiceSmall — `providers/asr/funasr_local.py`

`asr.provider = funasr_local`。`feed_audio` 只 append 进 buffer;
`end_utterance()` 重采样 48k→16k,在 executor 跑 `model.generate(...)`
(同步阻塞),由 `sensevoice_parse.py` 解析 `<|tag|>` 输出 →
`ASRResult(text, language, emotion)`。

- **类级模型缓存**(`_model_cache` 按 `(model_dir, device)` 键 + 锁):
  SenseVoice 冷加载 5-10 秒;多用户各有实例但共享已加载模型。别移回
  实例级。
- **投机 ASR**(仅批式模式):`speculate()` 在 300ms 静默时对 buffer
  快照(不清空)推理;finalize 仅当 fed 标记等于端点时的 buffer 大小
  (没有新音频)才采纳。省掉端点后串行等 ASR 的时间。
- tag parser 同时兼容 `<|zh|><|SAD|><|EVENT|>content` 和
  `<|EMO_UNKNOWN|><|Speech|><|withitn|>content` 两种顺序;emotion 来自
  固定集(NEUTRAL、HAPPY、SAD、ANGRY、FEARFUL、SURPRISED、DISGUSTED)。

### Emotion sidecar — `bot.py:_spawn_emotion_sidecar`

流式 ASR 生效且 `asr.emotion_sidecar: true` 时,每条被接受话语的留存 PCM
会**后台**再过一遍共享的 SenseVoice。结果写进
`session.last_emotion[user_id]`,**下一轮**才生效 — 本轮的 LLM 消息在
sidecar 跑完前就已发出,绝不阻塞回复。consumer 取情绪时,本轮真实情绪
(批式路径)优先于 sidecar 缓存。

---

## Layer 4 — 受话判定

两种模式。`bot.organic.enabled: true`(线上配置)走下面的三层 organic
判定;`enabled: false` 走 legacy `pipeline/addressee.py` 4 规则(唤醒词 /
显式 @mention / continuation 窗口 / solo 频道 — 线上
`continuation_window_seconds` 为 0,该规则关闭)。

### 第一层 — 查表秒判:`pipeline/organic.py:hard_verdict`

纯查表计数,零语义判断,微秒级:

1. **唤醒词在句首/句尾**(呼格)→ 秒 ACCEPT。名字在句中 → 灰区
   (可能是第三人称提及)。
2. **solo**(频道里只有说话人 + bot)→ 秒 ACCEPT。
3. **≤3 字无含义碎渣** → 秒 REJECT — 只拒 ASR 碎渣("你帮你"、"喽"):
   ACK 集合里的短词("好的"、"哈哈")、疑问句、语气词结尾、以及
   clarify 待答期间的短句统统进灰区。

其余返回 None = **灰区**。保留两条 accept 规则是因为 accept 路径必须
零延迟(快回复缓存 + 投机流都挂在它上面);reject 规则纯属省钱。

### 第二层 — LLM 仲裁:`pipeline/arbiter.py:arbitrate`

灰区话语交给小 LLM 判(`organic.gray_zone: llm` 时)。输入:本句 +
说话人、最近 6 句旁听、bot 上一句、bot 上次在回应谁、in_window 标志、
clarify_pending 标志。输出一行 JSON:
`{"verdict": "accept|reject|clarify|open_floor", "reason": "..."}`。

- Provider:`organic.arbiter_provider: groq` → 独立 Groq 客户端
  (`llm.groq`,默认 `qwen/qwen3-32b`,`reasoning_effort: none`,
  ~150-350ms,log 为 `[arbiter] ... Nms`)。缺 `GROQ_API_KEY`(或
  `arbiter_provider: ""`)则复用对话 Haiku。
- 保险:`arbiter_timeout_ms`(1.5s)超时 + 每 guild 每分钟
  `arbiter_max_per_min`(20)次的频率保险丝 — 超时/失败/坏 JSON/超频
  一律回落到第三层。
- system prompt 里的 few-shot 例句是**刚需,不是装饰**:零样本受话判定
  接近瞎猜,Groq 冒烟测试不带例句时"你指的是别人"那个 case 直接挂。
- 仲裁也是付费调用:usage 记入 cost tracker(Groq 用 qwen3 32b 价目,
  否则按 Haiku 记)。qwen 的 `<think>` 块在抽 JSON 前剥掉。

### 第三层 — 启发式兜底:`pipeline/organic.py:classify`

完整打分规则集(呼格/solo/clarify 接续/开放提问/ACK/窗口内打分:第二
人称、话题双字重叠、技能祈使、自言自语与第三人称扣分等)。它是仲裁失败
时的安全网,`gray_zone: heuristic` 时则是整个判定器。验收由黄金集
`tests/fixtures/addressee_golden.jsonl` 驱动(漏接 ≤10%、误接 ≤10%、
灰区倾向 accept)。这也是唯一能产出 MENTION 的一层。

### Verdict 分发(在 `bot.py:_finalize_utterance` 里)

每个 verdict 打一行 `[organic]`(`verdict=... score=... signals=...`)。

- **REJECT** → 旁听:该句 append 进 `session.ambient`(deque,maxlen
  30),在下一个被接受的轮次注入 payload 的 `recent_room_chat` — 上限
  12 行 / 500 字,只取比 bot 上一句新、且在 `ambient_max_age_s`(120s)
  内的条目。`recent_room_chat` 在**提交历史前剥离**(它是即时参考,不是
  记忆)。
- **CLARIFY** → 同样旁听;播放缓存的反问音频("诶,是在叫我吗?",
  启动时按 persona 声音合成),per-user `clarify_cooldown_s`(60s)冷却。
  说话人随后有 10s 的 `clarify_pending` 窗口,期间有针对性的回答会被
  接受(三层判定都看得到这个标志)。
- **OPEN_FLOOR** → 同样旁听;`_arm_open_floor` 等 `open_floor_wait_ms`
  (1.5s)— 期间有别的真人开口就让位;没人接就自荐,把这句入队
  (自带 TTS 预开)。
- **MENTION**(被第三人称提到)→ 按 `mention_reply_rate` 概率接话
  (线上 0.0 = 关),否则旁听。
- **ACCEPT** → 说话人进入对话活跃窗口(`conversation_window_s`,45s),
  按 Layer 1 描述继续走快通道 / barge-in / 入队。

### 唤醒词快通道

`wake_word/matcher.py:match_only` (L24) 仅当 wake word 出现**且**前后
额外字 ≤2 时返回 True。纯唤醒语句("一点点点"、"嗨 点点")从
`FastResponseCache`(`wake_word/fast_response.py` — `voice_id:text` 的
SHA1 → 文件;过期文件清理;persona/声音切换时重新合成)播随机缓存
`.ogg`,跳过整个 LLM 往返。

---

## Layer 5 — LLM 流 + 工具循环

### Typed events — `providers/llm/base.py`

```python
@dataclass class TextDelta:           text: str
@dataclass class ToolUseStart:        tool_use_id: str; name: str
@dataclass class ToolUseInputDelta:   tool_use_id: str; partial_json: str
@dataclass class ToolUseEnd:          tool_use_id: str
@dataclass class MessageEnd:          stop_reason: str  # + token usage 字段
```

`MessageEnd` 携带 token usage(input/output/cache_write/cache_read)——
新 LLM provider 不填它,成本追踪就瞎了。
`stream_text_only(provider, system, messages)` 是个 thin adapter,只 yield
`TextDelta.text` 字符串 — greeting / farewell 路径用这个。

### Providers

`ClaudeHaikuProvider.stream_chat`(`providers/llm/claude_haiku.py:35`)把
Anthropic SDK 事件映射成 typed events。**Prompt cache**:打开时 system 块
带 `cache_control={"type": "ephemeral"}`,历史里最后一个非末尾 assistant
turn 也加 cache flag。5 分钟 TTL;窗口内 TTFT < 200ms。

`GroqProvider`(`providers/llm/groq.py`)是 OpenAI 兼容的非流式单发,
只给 Layer 4 仲裁用;实现同一个 `stream_chat` 接口(一个 TextDelta +
MessageEnd),不支持 tool use。

### 投机 LLM — `pipeline/speculative.py` + `bot.py:_maybe_spawn_spec_llm`

`bot.speculative_llm: true` 且流式 ASR 时:静默 300ms,watchdog 确认流式
管线已排干后取 partial 文本,过**和 finalize 同一套受话闸**(organic
模式下只对 hard_verdict ACCEPT 投机 — 绝不为灰区烧付费流),去掉唤醒词,
开 `SpeculativeLLM` — 事件先缓冲,什么都不推 TTS。

端点确认时,finalize 仅当最终文本与对话长度快照都匹配才把流附到
Utterance;`respond_to_user` 把它当第 0 轮经 `events()` 消费(缓冲回放 +
实时续流),`[latency]` 里 `llm_first_delta≈0`。任何不匹配或丢弃路径都
abort 它(`[spec-llm] wasted` log)。被 abort 的投机 Anthropic 照样计费,
但没有 MessageEnd 就没有 usage,**不在 costs.db 里** — 数 wasted log。

### Consumer 循环 + 排队合并 — `bot.py:_consumer_loop`

每 guild 一个 consumer,从 `session.utterance_queue` 串行出队。出队后:
SLEEPING 或没有 voice client → 丢弃(走 `_discard_utterance`,释放预开的
TTS WS、abort 附着的投机流)。然后 `_drain_merge_extras`(bot.py:1512)
把上一轮播音期间积压在队列里的话语全部捞出合并进这一轮
(`[merge] folding N queued utterance(s)`):被合并条目的预开资源逐个
释放;只要有合并,主话语的投机流也作废(payload 已不是它预跑的那个)。
被合并的发言以 `queued_speakers` 出现在 LLM payload 里,附一条"综合所有
人一次性简短回应"的 note;这一轮结束后他们都进入对话窗口。

### `respond_to_user` — 编排器

`pipeline/think_speak.py:33`。关键集成点。

**Setup**:
- quota 闸(`bot.quota_guard.should_block`);超限 → 播缓存 `_limit.ogg`
  → return。
- `session.new_turn()`;set `current_addressee_id`;state 设 PROCESSING。
- 构造 user message:JSON `{"speaker", "emotion", "content"}` — 合并轮
  追加 `queued_speakers`/`note`,organic 开着时追加 `recent_room_chat`
  (Layer 4 旁听)。若附着了投机流,直接用**它的** payload(那才是模型
  实际看到的)。append 进历史并裁剪(`trim_history` 保证对话以 user
  开头)。
- `SentenceChunker` + `OggDemuxer` + `frame_queue`(maxsize 200)。
- **Filler 垫话**:`should_play_filler(user_text, filler_mode,
  filler_keywords)` — `smart` 只垫预判慢的轮(关键词命中 → 大概率多一次
  tool 往返),`always`/`off` 如其名。缓存 persona 短语的 opus 包预喂进
  本轮 `frame_queue`(`[filler] queued N packets`):先播垫话,LLM 音频
  无缝接上。没有第二条播放路径;barge-in / 清理语义不变。
- **TTS WS**:优先用端点时预开的连接;没有或已失败就开新的。两种情况下
  握手都和 LLM 流**并发**(`tts_open_task`,首次 push_text 前 await ——
  别把它重新串行化)。

**Producer task `drain_tts`**:异步迭代 `tts.packets()`(OGG 字节块),
demux 后把 opus 包推帧队列(满了 sleep 5ms);`client_abort` 提前退出。
末尾**可靠地**塞 `None` sentinel(最多 250 次重试 / 5s)— 没有它
audio source 永远看不到 EOF,`play_done` 不触发。若一字节没收到且 TTS
记录了服务端协议错误,会显式打"user heard silence"错误。

**Playback**:`StreamingOpusAudioSource(frame_queue)`;停掉前一段播放;
`voice_client.play(source, after=→ play_done.set)`;
`session.is_audible = True`(切 Layer 4 ack-word 过滤的开关)。

**LLM 流循环**,max 4 轮:
- 第 0 轮有附着的投机流就用它,否则
  `bot.llm.stream_chat(system, messages, tools)`。per-event 20s 超时。
- `TextDelta` → 累积 + `SentenceChunker`。每完整一句先过 `speakable()`
  护栏(Layer 6),再 `tts.push_text` + `tts.flush`(按 UTF-8 字节计费)。
- `ToolUseStart/InputDelta/End` → 累积 tool_use 块。
- `MessageEnd` → 记 stop_reason,**跨轮累加 token usage**。
- `stop_reason == "tool_use"` → 执行工具,append tool_result,再 stream。
  否则 flush chunker 余量,`tts.end_turn()`,break。

**Playback 等待**,30s deadline,每 250ms 轮询:
`play_done | client_abort | drain 结束 + 8s 宽限 | deadline`。裸
`wait_for(play_done)` 在 barge-in 时不会解除阻塞;这个循环在 Fish 不回
`finish` 时也能恢复(见 Layer 7)。

**Cleanup — 单一 `finally` 块,任何退出路径都执行**(含异常和
CancelledError):
- settle `tts_open_task`,abort 附着的投机流(幂等),取消 `drain_task`,
  `tts.close()`,`session.is_audible = False`。
- **成本记录**:LLM token usage + TTS UTF-8 字节 →
  `bot.cost_tracker.record(...)`。每条付费路径都必须上报,否则 quota
  guard 看不见。
- 打 `[latency]` 行:阶段差
  `endpoint→asr_done→consumer_start→llm_first_delta→first_audio→...`。
- 历史:正常完成时先从 user message 剥掉 `recent_room_chat` 再提交
  assistant 回复;否则回滚 user message(被打断的轮不污染历史)。
- state 回 IDLE(compare-and-set — 中途 `/sleep` 的 SLEEPING 不被覆盖);
  时间戳、`last_bot_reply`、对话窗口名单仅在正常完成时更新。

try 里加新的提前退出没问题;新占用的资源必须在那个 finally 里释放。
`is_audible` 卡在 True 会让 ACK 过滤吃掉这个 guild 里所有短话语。

---

## Layer 6 — 句子切分

`utils/sentence_chunker.py` — `SentenceChunker.feed(delta) -> list[str]`。

两个标点集:
- `FIRST_PUNCT = {"。", "!", "?", ",", ";", ",", "~", "、", ".", "\n"}` — 宽松
- `PUNCT = {"。", "!", "?", ";", ".", "\n"}` — 严格

首句另有 **16 字上限**(`FIRST_MAX_CHARS`):16 字内没等到标点也强制切
— 上限必须赢,不然恰好在"首句最长"的场景(大 delta + 标点很远)失效,
低 TTFT 的目的就没了。代价:可能切在词中间留合成接缝;调大常量即可回退。
`_is_first` 在首句吐出后翻转;后续句保持完整,韵律自然。`flush()` 返回
并清空未结束的余量。

### `speakable()` 护栏

`speakable(text)` 剥掉 `[情绪标签]`、标点和空白,只有剩下真实内容才返回
True。调用方(think_speak)**绝不把不可读块推给 Fish**:空块(`\n`、
纯标签、纯标点)会让 Fish 报 "empty audio" 错误并 `finish` **整条**流
— 之后推的所有句子全部静音。

---

## Layer 7 — TTS WebSocket (Fish Audio)

### `FishConfig` — 6 个 persona TTS 调音参数

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
    # Per-persona TTS 调音
    temperature: float = 0.7    # 0-1;声音稳定度
    top_p: float = 0.7          # 0-1;采样多样性
    speed: float = 1.0          # prosody.speed 倍数
    volume_db: float = 0.0      # prosody.volume in dB
    chunk_length: int = 200     # 生成 chunk 大小
```

`providers/factory.py:make_tts(cfg, voice_id, persona)` 从 persona
frontmatter 读 `tts_temperature` / `tts_top_p` / `tts_speed` /
`tts_volume_db` / `tts_latency` / `tts_chunk_length`,塞进 `FishConfig`。

### 连接

`_open_with_voice(voice_id)`:
1. URL `{base_url}/v1/tts/live`;headers `Authorization: Bearer
   {api_key}`、`Model: {model}`。
2. `websockets.connect(...)` 带 `connect_timeout`。`OSError` /
   `asyncio.TimeoutError` 包成 `FishConnectError`。`make_tts` 重试装饰器
   3× 重试,0.5/1/2s 退避。
3. 拼 start payload:

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

4. msgpack 打包发送;起 `_read_loop()` task。

正常流程里这个 open 发生在**入队时**(Layer 4 预开),握手和调度间隙、
LLM 流重叠。

### 收发

| 方向 | 方法 | 内容 |
|---|---|---|
| → | `push_text(s)` | `{"event": "text", "text": s}` |
| → | `flush()` | `{"event": "flush"}` |
| → | `end_turn()` | `{"event": "stop"}` |
| ← | `_read_loop` | `{"event": "audio", "audio": bytes}` → 队列 |
| ← | | `{"event": "finish", "reason": "stop"}` → put None,return |

`packets()` 是调用方 drain 的异步迭代器。None sentinel 来了就结束。
服务端协议错误(如 reference_id 无效、空音频输入)会以
`finish`(`reason != "stop"`)的形式到达,存进 `last_error` 供
drain_tts 诊断。

**失败模式**:Fish Audio 偶尔接受了 turn 但永远不回 `finish`。
`_read_loop` 卡住 → packets() 不结束 → `drain_tts` 不结束 →
`play_done` 不触发。`respond_to_user` 里的 30 秒 deadline +
`bytes_received` log 是兜底保险。别加大 deadline。

---

## Layer 8 — OGG demux + 帧队列

### `OggDemuxer` — `audio/ogg_demux.py`

RFC 3533 Ogg page parser。每个 page header 27 字节(`OggS` magic 在
offset 0,segment count 在 offset 26),后接 N 字节 segment table,然后
body。一个 packet 是连续 segment 序列,以长度 < 255 的 segment 收尾。

- `feed(data)` (L26) — 扩 buffer,调 `_parse_pages()`。
- `_parse_pages()` — 同步到 `OggS` magic;整页齐了就把 segment 拍进
  `_packet_carry`;遇到 < 255 字节 segment 就发射 packet。
- `_emit_packet` 跳过头两次发射(OpusHead、OpusTags 元数据)。
- `packets()` (L32) — 待出 audio packet deque 的 generator。
- `flush()` (L36) — 最后再 parse 一次 + drain。

### `StreamingOpusAudioSource` — `audio/audio_source.py`

`discord.py` 在 player 线程里每 20ms 调 `read()` (L35) 一次。返回什么
字节就按 `is_opus() = True` 直接送 UDP。

```python
SILENCE_OPUS = b"\xf8\xff\xfe"   # L20 — 3 字节 Celt 20ms mono 静音

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

15ms 超时短于 discord 的 20ms 节拍,避免欠流。返回 `b""` 触发 discord 的
`after` 回调 → set `play_done`。filler 垫话包(Layer 5)排在同一个队列
头部,所以它先于 LLM 首段音频播出,没有独立路径。

---

## Layer 9 — Discord 播放

discord.py 内部。`voice_client.play(source, after=callback)` 起 player
线程,每 20ms 调 `source.read()`,从 voice UDP socket 送出去,`b""` 时从
player 线程调 `after(error=None)`。我们用 `bot.loop.call_soon_threadsafe`
把 `play_done.set()` 调度回主 loop。

这一层是黑盒。`play_done` 不触发,几乎肯定是 Layer 8(队列没收到 None
sentinel)或 Layer 7(Fish Audio 没发 `finish`)的问题。

---

## 旁路模块

不在数据流上,但能丢掉或改向包。深挖某层之前先看看这些。

- **白名单** — `config.bot.listen_only_users` + `/admin whitelist`。
  Layer 1 入口处过滤,每个被跳过的用户各 log 一次。非空 = bot **无视**
  其他所有人 — "bot 听不见某人"先查这里。
- **副 owner** — `bot.extra_owner_ids` + `/admin owner`。不影响
  pipeline,只授权额外用户能跑 owner-only 命令。
- **i18n** — `src/echotwin/i18n/`。包 slash 命令 UI 文本,别的不动。
- **`voice_recv_patch.py`** — 见 Layer 0。

---

## 端到端 log 流

正常工作时,每轮你会看到这些行:

```
[utt] user U START (first real packet)
[watchdog] user U: no real audio in 0.6Xs — finalizing utterance
[asr] speculation HIT for user_name              # 批式 ASR + speculative_asr
[ASR/watchdog] user_name(uid): text  emotion=NEUTRAL
[organic] user_name: verdict=accept score=99 signals=['wake_word'] text='...'
[arbiter] accept (回答她的提问) 312ms for '...'   # 仅灰区轮
[spec-llm] speculative stream opened for '...'   # speculative_llm 开着时
[spec-llm] speculation MATCHED — attaching to turn
[consumer] dequeued: user_name: '...'
[merge] folding N queued utterance(s) into this turn: ...   # 有积压时
[respond] start: user=U text='...'
[filler] queued N packets from _filler_xxx.ogg   # 慢轮垫话
[respond] starting LLM stream
[respond] first TTS audio chunk received (bytes=N)
[respond] LLM done, total_chars=N text='...'
[respond] drain_tts done, total bytes_received=N
[latency] endpoint→asr_done=Xms asr_done→consumer_start=Xms ... total=Xms
[emotion-sidecar] uid=U emotion=HAPPY took=Xms   # 流式 ASR 模式
[heartbeat] uptime=Xs guilds=N sessions=M cost_today=$X
```

在频道里时每 5s:
```
[stats] guild G: dave_epoch=N reader=alive mls_users=[...] u<id> att/ok/fail/pass=...
[stats] user U: real_ok=Δ fail=Δ (last 5s)  in_speech=bool  last_real=Xs ago  is_audible=bool  state=...
```

另有每 100 个解码帧一条 `[amp] frames=N rms=X peak=Y` — 确认真实音频
能量有没有到服务器,看这条最快。

---

## Debug 顺序

症状:bot 不响应。一层层往下走,在第一条没出现的 log 那里停下:

| 没看到的 log | 怀疑层 | 常见原因 |
|---|---|---|
| `[stats] real_ok=0` | 0/1 | Discord 没送(att 不涨);白名单挡了;DAVE 解密失败(fail 在涨);reader=DEAD(自动重启应触发) |
| `[utt] user U START` | 1 | 包全是 sentinel;白名单;SLEEPING 状态 |
| `[amp] rms≈0` | 物理层 | **外放(扬声器)用户在 bot 播音期间被自己客户端的回声消除压麦 — 服务器收到的 RMS 接近 0。客户端物理问题,戴耳机解决** |
| `[ASR/...] text` | 3 | sherpa/SenseVoice 模型加载失败;音频 <100ms |
| `[ASR/...] dropping pure-punct` / `dropping {N}ms utterance` | 4(过滤) | 在按设计工作 — 短于 600ms 或无内容 |
| `[ASR/...] treating ack-word` | 4(过滤) | 在按设计工作 — bot 在说话期间你说了"嗯" |
| `[organic] verdict=reject` | 4(判定) | 故意旁听;看 signals 列表 / `[arbiter]` 的 reason |
| `[arbiter]` 很慢或 `timeout` | 4(第二层) | Groq 挂了 / 缺 key → 启发式兜底(signals 里有 `arbiter_fallback`) |
| `[addressee] dropping non-addressed` | 4(legacy) | organic 关闭 + continuation_window=0 → 必须显式喊唤醒词 |
| `[respond] start` | 4/5(队列) | utterance 没入队,或 consumer 丢了(SLEEPING / 没有 voice client) |
| `[spec-llm] wasted` 频繁出现 | 5 | partial 不稳定或灰区流量大 — 付费流在白烧;考虑关 speculative_llm |
| `[respond] LLM done, total_chars=0` | 5 | LLM 返回空 — 检查 Anthropic API key |
| `[respond] first TTS audio chunk received` | 7 | Fish 没发音频 — 检查 API key、voice_id、model=s2-pro;另看 `TTS produced NO audio` + last_error |
| `[respond] drain_tts done` | 7 | Fish 没发 `finish` — 30s deadline 兜底 |
| `[respond] play_done` 等待超时 | 8/9 | None sentinel 没到队列,或 discord.py player 线程崩了 |

**速度**问题(回但是慢)看每轮的 `[latency]` 行:阶段差
(`endpoint→asr_done→consumer_start→llm_first_delta→first_audio`)直接
指向慢的那层。`[merge]` 行解释"它一次回了三个人";`llm_first_delta≈0`
说明投机流接上了。

Barge-in 后语音连接静默断:Layer 0 patch 3 那种情况。应该已经修了;再
出现就 `/leave` + `/join` 恢复,然后回看 `audio/voice_recv_patch.py`。
