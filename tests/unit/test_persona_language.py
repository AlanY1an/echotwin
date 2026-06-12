"""Persona language field — selects LLM prompt language and default voice lines (zh/en)."""
import pytest

from echotwin.i18n import prompts as P
from echotwin.persona import PersonaError, load_persona, render_system_prompt


@pytest.fixture
def persona_dir(tmp_path):
    d = tmp_path / "personas"
    d.mkdir()
    (tmp_path / "base_template.md").write_text("ZH BASE {persona} {bot_name} {current_time} {channel_name} {members_online} {emotion_tags_help}", encoding="utf-8")
    (tmp_path / "base_template.en.md").write_text("EN BASE {persona} {bot_name} {current_time} {channel_name} {members_online} {emotion_tags_help}", encoding="utf-8")
    return tmp_path


def _write(persona_dir, body="测试人设", **meta):
    lines = ["---", "name: 测试", "voice_id: v1"]
    for k, v in meta.items():
        lines.append(f"{k}: {v}")
    lines += ["---", body]
    p = persona_dir / "personas" / "t.md"
    p.write_text("\n".join(lines), encoding="utf-8")
    return p


def test_language_defaults_to_zh(persona_dir):
    p = load_persona(_write(persona_dir))
    assert p.language == "zh"
    assert p.fast_responses == P.DEFAULT_FAST_RESPONSES["zh"]
    assert p.farewell_text == P.DEFAULT_FAREWELL["zh"]


def test_english_persona_gets_english_defaults(persona_dir):
    p = load_persona(_write(persona_dir, language="en"))
    assert p.language == "en"
    assert p.fast_responses == P.DEFAULT_FAST_RESPONSES["en"]
    assert p.limit_exceeded_text == P.DEFAULT_LIMIT_TEXT["en"]
    assert p.farewell_text == P.DEFAULT_FAREWELL["en"]


def test_invalid_language_rejected(persona_dir):
    with pytest.raises(PersonaError):
        load_persona(_write(persona_dir, language="fr"))


def test_render_picks_language_template(persona_dir):
    zh = load_persona(_write(persona_dir))
    out = render_system_prompt(zh, "x", prompts_dir=persona_dir)
    assert out.startswith("ZH BASE")
    en = load_persona(_write(persona_dir, language="en"))
    out = render_system_prompt(en, "x", prompts_dir=persona_dir)
    assert out.startswith("EN BASE")
    assert "NEUTRAL" in out  # emotion help injected in the right language


def test_locale_tables_cover_both_languages():
    for table in (
        P.DEFAULT_FAST_RESPONSES, P.DEFAULT_LIMIT_TEXT, P.DEFAULT_FAREWELL,
        P.DEFAULT_FILLERS, P.DEFAULT_CLARIFY, P.WAKE_FALLBACK,
        P.DEFAULT_FILLER_KEYWORDS, P.MERGE_NOTE, P.EMOTION_HELP,
        P.ARBITER_SYSTEM, P.GREETING_PROMPT, P.FAREWELL_PROMPT,
    ):
        assert set(table.keys()) >= {"zh", "en"}, f"missing language in {table}"


def test_arbiter_system_en_has_fewshots():
    """Zero-shot addressee prompting is near chance — EN prompt must carry examples too."""
    en = P.ARBITER_SYSTEM["en"].format(bot_name="Echo")
    assert '"verdict"' in en and "open_floor" in en
    assert en.count("→") >= 4 or en.lower().count("example") >= 1
