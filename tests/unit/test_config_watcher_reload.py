"""ConfigWatcher.reload — SIGHUP must not roll runtime overrides back to yaml values.

Historical bug: reload replaced the Config object wholesale, so runtime state set by
/persona-admin, /voice-admin and /admin whitelist (persona / voice override / whitelist)
was silently wiped by a single SIGHUP.
"""
import json
import re
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from echotwin.config import load_config
from echotwin.config_watcher import ConfigWatcher
from echotwin.persona import load_persona

REPO_ROOT = Path(__file__).parents[2]

PERSONA_MD = """---
name: {name}
voice_id: {voice}
wake_words:
  - {wake}
---
body
"""


async def test_sighup_does_not_revert_runtime_overrides(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    # Real config.yaml, with active_persona pointed at the yaml persona
    yaml_text = (REPO_ROOT / "config.yaml").read_text(encoding="utf-8")
    yaml_text = re.sub(r"active_persona:\s*\S+", "active_persona: yamlchan", yaml_text)
    (tmp_path / "config.yaml").write_text(yaml_text, encoding="utf-8")

    persona_dir = tmp_path / "prompts" / "personas"
    persona_dir.mkdir(parents=True)
    (persona_dir / "yamlchan.md").write_text(
        PERSONA_MD.format(name="Yaml酱", voice="yaml-voice", wake="雅ml"), encoding="utf-8"
    )
    (persona_dir / "runtimechan.md").write_text(
        PERSONA_MD.format(name="Runtime酱", voice="runtime-voice", wake="阿润"),
        encoding="utf-8",
    )
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "runtime_config.json").write_text(
        json.dumps(
            {
                "active_persona": "runtimechan",
                "voice_id_override": "override-voice",
                "listen_only_users": [11, 22],
            }
        ),
        encoding="utf-8",
    )

    # The bot's current runtime state (previously set via owner commands)
    cfg = load_config(str(tmp_path / "config.yaml"))
    cfg.bot.active_persona = "runtimechan"
    cfg.tts.fish_audio_stream.voice_id = "override-voice"
    cfg.bot.listen_only_users = [11, 22]
    bot = SimpleNamespace(
        config=cfg,
        persona=load_persona(persona_dir / "runtimechan.md"),
        extra_owner_ids=set(),
        user=None,
        wake_matcher=None,
        fast_cache=None,
        addressee_detector=None,
        limit_audio_path=None,
        _synth_with_persona=AsyncMock(return_value=None),
        sync_nickname_in_active_guilds=AsyncMock(),
    )

    watcher = ConfigWatcher(bot, str(tmp_path / "config.yaml"))
    await watcher.reload()

    assert bot.persona.id == "runtimechan", "SIGHUP 把 runtime persona 回滚成了 yaml persona"
    assert bot.config.bot.active_persona == "runtimechan"
    assert bot.config.tts.fish_audio_stream.voice_id == "override-voice", (
        "voice 覆盖被 SIGHUP 清掉"
    )
    assert bot.config.bot.listen_only_users == [11, 22], "白名单被 SIGHUP 清掉"
