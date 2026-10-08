"""The scorer. Every matcher in this repo is measured here, the same way.

    python pipeline/score.py ingest     # stage 0 against the truth

This is the only pipeline file that reads a truth file: data/detections_truth.csv and
data/malformed_truth.csv. A matcher hands over its answers and gets back right / wrong / no answer.
A stage hands over its output and gets back what it kept, lost or let through that it should not have.
"""
import collections
import csv
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DET_TRUTH = ROOT / "data" / "detections_truth.csv"
MALFORMED_TRUTH = ROOT / "data" / "malformed_truth.csv"
PIPELINE = ROOT / "data" / "pipeline"

OUTCOMES = ("right", "wrong", "no answer")

# what the simulation calls each kind of damage, and what the ingest stage should call it
DAMAGE_REASON = {"cut_line": "unreadable", "missing_field": "missing_field", "wrong_type": "wrong_type",
                 "bad_timestamp": "bad_timestamp", "out_of_range": "out_of_range", "empty_read": "empty_read",
                 "id_conflict": "id_conflict"}


def score(answers):
    """answers: {detection_id: catalog code, or None for 'no answer'}.

    Returns one row per detection: the truth row, plus the answer and its outcome.
    """
    with DET_TRUTH.open(encoding="utf-8") as f:
        truth = list(csv.DictReader(f))
    rows = []
    for t in truth:
        answer = answers.get(t["detection_id"])
        if answer is None:
            outcome = "no answer"
        elif answer == t["true_code"]:
            outcome = "right"
        else:
            outcome = "wrong"
        rows.append({**t, "answer": answer, "outcome": outcome})
    return rows


def tally(rows):
    counts = collections.Counter(row["outcome"] for row in rows)
    return {outcome: counts[outcome] for outcome in OUTCOMES}


def print_table(results):
    """results: {attempt name: scored rows}."""
    print(f"  {'attempt':<28} {'right':>5} {'wrong':>5} {'no answer':>9}   right %")
    for name, rows in results.items():
        t = tally(rows)
        print(f"  {name:<28} {t['right']:>5} {t['wrong']:>5} {t['no answer']:>9}   "
              f"{100 * t['right'] / len(rows):5.1f}%")


def read_jsonl(path):
    with path.open(encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def score_ingest():
    """Stage 0 against the truth: every real detection comes through once, nothing else does, the
    rows dropped as duplicates are the rows the simulation sent twice, and the rows quarantined are
    the rows the simulation damaged, line for line."""
    with DET_TRUTH.open(encoding="utf-8") as f:
        truth = list(csv.DictReader(f))
    with MALFORMED_TRUTH.open(encoding="utf-8") as f:
        planted = list(csv.DictReader(f))
    out = read_jsonl(PIPELINE / "00_reads.jsonl")
    quarantine = read_jsonl(PIPELINE / "00_quarantine.jsonl")
    manifest = json.loads((PIPELINE / "00_manifest.json").read_text(encoding="utf-8"))

    truth_ids = {t["detection_id"] for t in truth}
    out_ids = [r["detection_id"] for r in out]
    missing = truth_ids - set(out_ids)
    unknown = set(out_ids) - truth_ids
    twice = len(out_ids) - len(set(out_ids))
    resent = sum(1 for t in truth if t["times_sent"] == "2")
    planted_lines = {int(p["line"]): p for p in planted}
    quarantined_lines = {q["line"]: q for q in quarantine}
    same_lines = set(planted_lines) == set(quarantined_lines)
    reason_ok = sum(1 for n, p in planted_lines.items()
                    if n in quarantined_lines and quarantined_lines[n]["reason"] == DAMAGE_REASON[p["kind"]])

    print(f"SCORE  stage 0 ingest against the truth")
    print(f"  real detections {len(truth_ids)}: in the output {len(truth_ids) - len(missing)}, "
          f"missing {len(missing)}, present twice {twice}")
    print(f"  rows in the output that are not real detections: {len(unknown)}")
    print(f"  duplicates dropped {manifest['duplicates_dropped']} = rows the simulation sent twice {resent}: "
          f"{manifest['duplicates_dropped'] == resent}")
    print(f"  quarantined {len(quarantine)} = damaged rows planted {len(planted)}: {len(quarantine) == len(planted)}, "
          f"the same lines: {same_lines}, the expected reason on {reason_ok} of {len(planted)}")
    print(f"\n  {'planted as':<14} {'rows':>4}   {'quarantined as':<14} {'rows':>4}")
    for kind, reason in DAMAGE_REASON.items():
        print(f"  {kind:<14} {sum(1 for p in planted if p['kind'] == kind):>4}   "
              f"{reason:<14} {sum(1 for q in quarantine if q['reason'] == reason):>4}")
    for name, ok in manifest["checks"].items():
        print(f"  CHECK    {name}: {ok}")
    clean = (not missing and not unknown and not twice and same_lines
             and reason_ok == len(planted) and manifest["duplicates_dropped"] == resent)
    print(f"  RESULT   {'stage 0 kept every real detection once and nothing else' if clean else 'stage 0 FAILED'}")
    return clean


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    stage = sys.argv[1] if len(sys.argv) > 1 else ""
    if stage == "ingest":
        return 0 if score_ingest() else 1
    sys.exit("usage: python pipeline/score.py ingest")


if __name__ == "__main__":
    sys.exit(main())
