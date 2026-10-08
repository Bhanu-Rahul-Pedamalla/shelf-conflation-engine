"""The scorer. Every matcher in this repo is measured here, the same way.

    python pipeline/score.py ingest       # stage 0 against the truth
    python pipeline/score.py normalize    # stage 1 against the truth
    python pipeline/score.py exact        # stage 2 against the truth

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


def score_normalize():
    """Stage 1 against the truth: nothing lost, nothing changed but the added field, and how many reads
    the normalized text brings back to their label, next to what case-folding alone managed."""
    import normalize   # pipeline/normalize.py, for the function applied to the label side
    with DET_TRUTH.open(encoding="utf-8") as f:
        truth = {t["detection_id"]: t for t in csv.DictReader(f)}
    before = read_jsonl(PIPELINE / "00_reads.jsonl")
    after = read_jsonl(PIPELINE / "01_reads.jsonl")
    manifest = json.loads((PIPELINE / "01_manifest.json").read_text(encoding="utf-8"))

    same_rows = [r["detection_id"] for r in before] == [o["detection_id"] for o in after]
    untouched = all({k: v for k, v in o.items() if k != "text_norm"} == r for r, o in zip(before, after))
    missing_truth = sum(1 for o in after if o["detection_id"] not in truth)

    by_kind = {}
    for o in after:
        t = truth[o["detection_id"]]
        c = by_kind.setdefault(t["damage"], {"reads": 0, "casefold": 0, "normalized": 0})
        c["reads"] += 1
        c["casefold"] += o["ocr_text"].casefold() == t["clean_text"].casefold()
        c["normalized"] += o["text_norm"] == normalize.normalize_text(t["clean_text"])
    total = {k: sum(c[k] for c in by_kind.values()) for k in ("reads", "casefold", "normalized")}

    print("SCORE  stage 1 normalize against the truth")
    print(f"  rows {len(before)} in, {len(after)} out, same ids in the same order: {same_rows}, "
          f"original fields untouched: {untouched}, rows without a truth row: {missing_truth}")
    print(f"  reads equal to their label: case-folded alone {total['casefold']}, normalized {total['normalized']}, "
          f"of {total['reads']}  (gain {total['normalized'] - total['casefold']})")
    print(f"\n  {'damage to the read':<26} {'reads':>5} {'casefold':>9} {'normalized':>11}")
    for kind, c in sorted(by_kind.items(), key=lambda kv: -kv[1]["reads"]):
        print(f"  {kind:<26} {c['reads']:>5} {c['casefold']:>9} {c['normalized']:>11}")
    for name, ok in manifest["checks"].items():
        print(f"  CHECK    {name}: {ok}")
    clean = same_rows and untouched and not missing_truth and all(manifest["checks"].values())
    print(f"  RESULT   {'stage 1 lost nothing and changed nothing but text_norm' if clean else 'stage 1 FAILED'}")
    return clean


def score_exact():
    """Stage 2 against the truth: right, wrong and no answer, by method, next to the naive baseline;
    every wrong answer named; and the answers that name a product only v1 lists checked against
    the truth's own record of which catalog the label came from."""
    rows = read_jsonl(PIPELINE / "02_reads.jsonl")
    manifest = json.loads((PIPELINE / "02_manifest.json").read_text(encoding="utf-8"))
    products = {}
    for path in (ROOT / "data" / "catalog_raw.jsonl", ROOT / "data" / "catalog_v2.jsonl"):
        for p in read_jsonl(path):
            products.setdefault(p["code"], p)
    with (ROOT / "data" / "baseline_results.csv").open(encoding="utf-8") as f:
        baseline = collections.Counter(b["outcome"] for b in csv.DictReader(f))

    scored = score({r["detection_id"]: r["match_code"] for r in rows})
    by_id = {r["detection_id"]: r for r in rows}
    results = {"naive baseline": None, "stage 2, barcode": [s for s in scored if by_id[s["detection_id"]]["match_method"] == "barcode"],
               "stage 2, exact text": [s for s in scored if by_id[s["detection_id"]]["match_method"] == "exact text"],
               "stage 2, all": scored}
    t = tally(scored)
    print("SCORE  stage 2 exact match against the truth")
    print(f"  rows {len(rows)}, scored {len(scored)}, rows without a truth row: {len(rows) - len(scored)}")
    print(f"  {'attempt':<24} {'right':>5} {'wrong':>5} {'no answer':>9}   right %")
    print(f"  {'naive baseline':<24} {baseline['right']:>5} {baseline['wrong']:>5} {baseline['no answer']:>9}   "
          f"{100 * baseline['right'] / sum(baseline.values()):5.1f}%")
    for name, part in results.items():
        if part is None:
            continue
        c = tally(part)
        print(f"  {name:<24} {c['right']:>5} {c['wrong']:>5} {c['no answer']:>9}   "
              f"{100 * c['right'] / len(scored):5.1f}%")
    print(f"  gain over the baseline: {t['right'] - baseline['right']} more right, "
          f"{t['wrong'] - baseline['wrong']:+d} wrong")

    label = lambda code: " · ".join(x for x in (products[code].get("brands"), products[code].get("product_name"),
                                                 products[code].get("quantity")) if x)
    print("\n  every wrong answer")
    for s in scored:
        if s["outcome"] == "wrong":
            print(f"  {s['detection_id']}  read {by_id[s['detection_id']]['ocr_text']!r} ({s['damage']})")
            print(f"      answered {s['answer']}  {label(s['answer'])}")
            print(f"      truth    {s['true_code']}  {label(s['true_code'])}")

    v1_only = [s for s in scored if by_id[s["detection_id"]]["match_catalog"] == "v1"]
    v1_only_ok = sum(1 for s in v1_only if s["label_catalog"] == "v1" and s["outcome"] == "right")
    print(f"\n  answers naming a product only v1 lists: {len(v1_only)}, of which the label really came from v1 "
          f"and the answer is right: {v1_only_ok}")
    for name, ok in manifest["checks"].items():
        print(f"  CHECK    {name}: {ok}")
    clean = len(rows) == len(scored) and all(manifest["checks"].values()) and v1_only_ok == len(v1_only)
    print(f"  RESULT   {'stage 2 answered ' + str(t['right'] + t['wrong']) + ' reads, ' + str(t['wrong']) + ' wrong, nothing lost' if clean else 'stage 2 FAILED'}")
    return clean


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    stage = sys.argv[1] if len(sys.argv) > 1 else ""
    if stage == "ingest":
        return 0 if score_ingest() else 1
    if stage == "normalize":
        return 0 if score_normalize() else 1
    if stage == "exact":
        return 0 if score_exact() else 1
    sys.exit("usage: python pipeline/score.py ingest | normalize | exact")


if __name__ == "__main__":
    sys.exit(main())
