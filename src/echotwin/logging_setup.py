from __future__ import annotations
import re
import sys

from loguru import logger

PATTERNS = [
    (re.compile(r"Bearer\s+[\w\-_.]+"), "Bearer ***"),
    (re.compile(r"\bsk-ant-[\w\-]{8,}\b"), "sk-ant-***"),
    (re.compile(r"\bsk-[\w\-]{8,}\b"), "sk-***"),
    (
        re.compile(
            r"(DISCORD_TOKEN|FISH_AUDIO_API_KEY|ANTHROPIC_API_KEY|GROQ_API_KEY)\s*[=:]\s*\S+",
            re.IGNORECASE,
        ),
        r"\1=***",
    ),
]


def sanitize(s: str) -> str:
    for pat, sub in PATTERNS:
        s = pat.sub(sub, s)
    return s


def _patcher(record):
    record["message"] = sanitize(record["message"])


def setup_logging(level: str = "INFO", log_dir: str = "data/logs") -> None:
    logger.remove()
    logger.add(
        sys.stderr,
        level=level,
        format="<green>{time:HH:mm:ss.SSS}</green> | <level>{level: <7}</level> | "
        "<cyan>{name}</cyan>:<cyan>{line}</cyan> | <level>{message}</level>",
    )
    # Persist to disk: logs survive even when the terminal is closed;
    # [latency]/[organic]/[stats] diagnostics can be audited afterwards.
    # data/ is gitignored; rotation prevents bloat.
    logger.add(
        f"{log_dir}/echotwin_{{time:YYYY-MM-DD}}.log",
        level=level,
        rotation="20 MB",
        retention=10,
        encoding="utf-8",
        format="{time:HH:mm:ss.SSS} | {level: <7} | {name}:{line} | {message}",
    )
    logger.configure(patcher=_patcher)
