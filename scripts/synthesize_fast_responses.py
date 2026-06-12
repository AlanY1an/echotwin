"""Pre-synthesize fast-response audio for the active persona.

Usage: python -m scripts.synthesize_fast_responses [config.yaml]
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from dotenv import load_dotenv

from echotwin.config import load_config
from echotwin.persona import load_persona
from echotwin.providers.factory import make_tts
from echotwin.wake_word.fast_response import FastResponseCache


async def main():
    load_dotenv()
    cfg_path = sys.argv[1] if len(sys.argv) > 1 else "config.yaml"
    cfg = load_config(cfg_path)
    persona = load_persona(Path("prompts") / "personas" / f"{cfg.bot.active_persona}.md")
    cache = FastResponseCache(
        persona_id=persona.id,
        voice_id=persona.voice_id,
        responses=persona.fast_responses,
        data_dir=Path("data"),
    )

    async def synth(text: str) -> bytes:
        tts = make_tts(cfg, persona=persona)
        await tts.open()
        await tts.push_text(text)
        await tts.flush()
        await tts.end_turn()
        chunks: list[bytes] = []
        async for c in tts.packets():
            chunks.append(c)
        await tts.close()
        return b"".join(chunks)

    await cache.ensure_synthesized(synth)
    print(f"[done] cache at {cache.dir}")


if __name__ == "__main__":
    asyncio.run(main())
