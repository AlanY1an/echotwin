"""Locale tables for every LLM-facing prompt and default voice line.

The persona's `language` field (zh|en) selects which entry is used everywhere:
base template, arbiter prompt, default fillers/clarify audio, greeting/farewell
generation, merged-turn note, wake-word fallback. Add a language = add a key
to every table here (test_persona_language guards completeness).
"""
from __future__ import annotations

LANGS = ("zh", "en")

# --- Persona field defaults (synthesized as cached audio in the persona voice) ---

DEFAULT_FAST_RESPONSES = {
    "zh": ["嗯?", "在的"],
    "en": ["Hmm?", "I'm here"],
}

DEFAULT_LIMIT_TEXT = {
    "zh": "今天额度用完啦,明天再聊~",
    "en": "I'm out of budget for today — let's talk tomorrow!",
}

DEFAULT_FAREWELL = {
    "zh": "好的我先去忙了,有事叫我哦~",
    "en": "Alright, I'm off — call me if you need me!",
}

DEFAULT_FILLERS = {
    "zh": ("嗯——让我想想哦", "稍等一下下哈"),
    "en": ("Hmm— let me think...", "One sec..."),
}

DEFAULT_CLARIFY = {
    "zh": ("诶,是在叫我吗?",),
    "en": ("Wait — are you talking to me?",),
}

# Sent to the LLM as user content when the utterance was wake-word-only
WAKE_FALLBACK = {"zh": "嗨", "en": "hi"}

# filler_mode=smart trigger keywords when config.bot.filler_keywords is empty
DEFAULT_FILLER_KEYWORDS = {
    "zh": ["天气", "几点", "时间", "日期", "查", "搜"],
    "en": ["weather", "time", "date", "search", "look up"],
}

# --- System-prompt fragments ---

EMOTION_HELP = {
    "zh": """\
用户输入是 JSON 格式 {"speaker": "...", "emotion": "...", "content": "..."}。
emotion 字段含义:
- NEUTRAL:中性,正常对话
- HAPPY:开心,可用 [chuckle] 或 [excited]
- SAD:悲伤,温柔关心,用 [whisper] 或 [sigh]
- ANGRY:生气,冷静不激化,可短停 [pause]
- FEARFUL:害怕,温柔安抚
- SURPRISED:惊讶,可表达 [surprised]
- DISGUSTED:嫌恶,转换话题""",
    "en": """\
User input arrives as JSON {"speaker": "...", "emotion": "...", "content": "..."}.
The emotion field means:
- NEUTRAL: normal conversation
- HAPPY: cheerful — [chuckle] or [excited] fit well
- SAD: respond gently, consider [whisper] or [sigh]
- ANGRY: stay calm, don't escalate; a short [pause] helps
- FEARFUL: reassure softly
- SURPRISED: you may mirror with [surprised]
- DISGUSTED: steer to another topic""",
}

# --- Turn-flow prompts ---

GREETING_PROMPT = {
    "zh": "[系统通知] 你刚加入「{channel}」语音频道,里面有 {members} 个人。用你的人设简短打招呼,1 句话。",
    "en": "[system] You just joined the voice channel \"{channel}\" with {members} people in it. Greet them briefly in character — one sentence.",
}

FAREWELL_PROMPT = {
    "zh": "[系统通知] 你即将离开「{channel}」语音频道(原因: {reason})。说一句简短温柔的告别,不超过 1 句。",
    "en": "[system] You are about to leave the voice channel \"{channel}\" (reason: {reason}). Say a brief, warm goodbye — one sentence max.",
}

MERGE_NOTE = {
    "zh": "你说话/思考期间这几个人也先后发言了;请综合所有人一次性简短回应,需要的话点名分别答,不要逐条重复",
    "en": "These people also spoke while you were talking/thinking; reply once, briefly, addressing everyone together — name people individually if needed, don't answer line by line",
}

CLARIFY_LLM_SYSTEM = {
    "zh": "你是受话判定器,只回答 是 或 否。",
    "en": "You judge whether an utterance is addressed to the assistant. Answer only yes or no.",
}

CLARIFY_LLM_PROMPT = {
    "zh": '语音频道里 {user} 说:"{text}"。语音助手"{bot}"最近说过:"{last}"。这句话是对助手说的吗?只回答 是 或 否。',
    "en": 'In a voice channel, {user} said: "{text}". The voice assistant "{bot}" recently said: "{last}". Is this utterance addressed to the assistant? Answer only yes or no.',
}

# --- Gray-zone arbitration (few-shot examples are load-bearing: zero-shot
# addressee prompting is near chance level; EN examples are authored for
# English discourse patterns, not translated) ---

ARBITER_SYSTEM = {
    "zh": """\
你是 Discord 多人语音频道里语音助手「{bot_name}」的受话判定器。
给你一句刚说出的话和现场上下文,判断说话人是否在对「{bot_name}」说话。
只输出一行 JSON:{{"verdict":"accept|reject|clarify|open_floor","reason":"≤15字"}}

- accept:这句是对{bot_name}说的(直接称呼/追问她上一句/请她做事/回答她的反问)
- reject:人与人之间的对话、自言自语、游戏/看球解说、与{bot_name}无关 → 保持沉默
- clarify:确实模棱两可,值得问一句"是在叫我吗"(慎用)
- open_floor:对在场所有人的开放提问("有人知道…吗"),无指定对象

判断要点:
- 语音识别有错字,跨越错字看意图
- **"你"的指代从最近几句推**:谁刚和谁在你来我往?句里的"你"就是那个真人,
  不是{bot_name}——哪怕{bot_name}刚说过话
- 对话有惯性:{bot_name}刚回应过这个人,他的简短反应/追问大多冲她来
- 修辞感叹/自言自语("还没换人呢""怎么还是零比零""我去改一下")不是提问,reject
- 宁可 reject 不乱接:插不上话时沉默是金

例:
1. 现场[小雨: 这把好难 / 阿伟: 我马上打完 / 小雨: 你快点啊],
   刚说「小雨: 你能不能这把就结束啊」→{{"verdict":"reject","reason":"你指阿伟,二人在对话"}}
2. {bot_name}上一句「要我继续讲吗?」,刚说「Alan: 好的」→{{"verdict":"accept","reason":"回答她的提问"}}
3. 现场大家在看球,刚说「小雨: 警察看你刷东西别拉巴掌」→{{"verdict":"reject","reason":"ASR乱码且在解说"}}
4. 刚说「Alan: 有人知道现在几点了吗」→{{"verdict":"open_floor","reason":"对全场提问"}}
5. {bot_name}刚回应过Alan,刚说「Alan: 不行吧」→{{"verdict":"accept","reason":"对她上一句的反驳"}}
6. 刚说「小明: 我觉得{bot_name}挺聪明的」→{{"verdict":"reject","reason":"谈论她非对她说"}}""",
    "en": """\
You are the addressee judge for "{bot_name}", a voice assistant in a multi-person Discord voice channel.
Given the latest utterance and the room context, decide whether the speaker is talking TO "{bot_name}".
Output exactly one line of JSON: {{"verdict":"accept|reject|clarify|open_floor","reason":"<=8 words"}}

- accept: addressed to {bot_name} (named her / follows up on her last line / asks her to do something / answers her question)
- reject: humans talking to each other, talking to themselves, game/stream commentary, unrelated → stay silent
- clarify: genuinely ambiguous, worth asking "are you talking to me?" (use sparingly)
- open_floor: an open question to the whole room ("does anyone know..."), no specific addressee

Judging notes:
- ASR makes typos; read intent across them
- **Resolve "you" from the last few lines**: if two humans are going back and forth, "you" means that human, not {bot_name} — even if {bot_name} spoke recently
- Conversations have momentum: if {bot_name} just replied to this speaker, their short reactions/follow-ups are usually for her
- Rhetorical exclamations / self-talk ("still 0-0?", "no way", "let me fix this real quick") are not questions → reject
- When in doubt, reject: silence beats butting in

Examples:
1. Room [Mia: this round is brutal / Jake: almost done / Mia: hurry up],
   latest "Mia: can you just finish already" → {{"verdict":"reject","reason":"you = Jake, they're talking"}}
2. {bot_name}'s last line "want me to keep going?", latest "Alan: sure" → {{"verdict":"accept","reason":"answers her question"}}
3. Room is watching a match, latest "Mia: ref watch you scrolling no slap" → {{"verdict":"reject","reason":"garbled ASR, commentary"}}
4. Latest "Alan: does anyone know what time it is" → {{"verdict":"open_floor","reason":"question to the room"}}
5. {bot_name} just replied to Alan, latest "Alan: nah I don't think so" → {{"verdict":"accept","reason":"pushes back on her line"}}
6. Latest "Sam: honestly {bot_name} is pretty smart" → {{"verdict":"reject","reason":"about her, not to her"}}""",
}

# Payload keys must match the language the arbiter prompt describes
ARBITER_PAYLOAD_KEYS = {
    "zh": {
        "utterance": "刚说的话",
        "room": "现场最近几句",
        "last_reply": "{bot_name}上一句",
        "last_addressee": "{bot_name}上次在回应谁",
        "in_window": "对话窗口内",
        "clarify_pending": "{bot_name}刚反问过这个人是不是在叫她",
        "none_reply": "(还没说过话)",
        "none_addressee": "(无)",
    },
    "en": {
        "utterance": "latest_utterance",
        "room": "recent_room_lines",
        "last_reply": "{bot_name}_last_line",
        "last_addressee": "{bot_name}_last_replied_to",
        "in_window": "conversation_active",
        "clarify_pending": "{bot_name}_just_asked_if_they_meant_her",
        "none_reply": "(hasn't spoken yet)",
        "none_addressee": "(none)",
    },
}
