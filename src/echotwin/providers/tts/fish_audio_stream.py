"""Fish Audio /v1/tts/live streaming WebSocket TTS provider.

Wire format: MessagePack (verified empirically, see _test_fish_protocol.py).
Audio format: Ogg/Opus 48kHz mono.
Per-turn WS: open() + push_text()*N + flush()*M + end_turn() → reader sees 'finish' → close.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import AsyncIterator, Awaitable, Callable

import msgpack
import websockets
from loguru import logger

from .base import TTSProvider


class FishAudioError(Exception):
    pass


class FishConnectError(FishAudioError):
    pass


class FishProtocolError(FishAudioError):
    pass


class FishTimeoutError(FishAudioError):
    def __init__(self, stage: str):
        super().__init__(f"Fish Audio timeout at stage={stage}")
        self.stage = stage


@dataclass
class FishConfig:
    api_key: str
    voice_id: str
    fallback_voice_id: str = ""
    model: str = "s2-pro"
    latency: str = "low"
    base_url: str = "wss://api.fish.audio"
    connect_timeout: float = 5.0
    first_audio_timeout: float = 8.0
    idle_timeout: float = 5.0
    # Per-persona TTS tuning (passed through to Fish API in start payload)
    temperature: float = 0.7        # 0-1 voice consistency
    top_p: float = 0.7              # 0-1 sampling diversity
    speed: float = 1.0              # prosody.speed multiplier
    volume_db: float = 0.0          # prosody.volume in dB
    chunk_length: int = 200         # generation chunk size


class FishAudioStreamProvider(TTSProvider):
    def __init__(
        self,
        cfg: FishConfig,
        on_voice_fallback: Callable[[str], Awaitable[None]] | None = None,
    ):
        self._cfg = cfg
        self._on_voice_fallback = on_voice_fallback
        self._ws: websockets.WebSocketClientProtocol | None = None
        self._packet_queue: asyncio.Queue[bytes | None] = asyncio.Queue()
        self._reader_task: asyncio.Task[None] | None = None
        self._using_fallback = False
        # Server-side protocol error (e.g. invalid reference_id). Fish reports
        # these asynchronously via finish(reason != "stop"), so they can't be
        # raised from open(); callers seeing zero audio should check this.
        self.last_error: str | None = None

    async def open(self) -> None:
        from echotwin.utils.retry import async_retry

        async def _do_open():
            await self._open_with_voice(self._cfg.voice_id)

        try:
            await async_retry(
                _do_open,
                attempts=3,
                base_delay=0.5,
                backoff=2.0,
                retry_on=(FishConnectError, ConnectionError, TimeoutError, asyncio.TimeoutError, OSError),
                name="tts.open",
            )
        except FishProtocolError as e:
            msg = str(e).lower()
            if ("voice" in msg or "reference" in msg or "not found" in msg) and self._cfg.fallback_voice_id:
                logger.warning(
                    f"Voice {self._cfg.voice_id} failed: {e}. Falling back to {self._cfg.fallback_voice_id}"
                )
                await self._open_with_voice(self._cfg.fallback_voice_id)
                self._using_fallback = True
                if self._on_voice_fallback:
                    asyncio.create_task(self._on_voice_fallback(self._cfg.voice_id))
            else:
                raise

    async def _open_with_voice(self, voice_id: str) -> None:
        url = f"{self._cfg.base_url.rstrip('/')}/v1/tts/live"
        headers = {
            "Authorization": f"Bearer {self._cfg.api_key}",
            "model": self._cfg.model,
        }
        try:
            self._ws = await asyncio.wait_for(
                websockets.connect(url, additional_headers=headers),
                timeout=self._cfg.connect_timeout,
            )
        except (asyncio.TimeoutError, OSError, websockets.WebSocketException) as e:
            raise FishConnectError(str(e)) from e

        request_body: dict = {
            "text": "",
            "reference_id": voice_id,
            "format": "opus",
            "temperature": self._cfg.temperature,
            "top_p": self._cfg.top_p,
            "chunk_length": self._cfg.chunk_length,
            "prosody": {
                "speed": self._cfg.speed,
                "volume": self._cfg.volume_db,
            },
        }
        if self._cfg.latency != "normal":
            request_body["latency"] = self._cfg.latency
        start_payload: dict = {"event": "start", "request": request_body}

        try:
            await self._ws.send(msgpack.packb(start_payload, use_bin_type=True))
        except Exception as e:
            # Close the freshly-opened socket before raising, otherwise the
            # retry in open() overwrites self._ws and leaks the old connection.
            try:
                await self._ws.close()
            except Exception:
                pass
            self._ws = None
            raise FishConnectError(f"send start failed: {e}") from e

        self._reader_task = asyncio.create_task(self._read_loop())

    async def _read_loop(self) -> None:
        assert self._ws is not None
        try:
            async for msg in self._ws:
                if isinstance(msg, str):
                    continue
                try:
                    evt = msgpack.unpackb(msg, raw=False)
                except Exception:
                    continue
                if not isinstance(evt, dict):
                    continue
                ev = evt.get("event")
                if ev == "audio":
                    audio = evt.get("audio")
                    if isinstance(audio, (bytes, bytearray)):
                        await self._packet_queue.put(bytes(audio))
                elif ev == "finish":
                    if evt.get("reason") != "stop":
                        self.last_error = str(evt.get("message") or evt)
                        logger.error(
                            f"Fish protocol error (voice_id={self._cfg.voice_id}): "
                            f"{self.last_error}"
                        )
                    await self._packet_queue.put(None)
                    return
        except websockets.ConnectionClosed:
            await self._packet_queue.put(None)
        except Exception as e:
            logger.warning(f"Fish read loop error: {e}")
            await self._packet_queue.put(None)

    async def push_text(self, text: str) -> None:
        if self._ws is None:
            raise FishAudioError("WS not open; call open() first")
        await self._ws.send(msgpack.packb({"event": "text", "text": text}, use_bin_type=True))

    async def flush(self) -> None:
        if self._ws is None:
            return
        try:
            await self._ws.send(msgpack.packb({"event": "flush"}, use_bin_type=True))
        except websockets.ConnectionClosed:
            pass

    async def end_turn(self) -> None:
        if self._ws is None:
            return
        try:
            await self._ws.send(msgpack.packb({"event": "stop"}, use_bin_type=True))
        except websockets.ConnectionClosed:
            pass

    async def packets(self) -> AsyncIterator[bytes]:
        while True:
            pkt = await self._packet_queue.get()
            if pkt is None:
                return
            yield pkt

    async def cancel(self) -> None:
        if self._reader_task and not self._reader_task.done():
            self._reader_task.cancel()
        if self._ws:
            try:
                await self._ws.close()
            except Exception:
                pass
        self._ws = None
        await self._packet_queue.put(None)

    async def close(self) -> None:
        await self.cancel()
