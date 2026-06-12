from echotwin.providers.asr.sensevoice_parse import parse_sensevoice_output


def test_full_tags_legacy_format():
    raw = "<|zh|><|SAD|><|EVENT|>你好啊"
    r = parse_sensevoice_output(raw)
    assert r["language"] == "zh"
    assert r["emotion"] == "SAD"
    assert r["content"] == "你好啊"


def test_funasr_new_format():
    raw = "<|EMO_UNKNOWN|><|Speech|><|withitn|>你好"
    r = parse_sensevoice_output(raw)
    assert r["emotion"] == "NEUTRAL"  # EMO_UNKNOWN → NEUTRAL fallback
    assert r["content"] == "你好"


def test_emo_happy_funasr():
    raw = "<|EMO_HAPPY|><|Speech|><|withitn|>great!"
    r = parse_sensevoice_output(raw)
    assert r["emotion"] == "HAPPY"
    assert r["content"] == "great!"


def test_english_happy():
    raw = "<|en|><|HAPPY|><|SPEECH|>Hello"
    r = parse_sensevoice_output(raw)
    assert r["language"] == "en"
    assert r["emotion"] == "HAPPY"
    assert r["content"] == "Hello"


def test_no_tags_passthrough():
    r = parse_sensevoice_output("普通文本")
    assert r["content"] == "普通文本"
    assert r["emotion"] == "NEUTRAL"
    assert r["language"] == "zh"


def test_partial_language_tag():
    r = parse_sensevoice_output("<|en|>hello world")
    assert r["language"] == "en"
    assert r["content"] == "hello world"
    assert r["emotion"] == "NEUTRAL"


def test_strips_whitespace():
    r = parse_sensevoice_output("<|zh|><|NEUTRAL|><|SPEECH|>  hello  ")
    assert r["content"] == "hello"
