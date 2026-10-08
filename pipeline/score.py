"""The scorer. Every matcher in this repo is measured here, the same way.

    python pipeline/score.py ingest       # stage 0 against the truth
    python pipeline/score.py normalize    # stage 1 against the truth
    python pipeline/score.py exact        # stage 2 against the truth
    python pipeline/score.py fuzzy        # stage 3 against the truth; --curve re-measures the spelling rule

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


def product_label(products, code):
    p = products[code]
    return " · ".join(x for x in (p.get("brands"), p.get("product_name"), p.get("quantity")) if x)


def score_fuzzy(curve=False):
    """Stage 3 against the truth: right, wrong and no answer by method next to stage 2 and the
    baseline; every wrong answer named; the spots checked against the truth's facings; and, with
    --curve, the right/wrong counts of the spelling rule across thresholds and gaps, re-measured."""
    rows = read_jsonl(PIPELINE / "03_reads.jsonl")
    prev = read_jsonl(PIPELINE / "02_reads.jsonl")
    manifest = json.loads((PIPELINE / "03_manifest.json").read_text(encoding="utf-8"))
    products = {}
    for path in (ROOT / "data" / "catalog_raw.jsonl", ROOT / "data" / "catalog_v2.jsonl"):
        for p in read_jsonl(path):
            products.setdefault(p["code"], p)
    with (ROOT / "data" / "baseline_results.csv").open(encoding="utf-8") as f:
        baseline = collections.Counter(b["outcome"] for b in csv.DictReader(f))

    scored = score({r["detection_id"]: r["match_code"] for r in rows})
    stage2 = tally(score({r["detection_id"]: r["match_code"] for r in prev}))
    by_id = {r["detection_id"]: r for r in rows}
    t = tally(scored)
    print("SCORE  stage 3 close spelling against the truth")
    print(f"  rows {len(rows)}, scored {len(scored)}, rows without a truth row: {len(rows) - len(scored)}")
    print(f"  {'attempt':<26} {'right':>5} {'wrong':>5} {'no answer':>9}   right %")
    for name, c in (("naive baseline", baseline), ("stage 2", stage2)):
        print(f"  {name:<26} {c['right']:>5} {c['wrong']:>5} {c['no answer']:>9}   {100 * c['right'] / len(scored):5.1f}%")
    for method in ("barcode", "exact text", "same spot", "close spelling"):
        c = tally([s for s in scored if by_id[s["detection_id"]]["match_method"] == method])
        print(f"  {'stage 3, ' + method:<26} {c['right']:>5} {c['wrong']:>5} {'':>9}")
    print(f"  {'stage 3, all':<26} {t['right']:>5} {t['wrong']:>5} {t['no answer']:>9}   {100 * t['right'] / len(scored):5.1f}%")
    print(f"  gain over stage 2: {t['right'] - stage2['right']} more right, {t['wrong'] - stage2['wrong']:+d} wrong")

    print("\n  every wrong answer")
    for s in scored:
        if s["outcome"] == "wrong":
            r = by_id[s["detection_id"]]
            how = f"{r['match_method']}" + (f", score {r['match_score']} gap {r['match_gap']}" if "match_score" in r else "")
            print(f"  {s['detection_id']}  read {r['ocr_text']!r} ({s['damage']}; {how})")
            print(f"      answered {s['answer']}  {product_label(products, s['answer'])}")
            print(f"      truth    {s['true_code']}  {product_label(products, s['true_code'])}")

    facing = lambda s: (s["store_id"], s["aisle"], s["shelf"], s["position"], s["facing"])
    truth_of = {s["detection_id"]: s for s in scored}
    spots = {}
    for r in rows:
        spots.setdefault(r["spot_id"], []).append(r)
    mixed = [spot for spot, ms in spots.items() if len({facing(truth_of[m["detection_id"]]) for m in ms}) > 1]
    doubles = sum(1 for s in scored if s["extra"] == "double_in_frame")
    second = sum(1 for s in scored if s["extra"] == "second_badge")
    joined_doubles = sum(1 for s in scored if s["extra"] == "double_in_frame"
                         and by_id[s["detection_id"]]["spot_id"] == by_id[s["duplicate_of"]]["spot_id"])
    print(f"\n  spots {len(spots)}: holding reads of more than one facing {len(mixed)}")
    print(f"  second boxes joined to their first box: {joined_doubles} of {doubles}; second-badge reads: {second} "
          f"(a second-badge read is joined when the first badge's read of the facing lies within the rule's distance)")
    for name, ok in manifest["checks"].items():
        print(f"  CHECK    {name}: {ok}")

    if curve:
        import match_fuzzy   # pipeline/match_fuzzy.py, for the same candidates and block
        from rapidfuzz import fuzz, process
        codes_of_text, _, by_first = match_fuzzy.load_candidates()
        open_rows = [r for r in prev if not r["match_code"] and r["text_norm"]]
        print(f"\n  the spelling rule re-measured on the {len(open_rows)} reads with text that stage 2 left open "
              f"(before the spot rule), right/wrong at each threshold")
        for scorer_name, scorer in (("partial_ratio", fuzz.partial_ratio), ("ratio", fuzz.ratio)):
            for block in ("first word", "none"):
                found = []
                for r in open_rows:
                    pool = (by_first.get(r["text_norm"].split()[0]) if block == "first word" else None) or list(codes_of_text)
                    got = process.extract(r["text_norm"], pool, scorer=scorer, limit=2)
                    codes = codes_of_text[got[0][0]]
                    found.append((truth_of[r["detection_id"]]["true_code"], next(iter(codes)) if len(codes) == 1 else None,
                                  got[0][1], got[1][1] if len(got) > 1 else 0))
                print(f"  {scorer_name} / block {block}")
                print("    gap    " + "".join(f"{'t=' + str(th):>10}" for th in range(50, 101, 5)))
                for gap in (0, 5, 10, 15):
                    cells = []
                    for th in range(50, 101, 5):
                        right = sum(1 for tc, c, s1, s2 in found if c and s1 >= th and s1 - s2 >= gap and c == tc)
                        wrong = sum(1 for tc, c, s1, s2 in found if c and s1 >= th and s1 - s2 >= gap and c != tc)
                        cells.append(f"{right:>5}/{wrong:<4}")
                    print(f"    {gap:<6} " + "".join(cells))

    clean = len(rows) == len(scored) and all(manifest["checks"].values())
    print(f"  RESULT   {'stage 3 answered ' + str(t['right'] + t['wrong']) + ' reads in all, ' + str(t['wrong']) + ' wrong, nothing lost' if clean else 'stage 3 FAILED'}")
    return clean


def load_products():
    products = {}
    for path in (ROOT / "data" / "catalog_raw.jsonl", ROOT / "data" / "catalog_v2.jsonl"):
        for p in read_jsonl(path):
            products.setdefault(p["code"], p)
    return products


def score_stage(number, name, method, prev_number):
    """A matching stage against the truth: right, wrong and no answer next to the stage before it,
    the rows answered here by outcome, and every wrong answer named. Shared by the stages after 3."""
    rows = read_jsonl(PIPELINE / f"{number:02d}_reads.jsonl")
    prev = read_jsonl(PIPELINE / f"{prev_number:02d}_reads.jsonl")
    manifest = json.loads((PIPELINE / f"{number:02d}_manifest.json").read_text(encoding="utf-8"))
    products = load_products()
    scored = score({r["detection_id"]: r["match_code"] for r in rows})
    before = tally(score({r["detection_id"]: r["match_code"] for r in prev}))
    by_id = {r["detection_id"]: r for r in rows}
    t = tally(scored)
    here = tally([s for s in scored if by_id[s["detection_id"]]["match_method"] == method])
    print(f"SCORE  stage {number} {name} against the truth")
    print(f"  rows {len(rows)}, scored {len(scored)}, rows without a truth row: {len(rows) - len(scored)}")
    print(f"  {'attempt':<26} {'right':>5} {'wrong':>5} {'no answer':>9}   right %")
    print(f"  {'stage ' + str(prev_number):<26} {before['right']:>5} {before['wrong']:>5} {before['no answer']:>9}   {100 * before['right'] / len(scored):5.1f}%")
    print(f"  {'stage ' + str(number) + ', ' + method:<26} {here['right']:>5} {here['wrong']:>5}")
    print(f"  {'stage ' + str(number) + ', all':<26} {t['right']:>5} {t['wrong']:>5} {t['no answer']:>9}   {100 * t['right'] / len(scored):5.1f}%")
    print(f"  gain over stage {prev_number}: {t['right'] - before['right']} more right, {t['wrong'] - before['wrong']:+d} wrong")
    print("\n  every wrong answer")
    for s in scored:
        if s["outcome"] == "wrong":
            r = by_id[s["detection_id"]]
            how = r["match_method"] + (f", score {r['match_score']}" if "match_score" in r else "")
            print(f"  {s['detection_id']}  read {r['ocr_text']!r} ({s['damage']}; {how})")
            print(f"      answered {s['answer']}  {product_label(products, s['answer'])}")
            print(f"      truth    {s['true_code']}  {product_label(products, s['true_code'])}")
    for cname, ok in manifest["checks"].items():
        print(f"  CHECK    {cname}: {ok}")
    clean = len(rows) == len(scored) and all(manifest["checks"].values())
    print(f"  RESULT   {'stage ' + str(number) + ' answered ' + str(here['right'] + here['wrong']) + ' reads here, ' + str(here['wrong']) + ' wrong, nothing lost' if clean else 'stage ' + str(number) + ' FAILED'}")
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
    if stage == "fuzzy":
        return 0 if score_fuzzy(curve="--curve" in sys.argv) else 1
    if stage == "crossday":
        return 0 if score_stage(4, "other days", "other day", 3) else 1
    if stage == "llm" and "--measurement" in sys.argv:
        return 0 if score_llm_measurement(sys.argv[sys.argv.index("--model") + 1] if "--model" in sys.argv else None) else 1
    if stage == "llm":
        return 0 if score_stage(5, "language model", "language model", 4) else 1
    sys.exit("usage: python pipeline/score.py ingest | normalize | exact | fuzzy [--curve] | crossday | llm [--measurement [--model id]]")


def score_llm_measurement(model=None):
    """The measurement run against the truth: for each confidence word, how often the model's answer
    was right, so the stage can decide which words to accept; and what the bill would have been
    without the other-days tier."""
    import match_llm
    model = model or match_llm.MODEL
    path = PIPELINE / f"llm_measurement_{model}.json"
    m = json.loads(path.read_text(encoding="utf-8"))
    with DET_TRUTH.open(encoding="utf-8") as f:
        truth = {t["detection_id"]: t for t in csv.DictReader(f)}
    products = load_products()
    stage5 = json.loads((PIPELINE / "05_manifest.json").read_text(encoding="utf-8")) if (PIPELINE / "05_manifest.json").exists() else None

    table = {}
    wrong = []
    for spot, a in m["answers"].items():
        true_codes = {truth[i]["true_code"] for i in a["reads"]}
        key = a["confidence"] if a["code"] else "none"
        c = table.setdefault(key, {"spots": 0, "right": 0, "wrong": 0})
        c["spots"] += 1
        if a["code"]:
            if a["code"] in true_codes and len(true_codes) == 1:
                c["right"] += 1
            else:
                c["wrong"] += 1
                wrong.append((spot, a, sorted(true_codes)))
    print(f"SCORE  the language model on every spot stage 3 left open  ({model})")
    print(f"  spots asked {m['spots_asked']}, reads in them {m['reads_in_them']}, calls {m['bill']['calls']}, "
          f"tokens {m['bill']['input_tokens']} in / {m['bill']['output_tokens']} out, USD {m['bill']['usd']:.4f}")
    print(f"  {'the model said':<14} {'spots':>5} {'right':>5} {'wrong':>5}   right %")
    for key in ("sure", "likely", "unsure", "none"):
        if key in table:
            c = table[key]
            pct = f"{100 * c['right'] / c['spots']:5.1f}%" if a and c["spots"] and key != "none" else ""
            print(f"  {key:<14} {c['spots']:>5} {c['right']:>5} {c['wrong']:>5}   {pct}")
    print("\n  every wrong answer")
    for spot, a, true_codes in wrong:
        reads = [r for r in read_jsonl(PIPELINE / "03_reads.jsonl") if r["spot_id"] == spot]
        print(f"  {spot}  reads {[r['ocr_text'] for r in reads]}  ({a['confidence']}: {a['why']})")
        print(f"      answered {a['code']}  {product_label(products, a['code'])}")
        print(f"      truth    {', '.join(product_label(products, t) for t in true_codes)}")
    if stage5:
        print(f"\n  with the other-days tier, stage 5 asked about {stage5['input']['open_spots']} spots for USD {stage5['bill']['usd']:.4f}; "
              f"without it, {m['spots_asked']} spots for USD {m['bill']['usd']:.4f}")
    return True


if __name__ == "__main__":
    sys.exit(main())
