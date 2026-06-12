"""i18n smoke tests — ensure all keys have both locales and the translator works."""
from __future__ import annotations

import pytest

from echotwin.i18n.strings import STRINGS, DEFAULT_LOCALE, t


REQUIRED_LOCALES = ("en-US", "zh-CN")


def test_every_key_has_all_required_locales():
    missing = []
    for key, table in STRINGS.items():
        for loc in REQUIRED_LOCALES:
            if loc not in table:
                missing.append(f"{key} missing {loc}")
    assert not missing, "missing translations:\n" + "\n".join(missing)


def test_default_locale_is_en_us():
    assert DEFAULT_LOCALE == "en-US"


def test_t_basic():
    assert t("resp.joined", "en-US", channel="general") == "✅ Joined general"
    assert t("resp.joined", "zh-CN", channel="general") == "✅ 已加入 general"


def test_t_locale_alias_zh_tw_falls_back_to_zh_cn():
    assert t("resp.joined", "zh-TW", channel="general") == "✅ 已加入 general"


def test_t_unknown_locale_falls_back_to_default():
    # ja with no zh alias → en-US
    assert t("resp.joined", "ja", channel="general") == "✅ Joined general"


def test_t_unknown_key_returns_key():
    assert t("totally.fake.key", "en-US") == "totally.fake.key"


def test_t_swallows_missing_format_args():
    # missing 'channel' kw — should not raise
    assert "{channel}" in t("resp.joined", "en-US")


def test_t_default_locale_when_none():
    assert t("resp.joined", None, channel="general") == "✅ Joined general"


@pytest.mark.parametrize(
    "key, expected_substring_en, expected_substring_zh",
    [
        ("cmd.join.desc", "voice channel", "语音"),
        ("cmd.leave.desc", "Leave", "离开"),
        ("resp.persona_switched", "Switched", "切换"),
        ("resp.owner_primary_only", "Only the primary", "仅主"),
    ],
)
def test_translation_pairs(key, expected_substring_en, expected_substring_zh):
    assert expected_substring_en in t(key, "en-US")
    assert expected_substring_zh in t(key, "zh-CN")
