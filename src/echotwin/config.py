from __future__ import annotations
import os
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field


class DiscordCfg(BaseModel):
    token: str


class GreetingCfg(BaseModel):
    enabled: bool = True
    text: str | None = None


class FarewellCfg(BaseModel):
    enabled: bool = True
    text: str | None = None


class OrganicCfg(BaseModel):
    """Organic multi-user conversation (spec: dev-docs/2026-06-11-organic-multiparty).
    enabled=False = exact current behavior (rollback switch)."""
    enabled: bool = False
    ambient_context: bool = True       # eavesdropped transcripts go into context
    ambient_max_age_s: int = Field(default=120, ge=5)  # eavesdrop freshness window; older entries not injected
    conversation_window_s: int = 45    # active-conversation window
    clarify_cooldown_s: int = 60       # gray-zone clarification rate limit (per user)
    open_floor: bool = True            # volunteer when the floor is open
    open_floor_wait_ms: int = Field(default=1500, ge=200)
    clarify_texts: list[str] = Field(default_factory=list)  # empty = built-in defaults
    mention_reply_rate: float = Field(default=0.0, ge=0.0, le=1.0)  # phase 2; 0=off
    clarify_llm: bool = False          # phase 2: ask Haiku in gray zone (superseded by gray_zone=llm)
    gray_zone: str = Field(default="llm", pattern="^(llm|heuristic)$")
    # Gray-zone ruling: llm = LLM arbitration (semantic judgment, falls back to
    # heuristics on failure); heuristic = pure scoring
    arbiter_timeout_ms: int = Field(default=1500, ge=200)
    arbiter_max_per_min: int = Field(default=20, ge=1)  # rate fuse, over-rate falls back
    arbiter_provider: str = Field(default="", pattern="^(|groq)$")
    # "" = reuse the conversation LLM (Haiku); groq = standalone Groq client (llm.groq config)


class BotCfg(BaseModel):
    name: str
    active_persona: str
    history_window: int = 20
    idle_timeout_seconds: int = 120
    barge_in_mode: str = "addressee_only"
    # Live barge-in: duck playback to this volume the moment the interrupter's
    # voice is heard, and hard-stop once their speech sustains past the
    # threshold. Backchannels stay under the threshold → brief duck, no stop.
    barge_in_duck: float = Field(default=0.35, ge=0.0, le=1.0)
    barge_in_sustain_ms: int = Field(default=500, ge=100)
    listen_only_users: list[int] = Field(default_factory=list)
    wake_word_required: bool = False
    # Endpoint detection: how many ms of silence mark an utterance as finished
    # (was hardcoded 800). Lower = faster, but sentences with long pauses get
    # split in two (the continuation window can catch the second half).
    endpoint_silence_ms: int = Field(default=600, ge=100)
    endpoint_tick_ms: int = Field(default=100, ge=20)
    # Speculative ASR: pre-run recognition at this many ms of silence (only
    # meaningful if < endpoint_silence_ms); adopted directly at endpoint
    # confirmation if no new audio arrived → saves ~(endpoint − this value)
    # of serial waiting.
    speculative_asr: bool = True
    speculative_asr_silence_ms: int = Field(default=300, ge=100)
    # Speculative LLM (Phase 2, default off): pre-open the LLM stream once the
    # streaming-ASR partial is stable; adopt only if the endpoint text matches.
    # Wasted calls are still billed by Anthropic.
    speculative_llm: bool = False
    # Filler: smart = only fill on slow turns (filler_keywords hit); always / off
    filler_mode: str = "smart"
    filler_keywords: list[str] = Field(
        default_factory=lambda: ["天气", "几点", "时间", "日期", "查", "搜"]
    )
    organic: OrganicCfg = Field(default_factory=OrganicCfg)
    empty_channel_timeout_seconds: int = 300
    inactivity_timeout_seconds: int = 120  # stage 1 — bot speaks goodbye
    hard_timeout_seconds: int = 300         # stage 2 — force disconnect even if goodbye fails
    greeting: GreetingCfg = Field(default_factory=GreetingCfg)
    farewell: FarewellCfg = Field(default_factory=FarewellCfg)


class VADSileroCfg(BaseModel):
    model_dir: str = "models/silero_vad"
    threshold: float = 0.5
    threshold_low: float = 0.3
    min_silence_duration_ms: int = 250
    frame_window: int = 2
    preroll_ms: int = 300                  # ring-buffer length prepended to ASR on speech_start


class VADCfg(BaseModel):
    provider: str = "silero"
    silero: VADSileroCfg = Field(default_factory=VADSileroCfg)


class FunASRCfg(BaseModel):
    model_dir: str = "models/SenseVoiceSmall"
    device: str = "cpu"
    language: str = "auto"


class SherpaStreamCfg(BaseModel):
    # streaming zipformer bilingual zh-en, int8 (~100MB, auto-downloaded from HF).
    # Engine decision: funasr paraformer online failed all three spike gates on
    # M-series CPU (RTF≈1.5); sherpa measured chunk 15ms — see scripts/spike_*.py
    repo: str = ""  # empty = auto-select by persona language (zh: bilingual model, en: English zipformer)
    num_threads: int = 2


class ASRCfg(BaseModel):
    provider: str = "sherpa_stream"  # sherpa_stream (streaming, default) / funasr_local (batch)
    funasr_local: FunASRCfg = Field(default_factory=FunASRCfg)
    sherpa_stream: SherpaStreamCfg = Field(default_factory=SherpaStreamCfg)
    # In streaming mode, backfill emotion via a SenseVoice side-channel (lags one turn, doesn't block the reply)
    emotion_sidecar: bool = True


class ClaudeHaikuCfg(BaseModel):
    model: str = "claude-haiku-4-5"
    api_key: str | None = None
    max_tokens: int = 300
    temperature: float = 0.7
    enable_prompt_cache: bool = True


class GroqCfg(BaseModel):
    """Groq (OpenAI-compatible) — organic gray-zone arbitration."""
    api_key: str | None = None
    model: str = "qwen/qwen3-32b"
    max_tokens: int = 100
    temperature: float = 0.0


class GroqChatCfg(BaseModel):
    """OpenAI-compatible MAIN conversation brain (streaming + tool use) —
    Groq, Cerebras, or any /chat/completions endpoint. Separate from the
    arbiter's GroqCfg — different token/temperature needs."""
    api_key: str | None = None
    model: str = "qwen/qwen3-32b"
    max_tokens: int = 300
    temperature: float = 0.7
    base_url: str = "https://api.groq.com/openai/v1/chat/completions"
    reasoning_effort: str | None = None  # e.g. "low" for gpt-oss; None = provider default


class LLMCfg(BaseModel):
    provider: str = "claude_haiku"  # claude_haiku | groq_chat
    claude_haiku: ClaudeHaikuCfg = Field(default_factory=ClaudeHaikuCfg)
    groq: GroqCfg = Field(default_factory=GroqCfg)
    groq_chat: GroqChatCfg = Field(default_factory=GroqChatCfg)


class FishAudioStreamCfg(BaseModel):
    api_key: str | None = None
    voice_id: str = ""  # default empty — bot injects from active persona at TTS construction time
    fallback_voice_id: str = ""
    model: str = "s2-pro"
    latency: str = "low"


class TTSCfg(BaseModel):
    provider: str = "fish_audio_stream"
    fish_audio_stream: FishAudioStreamCfg


class AddresseeCfg(BaseModel):
    continuation_window_seconds: float = 15.0
    solo_channel_auto: bool = True


class GetWeatherCfg(BaseModel):
    default_city: str = "台北"
    # Legacy field — kept for backward compat, no longer used (we switched to wttr.in)
    qweather_api_key: str = ""


class ToolsCfg(BaseModel):
    enabled: list[str] = Field(default_factory=list)
    default_timezone: str = "Asia/Taipei"
    get_weather: GetWeatherCfg = Field(default_factory=GetWeatherCfg)


class CostCfg(BaseModel):
    daily_budget_usd: float = 5.0
    monthly_budget_usd: float = 50.0
    on_exceed: str = "warn"
    store_path: str = "data/costs.db"


class MonitoringCfg(BaseModel):
    http_port: int = 9090
    heartbeat_interval_seconds: int = 60


class Config(BaseModel):
    discord: DiscordCfg
    bot: BotCfg
    vad: VADCfg = Field(default_factory=VADCfg)
    asr: ASRCfg = Field(default_factory=ASRCfg)
    llm: LLMCfg = Field(default_factory=LLMCfg)
    tts: TTSCfg
    addressee: AddresseeCfg = Field(default_factory=AddresseeCfg)
    tools: ToolsCfg = Field(default_factory=ToolsCfg)
    cost: CostCfg = Field(default_factory=CostCfg)
    monitoring: MonitoringCfg = Field(default_factory=MonitoringCfg)


def _resolve_env(value: Any) -> Any:
    if isinstance(value, str):
        if value.startswith("env:"):
            return os.environ.get(value[4:], "")
        if value.startswith("${") and value.endswith("}"):
            return os.environ.get(value[2:-1], "")
    return value


def _walk(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {k: _walk(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_walk(v) for v in obj]
    return _resolve_env(obj)


def load_config(path: Path | str) -> Config:
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    raw = _walk(raw)
    return Config.model_validate(raw)


def missing_required_keys(cfg: Config) -> list[str]:
    """Names of required credentials that are empty — used for a friendly
    startup error instead of a raw discord.py traceback on a fresh clone."""
    missing = []
    if not (cfg.discord.token or "").strip():
        missing.append("DISCORD_TOKEN")
    if not (cfg.tts.fish_audio_stream.api_key or "").strip():
        missing.append("FISH_AUDIO_API_KEY")
    if not (cfg.llm.claude_haiku.api_key or "").strip():
        missing.append("ANTHROPIC_API_KEY")
    return missing
