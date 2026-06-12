"""Filler phrases: right after endpoint confirmation, play a cached phrase
to cover LLM thinking time (perceived <1s).

The filler's opus packets are pre-fed into this turn's frame_queue — the
filler plays first and the LLM audio appends seamlessly. No second playback
path is introduced; barge-in / cleanup semantics stay unchanged.
"""
from __future__ import annotations

import queue as sync_queue
from pathlib import Path

from loguru import logger

from echotwin.audio.ogg_demux import OggDemuxer


def should_play_filler(user_text: str, mode: str, keywords: list[str]) -> bool:
    if mode == "off":
        return False
    if mode == "always":
        return True
    # smart: only fill on turns predicted to be slow (tool-call keywords → one extra LLM round-trip)
    return any(kw in user_text for kw in keywords)


def enqueue_filler_packets(ogg_path: Path, frame_queue: sync_queue.Queue) -> int:
    """Pre-feed the cached OGG's opus packets into the playback queue; returns packet count.

    Best-effort: any failure returns 0 and the turn proceeds normally
    (the filler is only a perceived-latency optimization).
    Synchronously reading a small file (~20KB) is acceptable.
    """
    try:
        demux = OggDemuxer()
        demux.feed(ogg_path.read_bytes())
        n = 0
        for pkt in list(demux.packets()) + list(demux.flush()):
            try:
                frame_queue.put_nowait(pkt)
                n += 1
            except sync_queue.Full:
                break
        return n
    except Exception as e:
        logger.warning(f"[filler] enqueue failed: {e}")
        return 0
