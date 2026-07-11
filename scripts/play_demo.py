#!/usr/bin/env python3
"""Demo director — plays Sam's pre-synthesized lines into BlackHole so the bot
hears them as mic input. You drive it with the ENTER key: one press = play the
next line. This keeps you in control of pacing during the recording and lets
the bot fully answer before the next line goes out.

Special cues:
  - Before S8 (the barge-in), the script waits for you to confirm the bot has
    STARTED its long answer, then fires S8 ~2s in to cut it off.
  - Before S10 it pauses so you can type `/persona-admin use hinata` in Discord.

Setup (see dev-docs/demo-script.md checklist):
  - Discord input device  = BlackHole 2ch
  - This script's output  = BlackHole 2ch (default below, or --device)
  - You'll want a Multi-Output Device so you can also HEAR Sam while recording.

Usage:
  .venv/bin/python scripts/play_demo.py            # list BlackHole, interactive
  .venv/bin/python scripts/play_demo.py --device 3 # force output device index
  .venv/bin/python scripts/play_demo.py --line S5  # play one line and exit
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import sounddevice as sd
import soundfile as sf

REPO = Path(__file__).resolve().parent.parent
LINES_DIR = REPO / "scripts" / "demo_lines"

# The running order, with stage headers and director cues.
SCRIPT = [
    ("act", "ACT 1 · Hello (speed)"),
    ("S1", "So. You're the famous Ariana everyone keeps talking about..."),
    ("S2", "People say you're the fastest voice bot on Discord. Prove it..."),
    ("act", "ACT 2 · Tools (utility)"),
    ("S3", "Make yourself useful. What's the weather in Houston?"),
    ("S4", "And what time is it over there?"),
    ("act", "ACT 3 · Emotion (the money shot)"),
    ("S5", "[sad] I've had a genuinely rough day. Flight cancelled. Twice."),
    ("S6", "[excited] I GOT THE JOB!"),
    ("act", "ACT 4 · Barge-in (full duplex)"),
    ("S7", "Tell me your whole life story. Every detail."),
    ("barge", "S8", "Boring! Skip to the good part."),  # special: fire mid-answer
    ("S9", "Okay that was rude. Sorry. You're actually kind of fun."),
    ("act", "ACT 5 · Soul swap (persona + bilingual)"),
    ("cue", "In Discord, type:  /persona-admin use hinata   — then press ENTER"),
    ("S10", "哎?你声音怎么变啦?你现在是谁呀?"),
    ("S11", "太可爱了吧。行,我先撤了,下次聊!"),
    ("end", "— fin —"),
]

BOLD = "\033[1m"; DIM = "\033[2m"; CYAN = "\033[96m"; YELLOW = "\033[93m"
GREEN = "\033[92m"; MAGENTA = "\033[95m"; R = "\033[0m"


def find_blackhole() -> int | None:
    for i, d in enumerate(sd.query_devices()):
        if d["max_output_channels"] > 0 and "blackhole" in d["name"].lower():
            return i
    return None


def play(line_id: str, device: int, blocking: bool = True) -> float:
    """Play a line file to the device. Returns its duration in seconds."""
    path = LINES_DIR / f"{line_id}.wav"
    data, sr = sf.read(path, dtype="float32")
    sd.play(data, sr, device=device)
    dur = len(data) / sr
    if blocking:
        sd.wait()
    return dur


def wait_enter(prompt: str = "") -> None:
    try:
        input(prompt)
    except EOFError:
        sys.exit(0)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--device", type=int, default=None, help="output device index (default: BlackHole)")
    ap.add_argument("--line", default=None, help="play one line id and exit")
    ap.add_argument("--barge-delay", type=float, default=2.0, help="seconds into bot's answer before firing S8")
    args = ap.parse_args()

    device = args.device if args.device is not None else find_blackhole()
    if device is None:
        sys.exit("BlackHole not found — pass --device <index> (see `python scripts/play_demo.py` device list)")
    dev_name = sd.query_devices(device)["name"]

    if args.line:
        print(f"Playing {args.line} → {dev_name}")
        play(args.line, device)
        return

    print(f"\n{BOLD}{MAGENTA}EchoTwin demo director{R}")
    print(f"{DIM}output → {dev_name} (index {device}){R}")
    print(f"{DIM}Discord input device must be set to BlackHole 2ch.{R}")
    print(f"{DIM}Each ENTER plays the next line; let the bot finish before the next.{R}\n")
    wait_enter(f"{GREEN}Press ENTER to start ▶{R}")

    i = 0
    while i < len(SCRIPT):
        item = SCRIPT[i]
        kind = item[0]

        if kind == "act":
            print(f"\n{BOLD}{YELLOW}━━ {item[1]} ━━{R}")
        elif kind == "end":
            print(f"\n{BOLD}{MAGENTA}{item[1]}{R}\n")
            break
        elif kind == "cue":
            print(f"\n{BOLD}{CYAN}⚑ {item[1]}{R}")
            wait_enter()
        elif kind == "barge":
            _, lid, text = item
            print(f"\n{YELLOW}{BOLD}⚡ BARGE-IN next.{R} {DIM}{text}{R}")
            print(f"{DIM}   Press ENTER the MOMENT the bot starts its long answer;{R}")
            print(f"{DIM}   S8 fires {args.barge_delay:.0f}s later to cut it off.{R}")
            wait_enter(f"{YELLOW}   ready → ENTER when bot is talking{R}")
            time.sleep(args.barge_delay)
            print(f"{YELLOW}   ✂  cutting in!{R}")
            play(lid, device)
        else:
            lid, text = item
            print(f"\n{CYAN}▶ {lid}{R}  {DIM}{text}{R}")
            wait_enter()
            dur = play(lid, device)
            print(f"{DIM}   ({dur:.1f}s played — wait for the bot, then ENTER){R}")
        i += 1


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n[stopped]")
