import time

import pytest

from echotwin.persona import Persona
from echotwin.pipeline.addressee import AddresseeDetector


@pytest.fixture
def persona():
    return Persona(
        id="yidiandian",
        name="一点点点",
        voice_id="abc",
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


class FakeSession:
    def __init__(self, last_bot_speak_time=0.0, last_addressee_id=None):
        self.last_bot_speak_time = last_bot_speak_time
        self.last_addressee_id = last_addressee_id


def test_wake_word_prefix_addressed(detector):
    s = FakeSession()
    assert detector.is_addressed("一点点点 今天天气", speaker_id=1, session=s, channel_member_count=5)


def test_wake_word_short_addressed(detector):
    s = FakeSession()
    assert detector.is_addressed("点点", speaker_id=1, session=s, channel_member_count=5)


def test_no_wake_no_continuation_not_addressed(detector):
    s = FakeSession()
    assert not detector.is_addressed("今天吃啥", speaker_id=1, session=s, channel_member_count=5)


def test_continuation_within_window(detector):
    s = FakeSession(last_bot_speak_time=time.time() - 5, last_addressee_id=1)
    assert detector.is_addressed("好的", speaker_id=1, session=s, channel_member_count=5)


def test_continuation_expired(detector):
    s = FakeSession(last_bot_speak_time=time.time() - 30, last_addressee_id=1)
    assert not detector.is_addressed("好的", speaker_id=1, session=s, channel_member_count=5)


def test_continuation_wrong_speaker(detector):
    s = FakeSession(last_bot_speak_time=time.time() - 5, last_addressee_id=2)
    assert not detector.is_addressed("好的", speaker_id=1, session=s, channel_member_count=5)


def test_solo_channel_always_addressed(detector):
    s = FakeSession()
    assert detector.is_addressed("随便说点什么", speaker_id=1, session=s, channel_member_count=2)


def test_bot_mention_addressed(detector):
    s = FakeSession()
    assert detector.is_addressed("<@999> 你好", speaker_id=1, session=s, channel_member_count=5)


def test_empty_text_not_addressed(detector):
    s = FakeSession()
    assert not detector.is_addressed("", speaker_id=1, session=s, channel_member_count=5)
    assert not detector.is_addressed("", speaker_id=1, session=s, channel_member_count=2)


def test_punctuation_robust_wake_match(detector):
    s = FakeSession()
    assert detector.is_addressed("一点点点,你好", speaker_id=1, session=s, channel_member_count=5)
    assert detector.is_addressed("【一点点点】", speaker_id=1, session=s, channel_member_count=5)


def test_strip_wake_word_method(detector):
    assert detector.strip_wake_word("一点点点 今天天气") == "今天天气"
    assert detector.strip_wake_word("点点,你好") == "你好"
    assert detector.strip_wake_word("hello") == "hello"
