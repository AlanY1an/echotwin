import pytest
from echotwin.config import load_config


def test_load_minimal_config(tmp_path):
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text("""
discord:
  token: env:DISCORD_TOKEN
bot:
  name: test_bot
  active_persona: yidiandian
vad:
  provider: silero
asr:
  provider: funasr_local
llm:
  provider: claude_haiku
tts:
  provider: fish_audio_stream
  fish_audio_stream:
    voice_id: abc123
""", encoding="utf-8")
    cfg = load_config(cfg_file)
    assert cfg.bot.name == "test_bot"
    assert cfg.tts.fish_audio_stream.voice_id == "abc123"


def test_env_interpolation(tmp_path, monkeypatch):
    monkeypatch.setenv("DISCORD_TOKEN", "abc-secret")
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text("""
discord:
  token: env:DISCORD_TOKEN
bot:
  name: t
  active_persona: x
tts:
  provider: fish_audio_stream
  fish_audio_stream:
    voice_id: v
""", encoding="utf-8")
    cfg = load_config(cfg_file)
    assert cfg.discord.token == "abc-secret"


def test_missing_required_field_raises(tmp_path):
    cfg_file = tmp_path / "bad.yaml"
    cfg_file.write_text("bot: {}", encoding="utf-8")
    with pytest.raises(Exception):
        load_config(cfg_file)


def test_endpoint_tuning_defaults():
    """Endpoint detection params are configurable; defaults moved from hardcoded 800ms/200ms to 600ms/100ms."""
    from echotwin.config import BotCfg

    # name/active_persona are required fields, everything else uses defaults
    cfg = BotCfg(name="x", active_persona="y")
    assert cfg.endpoint_silence_ms == 600
    assert cfg.endpoint_tick_ms == 100


def test_speculative_asr_defaults():
    from echotwin.config import BotCfg

    cfg = BotCfg(name="x", active_persona="y")
    assert cfg.speculative_asr is True
    assert cfg.speculative_asr_silence_ms == 300


def test_filler_defaults():
    from echotwin.config import BotCfg

    cfg = BotCfg(name="x", active_persona="y")
    assert cfg.filler_mode == "smart"
    assert "天气" in cfg.filler_keywords


def test_phase2_defaults():
    from echotwin.config import ASRCfg, BotCfg

    bot = BotCfg(name="x", active_persona="y")
    assert bot.speculative_llm is False, "投机 LLM 必须默认关(验收期手动开)"
    asr = ASRCfg()
    assert asr.emotion_sidecar is True
    assert asr.sherpa_stream.repo == ""  # empty = auto-select by persona language


def _cfg_with_keys(tmp_path, token, fish, anthropic):
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(f"""
discord:
  token: "{token}"
bot:
  name: test_bot
  active_persona: p
llm:
  claude_haiku:
    api_key: "{anthropic}"
tts:
  provider: fish_audio_stream
  fish_audio_stream:
    api_key: "{fish}"
    voice_id: abc
""", encoding="utf-8")
    from echotwin.config import load_config
    return load_config(cfg_file)


def test_missing_required_keys_listed(tmp_path):
    """Fresh-clone UX: empty .env must produce a friendly list, not a discord traceback."""
    from echotwin.config import missing_required_keys

    missing = missing_required_keys(_cfg_with_keys(tmp_path, "", "", ""))
    assert {"DISCORD_TOKEN", "FISH_AUDIO_API_KEY", "ANTHROPIC_API_KEY"} <= set(missing)
    assert missing_required_keys(_cfg_with_keys(tmp_path, "x", "y", "z")) == []


def test_sherpa_repo_auto_selects_by_language(tmp_path):
    """Empty repo config = pick the ASR model matching the persona language."""
    from echotwin.providers.factory import resolve_sherpa_repo

    repo, files = resolve_sherpa_repo("", "zh")
    assert "bilingual-zh-en" in repo and files
    repo, files = resolve_sherpa_repo("", "en")
    assert "zipformer-en" in repo and files
    # explicit repo wins; unknown repo gets no fixed file list (glob resolution)
    repo, files = resolve_sherpa_repo("someone/custom-model", "en")
    assert repo == "someone/custom-model" and files is None
