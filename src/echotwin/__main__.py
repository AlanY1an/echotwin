from __future__ import annotations
import os
import sys
from pathlib import Path

import discord
from dotenv import load_dotenv
from loguru import logger

from .bot import VoiceAgentBot
from .config import load_config, missing_required_keys
from .logging_setup import setup_logging


def _load_opus():
    """Find and load libopus on macOS / Linux."""
    if discord.opus.is_loaded():
        return
    # Common paths on macOS (homebrew) and Linux
    candidates = [
        "/opt/homebrew/lib/libopus.dylib",         # Apple Silicon brew
        "/opt/homebrew/Cellar/opus/1.6.1/lib/libopus.dylib",
        "/usr/local/lib/libopus.dylib",            # Intel Mac brew
        "/usr/lib/x86_64-linux-gnu/libopus.so.0",  # Debian/Ubuntu
        "/usr/lib64/libopus.so.0",                 # RHEL/CentOS
        "libopus.so.0",
        "libopus",
    ]
    for path in candidates:
        try:
            discord.opus.load_opus(path)
            if discord.opus.is_loaded():
                logger.info(f"libopus loaded from {path}")
                return
        except Exception:
            continue
    logger.warning("libopus not loaded (voice receive may fail)")


def main() -> int:
    setup_logging(os.environ.get("LOG_LEVEL", "INFO"))
    _load_opus()

    # Apply DAVE decryption patch to discord-ext-voice-recv
    from .audio.dave_patch import apply_dave_patch
    apply_dave_patch()

    # Defensive patches against voice_recv 0.5.2a179 bugs (silent voice WS
    # death from AttributeError in _remove_ssrc when _reader is MISSING)
    from .audio.voice_recv_patch import apply_voice_recv_patches
    apply_voice_recv_patches()

    # Route voice_recv DEBUG logs to a separate file so we can see internal
    # state when voice connection silently dies (without flooding the main log).
    import logging
    Path("data").mkdir(parents=True, exist_ok=True)
    voice_recv_log = logging.FileHandler("data/voice_recv.log", mode="w")
    voice_recv_log.setLevel(logging.DEBUG)
    voice_recv_log.setFormatter(
        logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    )
    for name in ("discord.ext.voice_recv", "discord.voice_client", "discord.voice_state"):
        lg = logging.getLogger(name)
        lg.setLevel(logging.DEBUG)
        lg.addHandler(voice_recv_log)
    logger.info("voice_recv/voice_client DEBUG logs → data/voice_recv.log")

    env_file = Path(".env")
    if env_file.exists():
        load_dotenv(env_file)

    config_path = Path(os.environ.get("CONFIG_PATH", "config.yaml"))
    if not config_path.exists():
        logger.error(f"Config not found: {config_path}")
        logger.error("Copy config.example.yaml to config.yaml and edit.")
        return 1

    config = load_config(config_path)

    missing = missing_required_keys(config)

    if missing:

        logger.error(

            "Missing required credentials: "

            + ", ".join(missing)

            + " — fill them in .env (cp .env.example .env), see docs/SETUP.md"

        )

        return 1

    if not config.discord.token:
        logger.error("DISCORD_TOKEN not set (check .env)")
        return 1

    bot = VoiceAgentBot(config)
    bot.run(config.discord.token, log_handler=None)
    return 0


if __name__ == "__main__":
    sys.exit(main())
