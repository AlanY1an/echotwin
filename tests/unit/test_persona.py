from pathlib import Path

import pytest

from echotwin.persona import PersonaError, list_personas, render_system_prompt


_FRONTMATTER_FIXTURE = """---
name: 一点点点
voice_id: testvid
---

我是一点点点
"""


def _setup(tmp_path: Path) -> Path:
    base = tmp_path / "base_template.md"
    base.write_text(
        "BOT={bot_name}\n"
        "<identity>\n{persona}\n</identity>\n"
        "EMO={emotion_tags_help}\n"
        "TIME={current_time}\n"
        "CH={channel_name}\n"
        "N={members_online}\n",
        encoding="utf-8",
    )
    pdir = tmp_path / "personas"
    pdir.mkdir()
    (pdir / "yidiandian.md").write_text(_FRONTMATTER_FIXTURE, encoding="utf-8")
    (pdir / "_template.md").write_text("template only", encoding="utf-8")
    return tmp_path


def test_render_includes_persona(tmp_path):
    _setup(tmp_path)
    out = render_system_prompt("yidiandian", "test_bot", prompts_dir=tmp_path)
    assert "我是一点点点" in out
    # When passing string id, render uses the bot_name parameter
    assert "BOT=test_bot" in out


def test_list_excludes_template(tmp_path):
    _setup(tmp_path)
    assert list_personas(tmp_path) == ["yidiandian"]


def test_missing_persona_raises(tmp_path):
    _setup(tmp_path)
    with pytest.raises(PersonaError):
        render_system_prompt("nonexistent", "x", prompts_dir=tmp_path)


@pytest.mark.skipif(
    not Path("prompts/personas/yidiandian.md").exists(),
    reason="local persona not present (personas are gitignored except templates)",
)
def test_real_yidiandian_renders():
    """Verify the real prompts/ files render without error (local dev only)."""
    out = render_system_prompt("yidiandian", "一点点点", prompts_dir="prompts")
    assert "一点点点" in out
    assert "[chuckle]" in out  # base_template emotion section
