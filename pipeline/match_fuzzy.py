"""Stage 3, close spelling: the reads the free checks left. First the reads of one shelf spot on one day
are grouped, so a spot answered once is answered for all its reads. Then each read still open is
compared with the catalog by spelling, and answered only when one row fits clearly better than any other.

    python pipeline/match_fuzzy.py

Reads   data/pipeline/02_reads.jsonl         stage 2's output
        data/catalog_raw.jsonl               catalog v1
        data/catalog_v2.jsonl                catalog v2
Writes  data/pipeline/03_reads.jsonl         the same rows, same order, plus spot_id; match_* filled in for
                                             the reads answered here, with match_score and match_gap
        data/pipeline/03_manifest.json       the counts

Every rule here was measured on the feed before it was chosen; the scorer's --curve option re-measures.

THE SPOT. Two reads are the same shelf spot on the same day when they are in one frame and their boxes
overlap by half or more (a second box drawn on one product: 288 pairs on this feed, every one the same
facing, where an overlap of 0.3 lets six neighbours through), or when two badges on the same day read
the same store, aisle and shelf within 0.06 m along the shelf (facings are 0.2 m apart and a read's
position jitters by 0.03 m: 174 pairs right, 1 wrong; at 0.10 m, 4 wrong). A spot takes the answer of
its best-answered read: a barcode over an exact text over a majority. A spot whose answered reads
disagree gives nothing to its other reads and says so.

THE SPELLING. partial_ratio on the normalized text, because the damage on this feed is a cut-off end
and lost characters, which is what a best-substring score forgives; ratio, token_sort and token_set
were measured and each gives fewer right at the same wrong count. Candidates are the catalog rows whose
first word is the read's first word, every row when no first word matches: measured, the block gains
about 70 right at the same one wrong; blocking by the aisle's already-answered products was measured
and rejected, since it removes the right product from the list exactly when it has not been read yet.
An answer needs a score of at least 80 and a gap of at least 10 to the runner-up. The gap is the real
guard: with it the right count barely moves between a threshold of 50 and one of 80 (1,837 to 1,832,
one wrong throughout); without it the same thresholds give about 190 wrong. 80 is the floor at which
the read still shares most of its letters with the row it names. The whole table is printed by
python pipeline/score.py fuzzy --curve.
"""
import json
import sys
from pathlib import Path

from rapidfuzz import fuzz, process

import normalize                          # pipeline/normalize.py
from match_exact import catalog_text, CATALOGS   # the catalog row as the label prints it

ROOT = Path(__file__).resolve().parents[1]
IN = ROOT / "data" / "pipeline" / "02_reads.jsonl"
OUT = ROOT / "data" / "pipeline" / "03_reads.jsonl"
MANIFEST = ROOT / "data" / "pipeline" / "03_manifest.json"

BOX_OVERLAP = 0.5       # intersection over union of two boxes in one frame
SAME_SPOT_M = 0.06      # metres along the shelf, between two badges' reads of one facing
SCORE_MIN = 80          # partial_ratio, 0 to 100
GAP_MIN = 10            # best minus runner-up
RANK = {"barcode": 0, "exact text": 1, "close spelling": 2, "same spot": 3}


def overlap(a, b):
    """Intersection over union of two boxes given as x, y, width, height."""
    ix = max(0, min(a[0] + a[2], b[0] + b[2]) - max(a[0], b[0]))
    iy = max(0, min(a[1] + a[3], b[1] + b[3]) - max(a[1], b[1]))
    inter = ix * iy
    return inter / (a[2] * a[3] + b[2] * b[3] - inter) if inter else 0.0


def build_spots(rows):
    """detection_id -> spot_id. Union of the two rules; ids named by store, day, aisle, shelf and a number."""
    parent = {r["detection_id"]: r["detection_id"] for r in rows}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def join(a, b):
        parent[find(a)] = find(b)

    by_frame, by_shelf = {}, {}
    for r in rows:
        by_frame.setdefault(r["image_ref"], []).append(r)
        by_shelf.setdefault((r["store_id"], r["ts"][:10], r["aisle"], r["shelf"]), []).append(r)
    for rs in by_frame.values():                       # a second box on one product, in one frame
        for i, a in enumerate(rs):
            for b in rs[i + 1:]:
                if overlap(a["bbox"], b["bbox"]) >= BOX_OVERLAP:
                    join(a["detection_id"], b["detection_id"])
    for rs in by_shelf.values():                       # two badges, one facing, one day
        rs.sort(key=lambda r: r["x"])
        for i, a in enumerate(rs):
            for b in rs[i + 1:]:
                if b["x"] - a["x"] > SAME_SPOT_M:
                    break
                if a["walk_id"] != b["walk_id"]:
                    join(a["detection_id"], b["detection_id"])

    spot_of, number = {}, {}
    for r in rows:                                     # rows are in time order, so numbering is stable
        root = find(r["detection_id"])
        if root not in spot_of:
            key = (r["store_id"], r["ts"][:10], r["aisle"], r["shelf"])
            number[key] = number.get(key, 0) + 1
            spot_of[root] = f"{key[0]}-{key[1]}-{key[2]}-S{key[3]}-{number[key]:03d}"
    return {r["detection_id"]: spot_of[find(r["detection_id"])] for r in rows}


def spot_answer(members):
    """The spot's answer from its answered reads: (code, method) or (None, 'disagree'|'none')."""
    answered = [m for m in members if m["match_code"]]
    if not answered:
        return None, "none"
    if len({m["match_code"] for m in answered}) > 1:
        return None, "disagree"
    best = min(answered, key=lambda m: RANK[m["match_method"]])
    return best["match_code"], best["match_method"]


def propagate(rows, spot_of, counts):
    """Every unanswered read in a spot with one clear answer takes it, as method 'same spot'."""
    members = {}
    for r in rows:
        members.setdefault(spot_of[r["detection_id"]], []).append(r)
    for spot, ms in members.items():
        code, how = spot_answer(ms)
        for m in ms:
            if m["match_code"]:
                continue
            if code:
                m["match_code"], m["match_method"], m["match_catalog"] = code, "same spot", next(
                    x["match_catalog"] for x in ms if x["match_code"] == code and x["match_method"] != "same spot")
                m["match_note"] = f"from a {how} read of the same spot"
                counts["same spot"] = counts.get("same spot", 0) + 1
            elif how == "disagree":
                m["match_note"] = m.get("match_note", "") + "; the spot's answered reads disagree"
                counts["spot disagrees"] = counts.get("spot disagrees", 0) + 1


def load_candidates():
    """normalized catalog text -> set of codes, code -> versions, first word -> texts."""
    codes_of_text, versions_of, by_first = {}, {}, {}
    for version, path in CATALOGS.items():
        for line in path.open(encoding="utf-8"):
            if line.strip():
                p = json.loads(line)
                text = normalize.normalize_text(catalog_text(p))
                codes_of_text.setdefault(text, set()).add(p["code"])
                versions_of.setdefault(p["code"], set()).add(version)
    for text in codes_of_text:
        by_first.setdefault(text.split()[0], []).append(text)
    return codes_of_text, versions_of, by_first


def closest(text, codes_of_text, by_first):
    """The best and runner-up catalog texts for a read, by partial_ratio within the first-word block."""
    pool = by_first.get(text.split()[0]) if text else None
    pool = pool or list(codes_of_text)
    found = process.extract(text, pool, scorer=fuzz.partial_ratio, limit=2)
    best, score = found[0][0], found[0][1]
    second = found[1][1] if len(found) > 1 else 0
    return best, round(score), round(score - second)


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    if not IN.exists():
        sys.exit(f"STOP: {IN.relative_to(ROOT)} not found. Run pipeline/match_exact.py first.")
    rows = [json.loads(line) for line in IN.open(encoding="utf-8") if line.strip()]
    before = [dict(r) for r in rows]
    codes_of_text, versions_of, by_first = load_candidates()

    spot_of = build_spots(rows)
    for r in rows:
        r["spot_id"] = spot_of[r["detection_id"]]
    sizes = {}
    for spot in set(spot_of.values()):
        n = sum(1 for r in rows if spot_of[r["detection_id"]] == spot)
        sizes[n] = sizes.get(n, 0) + 1
    counts = {}
    propagate(rows, spot_of, counts)                   # the free checks' answers reach their spot-mates

    open_rows = [r for r in rows if not r["match_code"]]
    for r in open_rows:                                # then the spelling
        if not r["text_norm"]:
            continue
        best, score, gap = closest(r["text_norm"], codes_of_text, by_first)
        codes = codes_of_text[best]
        if score >= SCORE_MIN and gap >= GAP_MIN and len(codes) == 1:
            r["match_code"], r["match_method"] = next(iter(codes)), "close spelling"
            r["match_catalog"] = " ".join(sorted(versions_of[r["match_code"]]))
            r["match_score"], r["match_gap"] = score, gap
            r.pop("match_note", None)
            counts["close spelling"] = counts.get("close spelling", 0) + 1
        else:
            why = (f"fits {len(codes)} rows" if len(codes) > 1 else
                   f"best {score} below {SCORE_MIN}" if score < SCORE_MIN else f"gap {gap} below {GAP_MIN}")
            r["match_note"] = f"{r.get('match_note', '')}; close spelling: {why}".strip("; ")
    propagate(rows, spot_of, counts)                   # a spelling answer reaches its spot-mates too

    by_method = {}
    for r in rows:
        key = r["match_method"] or "no answer"
        by_method[key] = by_method.get(key, 0) + 1
    answered = sum(1 for r in rows if r["match_code"])
    checks = {
        "rows in = rows out": len(before) == len(rows),
        "same ids in the same order": [b["detection_id"] for b in before] == [r["detection_id"] for r in rows],
        "every original field unchanged": all(all(r[k] == b[k] for k in b if not k.startswith("match_")) for b, r in zip(before, rows)),
        "no stage 2 answer changed": all(not b["match_code"] or (b["match_code"], b["match_method"]) == (r["match_code"], r["match_method"])
                                         for b, r in zip(before, rows)),
        "every answer is a catalog code": all(r["match_code"] in versions_of for r in rows if r["match_code"]),
        "answered + no answer = rows": answered + by_method.get("no answer", 0) == len(rows),
        "every unanswered row says why": all(r.get("match_note") for r in rows if not r["match_code"]),
        "every read has a spot": all(r["spot_id"] for r in rows),
    }

    print(f"STAGE 3  CLOSE SPELLING   {IN.relative_to(ROOT)} -> {OUT.relative_to(ROOT)}")
    print(f"  rows in             {len(rows):>6}")
    print(f"  spots               {len(set(spot_of.values())):>6}   sizes " + " | ".join(f"{k} reads: {v}" for k, v in sorted(sizes.items())))
    print(f"  answered here       {counts.get('same spot', 0) + counts.get('close spelling', 0):>6}   "
          f"same spot {counts.get('same spot', 0)} | close spelling {counts.get('close spelling', 0)} | spot disagrees {counts.get('spot disagrees', 0)}")
    print(f"  answered in all     {answered:>6}   " + " | ".join(f"{k} {v}" for k, v in sorted(by_method.items(), key=lambda kv: -kv[1])))
    print(f"  no answer           {by_method.get('no answer', 0):>6}")
    for name, ok in checks.items():
        print(f"  CHECK    {name}: {ok}")
    print("\n  one example per method answered here")
    shown = set()
    for r in rows:
        if r["match_method"] in ("same spot", "close spelling") and r["match_method"] not in shown:
            shown.add(r["match_method"])
            extra = f"  score {r['match_score']} gap {r['match_gap']}" if "match_score" in r else f"  {r['match_note']}"
            print(f"  {r['match_method']:<15} {r['detection_id']}  {r['ocr_text']!r} -> {r['match_code']}{extra}")

    if not all(checks.values()):
        sys.exit("STOP: a check failed. Nothing was written.")
    OUT.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8", newline="\n")
    manifest = {
        "stage": 3, "name": "close spelling",
        "input": {"path": IN.relative_to(ROOT).as_posix(), "rows": len(rows)},
        "output": {"path": OUT.relative_to(ROOT).as_posix(), "rows": len(rows),
                   "fields_added": ["spot_id", "match_score", "match_gap"]},
        "rules": {"box_overlap": BOX_OVERLAP, "same_spot_m": SAME_SPOT_M, "scorer": "partial_ratio",
                  "block": "first word", "score_min": SCORE_MIN, "gap_min": GAP_MIN},
        "spots": {"count": len(set(spot_of.values())), "sizes": {str(k): v for k, v in sorted(sizes.items())}},
        "answered_here": counts, "answered": answered, "by_method": by_method,
        "checks": checks,
    }
    MANIFEST.write_text(json.dumps(manifest, indent=1) + "\n", encoding="utf-8", newline="\n")
    print(f"\nWrote {OUT.relative_to(ROOT)} ({len(rows)} rows) and {MANIFEST.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
