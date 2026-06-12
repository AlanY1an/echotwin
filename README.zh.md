[English](README.md) | **简体中文**

# EchoTwin

*用克隆声和你语聊的 AI Discord 语音 bot——给一个声音造一个回声双生。*

Discord 全双工实时语音 bot。Fish Audio 克隆声音 TTS + Claude Haiku 4.5 LLM(支持 tool calling)+ 本地流式 ASR(sherpa-onnx zipformer,边说边出 partial)+ Silero VAD。

**当前最稳的体验:一对一语音对话。** 进频道直接说话,克隆声回复稳定亚秒级(真机实测 ~400-1100ms 出声)——投机执行(ASR/LLM)、TTS 连接预开、垫话缓存共同发力。支持打断、工具调用(时间/日期/天气)、人格热切换、逐轮记账 + 预算封顶。

**实验性:organic 多人模式。** 群聊频道里由三层受话判定决定每句话是不是对 bot 说的:查表反射秒判明显情况,歧义句交给快速 LLM 仲裁(Groq qwen3-32b,~350ms,读着房间最近转写判),启发式规则集兜底。被拒绝的闲聊进滚动旁听转写,让接住的回复有语境;开放提问先让真人接;播音期间积压的发言合并成一轮。默认开启、可正常使用,但仍在积极开发中——偶尔会在"该不该说话"上判断失误。

> 新手?**[`docs/SETUP.zh.md`](docs/SETUP.zh.md)** 从零(Discord 应用、克隆声、API key)到 bot 开口说话,约 15 分钟。分层 pipeline + debug 指南:[`docs/PIPELINE.zh.md`](docs/PIPELINE.zh.md)

## Quick start (Mac dev)

```bash
# 1. 克隆 & 安装
git clone https://github.com/AlanY1an/echotwin.git
cd echotwin
python3.11 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

# 2. 下载本地模型(VAD + SenseVoice + 中英流式 ASR,共 ~440MB)
bash scripts/download_models.sh

# 3. 配置
cp .env.example .env       # 填 DISCORD_TOKEN / FISH_AUDIO_API_KEY / ANTHROPIC_API_KEY
                           # 可选:GROQ_API_KEY(多人灰区仲裁;不填自动复用对话 Haiku)
cp config.example.yaml config.yaml   # 默认人格、voice ID、阈值都已预填

# 4. 跑
python -m echotwin
```

加速命令同步(开发期):

```bash
TEST_GUILD_ID=<你的测试 guild id> python -m echotwin
```

## Discord 命令

所有 slash 命令的描述都已国际化 — Discord 客户端按用户语言自动显示(目前支持英文 / 简体中文,繁体中文回退到简体,其他语言回退到英文)。要加新语言只改 `src/echotwin/i18n/strings.py` 即可。

### 公开(频道里 slash)

| 命令 | 作用 |
|---|---|
| `/join` | bot 加入你当前的语音频道 |
| `/leave` | bot 离开(带告别) |
| `/say <text>` | 让 bot 用克隆声念一段(调试) |
| `/sleep` | bot 留频道但安静(`/wake` 唤醒) |
| `/wake` | 解除 sleep |
| `/persona current\|list` | 看当前/所有人格 |

### Owner only(私聊或频道里都能用——回复只有你可见)

| 命令 | 作用 |
|---|---|
| `/persona-admin use <name>` | 切换人格(清历史 + 重建唤醒词、addressee 检测、快速回应缓存) |
| `/persona-admin reload` | 重读 prompt 文件(不重启) |
| `/voice-admin set <id>` | 切换 Fish Audio voice ID |
| `/voice-admin show` | 看当前 effective voice ID |
| `/admin cost` | 看本月/今日花费 |
| `/admin health` | 看 bot 内部状态 |
| `/admin wakeword on\|off` | 开关 wake word 模式 |
| `/admin reload-config` | 热加载 `config.yaml` + persona 文件 |
| `/admin restart` | 软重启所有 session |
| `/admin whitelist add\|remove\|list\|clear <user>` | 限制 bot 只听某些用户(跨服务器) |
| `/admin owner add\|remove\|list <user>` | 副 owner 管理(仅主 owner 能操作 — 副 owner 可以用其他所有 admin 命令,但不能管 owner 列表) |

### 语音对话

`/join` 后直接开口说话,bot 自动 VAD → 流式 ASR → 受话判定 → LLM(可调工具)→ 用克隆声回。

- **Organic 多人模式**(`bot.organic.enabled`,默认开):对话进行中不需要唤醒词。叫名字(句首尾)或二人世界永远秒接;歧义句由 LLM 仲裁读着房间最近几句转写来判;被拒绝的话进旁听上下文而不是丢弃。对全场的开放提问("有人知道…吗")先等 ~1.5 秒,没真人接才自荐回答。bot 播音期间排队的发言会合并成一轮统一回应。
- **多人安全**:Discord 自带分轨;per-user VAD/ASR;共享队列串行回复。
- **打断**:bot 在回你时,你再开口 → bot 闭嘴(默认 `addressee_only`,只有当前受话人能打断)。注意:**外放扬声器的人通常打断不了**——bot 播音时,说话人自己客户端的回声消除会把他的麦克风一起压掉(服务器收到的是静音),戴耳机即可解决。
- **附和过滤**:短附和词("嗯"、"对"、"好"、"ok"、"yeah" 等)和 600ms 以下的片段在 bot 说话期间会被忽略,普通点头不会截断回复。
- **唤醒词(可选)**:`/admin wakeword on` 后必须叫名字才回应(legacy 模式;organic 模式下不需要)。
- **白名单(可选)**:`/admin whitelist add @user` 后,bot 完全无视其他人语音,直到 clear 为止。

## 热配置

大多数设置可热加载,不需要重启:

- `kill -HUP <pid>` 或 `/admin reload-config` 重读 `config.yaml` 和当前 persona 文件
- `/voice-admin set <id>` / `/admin wakeword` / `/admin whitelist` / `/admin owner`都会写到 `data/runtime_config.json`,重启后保留
- 切 ASR provider(流式 ↔ 批式)需要重启

```yaml
# 切到自定义 Fish voice(也可在运行时通过 /voice-admin set 热切)
tts:
  fish_audio_stream:
    voice_id: <new_id>
    fallback_voice_id: <backup_id>
```

## 健康端点

启动后默认监听 `:9090`:

```
GET /healthz       → 200 ok / 503 not_ready
GET /readyz        → 200 ok / 503
GET /stats.json    → {uptime_seconds, guilds, active_sessions}
```

## 写新 persona

`prompts/personas/` 下加 `.md` 文件。Frontmatter 是 YAML,只有 `name` 和 `voice_id` 必填。`language: zh|en`(默认 `zh`)决定发给 LLM 的全部 prompt 语言和**流式 ASR 模型**(耳朵跟着嘴走:中文配双语 zipformer,英文配英文 zipformer)——底座模板、仲裁 few-shot、默认垫话/反问、问候告别都跟着切;正文就是 system prompt,用同一种语言写。Per-persona 的 Fish Audio TTS 调音参数(temperature、 speed、volume 等)都可选 — 完整字段 + 注释见 `prompts/personas/_template.md`。基础模板(`prompts/base_template.md`)会自动给每个 persona 套上语音规则、情感标签、prompt 注入防御。

切换:`/persona-admin use <id>`(仅 owner,私聊或频道均可)或 `config.yaml:bot.active_persona`。persona 是全局的——切换会影响 bot 所在的所有服务器(所以才限 owner)。切人格会自动重建唤醒词匹配器、addressee 检测器、快速回应音频缓存。

## 测试

```bash
# 全套(无需 API key)— ~320 个测试,~15 秒;live 测试自动排除
.venv/bin/pytest tests/

# 仅 live 测试(真实 Anthropic/Fish 调用,花钱)
.venv/bin/pytest tests/ -m live

# 多人受话剧本回放(离线,13 句三人对话)
.venv/bin/python -m scripts.verify_organic

# E2E 延迟基准(会调用真实 API)
.venv/bin/python -m tests.perf.bench_e2e_latency
```

受话启发式由黄金集验收(`tests/fixtures/addressee_golden.jsonl`,~70 条真机标注样例;指标:漏接 ≤10%、误接 ≤10%)。测试数据刻意保持中文——那就是 bot 工作的语言。

## 故障排查

| 症状 | 排查 |
|---|---|
| bot 上线但 `/join` 加入后无声 | Fish Audio API 配额、`voice_id` 失效?看日志 `Fish Audio` 行 |
| 用户说话 bot 不响应 | 1) 白名单是否限制了?`/admin whitelist list`。2) `/sleep` 状态?3) 看 `[ASR]` 日志确认转写 |
| bot 老被"嗯/ok"打断 | 已有附和过滤;如果还嫌敏感,调大 `bot.py:_finalize_utterance` 里的 `utt_ms` 阈值 |
| LLM 慢 | Anthropic prompt cache miss?连续对话 5 分钟内才命中 |
| 启动时模型加载失败 | `bash scripts/download_models.sh` 重新拉模型 |
| 命令没生效 | 全局 sync 慢,设 `TEST_GUILD_ID` 即时同步到测试服 |
| 打断后语音连接静默断开 | `audio/voice_recv_patch.py` 已 patch;如果再次出现,`/leave` + `/join` 可恢复 |

## 反应速度

真机 Discord 流量实测(macOS M 系列,本地 ASR,每轮 `[latency]` 日志):

| 阶段 | 典型耗时 |
|---|---|
| 端点检测 → ASR 最终文本 | 15-30 ms(流式 ASR 已边说边消化) |
| 调度/队列 | ~40 ms |
| LLM 首 token | **投机命中时 ~0 ms**(你还没说完流已预开)/ 其余 100-700 ms |
| TTS 首块音频 | 230-550 ms |
| **端到端出声** | **最快 361 ms,典型 0.6-1.1 秒** |

实现路径:流式 ASR partial 触发投机 LLM 流(话音未落回复已在生成)、入队时预开 TTS WebSocket、慢轮(工具调用)立即播缓存垫话不冷场。复现:`.venv/bin/python -m tests.perf.bench_e2e_latency` 或看每轮 `[latency]` 日志行。

## 设计要点

1. **Fish Audio 用 msgpack** 走 WebSocket(实测过 JSON 不工作)
2. **Discord 帧 = 20ms Opus 48kHz**,Silero VAD 内部 48→16 重采样
3. **Per-user VAD/ASR 实例**(Discord 自带分轨),共享 utterance queue 串行回复;播音期间积压的发言合并成一轮
4. **流式 ASR + 投机**:sherpa zipformer 边说边出 partial;稳定 partial 若会被反射层秒接,就预开 LLM 流——经常用户话音未落回复已在生成(命中时 llm_first_delta≈0)
5. **三层受话判定**:查表反射(零成本,~80% 流量)→ 灰区 LLM 仲裁(带房间上下文;few-shot 例句是必需品,零样本接近瞎猜)→ 启发式打分兜底(黄金集看住)。语义判断永远不写成正则规则
6. **旁听是临场参考不是记忆**:被拒绝的话进滚动转写,注入下一个接受轮(≤120 秒新鲜),提交历史前剥离
7. **可说性护栏**:无可合成内容的块(\n/纯情绪标签/纯标点)绝不推给 Fish——空块会让 Fish 终止整条流,后半段全静音
8. **Anthropic prompt cache**:系统 prompt + 历史末尾 assistant 都打了 cache_control,5 分钟内 TTFT < 200ms
9. **Voice fallback**:主 voice_id 失效时自动用 fallback_voice_id,DM owner 通知
10. **Cost tracking**:SQLite 记账覆盖所有付费路径(LLM 轮次、TTS 字节、仲裁调用),`/admin cost` 查看;配额守卫超预算拦截新轮
11. **错误反馈**:致命错误 DM owner,普通错误 ephemeral 给触发者,不污染频道
12. **DAVE 端到端加密**:Discord 语音强制 E2EE。`audio/dave_patch.py` 给 `discord-ext-voice-recv` 打 monkey-patch,在 libopus 之前先把 opus 解密。**不要删**。

每层细节和 debug 流程见 [`docs/PIPELINE.md`](docs/PIPELINE.md)。
