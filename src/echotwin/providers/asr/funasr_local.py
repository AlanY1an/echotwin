"""Local FunASR (SenseVoiceSmall) ASR provider — batch mode."""
from __future__ import annotations

import asyncio
from typing import Optional

import numpy as np
import opuslib_next
from loguru import logger

from echotwin.audio.resampler import Resampler
from .base import ASRProvider, ASRMode, ASRResult
from .sensevoice_parse import parse_sensevoice_output


class FunASRLocal(ASRProvider):
    """SenseVoiceSmall via FunASR. Batch: collect PCM → infer.

    The underlying FunASR model is **shared across all instances** (cached at
    the class level). Each instance has its own pcm_buffer for per-user isolation.
    """

    mode = ASRMode.BATCH

    # Class-level model cache: keyed by (model_dir, device).
    _model_cache: dict = {}
    _model_cache_lock = asyncio.Lock()
    # Class-level inference locks, same key: the cached model object is NOT
    # documented thread-safe, so ALL generate() calls against one model must
    # serialize — across users, speculation and (future) emotion sidecar.
    _infer_locks: dict = {}

    def __init__(
        self,
        model_dir: str = "models/SenseVoiceSmall",
        device: str = "cpu",
        language: str = "auto",
    ):
        self._model_dir = model_dir
        self._device = device
        self._language = language
        self._model = None
        self._opus_decoder: Optional[opuslib_next.Decoder] = None
        self._pcm_buffer = bytearray()  # 48k PCM int16 mono

    async def preload(self) -> None:
        if self._model is not None:
            return
        key = (self._model_dir, self._device)
        async with FunASRLocal._model_cache_lock:
            cached = FunASRLocal._model_cache.get(key)
            if cached is not None:
                self._model = cached
                return
            logger.info(f"Loading FunASR SenseVoice from {self._model_dir}…")
            from funasr import AutoModel  # local import; heavy
            loop = asyncio.get_event_loop()
            model = await loop.run_in_executor(
                None,
                lambda: AutoModel(
                    model=self._model_dir,
                    device=self._device,
                    disable_update=True,
                ),
            )
            FunASRLocal._model_cache[key] = model
            self._model = model
            logger.info("FunASR SenseVoiceSmall loaded (shared)")

    async def open(self) -> None:
        await self.preload()
        self._pcm_buffer.clear()

    async def feed_audio(self, pcm_48k_mono: bytes) -> None:
        """Receive 48kHz mono int16 PCM (already decoded + downmixed)."""
        self._pcm_buffer.extend(pcm_48k_mono)

    def _infer_lock(self) -> asyncio.Lock:
        key = (self._model_dir, self._device)
        lock = FunASRLocal._infer_locks.get(key)
        if lock is None:
            lock = FunASRLocal._infer_locks[key] = asyncio.Lock()
        return lock

    async def _infer(self, pcm_snapshot: bytes) -> Optional[ASRResult]:
        """Inference on an immutable 48k PCM snapshot. LOCK-FREE — callers
        must hold _infer_lock() (double-acquire on asyncio.Lock deadlocks)."""
        # 48k → 16k for SenseVoice
        resampler = Resampler(48000, 16000)
        pcm_16k = resampler.feed(pcm_snapshot) + resampler.flush()

        audio = np.frombuffer(pcm_16k, dtype=np.int16).astype(np.float32) / 32768.0
        if audio.size < 1600:  # < 100ms,too short
            return None

        loop = asyncio.get_event_loop()
        try:
            result = await loop.run_in_executor(
                None,
                lambda: self._model.generate(
                    input=audio,
                    cache={},
                    language=self._language,
                    use_itn=True,
                    batch_size_s=60,
                ),
            )
        except Exception as e:
            logger.error(f"FunASR inference failed: {e}")
            return None

        if not result:
            return None
        raw_text = result[0].get("text", "")
        if not raw_text.strip():
            return None
        parsed = parse_sensevoice_output(raw_text)
        return ASRResult(
            text=parsed["content"],
            language=parsed["language"],
            emotion=parsed["emotion"],
            is_final=True,
        )

    async def end_utterance(self) -> Optional[ASRResult]:
        if len(self._pcm_buffer) < 1000:
            self._pcm_buffer.clear()
            return None
        # Snapshot + clear SYNCHRONOUSLY before the first await — feed_audio
        # for the user's NEXT utterance may interleave once we suspend.
        snapshot = bytes(self._pcm_buffer)
        self._pcm_buffer.clear()
        async with self._infer_lock():
            return await self._infer(snapshot)

    def buffered_bytes(self) -> int:
        return len(self._pcm_buffer)

    async def speculate(self) -> tuple[Optional[ASRResult], int]:
        # 100ms @48k int16 — a noise blip isn't worth an inference pass
        if len(self._pcm_buffer) < 9600:
            return (None, -1)
        snapshot = bytes(self._pcm_buffer)  # NOT cleared — speculation only
        fed = len(self._pcm_buffer)
        async with self._infer_lock():
            result = await self._infer(snapshot)
        return (result, fed)

    def drop_buffer(self) -> None:
        self._pcm_buffer.clear()

    async def close(self) -> None:
        self._opus_decoder = None
        self._pcm_buffer.clear()
