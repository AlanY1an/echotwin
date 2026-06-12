"""sherpa-onnx streaming zipformer spike — the fallback after the funasr paraformer gate failed.

(funasr paraformer-large online measured at RTF≈1.5, slower than realtime; all three gates failed.)

Same set of gates:
  ① single-chunk (600ms) decode P50 < 200ms (+P95)
  ② tail chunk (remaining decode) < 150ms
  ③ memory increase < 1.5GB
  ④ text matches SenseVoice ('现在几点了?')

Model: streaming zipformer bilingual zh-en (int8 quantized, ~100MB, downloaded from HF).
Run: .venv/bin/python -m scripts.spike_sherpa_streaming
"""
from __future__ import annotations

import resource
import statistics
import sys
import time
from pathlib import Path

import numpy as np
import soxr

REPO = "csukuangfj/sherpa-onnx-streaming-zipformer-bilingual-zh-en-2023-02-20"
CHUNK_SAMPLES = 9600  # 600ms @16k


def rss_mb() -> float:
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / (1024 * 1024)


def fetch_model() -> Path:
    from huggingface_hub import snapshot_download

    d = Path(
        snapshot_download(
            REPO,
            allow_patterns=[
                "encoder-epoch-99-avg-1.int8.onnx",
                "decoder-epoch-99-avg-1.onnx",
                "joiner-epoch-99-avg-1.int8.onnx",
                "tokens.txt",
            ],
        )
    )
    return d


def main() -> None:
    sys.path.insert(0, "tests")
    from harness._utils import load_pcm48k_mono

    pcm48 = load_pcm48k_mono("medium")
    audio16 = soxr.resample(pcm48.astype(np.float32) / 32768.0, 48000, 16000)
    print(f"fixture: {len(audio16)/16000:.2f}s audio @16k")

    mem0 = rss_mb()
    t0 = time.perf_counter()
    d = fetch_model()
    import sherpa_onnx

    recognizer = sherpa_onnx.OnlineRecognizer.from_transducer(
        tokens=str(d / "tokens.txt"),
        encoder=str(d / "encoder-epoch-99-avg-1.int8.onnx"),
        decoder=str(d / "decoder-epoch-99-avg-1.onnx"),
        joiner=str(d / "joiner-epoch-99-avg-1.int8.onnx"),
        num_threads=2,
        sample_rate=16000,
        feature_dim=80,
    )
    print(f"model ready in {time.perf_counter()-t0:.1f}s, rss +{rss_mb()-mem0:.0f}MB")

    stream = recognizer.create_stream()
    chunk_ms: list[float] = []
    n_full = len(audio16) // CHUNK_SAMPLES
    for i in range(n_full):
        chunk = audio16[i * CHUNK_SAMPLES:(i + 1) * CHUNK_SAMPLES]
        t = time.perf_counter()
        stream.accept_waveform(16000, chunk)
        while recognizer.is_ready(stream):
            recognizer.decode_stream(stream)
        ms = (time.perf_counter() - t) * 1000
        chunk_ms.append(ms)
        print(f"  chunk {i+1}/{n_full}: {ms:.0f}ms  partial={recognizer.get_result(stream)!r}")

    t = time.perf_counter()
    tail = audio16[n_full * CHUNK_SAMPLES:]
    if len(tail):
        stream.accept_waveform(16000, tail)
    # trailing silence flushes the decoder's internal buffer
    stream.accept_waveform(16000, np.zeros(int(0.4 * 16000), dtype=np.float32))
    stream.input_finished()
    while recognizer.is_ready(stream):
        recognizer.decode_stream(stream)
    final_ms = (time.perf_counter() - t) * 1000
    text = recognizer.get_result(stream)
    mem_delta = rss_mb() - mem0

    p50 = statistics.median(chunk_ms)
    p95 = sorted(chunk_ms)[max(0, int(len(chunk_ms) * 0.95) - 1)]
    print(f"\n=== Gate 结果(sherpa-onnx)===")
    print(f"① chunk P50={p50:.0f}ms P95={p95:.0f}ms (gate <200) {'✅' if p50 < 200 else '❌'}")
    print(f"② 尾块={final_ms:.0f}ms (gate <150) {'✅' if final_ms < 150 else '❌'}")
    print(f"③ 内存增量={mem_delta:.0f}MB (gate <1500) {'✅' if mem_delta < 1500 else '❌'}")
    print(f"④ 流式文本: {text!r}  (SenseVoice 对照: '现在几点了?')")


if __name__ == "__main__":
    main()
