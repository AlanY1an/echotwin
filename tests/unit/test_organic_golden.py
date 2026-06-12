"""Organic addressee judgment — driven by the golden sample set (spec acceptance metrics).

Metrics: miss rate (expected accept judged REJECT) ≤10%; false-accept rate (expected reject judged
ACCEPT) ≤10%; expected clarify passes as either accept or clarify (cases whose note contains
"也可判reject" may also be rejected); open_floor must be judged OPEN_FLOOR.
"""
import json
from pathlib import Path

import pytest

from echotwin.pipeline.organic import OrganicContext, Verdict, classify

GOLDEN = Path(__file__).parents[1] / "fixtures" / "addressee_golden.jsonl"
WAKE_WORDS = ["Hinata", "雏田老师", "Hinata宝宝", "宝宝", "老师"]


def _cases():
    return [json.loads(l) for l in GOLDEN.read_text(encoding="utf-8").strip().split("\n")]


def _ctx(c):
    ctx = c["context"]
    return OrganicContext(
        wake_words=WAKE_WORDS,
        in_window=ctx["in_window"],
        solo=ctx["solo"],
        last_bot_text=ctx["last_bot_text"],
        last_speaker_was_bot=ctx["last_speaker_was_bot"],
        others_present=ctx["others_present"],
        clarify_pending=ctx["last_bot_text"].startswith("诶,是在叫我吗"),
    )


def test_golden_metrics():
    cases = _cases()
    miss = wrong = 0
    n_accept = n_reject = 0
    failures = []
    for c in cases:
        verdict, score, signals = classify(c["utterance"], _ctx(c))
        exp = c["expected"]
        if exp == "accept":
            n_accept += 1
            if verdict == Verdict.REJECT:
                miss += 1
                failures.append((c["id"], exp, verdict.name, c["utterance"], signals))
        elif exp == "reject":
            n_reject += 1
            if verdict == Verdict.ACCEPT:
                wrong += 1
                failures.append((c["id"], exp, verdict.name, c["utterance"], signals))
        elif exp == "clarify":
            ok = verdict in (Verdict.ACCEPT, Verdict.CLARIFY) or (
                "也可判reject" in c.get("note", "") and verdict == Verdict.REJECT
            )
            if not ok:
                failures.append((c["id"], exp, verdict.name, c["utterance"], signals))
        elif exp == "open_floor":
            if verdict != Verdict.OPEN_FLOOR:
                failures.append((c["id"], exp, verdict.name, c["utterance"], signals))
        elif exp == "mention":
            if verdict != Verdict.MENTION:
                failures.append((c["id"], exp, verdict.name, c["utterance"], signals))
    miss_rate = miss / n_accept
    wrong_rate = wrong / n_reject
    detail = "\n".join(f"  #{i} 期望{e} 实判{v}: {u!r} signals={s}" for i, e, v, u, s in failures)
    assert miss_rate <= 0.10, f"漏接率 {miss_rate:.0%} > 10%\n{detail}"
    assert wrong_rate <= 0.10, f"误接率 {wrong_rate:.0%} > 10%\n{detail}"
    assert not [f for f in failures if f[1] in ("clarify", "open_floor", "mention")], (
        f"clarify/open_floor/mention 失败:\n{detail}"
    )
    print(f"\n漏接 {miss}/{n_accept} ({miss_rate:.0%}), 误接 {wrong}/{n_reject} ({wrong_rate:.0%})")
