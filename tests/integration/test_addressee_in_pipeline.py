"""Verify addressee filter behavior in a multi-user channel simulation."""
import time

import pytest

from echotwin.persona import Persona
from echotwin.pipeline.addressee import AddresseeDetector


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


def test_solo_channel_lets_everything_through(persona):
    detector = AddresseeDetector(persona, bot_user_id=999)

    class S:
        last_bot_speak_time = 0.0
        last_addressee_id = None

    s = S()
    # 2-person channel: bot + Alan
    assert detector.is_addressed(
        "今天吃啥", speaker_id=1, session=s, channel_member_count=2
    )


def test_three_person_channel_filters_chatter(persona):
    detector = AddresseeDetector(persona, bot_user_id=999)

    class S:
        last_bot_speak_time = 0.0
        last_addressee_id = None

    s = S()
    # 3-person: bot + Alan + Bob
    # Alan asks the bot — wake word → addressed
    assert detector.is_addressed(
        "一点点点 你好", speaker_id=1, session=s, channel_member_count=3
    )
    # Bob chats with Alan, no wake word → not addressed
    assert not detector.is_addressed(
        "今晚吃啥", speaker_id=2, session=s, channel_member_count=3
    )


def test_continuation_only_for_recent_addressee(persona):
    detector = AddresseeDetector(persona, bot_user_id=999)

    class S:
        last_bot_speak_time = time.time()
        last_addressee_id = 1  # bot was just talking with Alan

    s = S()
    # Alan continues the conversation — addressed
    assert detector.is_addressed(
        "好的", speaker_id=1, session=s, channel_member_count=3
    )
    # Bob's chatter is NOT a continuation
    assert not detector.is_addressed(
        "我去厕所", speaker_id=2, session=s, channel_member_count=3
    )


def test_continuation_window_expires(persona):
    detector = AddresseeDetector(
        persona, bot_user_id=999, continuation_window_seconds=10
    )

    class S:
        last_bot_speak_time = time.time() - 30
        last_addressee_id = 1

    s = S()
    assert not detector.is_addressed(
        "好的", speaker_id=1, session=s, channel_member_count=3
    )
