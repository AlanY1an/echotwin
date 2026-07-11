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

BANNER = f"""{BOLD}{MAGENTA}EchoTwin — live pipeline trace{R}{DIM}
ASR → addressee → LLM → tools → Fish TTS, per-turn timing{R}
"""

_PATTERNS: list[tuple[re.Pattern, callable]] = []


def on(pattern: str):
    def deco(fn):
        _PATTERNS.append((re.compile(pattern), fn))
        return fn
    return deco


def _ts(line: str) -> str:
    return line.split(" | ", 1)[0].split(".")[0]


def _stage(label: str, body: str, color: str = R) -> str:
    """One pipeline event: a fixed-width stage tag + the real data."""
    return f"{color}{BOLD}{label:>8}{R}  {color}{body}{R}"


# text is a Python repr — single OR double quoted depending on apostrophes.
@on(r"""\[organic\] (\w+): verdict=(\w+) score=(\d+) signals=(\[[^\]]*\]) text=(['"])(.*)\5""")
def _user(m, line):
    name, verdict, score, signals, _q, text = m.groups()
    heard = _stage("ASR", f'{name}: "{text}"', CYAN)
    addr = _stage("ADDRESSEE", f"{verdict} · {signals} · score {score}", GRAY)
    return heard + "\n" + addr


@on(r"\[respond\] starting LLM stream")
def _stage_llm(m, line):
    return _stage("LLM", "stream start", GRAY)


@on(r"\[emotion-sidecar\] uid=\d+ emotion=(\w+)")
def _emotion(m, line):
    emo = m.group(1)
    return _stage("EMOTION", f"{emo}  (SenseVoice, from voice)", GRAY if emo == "NEUTRAL" else PINK)


@on(r"\[tools\] (\w+)\((.*)\) → '(.*)'")
def _tool(m, line):
    name, args, result = m.groups()
    return _stage("TOOL", f"{name}({args}) → {result[:60]}", YELLOW)


@on(r"\[filler\] queued (\d+) packets")
def _filler(m, line):
    return _stage("FILLER", f"cached audio queued ({m.group(1)} packets)", BLUE)


@on(r"""\[respond\] LLM done, total_chars=\d+ text=(['"])(.*)\1""")
def _bot(m, line):
    text = m.group(2)
    if not text:
        return _stage("REPLY", "(empty)", GRAY)
    cues = re.findall(r"\[([^\]]{1,40})\]", text)
    out = ""
    if cues:
        out += _stage("TONE", f"{cues[0].strip()}  (model's own tag)", PINK) + "\n"
    out += _stage("REPLY", f'"{text}"', MAGENTA)
    return out


@on(r"\[respond\] first TTS audio chunk received")
def _stage_speak(m, line):
    return _stage("TTS", "first audio out", GREEN)


@on(r"\[respond\] drain_tts done")
def _stage_done(m, line):
    return _stage("TTS", "playback done", GRAY)


@on(r"\[respond\] interrupting prior playback")
def _barge(m, line):
    return _stage("BARGE-IN", "user spoke — stopping current playback", YELLOW)


@on(r"\[barge-in\] voice detected — ducking playback to (\d+%)")
def _duck(m, line):
    return _stage("DUCK", f"voice detected — playback volume → {m.group(1)}", YELLOW)


@on(r"\[barge-in\] sustained speech (\d+)ms — stopping playback")
def _barge_live(m, line):
    return _stage("BARGE-IN", f"sustained speech {m.group(1)}ms — playback stopped", YELLOW)


@on(r"\[barge-in\] false alarm — restoring playback volume")
def _unduck(m, line):
    return _stage("DUCK", "false alarm (backchannel) — volume restored", GRAY)


@on(r"\[latency\] (.*) total=(\d+)ms")
def _latency(m, line):
    stages, total = m.groups()
    total = int(total)
    parts = []
    for sm in re.finditer(r"(\w+)→(\w+)=(\d+)ms", stages):
        b, ms = sm.group(2), int(sm.group(3))
        label = {
            "asr_done": "asr", "consumer_start": "queue", "filler_queued": "filler",
            "llm_first_delta": "llm", "first_audio": "tts",
        }.get(b, b)
        parts.append(f"{label} {ms}")
    if total < 40 and "llm" not in " ".join(parts):
        return None
    color = GREEN if total < 1500 else YELLOW
    return _stage("TIME", f"{total}ms  ({' · '.join(parts)})", color)


@on(r"\[nickname\] guild \d+: set nick to '(.*)'")
def _persona(m, line):
    return _stage("PERSONA", m.group(1), PINK)


@on(r"""\[greeting\] speaking: (['"])(.*)\1""")
def _greeting(m, line):
    return _stage("GREETING", f'"{m.group(2)}"', MAGENTA)


@on(r"""\[farewell\] speaking \((\w+)\): (['"])(.*)\2""")
def _farewell(m, line):
    return _stage("FAREWELL", f'({m.group(1)}) "{m.group(3)}"', MAGENTA)


@on(r"\[groq_chat\] 429 rate-limited, retry (\d+)/(\d+) in ([\d.]+)s")
def _ratelimit(m, line):
    n, total, wait = m.groups()
    return _stage("RATE-LIMIT", f"429 from provider — waiting {wait}s, retry {n}/{total}", YELLOW)


@on(r"(?:LLM stream error|ERROR).*?(?:Groq HTTP (\d+)|(\w+Error))")
def _error(m, line):
    what = m.group(1) or m.group(2) or "error"
    return _stage("ERROR", f"{what} — see log", "\033[91m")  # bright red


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
