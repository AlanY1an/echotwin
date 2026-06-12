from pathlib import Path

import pytest

from echotwin.persona import Persona, PersonaError, load_persona


def write_persona(tmp_path: Path, name: str, body: str) -> Path:
    p = tmp_path / f"{name}.md"
    p.write_text(body, encoding="utf-8")
    return p


def test_full_frontmatter_parses(tmp_path):
    p = write_persona(
        tmp_path,
        "yidiandian",
        """---
name: 一点点点
voice_id: abc123
wake_words:
  - 一点点点
  - 点点
fast_responses:
  - 嗯?
  - 在的
limit_exceeded_text: 额度用完啦
farewell_text: 再见
---

你叫一点点点,讲话台湾腔。
""",
    )
    persona = load_persona(p)
    assert persona.id == "yidiandian"
    assert persona.name == "一点点点"
    assert persona.voice_id == "abc123"
    assert persona.wake_words == ["一点点点", "点点"]
    assert persona.fast_responses == ["嗯?", "在的"]
    assert persona.limit_exceeded_text == "额度用完啦"
    assert persona.farewell_text == "再见"
    assert "讲话台湾腔" in persona.system_prompt
    assert "voice_id" not in persona.system_prompt


def test_missing_voice_id_raises(tmp_path):
    p = write_persona(
        tmp_path,
        "bad",
        """---
name: x
---
body
""",
    )
    with pytest.raises(PersonaError, match="voice_id"):
        load_persona(p)


def test_missing_name_raises(tmp_path):
    p = write_persona(
        tmp_path,
        "bad",
        """---
voice_id: abc
---
body
""",
    )
    with pytest.raises(PersonaError, match="name"):
        load_persona(p)


def test_wake_words_default_to_name(tmp_path):
    p = write_persona(
        tmp_path,
        "x",
        """---
name: 阿点
voice_id: abc
---
body
""",
    )
    persona = load_persona(p)
    assert persona.wake_words == ["阿点"]


def test_fast_responses_default(tmp_path):
    p = write_persona(
        tmp_path,
        "x",
        """---
name: x
voice_id: y
---
body
""",
    )
    persona = load_persona(p)
    assert persona.fast_responses == ["嗯?", "在的"]


def test_text_defaults(tmp_path):
    p = write_persona(
        tmp_path,
        "x",
        """---
name: x
voice_id: y
---
body
""",
    )
    persona = load_persona(p)
    assert "额度" in persona.limit_exceeded_text
    assert persona.farewell_text


def test_no_frontmatter_fails_loudly(tmp_path):
    p = write_persona(tmp_path, "legacy", "你叫遗留 persona")
    with pytest.raises(PersonaError, match="name|voice_id"):
        load_persona(p)


def test_missing_file_raises(tmp_path):
    p = tmp_path / "nope.md"
    with pytest.raises(PersonaError, match="not found"):
        load_persona(p)


def test_wake_words_must_be_list_of_str(tmp_path):
    p = write_persona(
        tmp_path,
        "bad",
        """---
name: x
voice_id: y
wake_words: 不是列表
---
body
""",
    )
    with pytest.raises(PersonaError, match="wake_words"):
        load_persona(p)


def test_fast_responses_must_be_list_of_str(tmp_path):
    p = write_persona(
        tmp_path,
        "bad",
        """---
name: x
voice_id: y
fast_responses:
  - 1
  - 2
---
body
""",
    )
    with pytest.raises(PersonaError, match="fast_responses"):
        load_persona(p)
