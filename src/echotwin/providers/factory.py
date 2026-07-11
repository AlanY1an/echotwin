"""Provider factory: pick concrete implementations from config."""
from __future__ import annotations

from echotwin.config import Config

from .vad.base import VADProvider
from .vad.silero import SileroVAD
from .asr.base import ASRProvider
from .asr.funasr_local import FunASRLocal
from .tts.base import TTSProvider
from .tts.fish_audio_stream import FishAudioStreamProvider, FishConfig
from .llm.base import LLMProvider
from .llm.claude_haiku import ClaudeHaikuProvider


def make_vad(cfg: Config) -> VADProvider:
    name = cfg.vad.provider
    if name == "silero":
        s = cfg.vad.silero
        return SileroVAD(
            model_dir=s.model_dir,
            threshold=s.threshold,
            threshold_low=s.threshold_low,
            min_silence_duration_ms=s.min_silence_duration_ms,
            frame_window=s.frame_window,
        )
    raise ValueError(f"Unknown VAD provider: {name}")


# Per-language streaming ASR models. The bilingual model is Chinese-first
# (handles English words inside Chinese sentences, not full English speech);
# English personas need the English zipformer for usable transcription.
SHERPA_LANG_REPOS = {
    "zh": (
        "csukuangfj/sherpa-onnx-streaming-zipformer-bilingual-zh-en-2023-02-20",
        [
            "encoder-epoch-99-avg-1.int8.onnx",
            "decoder-epoch-99-avg-1.onnx",
            "joiner-epoch-99-avg-1.int8.onnx",
            "tokens.txt",
        ],
    ),
    "en": (
        "csukuangfj/sherpa-onnx-streaming-zipformer-en-2023-06-26",
        [
            "encoder-epoch-99-avg-1-chunk-16-left-128.int8.onnx",
            "decoder-epoch-99-avg-1-chunk-16-left-128.onnx",
            "joiner-epoch-99-avg-1-chunk-16-left-128.int8.onnx",
            "tokens.txt",
        ],
    ),
}


def resolve_sherpa_repo(configured: str, language: str) -> tuple[str, list[str] | None]:
    """Empty config repo = auto-select by persona language. An explicit custom
    repo gets no fixed file list — the provider falls back to glob resolution."""
    if configured:
        for repo, files in SHERPA_LANG_REPOS.values():
            if configured == repo:
                return repo, files
        return configured, None
    repo, files = SHERPA_LANG_REPOS.get(language, SHERPA_LANG_REPOS["zh"])
    return repo, files


def make_asr(cfg: Config, language: str = "zh") -> ASRProvider:
    name = cfg.asr.provider
    if name == "funasr_local":
        f = cfg.asr.funasr_local
        return FunASRLocal(
            model_dir=f.model_dir,
            device=f.device,
            language=f.language,
        )
    if name == "sherpa_stream":
        from .asr.sherpa_stream import SherpaStreamASR
        s = cfg.asr.sherpa_stream
        repo, files = resolve_sherpa_repo(s.repo, language)
        return SherpaStreamASR(repo=repo, num_threads=s.num_threads, model_files=files)
    raise ValueError(f"Unknown ASR provider: {name}")


def make_llm(cfg: Config) -> LLMProvider:
    name = cfg.llm.provider
    if name == "claude_haiku":
        c = cfg.llm.claude_haiku
        if not c.api_key:
            raise ValueError("Claude requires ANTHROPIC_API_KEY")
        return ClaudeHaikuProvider(
            api_key=c.api_key,
            model=c.model,
            max_tokens=c.max_tokens,
            temperature=c.temperature,
            enable_prompt_cache=c.enable_prompt_cache,
        )
    if name == "groq_chat":
        from .llm.groq_chat import GroqChatProvider

        g = cfg.llm.groq_chat
        if not g.api_key:
            raise ValueError("groq_chat requires GROQ_API_KEY")
        return GroqChatProvider(
            api_key=g.api_key,
            model=g.model,
            max_tokens=g.max_tokens,
            temperature=g.temperature,
        )
    raise ValueError(f"Unknown LLM provider: {name}")


def make_arbiter_llm(cfg: Config) -> tuple[LLMProvider, str] | None:
    """Dedicated LLM for organic gray-zone arbitration. Returns (provider,
    cost-tracking prefix); None = reuse the conversation LLM. Also returns
    None when groq is configured but GROQ_API_KEY is missing (degrade to
    reuse instead of crashing at startup)."""
    organic = getattr(cfg.bot, "organic", None)
    if organic is None or organic.arbiter_provider != "groq":
        return None
    g = cfg.llm.groq
    if not g.api_key:
        return None
    from .llm.groq import GroqProvider

    provider = GroqProvider(
        api_key=g.api_key, model=g.model,
        max_tokens=g.max_tokens, temperature=g.temperature,
    )
    prefix = "groq_" + g.model.split("/")[-1].replace("-", "_").replace(".", "_")
    return provider, prefix


def make_tts(cfg: Config, *, voice_id: str | None = None, persona=None) -> TTSProvider:
    """Build TTS provider.

    Priority:
      voice_id (explicit override) > persona.voice_id > cfg.tts.fish_audio_stream.voice_id

    When persona is given, all per-persona TTS knobs (temperature, top_p,
    speed, volume_db, latency, chunk_length) are pulled from it. Otherwise
    Fish API defaults are used.
    """
    name = cfg.tts.provider
    if name == "fish_audio_stream":
        f = cfg.tts.fish_audio_stream
        if not f.api_key:
            raise ValueError("Fish Audio requires FISH_AUDIO_API_KEY")
        effective_voice_id = voice_id or (persona.voice_id if persona else "") or f.voice_id
        if not effective_voice_id:
            raise ValueError("voice_id must be set in persona or config")
        return FishAudioStreamProvider(
            FishConfig(
                api_key=f.api_key,
                voice_id=effective_voice_id,
                fallback_voice_id=f.fallback_voice_id,
                model=f.model,
                latency=(persona.tts_latency if persona else f.latency),
                temperature=(persona.tts_temperature if persona else 0.7),
                top_p=(persona.tts_top_p if persona else 0.7),
                speed=(persona.tts_speed if persona else 1.0),
                volume_db=(persona.tts_volume_db if persona else 0.0),
                chunk_length=(persona.tts_chunk_length if persona else 200),
            )
        )
    raise ValueError(f"Unknown TTS provider: {name}")
