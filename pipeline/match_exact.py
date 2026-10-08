"""Stage 2, exact match: the two free checks. A barcode that is in the catalog, or a normalized text
that equals exactly one catalog row's normalized text, names the product. Everything else waits.

    python pipeline/match_exact.py

Reads   data/pipeline/01_reads.jsonl         stage 1's output
        data/catalog_raw.jsonl               catalog v1
        data/catalog_v2.jsonl                catalog v2
Writes  data/pipeline/02_reads.jsonl         the same rows, same order, plus match_code, match_method,
                                             match_catalog and, when there is no answer, match_note
        data/pipeline/02_manifest.json       the counts

Both catalog versions are searched at once, and the answer records which version holds the code
(v1, v2 or both). Measured on this feed before deciding: no normalized text maps to different
codes in the two versions, and no read from before v2 took effect is answered by a text that
exists only in v2, so searching both costs nothing and keeps a product that v2 dropped nameable
while its stock is still on the shelf. Which version was in effect on the read's day is the
golden record's question, not this stage's.

A barcode wins over text. On this feed the two never disagree (measured: 0 conflicts), and a
printed code is a smaller claim than a read line of text. A text that fits two catalog rows is
not an answer: one catalog row appears twice under two codes, and guessing between them would
be a wrong answer half the time.
"""
import json
import sys
from pathlib import Path

import normalize   # pipeline/normalize.py: the one text form

ROOT = Path(__file__).resolve().parents[1]
IN = ROOT / "data" / "pipeline" / "01_reads.jsonl"
CATALOGS = {"v1": ROOT / "data" / "catalog_raw.jsonl", "v2": ROOT / "data" / "catalog_v2.jsonl"}
OUT = ROOT / "data" / "pipeline" / "02_reads.jsonl"
MANIFEST = ROOT / "data" / "pipeline" / "02_manifest.json"


def gs1_check_digit_ok(code):
    """The GS1 check digit, for GTIN-12, -13 and -14. Weights 3 and 1 from the right, the last digit
    makes the sum a multiple of ten. The catalog's 8-digit codes are not GS1 codes (11 of 20 fail the
    GTIN-8 rule), so an 8-digit barcode is matched as a plain string and not checked."""
    if len(code) not in (12, 13, 14) or not code.isdigit():
        return True
    total = sum(int(d) * (3 if i % 2 == 0 else 1) for i, d in enumerate(reversed(code[:-1])))
    return (10 - total % 10) % 10 == int(code[-1])


# the rule is proven to fire before it is used: a real code, and the same code with its last digit off
assert gs1_check_digit_ok("0049000024692") and not gs1_check_digit_ok("0049000024693")


def catalog_text(p):
    """A catalog row as the label would print it: brand, name and size. The brand is not repeated
    when the name already carries it."""
    brand = (p.get("brands") or "").strip()
    name = (p.get("product_name") or "").strip()
    size = (p.get("quantity") or "").strip()
    parts = [name] if brand.casefold() in name.casefold() else [brand, name]
    return " ".join(part for part in parts + [size] if part)


def load_catalogs():
    """code -> set of versions it is in, and normalized text -> set of codes, over both versions."""
    versions_of, codes_of_text = {}, {}
    for version, path in CATALOGS.items():
        for line in path.open(encoding="utf-8"):
            if not line.strip():
                continue
            p = json.loads(line)
            versions_of.setdefault(p["code"], set()).add(version)
            codes_of_text.setdefault(normalize.normalize_text(catalog_text(p)), set()).add(p["code"])
    return versions_of, codes_of_text


def match(r, versions_of, codes_of_text):
    """One read -> (code, method, note). code is None when there is no answer, and note says why."""
    barcode = r["read_barcode"]
    if barcode is not None:
        if not gs1_check_digit_ok(barcode):
            return None, None, "barcode fails its check digit"
        if barcode in versions_of:
            return barcode, "barcode", None
        text_note = "barcode not in the catalog"
    else:
        text_note = "no barcode"
    codes = codes_of_text.get(r["text_norm"], set())
    if len(codes) == 1:
        return next(iter(codes)), "exact text", None
    if not r["text_norm"]:
        return None, None, f"{text_note}, no text"
    return None, None, f"{text_note}, text fits {len(codes)} rows"


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    if not IN.exists():
        sys.exit(f"STOP: {IN.relative_to(ROOT)} not found. Run pipeline/normalize.py first.")
    rows = [json.loads(line) for line in IN.open(encoding="utf-8") if line.strip()]
    versions_of, codes_of_text = load_catalogs()

    out, by_method, by_note, by_catalog, by_walk = [], {}, {}, {}, {}
    both = conflicts = 0
    for r in rows:
        code, method, note = match(r, versions_of, codes_of_text)
        if r["read_barcode"] in versions_of and len(codes_of_text.get(r["text_norm"], ())) == 1:
            both += 1                                    # a barcode and a text answer were both there
            conflicts += next(iter(codes_of_text[r["text_norm"]])) != r["read_barcode"]
        catalog = " ".join(sorted(versions_of[code])) if code else None
        o = {**r, "match_code": code, "match_method": method, "match_catalog": catalog}
        if note:
            o["match_note"] = note
        out.append(o)
        key = method or "no answer"
        by_method[key] = by_method.get(key, 0) + 1
        if note:
            by_note[note] = by_note.get(note, 0) + 1
        if catalog:
            by_catalog[catalog] = by_catalog.get(catalog, 0) + 1
        w = by_walk.setdefault((r["store_id"], r["walk_id"]), {"rows": 0, "barcode": 0, "exact text": 0, "no answer": 0})
        w["rows"] += 1
        w[key] += 1

    answered = sum(1 for o in out if o["match_code"])
    checks = {
        "rows in = rows out": len(rows) == len(out),
        "same ids in the same order": [r["detection_id"] for r in rows] == [o["detection_id"] for o in out],
        "every original field unchanged": all({k: v for k, v in o.items() if not k.startswith("match_")} == r
                                              for r, o in zip(rows, out)),
        "every answer is a catalog code": all(o["match_code"] in versions_of for o in out if o["match_code"]),
        "answered + no answer = rows": answered + by_method.get("no answer", 0) == len(out),
        "every unanswered row says why": all(o.get("match_note") for o in out if not o["match_code"]),
    }

    print(f"STAGE 2  EXACT MATCH   {IN.relative_to(ROOT)} -> {OUT.relative_to(ROOT)}")
    print(f"  rows in             {len(rows):>6}")
    print(f"  catalog             {len(versions_of):>6}   codes over v1 and v2, {len(codes_of_text)} distinct texts")
    print(f"  answered            {answered:>6}   barcode {by_method.get('barcode', 0)} | exact text {by_method.get('exact text', 0)}")
    print(f"  no answer           {by_method.get('no answer', 0):>6}   "
          + " | ".join(f"{k} {v}" for k, v in sorted(by_note.items(), key=lambda kv: -kv[1])))
    print(f"  answer's catalog    " + " | ".join(f"{k} {v}" for k, v in sorted(by_catalog.items())))
    print(f"  barcode and text both present {both}, disagreeing {conflicts}")
    for name, ok in checks.items():
        print(f"  CHECK    {name}: {ok}")
    print("\n  store  walk     rows  barcode   text  none")
    for (store, walk), w in sorted(by_walk.items()):
        print(f"  {store:<6} {walk:<6} {w['rows']:>6} {w['barcode']:>8} {w['exact text']:>6} {w['no answer']:>5}")

    if not all(checks.values()):
        sys.exit("STOP: a check failed. Nothing was written.")
    OUT.write_text("".join(json.dumps(o, ensure_ascii=False) + "\n" for o in out), encoding="utf-8", newline="\n")
    manifest = {
        "stage": 2, "name": "exact match",
        "input": {"path": IN.relative_to(ROOT).as_posix(), "rows": len(rows),
                  "catalogs": {v: p.relative_to(ROOT).as_posix() for v, p in CATALOGS.items()},
                  "catalog_codes": len(versions_of), "catalog_texts": len(codes_of_text)},
        "output": {"path": OUT.relative_to(ROOT).as_posix(), "rows": len(out),
                   "fields_added": ["match_code", "match_method", "match_catalog", "match_note"]},
        "answered": answered, "by_method": by_method, "no_answer_by_note": by_note, "answer_catalog": by_catalog,
        "barcode_and_text_both": both, "barcode_and_text_disagree": conflicts,
        "by_walk": [{"store_id": s, "walk_id": k, **w} for (s, k), w in sorted(by_walk.items())],
        "checks": checks,
    }
    MANIFEST.write_text(json.dumps(manifest, indent=1) + "\n", encoding="utf-8", newline="\n")
    print(f"\nWrote {OUT.relative_to(ROOT)} ({len(out)} rows) and {MANIFEST.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
