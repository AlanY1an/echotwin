[English](LATENCY.md) | **简体中文**

# EchoTwin 延迟报告

一轮语音对话的每一毫秒去了哪里:实测、分解,并把**故意的对话设计等待**和
**管线成本**分开算账。

- 测量于 2026-07-10,macOS M 系列,家用网络(真实部署条件——上机房部署
  LLM 往返还会更快)。
- 下面每个数字都可复现:`scripts/bench_latency.py` 与
  `scripts/bench_llm_models.py`。

## 太长不看

| | p50 |
|---|---|
| 首声(缓存垫话) | 闭嘴后 **~0.6 s** |
| 完整回复,仅管线 | **~1.2 s**(ASR 19 ms / LLM 971 ms / Fish TTS 174 ms / 播出 40 ms) |
| 完整回复,嘴到耳 | **~1.75 s**(加上故意的 550 ms 判停等待) |
| 生产日志最快一轮 | **361 ms**(endpoint→出声,投机命中) |

Fish Audio TTS 只占**管线的 10%**——从来不是瓶颈。两个大头是 LLM 首句
(55%)和故意的 VAD 判停等待(31%)。

## 为什么生产日志有误导性

每轮的 `[latency]` 日志行对自己测的东西是诚实的,但它的聚合值会高估管线
成本。88 轮生产数据:

| 阶段(日志命名) | p50 | p90 | 什么污染了它 |
|---|---|---|---|
| `endpoint→asr_done` | 19 ms | 30 ms | 无——纯本地计算,分布很紧(9–33 ms),可信。 |
| `asr_done→consumer_start` | 39 ms | **2144 ms** | 多人场景:bot 说话时的排队等待、受话仲裁。是设计,不是管线。 |
| `consumer_start→llm_first_delta` | 519 ms | 1088 ms | 投机命中(~0 ms)和未命中混在一起;网络波动。 |
| `llm_first_delta→first_audio` | 435 ms | 549 ms | **这不是 TTS 的数字**:TTS 从切句器拿的是完整句子,这段里悄悄混进了 LLM 继续生成首句剩余部分的时间。 |
| total | 1032 ms | 3331 ms | 以上全部。 |

日志*结构上*看不到的两件事:

1. **VAD 判停等待。** 计时从 `endpoint` 开始——但用户在这之前
   `endpoint_silence_ms`(500 ms)+ 最多一个轮询粒度(100 ms)就已经闭嘴
   了。这 ~550 ms 是用户实打实感受到、却不出现在任何日志里的延迟。
2. **首句时间 vs 首 token 时间。** TTS 等的是完整*首句*,不是首 token。
   这个差值(Haiku 上 ~280 ms)藏在 `llm_first_delta→first_audio` 里,
   被错记到 TTS 头上。

## 受控基准方法论

`scripts/bench_latency.py` 把每个网络阶段隔离出来单独计时,一次一轮——
无排队、无仲裁——完全用生产配置:

- 真实 persona 系统提示词(base template + 当前 persona 正文)
- 附带工具 schema(生产永远会带)
- prompt cache 开启(system 块 + 最后一条 assistant 消息,与生产一致)
- Fish `s2-pro`、`latency: low`、开 socket 时绑定音色、计时段之前预开
  socket(生产热路径)
- 4 条滚动历史 + 轮换的日常问题
- 每阶段 N = 12 轮,报 p50/p90;第 1 轮(prompt cache 写入)不计入
  缓存命中聚合

## 结果

阶段统计(12 轮,2026-07-10):

| 阶段 | p50 | p90 | min | max |
|---|---|---|---|---|
| `llm_ttft`(请求 → 首个增量) | 687 ms | 908 ms | 560 ms | 1031 ms |
| `llm_ttfs`(请求 → 完整首句) | 971 ms | 1052 ms | 808 ms | 1114 ms |
| `tts_open`(WS 握手 + 音色绑定) | 181 ms | 228 ms | 136 ms | 236 ms |
| `tts_ttfa`(推送首句 → 首个 Opus 包) | 174 ms | 416 ms | 130 ms | 1232 ms |

诚实的嘴到耳分解(p50,单人):

| 组成 | 耗时 | 占比 | 性质 |
|---|---|---|---|
| VAD 判停静音窗 | 550 ms | 31% | **设计**(配置:500 ms + 轮询/2) |
| 流式 ASR 收尾 | 19 ms | 1% | 管线(本地计算,取自日志) |
| LLM 生成首句 | 971 ms | 55% | 管线(实测,缓存命中) |
| Fish TTS 首音频 | 174 ms | 10% | 管线(实测,预开 WS) |
| Discord 播出 | ~40 ms | 2% | 传输(估算) |
| **合计** | **~1754 ms** | | |

交叉验证:去掉 endpoint 段的和(19 + 971 + 174 + 40 ≈ 1204 ms)与日志侧
单人轮次(p50 980 ms + 播出)吻合;生产略快是因为垫话/投机偶尔抄了近路,
且真实回复的开场句往往比基准问题引出的更短。

## 快首句模型实验

LLM 首句是管线的大头,于是我们测了让小快模型来说第一句是否可行
(`scripts/bench_llm_models.py`——同款 persona 提示词、同款历史、流式、
无工具、6 轮):

| 模型 | TTFT p50 | 首句 p50 | 首句质量 |
|---|---|---|---|
| claude-haiku-4-5(现役) | 857 ms | 1055 ms | ✅ 在人设内,会用情绪标签 |
| **qwen/qwen3-32b**(Groq,`reasoning_effort: none`) | **360 ms** | **369 ms** | ✅ 出乎意料地好:开口短促自然("哎嘿嘿,""晴天呀!"),还会打 base template 的 `[laughing]` 情绪标签 |
| llama-3.1-8b-instant(Groq) | 389 ms | 389 ms | ❌ 快但跑偏:编造梗和人设事实 |
| openai/gpt-oss-20b(Groq) | — | — | 撞免费档 TPM 限速(8000/分钟),没测完 |

**qwen3-32b 到达首句比 Haiku 早 ~680 ms,而且它已经在技术栈里**(多人
仲裁就是它)。两段式方案——快模型说第一句,Haiku 接着续写——预计嘴到耳
**~1.1 s**。上线前要解决的三个问题:

1. **衔接缝**:第一句要作为 assistant 前缀喂给 Haiku,让续写接得上语气、
   不重复。
2. **人设漂移**:qwen 偶尔加戏(有一轮自己冒出男朋友)。第一句的职责要
   限定在寒暄开场。
3. **Groq 免费档**:8000 TPM 对上每轮 ~1.2k 提示词 token,真上生产要么
   付费档,要么给快路径裁一个精简提示词。

## 极限在哪里?分三层

**第一层——只调配置(~1.55 s):**`endpoint_silence_ms` 500 → 300,
立省 200 ms;代价是带自然停顿的长句可能被切碎。这个值已经从 800 → 600 →
500 一路走下来,再降之前先过一遍 harness fixtures 回归。

**第二层——当前架构(实质 ~1.3–1.4 s,体感 ~0.4–0.6 s):**

- 把 VAD 等待*藏起来*而不是缩短:`speculative_llm` 在 ASR partial 稳定时
  就预开 LLM 流,命中时 550 ms 等待与 LLM 完全重叠 → 嘴到耳 ~1.4 s。
  日志里当前命中率只有 **8%**——全项目杠杆最大的优化点。
- 体感延迟:垫话是预合成的本地 OGG(零网络),首声 ≈ VAD 550 + ASR 19 +
  读盘 ~10 ≈ **0.6 s**(VAD 窗 300 ms 时为 0.35 s——已进入人类对话
  停顿区间)。`filler_mode: always` 可把它扩展到每一轮。
- 压不动的地板:ASR 收尾 19 ms、Fish TTS 174 ms(s2-pro 低延迟档的服务
  水位;观测最快 130 ms)、Haiku TTFT ~690 ms(家用网络下的 API 水位)。

**第三层——换范式(~0.5 s):**上面的两段式快首句,或者用原生
speech-to-speech 模型整个替换 ASR→LLM→TTS 级联。都超出当前范围。

## 复现

```bash
# 逐阶段管线基准(需要 ANTHROPIC_API_KEY + FISH_AUDIO_API_KEY)
.venv/bin/python scripts/bench_latency.py --runs 12

# 首句模型对比(另需 GROQ_API_KEY)
.venv/bin/python scripts/bench_llm_models.py --runs 6
```

两者都从 `config.yaml` 读当前 persona(`--persona` 可覆盖;音色 id 取自
persona,或设 `TEST_VOICE_ID`)。完整跑一遍的 API 花费在几美分级别
(短提示词带缓存,每轮只合成一句短语音)。真机流量对照:`tests/perf/`
与每轮 `[latency]` 日志行。
