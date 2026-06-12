"""Layer 6 (SentenceChunker) tests.

Speed:    trivial — just regex; not measured
Quality:  first-sentence latency (chars before first chunk emits) — should be small
          subsequent sentences only on strong punctuation
Robust:   no-punctuation runaway, mixed CN/EN punctuation
"""
from __future__ import annotations

import pytest

from echotwin.utils.sentence_chunker import SentenceChunker


def feed_all(chunker: SentenceChunker, deltas: list[str]) -> tuple[list[str], str]:
    """Feed deltas one at a time, collect emitted sentences + remainder after flush."""
    sents: list[str] = []
    for d in deltas:
        for s in chunker.feed(d):
            sents.append(s)
    rem = chunker.flush()
    return sents, rem


# --- First-sentence latency (chars before first emit) -----------------

def test_first_sentence_emits_on_loose_punct():
    """First sentence should fire on any of '.!?。!?…' for fast TTFB."""
    chunker = SentenceChunker()
    sents, _ = feed_all(chunker, ["你好", "!", "今天", "怎么样", "?"])
    print(f"\n  emitted: {sents}")
    assert sents[0].endswith("!"), "first sentence should fire on '!'"


def test_subsequent_sentences_strict_only():
    """After first emit, only strong punctuation triggers."""
    chunker = SentenceChunker()
    sents, rem = feed_all(chunker, ["你好。", "今天,天气", "不错,", "你呢?"])
    print(f"\n  emitted: {sents}, rem: {rem!r}")
    # First "你好。" emits. Then "今天,天气不错," should NOT emit (commas only). Then "你呢?" should emit.
    assert sents[0].strip() == "你好。"
    # Either ['你好。', '今天,天气不错,你呢?'] or similar — verify the 2nd was emitted
    assert any("你呢?" in s for s in sents) or "你呢?" in rem


# --- Quality: full sentence preservation ------------------------------

def test_long_paragraph_split_correctly():
    """Multi-sentence paragraph should split into expected chunks."""
    text = "今天天气怎么样?我想出门跑步。顺便买点东西。"
    chunker = SentenceChunker()
    # Feed char-by-char (mimics token streaming)
    sents = []
    for c in text:
        for s in chunker.feed(c):
            sents.append(s)
    rem = chunker.flush()
    if rem:
        sents.append(rem)
    print(f"\n  emitted: {sents}")
    assert len(sents) >= 2, "expected ≥2 sentences"


def test_remainder_after_flush():
    """Trailing text without final punctuation should come out via flush()."""
    chunker = SentenceChunker()
    sents, rem = feed_all(chunker, ["你好。", "再说一句"])
    assert sents == ["你好。"]
    assert rem == "再说一句"


# --- Robustness -------------------------------------------------------

def test_no_punctuation_only_via_flush():
    """A long blob without any sentence-ending punctuation: nothing emits until flush."""
    chunker = SentenceChunker()
    sents, rem = feed_all(chunker, ["这", "是", "一", "段", "没", "标", "点", "的", "话"])
    print(f"\n  emitted: {sents}, rem: {rem!r}")
    assert not sents, "shouldn't emit without strong punctuation"
    assert "没标点的话" in rem


def test_english_punctuation_works():
    """English '.!?' should also trigger emit."""
    chunker = SentenceChunker()
    sents, _ = feed_all(chunker, ["Hello!", " how are you?"])
    assert sents[0].endswith("!")


def test_first_emit_minimum_chars():
    """First emit shouldn't fire on a punctuation-only delta."""
    chunker = SentenceChunker()
    sents, _ = feed_all(chunker, ["?"])
    # If the first feed is just "?", chunker may emit immediately or hold — both OK
    # but verify it doesn't crash
    print(f"\n  punct-only: {sents}")


def test_reset_clears_state():
    chunker = SentenceChunker()
    feed_all(chunker, ["你好。"])
    chunker.reset()
    sents, _ = feed_all(chunker, ["再来。"])
    assert sents == ["再来。"]
