"""sherpa-onnx streaming zipformer provider — recognizes audio as it arrives, near-zero wait at endpoint.

Engine decision record (2026-06-11): the spec's first choice was funasr
paraformer-zh-streaming, but the spike measured RTF≈1.5 on M-series CPU
(slower than realtime) — all three gates failed. The fallback, sherpa-onnx
streaming zipformer (int8), measured chunk 15ms / final flush 22ms / +647MB
— all gates passed. The two spike scripts (scripts/spike_*.py) are the verdict.

Concurrency contract: stream/recognizer are only touched inside serialized
executor tasks (class-level lock, shared by all instances of the same model);
feed_audio only touches the numpy buffer.
speculate() always refuses — the final flush (feeding 0.4s of silence +
input_finished) often emits the last word, skipping it would truncate the
tail; the final chunk itself is ~20ms, so speculation isn't needed.
"""
from __future__ import annotations

import asyncio
from typing import Optional

import numpy as np
from loguru import logger

from echotwin.audio.resampler import Resampler
from .base import ASRProvider, ASRMode, ASRResult

DEFAULT_REPO = "csukuangfj/sherpa-onnx-streaming-zipformer-bilingual-zh-en-2023-02-20"
_MODEL_FILES = [
    "encoder-epoch-99-avg-1.int8.onnx",
    "decoder-epoch-99-avg-1.onnx",
    "joiner-epoch-99-avg-1.int8.onnx",
    "tokens.txt",
]
_DECODE_MIN_SAMPLES = 1600   # 100ms @16k — partial refresh granularity
_FLUSH_SILENCE_S = 0.4       # silence to flush the decoder look-ahead before final


class SherpaStreamASR(ASRProvider):
    mode = ASRMode.STREAMING

    _recognizer_cache: dict = {}
    _cache_lock = asyncio.Lock()
    _infer_locks: dict = {}

    def __init__(
        self,
        repo: str = DEFAULT_REPO,
        num_threads: int = 2,
        keep_pcm_seconds: int = 30,
    ):
        self._repo = repo
        self._num_threads = num_threads
        self._recognizer = None
        self._stream = None
        self._keep_pcm_bytes = keep_pcm_seconds * 48000 * 2
        self.last_utterance_pcm: bytes = b""
        self._reset_buffers()

    def _reset_buffers(self) -> None:
        self._resampler = Resampler(48000, 16000)
        self._partial = ""
        self._pending = np.zeros(0, dtype=np.float32)
        self._raw48 = bytearray()
        self._received = 0
        self._inflight: asyncio.Task | None = None

    def _infer_lock(self) -> asyncio.Lock:
        lock = SherpaStreamASR._infer_locks.get(self._repo)
        if lock is None:
            lock = SherpaStreamASR._infer_locks[self._repo] = asyncio.Lock()
        return lock

    async def preload(self) -> None:
        if self._recognizer is not None:
            return
        async with SherpaStreamASR._cache_lock:
            cached = SherpaStreamASR._recognizer_cache.get(self._repo)
            if cached is not None:
                self._recognizer = cached
                return
            logger.info(f"Loading sherpa-onnx streaming zipformer ({self._repo})…")
            loop = asyncio.get_event_loop()

            def _load():
                from pathlib import Path
                from huggingface_hub import snapshot_download
                import sherpa_onnx

                d = Path(snapshot_download(self._repo, allow_patterns=_MODEL_FILES))
                return sherpa_onnx.OnlineRecognizer.from_transducer(
                    tokens=str(d / "tokens.txt"),
                    encoder=str(d / _MODEL_FILES[0]),
                    decoder=str(d / _MODEL_FILES[1]),
                    joiner=str(d / _MODEL_FILES[2]),
                    num_threads=self._num_threads,
                    sample_rate=16000,
                    feature_dim=80,
                )

            recognizer = await loop.run_in_executor(None, _load)
            SherpaStreamASR._recognizer_cache[self._repo] = recognizer
            self._recognizer = recognizer
            logger.info("sherpa-onnx streaming zipformer loaded (shared)")

    async def open(self) -> None:
        await self.preload()
        self._reset_buffers()
        self._stream = self._recognizer.create_stream()

    async def feed_audio(self, pcm_48k_mono: bytes) -> None:
        self._received += len(pcm_48k_mono)
        self._raw48.extend(pcm_48k_mono)
        if len(self._raw48) > self._keep_pcm_bytes:
            del self._raw48[: len(self._raw48) - self._keep_pcm_bytes]
        pcm16 = self._resampler.feed(pcm_48k_mono)
        if pcm16:
            f32 = np.frombuffer(pcm16, dtype=np.int16).astype(np.float32) / 32768.0
            self._pending = np.concatenate([self._pending, f32])
        if len(self._pending) >= _DECODE_MIN_SAMPLES and (
            self._inflight is None or self._inflight.done()
        ):
            samples, self._pending = self._pending, np.zeros(0, dtype=np.float32)
            self._inflight = asyncio.create_task(self._decode(samples))

    async def _decode(self, samples: np.ndarray) -> None:
        """Serialized decode task: after handling the samples at hand, loop back
        to drain pending samples that piled up meanwhile — otherwise the last
        batch waits for the next feed to be decoded (and with no next feed it
        is stuck forever)."""
        stream, recognizer = self._stream, self._recognizer
        if stream is None:
            return
        loop = asyncio.get_event_loop()
        while True:
            async with self._infer_lock():

                def _run(buf=samples):
                    stream.accept_waveform(16000, buf)
                    while recognizer.is_ready(stream):
                        recognizer.decode_stream(stream)
                    return recognizer.get_result(stream)

                try:
                    self._partial = await loop.run_in_executor(None, _run)
                except Exception as e:
                    logger.error(f"sherpa streaming decode failed: {e}")
                    return
            if len(self._pending) >= _DECODE_MIN_SAMPLES and self._stream is stream:
                samples, self._pending = self._pending, np.zeros(0, dtype=np.float32)
                continue
            return

    def partial_text(self) -> str:
        return self._partial

    def pipeline_drained(self) -> bool:
        return (
            len(self._pending) < _DECODE_MIN_SAMPLES
            and (self._inflight is None or self._inflight.done())
        )

    def buffered_bytes(self) -> int:
        return self._received

    async def speculate(self) -> tuple[Optional[ASRResult], int]:
        return (None, -1)  # final flush often emits the last word; final chunk ~20ms, no speculation needed

    def drop_buffer(self) -> None:
        self._reset_buffers()
        if self._recognizer is not None:
            self._stream = self._recognizer.create_stream()

    async def end_utterance(self) -> Optional[ASRResult]:
        if self._inflight is not None and not self._inflight.done():
            try:
                await self._inflight
            except Exception:
                pass
        stream, recognizer = self._stream, self._recognizer
        if stream is None:
            return None
        pending = self._pending

        async with self._infer_lock():
            loop = asyncio.get_event_loop()

            def _finalize():
                if len(pending):
                    stream.accept_waveform(16000, pending)
                stream.accept_waveform(
                    16000, np.zeros(int(_FLUSH_SILENCE_S * 16000), dtype=np.float32)
                )
                stream.input_finished()
                while recognizer.is_ready(stream):
                    recognizer.decode_stream(stream)
                return recognizer.get_result(stream)

            try:
                text = await loop.run_in_executor(None, _finalize)
            except Exception as e:
                logger.error(f"sherpa final decode failed: {e}")
                text = self._partial

        self.last_utterance_pcm = bytes(self._raw48)
        self._reset_buffers()
        self._stream = recognizer.create_stream()
        text = (text or "").strip()
        if not text:
            return None
        return ASRResult(text=text, language="zh", emotion="NEUTRAL", is_final=True)

    async def close(self) -> None:
        if self._inflight is not None and not self._inflight.done():
            self._inflight.cancel()
        self._reset_buffers()
        self._stream = None
