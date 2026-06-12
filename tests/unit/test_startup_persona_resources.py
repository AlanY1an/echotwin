"""Startup-order regression test — the persona selected by runtime_config must be loaded first,
before building the wake matcher / fast cache / quota-limit audio.

Historical bug: setup_hook built those resources from the config.yaml persona first, and only
afterwards did load_runtime_config swap the persona, so after a restart the wake words / cached
audio still belonged to the yaml persona.
"""
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from echotwin.bot import VoiceAgentBot
from echotwin.commands.owner_dm import load_runtime_config
from echotwin.persona import load_persona

YAML_PERSONA = """---
name: Yaml酱
voice_id: yaml-voice
wake_words:
  - 雅ml
fast_responses:
  - 在
---
yaml persona body
"""

RUNTIME_PERSONA = """---
name: Runtime酱
voice_id: runtime-voice
wake_words:
  - 阿润
fast_responses:
  - 来了
---
runtime persona body
"""


def _make_stub_bot(tmp_path):
    persona_dir = tmp_path / "prompts" / "personas"
    persona_dir.mkdir(parents=True)
    (persona_dir / "yamlchan.md").write_text(YAML_PERSONA, encoding="utf-8")
    (persona_dir / "runtimechan.md").write_text(RUNTIME_PERSONA, encoding="utf-8")
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "runtime_config.json").write_text(
        json.dumps({"active_persona": "runtimechan"}), encoding="utf-8"
    )

    bot = SimpleNamespace(
        persona=load_persona(persona_dir / "yamlchan.md"),
        config=SimpleNamespace(
            bot=SimpleNamespace(
                active_persona="yamlchan",
                wake_word_required=False,
                listen_only_users=[],
            ),
            tts=SimpleNamespace(fish_audio_stream=SimpleNamespace(voice_id="")),
        ),
        extra_owner_ids=set(),
        _synth_with_persona=AsyncMock(return_value=None),
    )
    return bot


async def test_persona_resources_built_from_runtime_persona(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    bot = _make_stub_bot(tmp_path)

    with patch(
        "echotwin.wake_word.FastResponseCache.ensure_synthesized",
        new=AsyncMock(return_value=None),
    ):
        # setup_hook's correct order: runtime config first, then build resources
        load_runtime_config(bot)
        await VoiceAgentBot._init_persona_resources(bot)

    assert bot.persona.id == "runtimechan"
    assert bot.wake_matcher._wake_words == ["阿润"], (
        "wake matcher 必须来自 runtime persona,而不是 config.yaml persona"
    )
    assert bot.fast_cache._persona_id == "runtimechan"
    assert "runtimechan" in str(bot.limit_audio_path)
