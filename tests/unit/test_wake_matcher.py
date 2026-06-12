import pytest

from echotwin.wake_word.matcher import WakeWordMatcher


@pytest.fixture
def matcher():
    return WakeWordMatcher(wake_words=["一点点点", "点点"])


def test_exact_match(matcher):
    assert matcher.match_only("一点点点") is True
    assert matcher.match_only("点点") is True


def test_match_with_trailing_punct(matcher):
    assert matcher.match_only("一点点点?") is True
    assert matcher.match_only("点点?") is True
    assert matcher.match_only("点点。") is True


def test_match_with_long_content_does_not_match_only(matcher):
    assert matcher.match_only("一点点点 今天天气怎么样") is False


def test_short_extra_still_match_only(matcher):
    assert matcher.match_only("点点啊") is True
    assert matcher.match_only("点点你好") is True


def test_no_match(matcher):
    assert matcher.match_only("今天吃啥") is False


def test_contains_wake(matcher):
    assert matcher.contains_wake("今天点点你好") is True
    assert matcher.contains_wake("吃饭了吗") is False


def test_empty_text(matcher):
    assert matcher.match_only("") is False
    assert matcher.contains_wake("") is False


def test_match_only_is_case_insensitive():
    """contains_wake was case-insensitive while match_only was not — an ASCII wake word ("Hinata")
    lowercased by ASR could never take the fast path, paying for full LLM+TTS every time."""
    m = WakeWordMatcher(wake_words=["Hinata"])
    assert m.contains_wake("hinata?")  # has always been correct
    assert m.match_only("hinata?"), "小写转写的纯唤醒词必须能走快速路径"
    assert m.match_only("HINATA!")
    assert not m.match_only("hinata 今天天气怎么样")  # more than 2 extra characters still doesn't count
