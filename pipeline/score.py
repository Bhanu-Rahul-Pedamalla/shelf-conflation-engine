"""The scorer. Every matcher in this repo is measured here, the same way.

This is the only pipeline file that reads data/detections_truth.csv.
A matcher hands over its answers and gets back right / wrong / no answer.
"""
import collections
import csv
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DET_TRUTH = ROOT / "data" / "detections_truth.csv"

OUTCOMES = ("right", "wrong", "no answer")


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
