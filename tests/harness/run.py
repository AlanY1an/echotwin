"""Run the full pipeline test harness, print a one-screen summary report.

Usage:
    .venv/bin/python -m tests.harness.run                    # all layers
    .venv/bin/python -m tests.harness.run --skip-live        # offline only
    .venv/bin/python -m tests.harness.run -k vad             # filter

This is just a wrapper around pytest with custom output formatting. The
real assertions live in test_layer*.py / test_e2e.py / test_robustness.py.
"""
from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from pathlib import Path

LAYERS = [
    ("L2 VAD", "tests/harness/test_layer2_vad.py", False),
    ("L3 ASR", "tests/harness/test_layer3_asr.py", False),
    ("L4 Addressee", "tests/harness/test_layer4_addressee.py", False),
    ("L5 LLM", "tests/harness/test_layer5_llm.py", True),
    ("L6 Chunker", "tests/harness/test_layer6_chunker.py", False),
    ("L7 TTS", "tests/harness/test_layer7_tts.py", True),
    ("E2E", "tests/harness/test_e2e.py", True),
    ("Robust", "tests/harness/test_robustness.py", True),  # has pure-chat test that needs key
]


def _run_layer(name: str, path: str, k_filter: str | None) -> dict:
    """Run pytest on one file. Return summary."""
    cmd = ["pytest", path, "-q", "--tb=line", "-s"]
    if k_filter:
        cmd += ["-k", k_filter]

    venv_pytest = Path(".venv/bin/pytest")
    if venv_pytest.exists():
        cmd[0] = str(venv_pytest)

    proc = subprocess.run(cmd, capture_output=True, text=True)
    out = proc.stdout + proc.stderr

    # Parse pytest summary line — anchor to "in NN.NNs" at line end
    # Format: "12 passed in 2.14s" or "11 passed, 1 failed in 2.14s"
    summary_re = re.compile(r"((?:\d+ \w+(?:, )?)+) in [\d.]+\s*s\b")
    m = summary_re.search(out)
    passed = failed = skipped = 0
    if m:
        for n_str, label in re.findall(r"(\d+) (\w+)", m.group(1)):
            n = int(n_str)
            if label == "passed": passed = n
            elif label == "failed": failed = n
            elif label == "skipped": skipped = n

    # Extract a few key metrics from -s output
    metrics: list[str] = []
    for line in out.split("\n"):
        s = line.strip()
        # Pull P50= / TTFT= / RTF= / CER= lines
        if any(k in s for k in ("P50=", "TTFA=", "TTFT=", "RTF=", "CER=", "rms=")):
            # Trim long lines
            if len(s) < 150:
                metrics.append(s)

    return {
        "name": name,
        "passed": passed,
        "failed": failed,
        "skipped": skipped,
        "returncode": proc.returncode,
        "metrics": metrics[:6],  # cap to first 6 interesting lines
        "raw": out if proc.returncode != 0 else "",
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--skip-live", action="store_true", help="skip layers needing API keys")
    ap.add_argument("-k", help="pytest -k filter")
    args = ap.parse_args()

    print("=" * 78)
    print(" EchoTwin — Layered Test Harness Report")
    print("=" * 78)

    total_p, total_f, total_s = 0, 0, 0
    for name, path, needs_live in LAYERS:
        if args.skip_live and needs_live:
            print(f"\n  {name:<14}  SKIPPED (--skip-live)")
            continue
        print(f"\n  {name:<14}  ", end="", flush=True)
        r = _run_layer(name, path, args.k)
        total_p += r["passed"]
        total_f += r["failed"]
        total_s += r["skipped"]
        status = "PASS" if r["failed"] == 0 else f"FAIL ({r['failed']})"
        print(f"{status}  {r['passed']} passed, {r['skipped']} skipped")
        for m in r["metrics"]:
            print(f"      {m}")
        if r["failed"] > 0 and r["raw"]:
            print(f"  --- failure tail ---")
            for line in r["raw"].split("\n")[-12:]:
                print(f"  {line}")

    print()
    print("=" * 78)
    print(f"  TOTAL: {total_p} passed, {total_f} failed, {total_s} skipped")
    print("=" * 78)
    sys.exit(0 if total_f == 0 else 1)


if __name__ == "__main__":
    main()
