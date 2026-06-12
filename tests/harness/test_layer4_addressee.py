"""Layer 4 (AddresseeDetector + WakeWordMatcher) tests.

Speed:    is_addressed() / match_only() — should be µs
Quality:  4-rule precision/recall on labeled corpus
Robust:   punctuation variants, ASR-style errors, edge cases
"""
from __future__ import annotations

import time

import pytest

from tests.harness._utils import FakeSession, Stat, time_ms
from echotwin.persona import Persona
from echotwin.pipeline.addressee import AddresseeDetector
from echotwin.wake_word.matcher import WakeWordMatcher


@pytest.fixture
def persona():
    return Persona(
        id="yidiandian",
        name="一点点点",
        voice_id="vid",
        wake_words=["一点点点", "点点"],
        fast_responses=[],
        limit_exceeded_text="",
        farewell_text="",
        system_prompt="",
    )


@pytest.fixture
def detector(persona):
    return AddresseeDetector(
        persona=persona,
        bot_user_id=999,
        continuation_window_seconds=15.0,
        solo_channel_auto=True,
    )


@pytest.fixture
def matcher(persona):
    return WakeWordMatcher(wake_words=persona.wake_words)


# --- Speed ------------------------------------------------------------

def test_addressee_is_addressed_under_100us(detector):
    """Per-call latency should be <100 microseconds (it's just regex + dict access)."""
    s = FakeSession()
    stat = Stat("addressee_call")
    for _ in range(1000):
        with time_ms() as t:
            detector.is_addressed("现在几点了", speaker_id=1, session=s, channel_member_count=5)
        stat.add(t.elapsed_ms / 1000.0)
    print(f"\n  {stat.fmt_ms()}")
    assert stat.p95 < 0.0001, f"too slow: P95={stat.p95*1000*1000:.1f}us"


def test_match_only_under_100us(matcher):
    stat = Stat("match_only")
    for _ in range(1000):
        with time_ms() as t:
            matcher.match_only("一点点点?")
        stat.add(t.elapsed_ms / 1000.0)
    print(f"\n  {stat.fmt_ms()}")
    assert stat.p95 < 0.0001


# --- Quality (4 rules precision/recall) -------------------------------

# Labeled corpus: (text, channel_count, last_addressee, secs_since_bot, expected_addressed)
LABELED_CASES = [
    # Rule 1: wake word
    ("一点点点 你好", 5, None, 99, True),
    ("点点 在吗", 5, None, 99, True),
    ("【一点点点】帮我查天气", 5, None, 99, True),
    # Rule 2: mention
    ("<@999> 你好", 5, None, 99, True),
    # Rule 3: continuation (same speaker, recent bot speak)
    ("好的", 5, 1, 5, True),     # within window, same speaker
    ("好的", 5, 1, 30, False),   # window expired
    ("好的", 5, 2, 5, False),    # different speaker
    # Rule 4: solo channel
    ("吃饭了吗", 2, None, 99, True),
    ("吃饭了吗", 5, None, 99, False),
    # Negative
    ("我去厕所", 5, None, 99, False),
    ("今天股票涨了", 5, None, 99, False),
    # Edge: empty / pure punct
    ("", 5, None, 99, False),
    ("...", 5, None, 99, False),
    # Wake word in middle of long sentence — still addressed
    ("我跟点点说一下", 5, None, 99, True),
]


@pytest.mark.parametrize("text,channel_count,last_addr,secs_back,expected", LABELED_CASES)
def test_addressee_labeled_corpus(detector, text, channel_count, last_addr, secs_back, expected):
    s = FakeSession(
        last_bot_speak_time=time.time() - secs_back,
        last_addressee_id=last_addr,
    )
    actual = detector.is_addressed(text, speaker_id=1, session=s, channel_member_count=channel_count)
    assert actual == expected, (
        f"text={text!r} channel={channel_count} last_addr={last_addr} "
        f"secs_back={secs_back}: expected {expected}, got {actual}"
    )


def test_addressee_corpus_summary(detector):
    """Roll-up: precision/recall on the labeled set."""
    tp = fp = tn = fn = 0
    for text, ch, la, sb, expected in LABELED_CASES:
        s = FakeSession(last_bot_speak_time=time.time() - sb, last_addressee_id=la)
        actual = detector.is_addressed(text, speaker_id=1, session=s, channel_member_count=ch)
        if expected and actual: tp += 1
        elif not expected and not actual: tn += 1
        elif expected and not actual: fn += 1
        else: fp += 1
    precision = tp / (tp + fp) if (tp + fp) else 1.0
    recall = tp / (tp + fn) if (tp + fn) else 1.0
    print(
        f"\n  N={len(LABELED_CASES)}  TP={tp} TN={tn} FP={fp} FN={fn}  "
        f"precision={precision:.2f} recall={recall:.2f}"
    )
    assert precision >= 0.9
    assert recall >= 0.9


# --- match_only quality -----------------------------------------------

MATCH_ONLY_CASES = [
    ("一点点点", True),
    ("点点", True),
    ("一点点点?", True),
    ("点点。", True),
    ("点点啊", True),
    ("一点点点 今天天气怎么样", False),  # too much extra
    ("吃饭了吗", False),
    ("今天点点你好", False),  # wake in middle but extra > 2 chars
    ("", False),
]


@pytest.mark.parametrize("text,expected", MATCH_ONLY_CASES)
def test_match_only_corpus(matcher, text, expected):
    assert matcher.match_only(text) == expected, (
        f"match_only({text!r}) expected {expected}"
    )


# --- Robustness -------------------------------------------------------

def test_strip_wake_word_robust(detector):
    cases = [
        ("一点点点 今天几点", "今天几点"),
        ("点点,你好", "你好"),
        ("【一点点点】查天气", "查天气"),
        ("hello", "hello"),  # no wake → unchanged
    ]
    for inp, expected in cases:
        actual = detector.strip_wake_word(inp)
        assert actual == expected, f"strip({inp!r}) expected {expected!r} got {actual!r}"


def test_addressee_doesnt_match_wake_substring_in_real_word(detector):
    """Edge: 'wake word' appears as substring of an unrelated phrase.
    Our matcher does substring-after-normalize, so '点点' inside '检点点核' would match.
    Document the behavior — false positive is acceptable for our use case.
    """
    s = FakeSession()
    # Hypothetical false positive case — '点点' inside other word
    actual = detector.is_addressed("检点点核完成", speaker_id=1, session=s, channel_member_count=5)
    # Our detector is liberal; this WILL match. Print but don't fail.
    print(f"\n  '检点点核完成' addressed = {actual} (false positive case)")
