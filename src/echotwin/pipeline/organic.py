"""Organic multi-user addressee detection — layered signal heuristics (spec: dev-docs organic-multiparty).

Pure function classify(), acceptance driven by the golden set
(tests/fixtures/addressee_golden.jsonl): missed-accept ≤10%, false-accept ≤10%,
gray zone leans accept. Zero LLM cost, microsecond-level.
Decision order: vocative → solo → clarify continuation → open-floor question
→ ACK → in-window scoring → out-of-window reject.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum


class Verdict(Enum):
    ACCEPT = "accept"
    REJECT = "reject"
    CLARIFY = "clarify"
    OPEN_FLOOR = "open_floor"
    MENTION = "mention"  # name mentioned in third person (not vocative) — low-rate pickup (phase 2)


@dataclass
class OrganicContext:
    wake_words: list[str]
    in_window: bool = False          # speaker is inside the active-conversation window
    solo: bool = False               # just the two of them (speaker + bot)
    last_bot_text: str = ""          # the bot's most recent reply
    last_speaker_was_bot: bool = False
    others_present: list[str] = field(default_factory=list)  # names of other real users
    clarify_pending: bool = False    # asked this speaker "are you talking to me?" within 10s


_PUNCT_RE = re.compile(r"[\s,。!?、:;\"\'《》【】()()「」.!?\-_~`]+")

# Question markers (sentence-form level)
_QUESTION_RE = re.compile(r"[??]|吗\b|吗$|呢$|为什么|怎么|什么|几点|星期几|来着|哪|多少|谁|是不是|有没有|要不要|会不会|真的假的")
# Open-floor crowd words (open questions addressed to "anyone")
_CROWD_RE = re.compile(r"有人|有没有人|谁知道|谁记得|谁会|哪位|大佬|来着")
# Skill imperatives (commands aimed at the bot's capabilities)
_SKILL_RE = re.compile(r"查一?下?|搜一?下?|帮我查|讲个|说个|给我讲|放首|播放|告诉我|报一?下")
# Explicit denial (after a clarifying question)
_DENIAL_RE = re.compile(r"没有|不是|没叫|跟你?没关|别多心")
# Interpersonal new-topic openers
_INTERPERSONAL_RE = re.compile(r"我跟你们说|你们猜|跟你说个事")
# Third-person talk about someone else (tested on norm_core, i.e. after stripping leading interjections)
_THIRD_PERSON_RE = re.compile(r"^他|^她|他们|她们|他刚|她刚")
# Leading interjections: strip before the ^他/^我 checks
# ("哎他实现多人对话了" was missed live)
_LEAD_INTERJ_RE = re.compile(r"^(哎+|诶+|欸+|唉+|嗯+|哦+|噢+|啊+|呀|哇|卧槽|我操|妈呀|草)+")
# backchannel / pure acknowledgments
_ACK_SET = {
    "嗯", "嗯嗯", "嗯哼", "哦", "哦哦", "噢", "啊", "诶", "欸", "呃",
    "对", "对对", "对啊", "是", "是的", "好", "好的", "好的好的", "好好好",
    "行", "行行", "可以", "ok", "okay", "哈哈", "哈哈哈", "草", "笑死",
}
_STOPCHARS = set("的了吗呢吧啊呀哦哟嘛么是不我你他她它们这那就也都还有和与跟在于很才把被")
# Filler-phrase bigrams: a collision doesn't count as topic continuity
# (both sides saying "感觉" ≠ talking about the same thing)
_STOP_BIGRAMS = {
    "感觉", "觉得", "可以", "知道", "时候", "东西", "现在", "这个", "那个",
    "就是", "真的", "什么", "怎么", "没有", "一下", "我们", "你们", "他们",
}


def _norm(text: str) -> str:
    return _PUNCT_RE.sub("", text).lower()


def _contains_wake(text: str, wake_words: list[str]) -> bool:
    norm = _norm(text)
    return any(w.lower() in norm for w in wake_words)


def _names_other(text: str, others: list[str]) -> bool:
    norm = _norm(text)
    return any(name and name.lower() in norm for name in others)


def _content_overlap(a: str, b: str) -> int:
    """Topic overlap with the bot's last reply: consecutive-bigram overlap → 2
    (strong continuity, e.g. "睡针"); no bigram but ≥3 scattered chars → 1 (weak).
    Scattered hits on common chars (感觉/可以/这) don't count as strong
    continuity — live round 2 on 2026-06-11: sports commentary with overlap 3-5
    caused chained false accepts."""
    if not a or not b:
        return 0

    def _bigrams(s: str) -> set[str]:
        return {
            s[i : i + 2]
            for i in range(len(s) - 1)
            if not s[i].isascii()
            and not s[i + 1].isascii()
            and not (s[i] in _STOPCHARS and s[i + 1] in _STOPCHARS)
            and s[i : i + 2] not in _STOP_BIGRAMS
        }

    if _bigrams(_norm(a)) & _bigrams(_norm(b)):
        return 2
    sa = {c for c in a if c not in _STOPCHARS and not c.isascii()}
    sb = {c for c in b if c not in _STOPCHARS and not c.isascii()}
    return 1 if len(sa & sb) >= 3 else 0


def hard_verdict(
    utterance: str, ctx: OrganicContext
) -> tuple[Verdict, int, list[str]] | None:
    """First-layer instant verdict: only 3 pure table-lookup rules remain;
    returning None = gray zone (handed to LLM arbitration).

    Design principle (two live rounds on 2026-06-11 + deep-research conclusion):
    rules only do "table lookups and counting", zero semantic pre-judgment —
    even the ACK word list was pulled (she asks "要我继续吗?", the other
    person answers "好的"; whether to pick that up depends on context).
    "Relevance" judgment belongs 100% to the LLM.
    The three kept rules all have hard reasons: ① and ② are accept paths that
    must be zero-latency (fast audio cache + speculative pre-opened stream);
    ③ is ASR debris — the LLM would just be guessing too, so it's pure cost saving.
    """
    text = utterance.strip()
    norm = _norm(text)

    # ① Wake word at sentence start/end = vocative, instant accept; name mid-sentence → gray zone
    matched = next((w for w in ctx.wake_words if w.lower() in norm), None)
    if matched is not None:
        w = matched.lower()
        if norm.startswith(w) or norm.endswith(w):
            return (Verdict.ACCEPT, 99, ["wake_word"])
        return None

    # ② Just the two of them, instant accept
    if ctx.solo:
        return (Verdict.ACCEPT, 99, ["solo"])

    # ③ Instant-reject ≤3-char debris — but only the "meaningless" kind: short
    # words in _ACK_SET ("好的"/"哈哈") are meaningful backchannel whose pickup
    # depends on context, so they go to the gray zone; same for questions,
    # particle-ending utterances, and the clarify-continuation period. The rest
    # ("你帮你"/"然"/"喽") is ASR debris — the LLM would just be guessing too.
    if (
        not ctx.clarify_pending
        and len(norm) <= 3
        and norm not in _ACK_SET
        and not _QUESTION_RE.search(text)
        and norm[-1:] not in "吧呀啊哦呗嘛哟诶"
    ):
        return (Verdict.REJECT, 0, ["fragment"])

    return None  # everything else → LLM arbitration


def classify(utterance: str, ctx: OrganicContext) -> tuple[Verdict, int, list[str]]:
    text = utterance.strip()
    norm = _norm(text)
    signals: list[str] = []

    names_other = _names_other(text, ctx.others_present)
    is_question = bool(_QUESTION_RE.search(text))

    # 1. Name present: distinguish vocative (always accept) from third-person
    # mention (MENTION, low-rate pickup). Vocative shapes: name at sentence
    # start/end, or sentence contains second person / question / skill
    # imperative; everything else (mid-sentence name + third-person predicate) = mention.
    matched_wake = next(
        (w for w in ctx.wake_words if w.lower() in norm), None
    )
    if matched_wake is not None:
        w = matched_wake.lower()
        at_edge = norm.startswith(w) or norm.endswith(w)
        if at_edge or "你" in text or is_question or _SKILL_RE.search(text):
            return (Verdict.ACCEPT, 99, ["wake_word"])
        return (Verdict.MENTION, 0, ["mention"])

    # 2. Just the two of them
    if ctx.solo:
        return (Verdict.ACCEPT, 99, ["solo"])

    # 3. Clarify continuation: we just asked "are you talking to me?".
    # Confirmation must be responsive — short sentence, contains "你", or
    # starts with an affirmative; a long continued narrative means they
    # ignored you (live 2026-06-11: sports commentary got picked up as a
    # confirmation, becoming a perpetual chained-reply machine), so fall
    # back to regular scoring.
    if ctx.clarify_pending:
        if _DENIAL_RE.search(text) or names_other:
            return (Verdict.REJECT, -9, ["clarify_denied"])
        if (
            "你" in text
            or len(norm) <= 10
            or re.match(r"^(对|是|嗯|没错|有|要|在)", norm)
        ):
            return (Verdict.ACCEPT, 9, ["clarify_confirmed"])
        signals.append("clarify_unresponsive")

    # 4. Open-floor question (can happen in or out of the window; naming a real user doesn't count)
    if is_question and not names_other and _CROWD_RE.search(text):
        return (Verdict.OPEN_FLOOR, 0, ["open_floor_crowd"])
    # Self-directed questions must be short + genuinely interrogative in form:
    # a long exclamation with an embedded "什么/怎么" doesn't count
    # (live round 2 on 2026-06-11: a 30-char Mengniu-ad exclamation was
    # treated as an open-floor question and the bot volunteered)
    if (
        is_question
        and not names_other
        and not ctx.in_window
        and "你" not in text
        and len(norm) <= 12
        and (re.search(r"[??]|吗|谁|哪|几|多少", text) or norm.endswith("呢"))
    ):
        return (Verdict.OPEN_FLOOR, 0, ["open_floor_self_question"])

    # 5. Pure acknowledgment / backchannel: eavesdrop, don't pick up
    if norm in _ACK_SET:
        return (Verdict.REJECT, 0, ["ack"])

    # 5.5 Vocative aimed at someone else (starts with another real user's name) → hard reject, skip scoring
    for name in ctx.others_present:
        if name and norm.startswith(name.lower()):
            return (Verdict.REJECT, -9, ["vocative_other"])

    # 6. Out of window with no addressing signal at all → eavesdrop
    if not ctx.in_window:
        return (Verdict.REJECT, 0, ["out_of_window"])

    # 7. In-window heuristic scoring
    # Live calibration (2026-06-11 real chat): with multiple users present,
    # "你" is overwhelmingly person-to-person — second_person alone is not
    # enough for ACCEPT; fragments/self-narration must be filtered out first.
    norm_core = _LEAD_INTERJ_RE.sub("", norm)
    bot_asked = ctx.last_speaker_was_bot and bool(_QUESTION_RE.search(ctx.last_bot_text))

    # 7.0 Ultra-short fragment (≤3 chars, not a question, bot didn't just ask)
    # → ASR debris, eavesdrop. Particle-ending ones don't count
    # ("不行吧" is a reaction, "你帮你" is debris).
    if (
        len(norm) <= 3
        and not is_question
        and not bot_asked
        and norm[-1:] not in "吧呀啊哦呗嘛哟诶"
    ):
        return (Verdict.REJECT, 0, ["fragment"])

    score = 0
    if names_other:
        score -= 3
        signals.append("names_other:-3")
    if "你们" in text:
        score -= 1
        signals.append("plural_you:-1")
    elif "你" in text or "妳" in text:
        if norm.endswith("你") or norm.endswith("妳"):
            # Sentence-final "你" is vocative ("不好笑啊你") — far more directed than a mid-sentence "你"
            score += 2
            signals.append("second_person_vocative:+2")
        else:
            score += 1
            signals.append("second_person:+1")
    if ctx.last_speaker_was_bot and is_question:
        score += 2
        signals.append("followup_question:+2")
    elif ctx.last_speaker_was_bot and len(norm) <= 10 and not norm_core.startswith("我"):
        # Self-narration like "我先去拿外卖" is not a reaction to the bot (caught by script replay)
        score += 1
        signals.append("short_reaction:+1")
    if norm_core.startswith("我") and "你" not in text and not bot_asked:
        # "我去改一下他PSO呢" is talking to oneself; except when the bot just asked (then it's an answer)
        score -= 2
        signals.append("self_narration:-2")
    overlap = _content_overlap(text, ctx.last_bot_text)
    if overlap >= 2:
        score += 2
        signals.append(f"topic_overlap{overlap}:+2")
    elif overlap == 1:
        score += 1
        signals.append("topic_overlap1:+1")
    if _SKILL_RE.search(text):
        score += 2
        signals.append("skill_imperative:+2")
    if _THIRD_PERSON_RE.search(norm_core):
        score -= 2
        signals.append("third_person:-2")
    if _INTERPERSONAL_RE.search(text):
        score -= 2
        signals.append("interpersonal_topic:-2")

    if score >= 2:
        return (Verdict.ACCEPT, score, signals)
    # In-window questions get a clarify fallback (never silently swallow a question possibly aimed at her)
    if score <= 0 and not is_question:
        return (Verdict.REJECT, score, signals)
    if is_question and not names_other and score >= 0:
        return (Verdict.CLARIFY, score, signals + ["question_floor"])
    if score == 1:
        return (Verdict.CLARIFY, score, signals)
    return (Verdict.REJECT, score, signals)
