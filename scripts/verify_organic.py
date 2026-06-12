"""Organic script replay — 20 lines in a three-person channel, per-line verdicts + assertions at key points.

Pure logic replay (classify + window state evolution); never touches audio/LLM.
Run: .venv/bin/python -m scripts.verify_organic
"""
from __future__ import annotations

import time

from echotwin.pipeline.organic import OrganicContext, Verdict, classify

WAKE = ["Hinata", "宝宝", "老师"]

# (speaker, utterance, expected verdict, whether the bot then replied to the speaker)
SCRIPT = [
    ("Alan", "走走走再来一把", "reject", False),
    ("小明", "等我喝口水", "reject", False),
    ("Alan", "宝宝今天天气怎么样", "accept", True),          # vocative → Alan enters the window
    ("Alan", "那明天呢", "accept", True),                    # follow-up inside the window
    ("小明", "你帮我也查一下上海的", "accept", True),         # second person from another user inside the window → 小明 enters the window
    ("阿杰", "我先去拿个外卖", "reject", False),
    ("Alan", "小明你昨天怎么没上线", "reject", False),        # addressing a real person by name
    ("小明", "加班去了惨死", "reject", False),
    ("Alan", "有人知道现在几点了吗", "open_floor", False),     # open floor
    ("Alan", "宝宝再讲个笑话", "accept", True),
    ("小明", "哈哈哈", "reject", False),                      # ACK/laughter from a bystander
    ("阿杰", "我跟你们说我今天碰到个离谱事", "reject", False),  # new person-to-person topic
    ("Alan", "笑话不好笑啊你", "accept", True),               # second person inside the window
]


def main() -> None:
    now = time.time()
    participants: dict[str, float] = {}
    bot_last_reply_at = 0.0
    last_bot = ""
    last_voice = None
    fails = 0
    for i, (speaker, text, expected, bot_replies) in enumerate(SCRIPT, 1):
        ctx = OrganicContext(
            wake_words=WAKE,
            # conversation-level window: active if the bot spoke recently (anyone may join)
            in_window=(now - bot_last_reply_at) < 45
            or (now - participants.get(speaker, 0)) < 45,
            solo=False,
            last_bot_text=last_bot,
            last_speaker_was_bot=(last_voice == "bot"),
            others_present=[s for s in ("Alan", "小明", "阿杰") if s != speaker],
        )
        verdict, score, sigs = classify(text, ctx)
        ok = verdict.value == expected
        fails += 0 if ok else 1
        print(f"{'✅' if ok else '❌'} {i:2d}. {speaker}: {text!r} → {verdict.value} "
              f"(期望 {expected}) score={score} {sigs}")
        # state evolution
        if verdict == Verdict.ACCEPT and bot_replies:
            participants[speaker] = now
            bot_last_reply_at = now
            last_bot = f"(回复{speaker}关于:{text[:6]})"
            last_voice = "bot"
        else:
            last_voice = speaker
    print(f"\n{'全部通过 ✅' if fails == 0 else f'{fails} 处不符 ❌'}")
    raise SystemExit(0 if fails == 0 else 1)


if __name__ == "__main__":
    main()
