"""Stage 4, other days: a read still open is answered by the same shelf position's answered reads on
other days, when they agree, when the product was in the catalog on the read's day, and when the
read's own text does not contradict it. Free, so it runs before anything that costs money.

    python pipeline/match_crossday.py

Reads   data/pipeline/03_reads.jsonl         stage 3's output
        data/catalog_raw.jsonl               catalog v1
        data/catalog_v2.jsonl                catalog v2
        data/catalog_v2.meta.json            when v2 took effect
Writes  data/pipeline/04_reads.jsonl         the same rows, same order; match_* filled in for the reads
                                             answered here, with match_score and match_support
        data/pipeline/04_manifest.json       the counts

A shelf position is the same store, aisle and shelf within 0.08 m along the shelf. Measured on this
feed: the position's other-day answers alone are right 555 times and wrong 11; every wrong one is a
product whose size and barcode changed between the days, where the neighbour names the new code for a
read of the old pack. Requiring the code to have been in the catalog version in effect on the read's
day leaves 1 wrong; requiring the read's text to score at least 60 against the product leaves none:
557 right, 0 wrong, 163 still open. At 0.06 m the same rules give 524 right; at 0.10 m, 570 right and
2 wrong. A position whose other-day answers disagree gives nothing and says so.

This is weaker evidence than a read's own barcode or text: it says what stood here yesterday, not what
stands here now, and a restocked or misplaced product breaks it. The method is recorded as 'other day'
so the golden record can rank it below the rest.
"""
import json
import sys
from pathlib import Path

from rapidfuzz import fuzz

import normalize                                 # pipeline/normalize.py
from match_exact import catalog_text, CATALOGS   # the catalog row as the label prints it

ROOT = Path(__file__).resolve().parents[1]
IN = ROOT / "data" / "pipeline" / "03_reads.jsonl"
V2_META = ROOT / "data" / "catalog_v2.meta.json"
OUT = ROOT / "data" / "pipeline" / "04_reads.jsonl"
MANIFEST = ROOT / "data" / "pipeline" / "04_manifest.json"

SAME_POSITION_M = 0.08   # metres along the shelf
TEXT_MIN = 60            # partial_ratio of the read's text against the product named by the other days


def load_catalog():
    """code -> set of versions, code -> normalized text, and the date v2 took effect."""
    versions_of, text_of = {}, {}
    for version, path in CATALOGS.items():
        for line in path.open(encoding="utf-8"):
            if line.strip():
                p = json.loads(line)
                versions_of.setdefault(p["code"], set()).add(version)
                text_of.setdefault(p["code"], normalize.normalize_text(catalog_text(p)))
    effective_from = json.loads(V2_META.read_text(encoding="utf-8"))["effective_from"]
    return versions_of, text_of, effective_from


def version_on(day, effective_from):
    """Which catalog version was in effect on a day (YYYY-MM-DD): v2 from its effective date, v1 before."""
    return "v2" if day >= effective_from else "v1"


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    if not IN.exists():
        sys.exit(f"STOP: {IN.relative_to(ROOT)} not found. Run pipeline/match_fuzzy.py first.")
    rows = [json.loads(line) for line in IN.open(encoding="utf-8") if line.strip()]
    before = [dict(r) for r in rows]
    versions_of, text_of, effective_from = load_catalog()

    by_shelf = {}
    for r in rows:
        by_shelf.setdefault((r["store_id"], r["aisle"], r["shelf"]), []).append(r)
    answered_before = {r["detection_id"]: r["match_code"] for r in rows if r["match_code"]}   # fixed: no chaining

    counts = {"other day": 0, "no neighbour": 0, "neighbours disagree": 0, "text disagrees": 0}
    for r in rows:
        if r["match_code"]:
            continue
        day = r["ts"][:10]
        version = version_on(day, effective_from)
        neighbours = [o for o in by_shelf[(r["store_id"], r["aisle"], r["shelf"])]
                      if o["detection_id"] in answered_before and o["ts"][:10] != day
                      and abs(o["x"] - r["x"]) <= SAME_POSITION_M
                      and version in versions_of[answered_before[o["detection_id"]]]]   # in the catalog on the read's day
        codes = {answered_before[o["detection_id"]] for o in neighbours}
        if not neighbours:
            counts["no neighbour"] += 1
            r["match_note"] += "; other days: no answered read at this position"
            continue
        if len(codes) > 1:
            counts["neighbours disagree"] += 1
            r["match_note"] += f"; other days: {len(neighbours)} reads name {len(codes)} products"
            continue
        code = next(iter(codes))
        score = round(fuzz.partial_ratio(r["text_norm"], text_of[code]))
        if score < TEXT_MIN:
            counts["text disagrees"] += 1
            r["match_note"] += f"; other days: {len(neighbours)} reads name {code}, text scores {score} below {TEXT_MIN}"
            continue
        r["match_code"], r["match_method"] = code, "other day"
        r["match_catalog"] = " ".join(sorted(versions_of[code]))
        r["match_score"], r["match_support"] = score, len(neighbours)
        r.pop("match_note", None)
        counts["other day"] += 1

    by_method = {}
    for r in rows:
        key = r["match_method"] or "no answer"
        by_method[key] = by_method.get(key, 0) + 1
    answered = sum(1 for r in rows if r["match_code"])
    checks = {
        "rows in = rows out": len(before) == len(rows),
        "same ids in the same order": [b["detection_id"] for b in before] == [r["detection_id"] for r in rows],
        "every original field unchanged": all(all(r[k] == b[k] for k in b if not k.startswith("match_")) for b, r in zip(before, rows)),
        "no earlier answer changed": all(not b["match_code"] or (b["match_code"], b["match_method"]) == (r["match_code"], r["match_method"])
                                         for b, r in zip(before, rows)),
        "every answer is a catalog code": all(r["match_code"] in versions_of for r in rows if r["match_code"]),
        "every answer here was in the catalog on the read's day": all(
            version_on(r["ts"][:10], effective_from) in versions_of[r["match_code"]] for r in rows if r["match_method"] == "other day"),
        "answered + no answer = rows": answered + by_method.get("no answer", 0) == len(rows),
        "every unanswered row says why": all(r.get("match_note") for r in rows if not r["match_code"]),
    }

    print(f"STAGE 4  OTHER DAYS   {IN.relative_to(ROOT)} -> {OUT.relative_to(ROOT)}")
    print(f"  rows in             {len(rows):>6}   open before this stage {sum(1 for b in before if not b['match_code'])}")
    print(f"  catalog v2 from     {effective_from}")
    print(f"  answered here       {counts['other day']:>6}   left open: no neighbour {counts['no neighbour']} | "
          f"neighbours disagree {counts['neighbours disagree']} | text disagrees {counts['text disagrees']}")
    print(f"  answered in all     {answered:>6}   " + " | ".join(f"{k} {v}" for k, v in sorted(by_method.items(), key=lambda kv: -kv[1])))
    print(f"  no answer           {by_method.get('no answer', 0):>6}")
    for name, ok in checks.items():
        print(f"  CHECK    {name}: {ok}")
    print("\n  one example answered here, and one left open for each reason")
    shown = set()
    for r in rows:
        key = r["match_method"] if r["match_method"] == "other day" else (r.get("match_note", "").split("other days: ")[-1][:20] if not r["match_code"] else None)
        if key and key not in shown:
            shown.add(key)
            tail = f"-> {r['match_code']}  score {r['match_score']}, {r['match_support']} reads" if r["match_code"] else r["match_note"].split("; ")[-1]
            print(f"  {r['detection_id']}  {r['ocr_text']!r}  {tail}")

    if not all(checks.values()):
        sys.exit("STOP: a check failed. Nothing was written.")
    OUT.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8", newline="\n")
    manifest = {
        "stage": 4, "name": "other days",
        "input": {"path": IN.relative_to(ROOT).as_posix(), "rows": len(rows), "catalog_v2_effective_from": effective_from},
        "output": {"path": OUT.relative_to(ROOT).as_posix(), "rows": len(rows), "fields_added": ["match_support"]},
        "rules": {"same_position_m": SAME_POSITION_M, "text_min": TEXT_MIN, "catalog_version_by_date": True},
        "answered_here": counts["other day"], "left_open_by_reason": {k: v for k, v in counts.items() if k != "other day"},
        "answered": answered, "by_method": by_method,
        "checks": checks,
    }
    MANIFEST.write_text(json.dumps(manifest, indent=1) + "\n", encoding="utf-8", newline="\n")
    print(f"\nWrote {OUT.relative_to(ROOT)} ({len(rows)} rows) and {MANIFEST.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
