#!/usr/bin/env python3
"""Demo HUD — a clean, colorful live view of the voice pipeline for screen
recordings. Tails the newest bot log and renders only the events a viewer
cares about: what was heard, tools fired, what the bot said, and where the
milliseconds went.

Usage:
  .venv/bin/python scripts/demo_hud.py            # live tail (for recording)
  .venv/bin/python scripts/demo_hud.py --replay   # re-render today's log (styling check)

Recording tips: dark terminal theme, bump the font (Cmd+'+'), narrow window
next to Discord.
"""
from __future__ import annotations

import argparse
import re
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
LOG_DIR = REPO / "data" / "logs"

# ANSI
R = "\033[0m"
DIM = "\033[2m"
BOLD = "\033[1m"
CYAN = "\033[96m"
MAGENTA = "\033[95m"
YELLOW = "\033[93m"
GREEN = "\033[92m"
PINK = "\033[38;5;213m"
BLUE = "\033[94m"
GRAY = "\033[90m"

BANNER = f"""{BOLD}{MAGENTA}
  ╔══════════════════════════════════════════════╗
  ║   EchoTwin · live pipeline telemetry         ║
  ║   ASR → LLM → Fish Audio TTS  (all realtime) ║
  ╚══════════════════════════════════════════════╝{R}
"""

_PATTERNS: list[tuple[re.Pattern, callable]] = []


def on(pattern: str):
    def deco(fn):
        _PATTERNS.append((re.compile(pattern), fn))
        return fn
    return deco


def _ts(line: str) -> str:
    return line.split(" | ", 1)[0].split(".")[0]


@on(r"\[organic\] (\w+): verdict=(\w+) score=\d+ signals=(\[[^\]]*\]) text='(.*)'")
def _user(m, line):
    name, verdict, signals, text = m.groups()
    if verdict == "accept":
        return f"{CYAN}{BOLD}  🎙  {name}{R}{CYAN}  “{text}”{R}"
    return f"{GRAY}  ·  {name} said “{text}” — not addressed to the bot ({verdict}), just eavesdropping{R}"


@on(r"\[respond\] LLM done, total_chars=\d+ text='(.*)'")
def _bot(m, line):
    text = m.group(1)
    if not text:
        return None
    # The model's own leading [bracket] cue, shown verbatim as its emotional
    # intent — no editorializing.
    out = ""
    cues = re.findall(r"\[([^\]]{1,40})\]", text)
    if cues:
        out += f"{PINK}{BOLD}  emotional tone: {cues[0].strip()}{R}\n"
    out += f"{MAGENTA}{BOLD}  🤖  EchoTwin{R}{MAGENTA}  “{text}”{R}"
    return out


@on(r"\[tools\] (\w+)\((.*)\) → '(.*)'")
def _tool(m, line):
    name, args, result = m.groups()
    return f"{YELLOW}  ⚙︎  tool call  {BOLD}{name}({args}){R}{YELLOW} → {result[:60]}{R}"


@on(r"\[filler\] queued \d+ packets")
def _filler(m, line):
    return f"{BLUE}  ⚡  instant filler audio playing (cached, 0ms network){R}"


@on(r"\[latency\] (.*) total=(\d+)ms")
def _latency(m, line):
    stages, total = m.groups()
    total = int(total)
    parts = []
    for sm in re.finditer(r"(\w+)→(\w+)=(\d+)ms", stages):
        a, b, ms = sm.group(1), sm.group(2), int(sm.group(3))
        label = {
            "asr_done": "heard (ASR)", "consumer_start": "queue",
            "filler_queued": "filler", "llm_first_delta": "thought (LLM)",
            "first_audio": "spoke (Fish TTS)",
        }.get(b, b)
        parts.append(f"{label} {ms}ms")
    # A turn with no LLM stage (empty/dropped) isn't worth a big timing line.
    if total < 40 and "LLM" not in " ".join(parts) and "thought" not in " ".join(parts):
        return None
    color = GREEN if total < 1500 else YELLOW
    return f"{color}  ⏱  {BOLD}{total}ms{R}{color} mouth-to-ear   {DIM}({'  →  '.join(parts)}){R}"


@on(r"\[emotion-sidecar\] uid=\d+ emotion=(\w+)")
def _emotion(m, line):
    emo = m.group(1)
    if emo == "NEUTRAL":
        return None
    return f"{PINK}{BOLD}  emotion detected in voice: {emo}{R}"


@on(r"\[nickname\] guild \d+: set nick to '(.*)'")
def _persona(m, line):
    return f"{PINK}  ✦  persona active: {BOLD}{m.group(1)}{R}"


@on(r"\[respond\] interrupting prior playback")
def _barge(m, line):
    return f"{YELLOW}{BOLD}  ✋  barge-in — user interrupted, bot stops mid-sentence{R}"


def render(line: str) -> str | None:
    for pat, fn in _PATTERNS:
        m = pat.search(line)
        if m:
            out = fn(m, line)
            if out is None:
                return None
            return f"{GRAY}{_ts(line)}{R} {out}"
    return None


def newest_log() -> Path:
    logs = sorted(LOG_DIR.glob("echotwin_*.log"), key=lambda p: p.stat().st_mtime)
    if not logs:
        sys.exit(f"no logs found in {LOG_DIR}")
    return logs[-1]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--replay", action="store_true", help="render existing log and exit")
    ap.add_argument("--file", type=Path, default=None)
    args = ap.parse_args()

    path = args.file or newest_log()
    print(BANNER)
    with open(path, encoding="utf-8", errors="replace") as f:
        if args.replay:
            for line in f:
                out = render(line)
                if out:
                    print(out)
            return
        f.seek(0, 2)  # tail from end
        while True:
            line = f.readline()
            if not line:
                time.sleep(0.05)
                continue
            out = render(line)
            if out:
                print(out, flush=True)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
