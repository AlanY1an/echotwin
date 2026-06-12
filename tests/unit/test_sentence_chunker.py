from echotwin.utils.sentence_chunker import SentenceChunker


def test_first_sentence_uses_first_punct():
    c = SentenceChunker()
    assert c.feed("你好,") == ["你好,"]


def test_subsequent_only_strong_punct():
    c = SentenceChunker()
    c.feed("你好,")  # consumes first-punct privilege
    # Chinese comma 「,」 is not in PUNCT, only FIRST_PUNCT
    out = c.feed("世界,这是测试")
    assert out == []
    out = c.feed("。")
    assert out == ["世界,这是测试。"]


def test_no_punct_no_emit():
    c = SentenceChunker()
    assert c.feed("你好世界") == []


def test_flush_returns_remainder():
    c = SentenceChunker()
    c.feed("你好")
    assert c.flush() == "你好"
    # After flush, buffer empty
    assert c.flush() == ""


def test_multiple_sentences_in_one_feed():
    c = SentenceChunker()
    c.feed("a")  # consume first
    c.feed("。")
    out = c.feed("第一句。第二句。")
    assert out == ["第一句。", "第二句。"]


def test_reset():
    c = SentenceChunker()
    c.feed("hello")
    c.reset()
    assert c.flush() == ""
    # After reset, first-punct rules restored
    assert c.feed("hi,") == ["hi,"]


def test_first_chunk_emitted_at_char_cap_without_punctuation():
    """When the first sentence has no punctuation at all, send to TTS once FIRST_MAX_CHARS accumulate instead of waiting for the full sentence."""
    from echotwin.utils.sentence_chunker import FIRST_MAX_CHARS

    c = SentenceChunker()
    text = "好呀我现在就帮你查今天台北的天气情况如何吧"  # 21 chars, no punctuation at all
    out = []
    for ch in text:  # simulate token-by-token streaming
        out.extend(c.feed(ch))
    assert out, "无标点首句必须在字数上限处切出"
    assert len(out[0]) == FIRST_MAX_CHARS


def test_first_chunk_cap_wins_over_far_punctuation():
    """LLM emits a large delta in one go with the punctuation past 16 chars — the cap must win,
    otherwise it fails exactly in the longest-first-sentence scenario (the very reason it exists)."""
    from echotwin.utils.sentence_chunker import FIRST_MAX_CHARS

    c = SentenceChunker()
    out = c.feed("好呀我现在就帮你查今天台北的天气情况如何吧。")  # punctuation at position 22
    assert out and len(out[0]) == FIRST_MAX_CHARS


def test_short_unpunctuated_first_fragment_still_waits():
    c = SentenceChunker()
    assert c.feed("你好呀") == []  # below the cap and no punctuation → keep waiting


def test_ascii_punctuation_chunks():
    """ASCII !?; must chunk too — English LLM output otherwise buffers until flush."""
    c = SentenceChunker()
    out = c.feed("Sure! It's 3pm now? Right; ok.")
    joined = "".join(out) + c.flush()
    assert any(s.endswith("!") for s in out), f"'!' must cut a chunk: {out!r}"
    assert any(s.endswith("?") for s in out), f"'?' must cut a chunk: {out!r}"
    assert joined == "Sure! It's 3pm now? Right; ok."
