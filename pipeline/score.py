"""The scorer. Every matcher in this repo is measured here, the same way.

    python pipeline/score.py ingest       # stage 0 against the truth
    python pipeline/score.py normalize    # stage 1 against the truth
    python pipeline/score.py exact        # stage 2 against the truth
    python pipeline/score.py fuzzy        # stage 3 against the truth; --curve re-measures the spelling rule
    python pipeline/score.py crossday     # stage 4; llm, llm --measurement: stage 5; golden: stage 6, the record against the shelf truth
    python pipeline/score.py versioning   # stage 7: the catalog changes found against the changes made, the records touched against the shelf
    python pipeline/score.py serve        # stage 8: every verdict against the shelf on the last day, compliance next to the truth's

This is the only pipeline file that reads a truth file: data/detections_truth.csv, data/malformed_truth.csv,
data/shelf_truth_day*.csv and data/catalog_changes_truth.csv. A matcher hands over its answers and gets back right / wrong / no answer.
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
CATALOG_TRUTH = ROOT / "data" / "catalog_changes_truth.csv"
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
    if stage == "golden":
        return 0 if score_golden() else 1
    if stage == "versioning":
        return 0 if score_versioning() else 1
    if stage == "serve":
        return 0 if score_serve() else 1
    sys.exit("usage: python pipeline/score.py ingest | normalize | exact | fuzzy [--curve] | crossday | llm [--measurement [--model id]] | golden | versioning | serve")


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
            pct = f"{100 * c['right'] / c['spots']:5.1f}%" if c["spots"] and key != "none" else ""
            print(f"  {key:<14} {c['spots']:>5} {c['right']:>5} {c['wrong']:>5}   {pct}")
    print(f"  {'no reply':<14} {len(m.get('spots_without_reply', [])):>5}   (left out of the model's array; those reads stay open)")
    print("\n  every wrong answer")
    for spot, a, true_codes in wrong:
        reads = [r for r in read_jsonl(PIPELINE / "03_reads.jsonl") if r["spot_id"] == spot]
        print(f"  {spot}  reads {[r['ocr_text'] for r in reads]}  ({a['confidence']}: {a['why']})")
        print(f"      answered {a['code']}  {product_label(products, a['code'])}")
        print(f"      truth    {', '.join(product_label(products, t) for t in true_codes)}")
    if stage5 and stage5["rules"]["model"] == model:
        print(f"\n  with the other-days tier, stage 5 asked about {stage5['input']['open_spots']} spots for USD {stage5['bill']['usd']:.4f}; "
              f"without it, {m['spots_asked']} spots for USD {m['bill']['usd']:.4f}")
    return True


def score_golden():
    """The golden record against the shelf truth: each record's product on the day it was last seen,
    and against the last day; every wrong or contested record named; the reads the matching stages
    got wrong, and whether their spot outvoted them."""
    from golden import RANK
    records = read_jsonl(PIPELINE / "06_golden.jsonl")
    manifest = json.loads((PIPELINE / "06_manifest.json").read_text(encoding="utf-8"))
    reads = read_jsonl(PIPELINE / "05_reads.jsonl")
    with DET_TRUTH.open(encoding="utf-8") as f:
        truth = {t["detection_id"]: t for t in csv.DictReader(f)}
    dates = sorted({d for rec in records for d in rec["days_read"]})
    day_of = {d: i + 1 for i, d in enumerate(dates)}
    shelf = collections.defaultdict(dict)
    for day in day_of.values():
        with (ROOT / "data" / f"shelf_truth_day{day}.csv").open(encoding="utf-8") as f:
            for t in csv.DictReader(f):
                shelf[(t["store_id"], t["aisle"], t["shelf"], t["position"], t["facing"])][day] = t
    last_day = max(day_of.values())
    products = load_products()

    def facing_of(rid):
        t = truth[rid]
        return (t["store_id"], t["aisle"], t["shelf"], t["position"], t["facing"])

    mixed, records_of_facing = 0, collections.defaultdict(set)
    correctness, currency, history = collections.Counter(), collections.Counter(), collections.Counter()
    named = []
    for rec in records:
        facings = collections.Counter(facing_of(i) for i in rec["source_reads"])
        mixed += len(facings) > 1
        for fc in facings:
            records_of_facing[fc].add(rec["spot_id"])
        facing = facings.most_common(1)[0][0]
        final = shelf[facing][last_day]["actual_code"]
        for h in rec["history"]:
            for d in h["days_seen"]:
                history["right" if shelf[facing][day_of[d]]["actual_code"] == h["product_code"] else "wrong"] += 1
        if not rec["product_code"]:
            correctness[rec["status"]] += 1
            currency[rec["status"]] += 1
            if rec["status"] == "contested":
                named.append((rec, facing, None))
            continue
        on_last_seen = shelf[facing][day_of[rec["last_seen"][:10]]]["actual_code"]
        ok = rec["product_code"] == on_last_seen
        correctness["right on the day last seen" if ok else "wrong on the day last seen"] += 1
        if not ok:
            named.append((rec, facing, on_last_seen))
        if rec["product_code"] == final:
            currency["current on the last day"] += 1
        elif final == "":
            currency["shelf emptied after last seen"] += 1
        elif ok:
            currency["shelf changed after last seen"] += 1
        else:
            currency["wrong"] += 1
    never_read = [fc for fc in shelf if fc not in records_of_facing]
    empty_all_days = sum(1 for fc in never_read if all(t["actual_code"] == "" for t in shelf[fc].values()))
    split = sum(1 for v in records_of_facing.values() if len(v) > 1)

    print("SCORE  stage 6 golden record against the shelf truth")
    print(f"  records {len(records)}, truth facings {len(shelf)}: read at least once {len(records_of_facing)}, never read {len(never_read)} "
          f"(empty on every day: {empty_all_days})")
    print(f"  records holding a read of a neighbouring facing: {mixed}; facings spread over two records: {split}")
    print(f"  {'the record, on the day its product was last seen':<52} {'records':>7}")
    for k, v in sorted(correctness.items(), key=lambda kv: -kv[1]):
        print(f"  {k:<52} {v:>7}")
    print(f"  {'the record, against day ' + str(last_day) + ' (the last day)':<52} {'records':>7}")
    for k, v in sorted(currency.items(), key=lambda kv: -kv[1]):
        print(f"  {k:<52} {v:>7}")
    print(f"  history entries (a product the catalog replaced) against the truth of their days: "
          f"{history.get('right', 0)} right, {history.get('wrong', 0)} wrong")
    print("\n  every record wrong on the day last seen, and every contested one")
    for rec, facing, on_last_seen in named:
        if rec["status"] == "contested":
            print(f"  {rec['spot_id']}  contested: " + " vs ".join(f"{c['product_label']} ({', '.join(c['reads'])})" for c in rec["candidates"]))
            print(f"      truth    {product_label(products, shelf[facing][last_day]['actual_code'])}")
        else:
            print(f"  {rec['spot_id']}  answered {rec['product_code']}  {rec['product_label']} by {rec['method']} ({', '.join(rec['supporting_reads'])})")
            print(f"      truth    {product_label(products, on_last_seen)} on {rec['last_seen'][:10]}")

    print("\n  the reads the matching stages got wrong, and what their spot made of them")
    by_id = {r["detection_id"]: r for r in reads}
    rec_of = {rid: rec for rec in records for rid in rec["source_reads"]}
    for s in score({r["detection_id"]: r["match_code"] for r in reads}):
        if s["outcome"] != "wrong":
            continue
        r, rec = by_id[s["detection_id"]], rec_of[s["detection_id"]]
        print(f"  {r['detection_id']}  {r['ocr_text']!r} said {product_label(products, r['match_code'])} by {r['match_method']}")
        if r["detection_id"] in rec["supporting_reads"]:
            verdict = f"NOT outvoted: the record {rec['spot_id']} took it ({len(rec['supporting_reads'])} reads for, {len(rec['dissenting_reads'])} against)"
        elif rec["status"] == "contested":
            cands = " vs ".join(f"{len(c['reads'])} for {c['product_label']}" for c in rec["candidates"])
            verdict = f"not outvoted: {rec['spot_id']} is contested, {cands}; no product recorded"
        elif RANK[r["match_method"]] > rec["confidence_rank"]:
            verdict = (f"outranked: {rec['spot_id']} recorded {rec['product_label']} by {rec['method']} "
                       f"({len(rec['supporting_reads'])} reads) over this {r['match_method']} read")
        else:
            verdict = (f"outvoted {len(rec['supporting_reads'])} to {len([d for d in rec['dissenting_reads'] if d['product_code'] == r['match_code']])}: "
                       f"{rec['spot_id']} recorded {rec['product_label']} by {rec['method']}")
        print(f"      {verdict}")
    for cname, ok in manifest["checks"].items():
        print(f"  CHECK    {cname}: {ok}")
    clean = all(manifest["checks"].values()) and correctness.get("wrong on the day last seen", 0) == 0 and history.get("wrong", 0) == 0
    print(f"  RESULT   {'stage 6: ' + str(correctness.get('right on the day last seen', 0)) + ' records right on the day last seen, 0 wrong, ' + str(correctness.get('contested', 0)) + ' contested, ' + str(correctness.get('no answer', 0)) + ' without an answer' if clean else 'stage 6 FAILED'}")
    return clean


def score_versioning():
    """Stage 7 against the truth: the catalog changes found from the two files against the list of changes the
    catalog was given; then each record by what the change did to it, against the shelf truth on the last day."""
    from versioning import STATUS_DISCONTINUED, STATUS_NEW_SIZE, STATUS_OLD_SIZE, STATUS_RENAMED, label, view
    changes = read_jsonl(PIPELINE / "07_catalog_changes.jsonl")
    manifest = json.loads((PIPELINE / "07_manifest.json").read_text(encoding="utf-8"))
    records = read_jsonl(PIPELINE / "07_golden.jsonl")
    with CATALOG_TRUTH.open(encoding="utf-8") as f:
        truth_changes = list(csv.DictReader(f))
    with DET_TRUTH.open(encoding="utf-8") as f:
        truth = {t["detection_id"]: t for t in csv.DictReader(f)}
    dates = sorted({d for rec in records for d in rec["days_read"]})
    day_of = {d: i + 1 for i, d in enumerate(dates)}
    last_day = max(day_of.values())
    with (ROOT / "data" / f"shelf_truth_day{last_day}.csv").open(encoding="utf-8") as f:
        shelf = {(t["store_id"], t["aisle"], t["shelf"], t["position"], t["facing"]): t for t in csv.DictReader(f)}
    catalogs = {}
    for version, path in (("v1", ROOT / "data" / "catalog_raw.jsonl"), ("v2", ROOT / "data" / "catalog_v2.jsonl")):
        catalogs[version] = {p["code"]: p for p in read_jsonl(path)}

    kind_of = {"new size, new barcode": "new_size", "renamed": "renamed", "discontinued": "discontinued", "new": "new"}
    found = {(kind_of.get(c["change"], c["change"]), c["v1_code"] or "", c["v2_code"] or ""): c for c in changes}
    wanted = {(t["change"], t["v1_code"], t["v2_code"]): t for t in truth_changes}
    matched = sorted(set(found) & set(wanted))
    values_right = sum(1 for k in matched if not wanted[k]["field"] or (found[k]["old"], found[k]["new"]) == (wanted[k]["old_value"], wanted[k]["new_value"]))
    missed, extra = sorted(set(wanted) - set(found)), sorted(set(found) - set(wanted))

    def facing_of(rid):
        t = truth[rid]
        return (t["store_id"], t["aisle"], t["shelf"], t["position"], t["facing"])

    by_status = collections.defaultdict(collections.Counter)
    for rec in records:
        if not rec["product_code"]:
            continue
        facings = collections.Counter(facing_of(i) for i in rec["source_reads"])
        on_shelf = shelf[facings.most_common(1)[0][0]]["actual_code"]
        status = rec["catalog_status"]
        if status == STATUS_OLD_SIZE:
            by_status[status]["successor named by the migration is what the shelf holds" if on_shelf == view(rec, "v2") else "successor wrong"] += 1
        elif status == STATUS_DISCONTINUED:
            by_status[status]["still on the shelf on the last day" if on_shelf == rec["product_code"] else "gone from the shelf"] += 1
        else:
            by_status[status]["product on the shelf on the last day" if on_shelf == rec["product_code"]
                              else "shelf empty" if on_shelf == "" else "shelf holds another product"] += 1
        if status in (STATUS_RENAMED, STATUS_NEW_SIZE) and on_shelf == rec["product_code"]:
            by_status[status]["label is v2's" if rec["product_label"] == label(catalogs["v2"][on_shelf]) else "label is not v2's"] += 1

    print("SCORE  stage 7 versioning against the truth")
    print(f"  {'catalog changes between v1 and v2':<40} {'truth':>5} {'found':>5} {'matched':>7}")
    for kind in sorted({k[0] for k in wanted} | {k[0] for k in found}):
        print(f"  {kind:<40} {sum(1 for k in wanted if k[0] == kind):>5} {sum(1 for k in found if k[0] == kind):>5} "
              f"{sum(1 for k in matched if k[0] == kind):>7}")
    print(f"  {'all':<40} {len(wanted):>5} {len(found):>5} {len(matched):>7}   missed {len(missed)}, extra {len(extra)}, "
          f"old and new values right {values_right} of {len(matched)}")
    for k in missed:
        print(f"      missed  {k}")
    for k in extra:
        print(f"      extra   {k}")
    print(f"\n  the records a change touched, against the shelf on day {last_day} (the last day)")
    for status, counts in sorted(by_status.items(), key=lambda kv: -sum(kv[1].values())):
        print(f"  {status:<42} " + " | ".join(f"{k} {v}" for k, v in counts.most_common()))
    for cname, ok in manifest["checks"].items():
        print(f"  CHECK    {cname}: {ok}")
    leftovers = by_status[STATUS_DISCONTINUED]
    old_size = by_status[STATUS_OLD_SIZE]
    clean = (all(manifest["checks"].values()) and not missed and not extra and values_right == len(matched)
             and leftovers.get("gone from the shelf", 0) == 0 and old_size.get("successor wrong", 0) == 0
             and all(c.get("label is not v2's", 0) == 0 for c in by_status.values()))
    print(f"  RESULT   {'stage 7: ' + str(len(matched)) + ' of ' + str(len(wanted)) + ' catalog changes found, ' + str(len(extra)) + ' extra; '
                        + str(leftovers.get('still on the shelf on the last day', 0)) + ' of ' + str(sum(leftovers.values()))
                        + ' dropped products still on the shelf; successor right ' + str(old_size.get('successor named by the migration is what the shelf holds', 0))
                        + ' of ' + str(sum(old_size.values())) if clean else 'stage 7 FAILED'}")
    return clean


def score_serve():
    """Stage 8 against the shelf truth: every verdict on the last day's shelf, compliance per store next to the
    truth's, and what the facings not seen on the latest walk really hold."""
    rows = read_jsonl(PIPELINE / "08_realogram.jsonl")
    manifest = json.loads((PIPELINE / "08_manifest.json").read_text(encoding="utf-8"))
    days = sorted(ROOT.glob("data/shelf_truth_day*.csv"))
    shelf = {}
    for day, path in enumerate(days, start=1):
        with path.open(encoding="utf-8") as f:
            for t in csv.DictReader(f):
                shelf.setdefault((t["store_id"], t["aisle"], int(t["shelf"]), int(t["position"]), int(t["facing"])), {})[day] = t
    last = len(days)
    products = load_products()
    successor = {c["v1_code"]: c["v2_code"] for c in read_jsonl(PIPELINE / "07_catalog_changes.jsonl") if c["change"] == "new size, new barcode"}

    def truth_of(r):
        return shelf[(r["store_id"], r["aisle"], r["shelf"], r["position"], r["facing"])]

    outcome = collections.defaultdict(collections.Counter)
    stale = collections.Counter()
    compliance = collections.defaultdict(lambda: {"planned": 0, "realogram": 0, "truth": 0})
    wrong = []
    for r in rows:
        t = truth_of(r)
        on_shelf = t[last]["actual_code"]
        v = r["verdict"]
        if v in ("as planned", "as planned, new size", "as planned, catalog dropped it"):
            if on_shelf == r["product_code"]:
                key = "product on the shelf"
            elif r["seen_on_latest_walk"] is False:   # the record says so itself: a later walk gave no read of this product here
                key = "changed since last seen: empty now" if on_shelf == "" else "changed since last seen: another product now"
            else:
                key = "another product"
        elif v == "different product":
            key = "that product, misplaced" if on_shelf == r["product_code"] and t[last]["status"] == "misplaced" else "wrong"
        elif v == "contested":
            key = "truth is one of the candidates" if on_shelf in {c["product_code"] for c in r["candidates"]} else "truth is neither"
        elif v == "open":
            key = "a product stands here" if on_shelf else "shelf empty now"
        else:
            key = "empty on every day" if all(x["actual_code"] == "" for x in t.values()) else "stocked on some day, never read"
        outcome[v][key] += 1
        if key in ("another product", "wrong", "truth is neither", "stocked on some day, never read"):
            wrong.append((r, t))
        if r["seen_on_latest_walk"] is False:
            stale["shelf empty now" if on_shelf == "" else "product still there" if on_shelf == r["product_code"] else "another product now"] += 1
        c = compliance[r["store_id"]]
        c["planned"] += 1
        c["realogram"] += v in manifest["compliant_verdicts"]
        c["truth"] += on_shelf == t[last]["planned_code"] or (on_shelf != "" and successor.get(t[last]["planned_code"]) == on_shelf)

    print("SCORE  stage 8 serve against the shelf truth")
    print(f"  realogram rows {len(rows)}, truth facings {len(shelf)}, days {last}")
    print(f"  {'verdict':<34} {'rows':>5}   against the shelf on day {last}")
    for v, counts in sorted(outcome.items(), key=lambda kv: -sum(kv[1].values())):
        print(f"  {v:<34} {sum(counts.values()):>5}   " + " | ".join(f"{k} {n}" for k, n in counts.most_common()))
    print(f"  {'not seen on the latest walk':<34} {sum(stale.values()):>5}   " + " | ".join(f"{k} {n}" for k, n in stale.most_common()))
    print(f"\n  {'compliance':<10} {'planned':>7} {'realogram says':>14} {'truth says':>10}")
    for store, c in sorted(compliance.items()):
        print(f"  {store:<10} {c['planned']:>7} {c['realogram']:>8} {100 * c['realogram'] / c['planned']:>4.1f}% {c['truth']:>5} {100 * c['truth'] / c['planned']:>4.1f}%")
    tot = {k: sum(c[k] for c in compliance.values()) for k in ("planned", "realogram", "truth")}
    print(f"  {'all':<10} {tot['planned']:>7} {tot['realogram']:>8} {100 * tot['realogram'] / tot['planned']:>4.1f}% {tot['truth']:>5} {100 * tot['truth'] / tot['planned']:>4.1f}%")
    if wrong:
        print("\n  every verdict the shelf contradicts")
        for r, t in wrong:
            print(f"  {r['spot_id'] or r['store_id'] + '-' + r['aisle'] + '-S' + str(r['shelf']) + ' slot ' + str(r['slot'])}  {r['verdict']}: "
                  f"record {r['product_label']}; truth {product_label(products, t[last]['actual_code']) if t[last]['actual_code'] else 'empty'}")
    for cname, ok in manifest["checks"].items():
        print(f"  CHECK    {cname}: {ok}")
    clean = all(manifest["checks"].values()) and not wrong
    print(f"  RESULT   {'stage 8: ' + str(len(rows) - len(wrong)) + ' of ' + str(len(rows)) + ' verdicts stand against the shelf; the ' + str(sum(stale.values())) + ' not seen on the latest walk hold ' + str(stale.get('shelf empty now', 0)) + ' empty facings' if clean else 'stage 8 FAILED'}")
    return clean


if __name__ == "__main__":
    sys.exit(main())
