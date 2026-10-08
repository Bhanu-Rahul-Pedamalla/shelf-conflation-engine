"""The simplest matchers anyone would write, and how far they get.

    python pipeline/baseline.py

Reads   data/catalog_raw.jsonl    the catalog
        data/detections.jsonl     what the badge reported
Writes  data/baseline_results.csv   one row per detection: the answer and how it scored

The matchers here never read the truth file. Only score.py does.
Best case for a naive matcher: catalog_text() joins brand, name and size the same
way the simulated label was built. A real label would not line up this neatly.
"""
import csv
import json
import sys
from pathlib import Path

import score  # pipeline/score.py

ROOT = Path(__file__).resolve().parents[1]
CATALOG = ROOT / "data" / "catalog_raw.jsonl"
DETECTIONS = ROOT / "data" / "detections.jsonl"
RESULTS = ROOT / "data" / "baseline_results.csv"


def load_jsonl(path):
    with path.open(encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def load_reads(path):
    """The reads file as the badge sent it. A line these matchers cannot read is skipped and counted.

    The simplest matchers take what they can read and leave the rest: a line that is not JSON, or a
    record without an id, a text or a barcode field. Anything else is matched as it is.
    """
    rows, skipped = [], 0
    with path.open(encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                skipped += 1
                continue
            if not (isinstance(d, dict) and isinstance(d.get("detection_id"), str)
                    and isinstance(d.get("ocr_text"), str) and "read_barcode" in d):
                skipped += 1
                continue
            rows.append(d)
    return rows, skipped


def catalog_text(p):
    """Brand, name and size of a catalog row as one string."""
    brand = (p.get("brands") or "").strip()
    name = (p.get("product_name") or "").strip()
    size = (p.get("quantity") or "").strip()
    parts = [name] if brand.casefold() in name.casefold() else [brand, name]
    return " ".join(part for part in parts + [size] if part)


def build_lookups(catalog):
    """Three lookups: text -> codes, lower-cased text -> codes, and the set of codes."""
    by_text, by_text_nocase = {}, {}
    for p in catalog:
        text = catalog_text(p)
        by_text.setdefault(text, set()).add(p["code"])
        by_text_nocase.setdefault(text.casefold(), set()).add(p["code"])
    return by_text, by_text_nocase, {p["code"] for p in catalog}


def only(codes):
    """An answer only when exactly one catalog row fits. Two rows fitting is not an answer."""
    return next(iter(codes)) if codes and len(codes) == 1 else None


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    catalog = load_jsonl(CATALOG)
    detections, skipped = load_reads(DETECTIONS)
    by_text, by_text_nocase, codes = build_lookups(catalog)

    exact, nocase, barcode, together = {}, {}, {}, {}
    for d in detections:
        i = d["detection_id"]
        if i in together:      # the first row with an id is the one answered; a later row with the same id is a repeat
            continue
        exact[i] = only(by_text.get(d["ocr_text"]))
        nocase[i] = only(by_text_nocase.get(d["ocr_text"].casefold()))
        barcode[i] = d["read_barcode"] if d["read_barcode"] in codes else None
        together[i] = barcode[i] or exact[i] or nocase[i]   # most trusted first

    results = {
        "exact text": score.score(exact),
        "exact text, ignoring case": score.score(nocase),
        "barcode": score.score(barcode),
        "all three together": score.score(together),
    }

    # A re-sent row repeats an id. The naive matchers answer per id, so repeats change nothing here.
    # A damaged row that still carries an id and a text is matched like any other; it has no truth
    # row, so it is not scored.
    print(f"\nNAIVE BASELINE - {len(detections) + skipped} rows in the file, {skipped} skipped as unreadable, "
          f"{len(together)} distinct ids, {len(catalog)} products in catalog v1\n")
    score.print_table(results)

    rows = results["all three together"]
    print("\nALL THREE TOGETHER, BY DAMAGE TO THE TEXT")
    print(f"  {'damage':<22} {'detections':>10} {'right':>6} {'no answer':>10}")
    by_count = {}
    for row in rows:
        k = 0 if row["damage"] == "none" else len(row["damage"].split("+"))
        by_count.setdefault(k, []).append(row)
    for k in sorted(by_count):
        t = score.tally(by_count[k])
        print(f"  {str(k) + ' kinds':<22} {len(by_count[k]):>10} {t['right']:>6} {t['no answer']:>10}")
    for kind in ("unit", "truncate", "drop", "case"):
        hit = [row for row in rows if kind in row["damage"].split("+")]
        t = score.tally(hit)
        print(f"  {'any with ' + kind:<22} {len(hit):>10} {t['right']:>6} {t['no answer']:>10}")

    t = score.tally(rows)
    print(f"\nLEFT UNMATCHED   {t['no answer']} of {len(rows)} detections have no answer, {t['wrong']} are wrong")

    # One row per detection: what the three matchers together answered, and how it scored.
    # Read by the replay page, so the page shows these results and computes none of its own.
    with RESULTS.open("w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f, lineterminator="\n")
        writer.writerow(["detection_id", "method", "answer", "outcome"])
        for row in rows:
            i = row["detection_id"]
            method = "barcode" if barcode[i] else "exact text" if exact[i] else "text ignoring case" if nocase[i] else ""
            writer.writerow([i, method, row["answer"] or "", row["outcome"]])
    print(f"Wrote {RESULTS.relative_to(ROOT)} ({len(rows)} rows)")


if __name__ == "__main__":
    main()
