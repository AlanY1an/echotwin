[English](SETUP.md) | **简体中文**

# 部署指南 — 从零到 bot 开口说话

把 bot 接进你的 Discord 服务器并开始对话所需的全部步骤。如果 API 账号都已就绪,大约 15 分钟。

## 前置条件

- **macOS (arm64) 或 Linux**,Python 3.11+
- **libopus** — `brew install opus`(macOS)/ `apt install libopus0`(Debian/Ubuntu)
- 一个你有管理权的 Discord 服务器
- API key(见第 3 步):Fish Audio(TTS)、Anthropic(LLM),可选 Groq(多人仲裁)

## 1. 创建 Discord 应用

1. 打开 [Discord 开发者后台](https://discord.com/developers/applications)→ **New Application** → 起名。
2. **Bot** 页:
   - 点 **Reset Token** 并复制——这就是 `DISCORD_TOKEN`。
   - 在 **Privileged Gateway Intents** 里开启 **Server Members Intent**
     (bot 需要语音频道成员列表和显示名)。
3. **Installation** 页(或 OAuth2 → URL Generator):
   - Scopes:`bot`、`applications.commands`
   - Bot 权限:**View Channels、Send Messages、Connect、Speak**
   - 浏览器打开生成的 URL,把 bot 邀进你的服务器。

## 2. 在 Fish Audio 克隆声音

1. 注册 [fish.audio](https://fish.audio),创建 API key(`FISH_AUDIO_API_KEY`)。
2. 上传声音样本创建声音模型——模型 ID 就是 persona 的 `voice_id`(fish.audio 上的公开模型 ID 也可以)。只克隆你有授权使用的声音。

## 3. API key → `.env`

```bash
cp .env.example .env
```

| 变量 | 必填 | 哪里拿 |
|---|---|---|
| `DISCORD_TOKEN` | 是 | 第 1 步 |
| `FISH_AUDIO_API_KEY` | 是 | fish.audio 后台 |
| `ANTHROPIC_API_KEY` | 是 | [console.anthropic.com](https://console.anthropic.com) |
| `GROQ_API_KEY` | 否 | [console.groq.com](https://console.groq.com) — 多人受话快速仲裁(~350ms);不填则回落到(较慢的)对话 LLM |
| `TEST_GUILD_ID` | 开发用 | 你的服务器 ID——slash 命令即时同步(否则全局同步要 ~1 小时) |

## 4. 安装 & 下载模型

```bash
python3.11 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
bash scripts/download_models.sh   # Silero VAD (~2MB) + SenseVoiceSmall (~234MB)
                                  # + 中英两个流式 ASR 模型(共 ~200MB)
```

## 5. 配置

```bash
cp config.example.yaml config.yaml
```

仓库自带两个现成 persona:`ariana_en`(英文,默认,开箱即用)和 `ouyang_zh`(中文——打开文件把 `voice_id` 换成第 2 步拿到的模型 ID)。想自建就复制 `prompts/personas/_template.zh.md`(英文模板用 `_template.md`),填 `name`、`voice_id`、`language`(zh|en,决定全部 LLM prompt、默认语音文案,**以及流式 ASR 模型**——两个模型下载脚本已预取,切换零等待)、唤醒词和人设 prompt(文件正文就是 system prompt)。然后在 `config.yaml` 里设 `bot.active_persona: my_persona`。

默认配置开箱即用:流式 ASR、organic 多人模式开启、每日预算 $5 封顶。

## 6. 运行

```bash
.venv/bin/python -m echotwin
```

在 Discord 里:进语音频道,输入 `/join`,开口说话。和 bot 单独相处时说什么都有回应;多人时叫一次它的名字,然后正常聊——受话判定管剩下的。

首跑核对清单:

- 日志里有 `Bot logged in as …` 和 `Slash commands synced`
- `/join` 后 bot 出现在你的语音频道
- 说话产生 `[ASR/watchdog] 你(…): <文本>` 日志行
- 克隆声回复在 ~1 秒内播出

## 7. 接下来

- `/say 你好` — 跳过 LLM 的 TTS 冒烟测试
- `/admin cost`(仅 owner,私聊或频道均可)— 查看花费
- `kill -HUP <pid>` — 热加载 config.yaml + persona 修改,无需重启
- 唤醒词快速路径:`.venv/bin/python -m scripts.synthesize_fast_responses`预合成秒回音频
- 分层管线参考和调试指南:[`PIPELINE.zh.md`](PIPELINE.zh.md)

## 部署排障

| 症状 | 解法 |
|---|---|
| 启动报 `PrivilegedIntentsRequired` | 去开发者后台开 **Server Members Intent**(第 1.2 步) |
| slash 命令不出现 | 全局同步最长 1 小时——`.env` 里设 `TEST_GUILD_ID` 即时同步 |
| bot 进频道但不出声 | 查 `FISH_AUDIO_API_KEY` 和 persona 的 `voice_id`;日志里搜 `Fish` |
| bot 听不到任何人 | 必须用 `/join` 进频道(不能拖进去);看 `[stats]` 日志行 |
| 日志刷 `corrupted stream` | 会话开始时少量属正常(Discord E2EE 握手);持续出现见 PIPELINE.zh.md |
| 打断不了 bot | 戴耳机——外放时你自己客户端的回声消除会在 bot 播音期间压掉你的麦克风 |
