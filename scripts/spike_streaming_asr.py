"""funasr streaming paraformer API spike — the gate for Phase 2.

Gate (all must pass before continuing with Phase 2):
  ① single-chunk (600ms) inference P50 < 200ms, with P95 reported
  ② is_final tail chunk < 150ms
  ③ process peak memory increase < 1.5GB
  ④ concatenated text semantically matches the SenseVoice result (printed side by side, judged manually)

Notes (review constraints):
  - must feed a float32 ndarray normalized to [-1,1] — int16 silently produces garbage features;
  - never pass a file path (load_utils forces is_final=True, breaking streaming);
  - model paraformer-zh-streaming is auto-downloaded via modelscope (~900MB, slow on first run).

Run: .venv/bin/python -m scripts.spike_streaming_asr
"""
from __future__ import annotations

import resource
import statistics
import sys
import time

import numpy as np
import soxr

CHUNK_SIZE = [0, 10, 5]          # 600ms chunk (10 × 60ms)
CHUNK_SAMPLES = 9600             # 600ms @ 16k
ENC_LOOK_BACK = 4
DEC_LOOK_BACK = 1


def rss_mb() -> float:
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / (1024 * 1024)


def main() -> None:
    sys.path.insert(0, "tests")
    from harness._utils import load_pcm48k_mono

    pcm48 = load_pcm48k_mono("medium")  # int16 ndarray @48k
    # 48k int16 → 16k float32 [-1,1] (same soxr as the production path)
    audio16 = soxr.resample(pcm48.astype(np.float32) / 32768.0, 48000, 16000)
    print(f"fixture: {len(audio16)/16000:.2f}s audio, {len(audio16)} samples @16k")

    mem_before = rss_mb()
    t0 = time.perf_counter()
    from funasr import AutoModel

    # hub="hf": the modelscope CDN keeps dropping on this network (2 failures, each >100 minutes);
    # the HuggingFace mirror funasr/paraformer-zh-streaming has identical content
    model = AutoModel(model="paraformer-zh-streaming", disable_update=True, hub="hf")
    load_s = time.perf_counter() - t0
    print(f"model loaded in {load_s:.1f}s, rss +{rss_mb()-mem_before:.0f}MB")

    cache: dict = {}
    partial = ""
    chunk_ms: list[float] = []
    n_full = len(audio16) // CHUNK_SAMPLES
    for i in range(n_full):
        chunk = audio16[i * CHUNK_SAMPLES:(i + 1) * CHUNK_SAMPLES]
        t = time.perf_counter()
        res = model.generate(
            input=chunk, cache=cache, is_final=False,
            chunk_size=CHUNK_SIZE,
            encoder_chunk_look_back=ENC_LOOK_BACK,
            decoder_chunk_look_back=DEC_LOOK_BACK,
        )
        ms = (time.perf_counter() - t) * 1000
        chunk_ms.append(ms)
        inc = res[0]["text"] if res else ""
        partial += inc
        print(f"  chunk {i+1}/{n_full}: {ms:.0f}ms  partial={partial!r}")

    tail = audio16[n_full * CHUNK_SAMPLES:]
    t = time.perf_counter()
    res = model.generate(
        input=tail if len(tail) else np.zeros(160, dtype=np.float32),
        cache=cache, is_final=True,
        chunk_size=CHUNK_SIZE,
        encoder_chunk_look_back=ENC_LOOK_BACK,
        decoder_chunk_look_back=DEC_LOOK_BACK,
    )
    final_ms = (time.perf_counter() - t) * 1000
    partial += res[0]["text"] if res else ""
    mem_delta = rss_mb() - mem_before

    p50 = statistics.median(chunk_ms)
    p95 = sorted(chunk_ms)[max(0, int(len(chunk_ms) * 0.95) - 1)]
    print(f"\n=== Gate 结果 ===")
    print(f"① chunk P50={p50:.0f}ms P95={p95:.0f}ms (gate <200) {'✅' if p50 < 200 else '❌'}")
    print(f"② is_final 尾块={final_ms:.0f}ms (gate <150) {'✅' if final_ms < 150 else '❌'}")
    print(f"③ 内存增量={mem_delta:.0f}MB (gate <1500) {'✅' if mem_delta < 1500 else '❌'}")
    print(f"④ 流式文本: {partial!r}")
    print(f"   SenseVoice 对照(已知): '现在几点了？'(medium fixture)")


if __name__ == "__main__":
    main()
