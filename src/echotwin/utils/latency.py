"""Per-turn latency journey — one log line showing where each turn's time went."""
from __future__ import annotations

import time


class LatencyJourney:
    """Monotonic stage timestamps for one utterance→reply turn.

    Marks may arrive out of append-order (drain task vs main loop run
    concurrently), so line() sorts by timestamp before computing deltas.
    """

    def __init__(self, first_stage: str = "start"):
        self._stages: list[tuple[str, float]] = [(first_stage, time.monotonic())]

    def mark(self, stage: str) -> None:
        self._stages.append((stage, time.monotonic()))

    def line(self) -> str:
        stages = sorted(self._stages, key=lambda s: s[1])
        parts = [
            f"{prev[0]}→{cur[0]}={int((cur[1] - prev[1]) * 1000)}ms"
            for prev, cur in zip(stages, stages[1:])
        ]
        total = int((stages[-1][1] - stages[0][1]) * 1000)
        return "[latency] " + " ".join(parts) + f" total={total}ms"
