"""The runner: every stage in order, each scored before the next starts, and one log every number in
REPORT.md is read from. It holds no logic of its own; it runs the scripts and keeps what they printed.

    python pipeline/run.py

Writes  data/pipeline/run.txt    what every stage and scorer printed, with the wall clock of each step

A step that exits with an error stops the run; the log says which one. The language model stage answers
from data/pipeline/llm_cache.jsonl when every prompt is cached, so a run of this feed makes no API call
and needs no key; a new feed would. The timings vary between runs and machines; every count does not.
"""
import datetime
import os
import platform
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LOG = ROOT / "data" / "pipeline" / "run.txt"

STEPS = (
    ("stage 0  ingest", ["pipeline/ingest.py"]),               ("score", ["pipeline/score.py", "ingest"]),
    ("stage 1  normalize", ["pipeline/normalize.py"]),         ("score", ["pipeline/score.py", "normalize"]),
    ("stage 2  exact match", ["pipeline/match_exact.py"]),     ("score", ["pipeline/score.py", "exact"]),
    ("stage 3  close spelling", ["pipeline/match_fuzzy.py"]),  ("score", ["pipeline/score.py", "fuzzy"]),
    ("stage 4  other days", ["pipeline/match_crossday.py"]),   ("score", ["pipeline/score.py", "crossday"]),
    ("stage 5  language model", ["pipeline/match_llm.py"]),    ("score", ["pipeline/score.py", "llm"]),
    ("stage 6  golden record", ["pipeline/golden.py"]),        ("score", ["pipeline/score.py", "golden"]),
    ("stage 7  versioning", ["pipeline/versioning.py"]),       ("test", ["pipeline/test_versioning.py"]),
    ("score", ["pipeline/score.py", "versioning"]),
    ("stage 8  serve", ["pipeline/serve.py"]),                 ("score", ["pipeline/score.py", "serve"]),
)


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    started = datetime.datetime.now(datetime.timezone.utc)
    lines = [f"RUN  {started.strftime('%Y-%m-%dT%H:%M:%SZ')}  python {platform.python_version()}  {platform.system()} {platform.release()}", ""]
    t_all = time.perf_counter()
    for name, args in STEPS:
        t = time.perf_counter()
        proc = subprocess.run([sys.executable, *args], cwd=ROOT, capture_output=True, text=True, encoding="utf-8",
                              env={**os.environ, "PYTHONIOENCODING": "utf-8"})
        secs = time.perf_counter() - t
        head = f"==== {name:<26} python {' '.join(args):<40} {secs:7.2f} s   exit {proc.returncode}"
        print(head)
        print(proc.stdout, end="")
        lines += [head, proc.stdout.rstrip("\n"), ""]
        if proc.returncode != 0:
            print(proc.stderr, end="")
            lines += [proc.stderr.rstrip("\n"), f"STOPPED at {name}: exit {proc.returncode}"]
            LOG.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
            sys.exit(f"STOP: {name} failed. {LOG.relative_to(ROOT)} has what it printed.")
    total = time.perf_counter() - t_all
    lines.append(f"DONE  {len(STEPS)} steps  {total:.1f} s wall clock")
    LOG.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    print(f"\nDONE  {len(STEPS)} steps  {total:.1f} s wall clock  -> {LOG.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
