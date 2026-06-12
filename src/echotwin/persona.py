"""Persona system: YAML frontmatter loader + system prompt rendering."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import frontmatter

from echotwin.i18n import prompts as _locale


class PersonaError(ValueError):
    """Raised when a persona file is missing required fields or malformed."""


@dataclass(frozen=True)
class Persona:
    id: str
    name: str
    voice_id: str
    wake_words: list[str]
    fast_responses: list[str]
    limit_exceeded_text: str
    farewell_text: str
    system_prompt: str
    # Language of this persona (zh | en): selects the base template, the
    # arbiter prompt, and every default voice line / LLM-facing string.
    language: str = "zh" 
    # Fish Audio TTS per-persona tuning. All optional with sane defaults.
    tts_temperature: float = 0.7        # 0-1, voice consistency (lower = more uniform)
    tts_top_p: float = 0.7              # 0-1, sampling diversity
    tts_speed: float = 1.0              # 0.5-2.0, prosody.speed (1.2 = 20% faster)
    tts_volume_db: float = 0.0          # -10..+10, prosody.volume in dB
    tts_latency: str = "low"            # "low" | "normal"
    tts_chunk_length: int = 200         # 50-300, generation chunk size
    # Filler phrases: played right after endpoint confirmation on slow turns
    # (e.g. tool calls) to cover LLM thinking time. Empty tuple = use built-in
    # default texts (bot.DEFAULT_FILLERS), still synthesized in the persona voice.
    fillers: tuple = ()




def _coerce_float(meta: dict, key: str, default: float, lo: float, hi: float, persona_name: str) -> float:
    v = meta.get(key, default)
    try:
        v = float(v)
    except (TypeError, ValueError):
        raise PersonaError(f"persona {persona_name}: {key} must be a number")
    if not lo <= v <= hi:
        raise PersonaError(
            f"persona {persona_name}: {key}={v} out of range [{lo}, {hi}]"
        )
    return v


def _coerce_int(meta: dict, key: str, default: int, lo: int, hi: int, persona_name: str) -> int:
    v = meta.get(key, default)
    try:
        v = int(v)
    except (TypeError, ValueError):
        raise PersonaError(f"persona {persona_name}: {key} must be an integer")
    if not lo <= v <= hi:
        raise PersonaError(
            f"persona {persona_name}: {key}={v} out of range [{lo}, {hi}]"
        )
    return v


# Backwards-compat alias; the per-language tables live in i18n/prompts.py
EMOTION_HELP = _locale.EMOTION_HELP["zh"]


def load_persona(path: Path | str) -> Persona:
    """Parse persona file: YAML frontmatter (metadata) + body (system prompt)."""
    path = Path(path)
    if not path.exists():
        raise PersonaError(f"persona file not found: {path}")

    try:
        post = frontmatter.load(str(path))
    except Exception as e:
        raise PersonaError(f"persona {path.name}: failed to parse frontmatter: {e}")

    meta = post.metadata or {}
    body = (post.content or "").strip()

    language = str(meta.get("language", "zh")).lower()
    if language not in _locale.LANGS:
        raise PersonaError(
            f"persona {path.name}: language must be one of {_locale.LANGS}"
        )

    name = meta.get("name")
    if not name:
        raise PersonaError(f"persona {path.name}: missing required field 'name'")
    voice_id = meta.get("voice_id")
    if not voice_id:
        raise PersonaError(f"persona {path.name}: missing required field 'voice_id'")

    wake_words = meta.get("wake_words")
    if wake_words is None:
        wake_words = [str(name)]
    elif not isinstance(wake_words, list) or not all(isinstance(w, str) for w in wake_words):
        raise PersonaError(f"persona {path.name}: wake_words must be list[str]")
    if not wake_words:
        wake_words = [str(name)]

    fast_responses = meta.get("fast_responses")
    if fast_responses is None:
        fast_responses = list(_locale.DEFAULT_FAST_RESPONSES[language])
    elif not isinstance(fast_responses, list) or not all(isinstance(r, str) for r in fast_responses):
        raise PersonaError(f"persona {path.name}: fast_responses must be list[str]")

    fillers = meta.get("fillers")
    if fillers is None:
        fillers = ()
    elif not isinstance(fillers, list) or not all(isinstance(f, str) for f in fillers):
        raise PersonaError(f"persona {path.name}: fillers must be list[str]")
    else:
        fillers = tuple(fillers)

    # Fish Audio TTS knobs — validated ranges, sensible defaults
    tts_latency = str(meta.get("tts_latency", "low")).lower()
    if tts_latency not in {"low", "normal"}:
        raise PersonaError(
            f"persona {path.name}: tts_latency must be 'low' or 'normal'"
        )

    return Persona(
        id=path.stem,
        name=str(name),
        voice_id=str(voice_id),
        wake_words=list(wake_words),
        fast_responses=list(fast_responses),
        limit_exceeded_text=str(
            meta.get("limit_exceeded_text") or _locale.DEFAULT_LIMIT_TEXT[language]
        ),
        farewell_text=str(meta.get("farewell_text") or _locale.DEFAULT_FAREWELL[language]),
        system_prompt=body,
        language=language,
        tts_temperature=_coerce_float(meta, "tts_temperature", 0.7, 0.0, 1.0, path.name),
        tts_top_p=_coerce_float(meta, "tts_top_p", 0.7, 0.0, 1.0, path.name),
        tts_speed=_coerce_float(meta, "tts_speed", 1.0, 0.5, 2.0, path.name),
        tts_volume_db=_coerce_float(meta, "tts_volume_db", 0.0, -10.0, 10.0, path.name),
        tts_latency=tts_latency,
        tts_chunk_length=_coerce_int(meta, "tts_chunk_length", 200, 50, 300, path.name),
        fillers=fillers,
    )


def render_system_prompt(
    persona: Persona | str,
    bot_name: str,
    *,
    channel_name: str = "",
    members_online: int = 1,
    prompts_dir: Path | str = "prompts",
) -> str:
    """Compose final system prompt from base_template + persona body.

    Accepts either a Persona object or a persona id string (loaded from disk).
    """
    base_dir = Path(prompts_dir)

    if isinstance(persona, str):
        persona_obj = load_persona(base_dir / "personas" / f"{persona}.md")
    else:
        persona_obj = persona

    lang = getattr(persona_obj, "language", "zh")
    template_name = "base_template.md" if lang == "zh" else f"base_template.{lang}.md"
    template_path = base_dir / template_name
    if not template_path.exists():
        template_path = base_dir / "base_template.md"
    base = template_path.read_text(encoding="utf-8")
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    return base.format(
        persona=persona_obj.system_prompt,
        bot_name=persona_obj.name if isinstance(persona, Persona) else bot_name,
        current_time=now,
        channel_name=channel_name,
        members_online=members_online,
        emotion_tags_help=_locale.EMOTION_HELP[lang],
    )


def list_personas(prompts_dir: Path | str = "prompts") -> list[str]:
    p = Path(prompts_dir) / "personas"
    if not p.exists():
        return []
    return sorted(
        f.stem for f in p.glob("*.md")
        if not f.stem.startswith("_")
    )
