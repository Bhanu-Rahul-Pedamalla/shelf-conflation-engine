"""Stage 1, normalize: every read gets one more field, text_norm, the read text written the one way
this pipeline compares text. Nothing else in the record changes.

    python pipeline/normalize.py

Reads   data/pipeline/00_reads.jsonl         stage 0's output
Writes  data/pipeline/01_reads.jsonl         the same rows, same order, plus text_norm
        data/pipeline/01_manifest.json       the counts: rows, which rules fired how often

The same function, normalize_text(), is applied to the catalog side by the matching stages, so a
read and a catalog row are compared in the same form. The rules are the ones the feed needs,
each measured on the feed before it was written:
  - letters without accents, in one case: an accent is the first thing a camera loses
  - punctuation becomes a space, except a decimal point inside a number: brands and sizes carry
    dashes, apostrophes, dots and brackets that a read may or may not keep
  - a unit of measure written one way: oz / OZ / ounces / oz. are one unit, and so on
  - one space between words, none at the ends
No alias table for brands: on this feed every brand is spelt as the catalog spells it, so a table
would catch nothing and claim something. It is added when a read needs one.
"""
import json
import re
import sys
import unicodedata
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
IN = ROOT / "data" / "pipeline" / "00_reads.jsonl"
OUT = ROOT / "data" / "pipeline" / "01_reads.jsonl"
MANIFEST = ROOT / "data" / "pipeline" / "01_manifest.json"

# every spelling of a unit the feed or the catalog uses, and the one spelling it becomes
UNITS = {
    "fl oz": ["fl oz", "floz", "fluid ounce", "fluid ounces"],
    "oz": ["oz", "ounce", "ounces"],
    "ml": ["ml", "milliliter", "milliliters", "millilitre", "millilitres"],
    "l": ["l", "liter", "liters", "litre", "litres"],
    "g": ["g", "gram", "grams"],
    "kg": ["kg", "kilogram", "kilograms"],
    "lb": ["lb", "lbs", "pound", "pounds"],
}
CANONICAL = {spelling: unit for unit, spellings in UNITS.items() for spelling in spellings}
# longest spelling first, so "fl oz" wins over "oz" and "lbs" over "l"; only right after a number,
# so the l in "lemon" or the g in "ginger" is never a unit
UNIT_RE = re.compile(r"(\d(?:\.\d+)?)\s*(" + "|".join(sorted(map(re.escape, CANONICAL), key=len, reverse=True))
                     + r")(?![a-z])")
# a dot or comma between two digits is a decimal mark; any other punctuation is a space
DECIMAL_COMMA_RE = re.compile(r"(?<=\d),(?=\d)")
PUNCT_RE = re.compile(r"(?!(?<=\d)\.(?=\d))[^\w\s]|_")


def normalize_text(text):
    """The read text, written the one way this pipeline compares text."""
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c)).casefold()
    text = DECIMAL_COMMA_RE.sub(".", text)
    text = PUNCT_RE.sub(" ", text)
    text = " ".join(text.split())
    text = UNIT_RE.sub(lambda m: f"{m.group(1)} {CANONICAL[m.group(2)]}", text)
    return " ".join(text.split())


def which_rules(text, norm):
    """Which rules changed this text, for the manifest. Each rule is tried on its own."""
    fired = []
    folded = unicodedata.normalize("NFKD", text).casefold()
    if folded != text:
        fired.append("case")
    if any(unicodedata.combining(c) for c in folded):
        fired.append("accent")
    if DECIMAL_COMMA_RE.search(folded):
        fired.append("decimal comma")
    if PUNCT_RE.search(folded):
        fired.append("punctuation")
    if " ".join(folded.split()) != folded:
        fired.append("spacing")
    stripped = " ".join(PUNCT_RE.sub(" ", folded).split())
    if UNIT_RE.sub(lambda m: f"{m.group(1)} {CANONICAL[m.group(2)]}", stripped) != stripped:
        fired.append("unit")
    return fired


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    if not IN.exists():
        sys.exit(f"STOP: {IN.relative_to(ROOT)} not found. Run pipeline/ingest.py first.")
    rows = [json.loads(line) for line in IN.open(encoding="utf-8") if line.strip()]
    out, fired = [], {}
    changed = changed_beyond_case = 0
    for r in rows:
        norm = normalize_text(r["ocr_text"])
        for rule in which_rules(r["ocr_text"], norm):
            fired[rule] = fired.get(rule, 0) + 1
        if norm != r["ocr_text"]:
            changed += 1
        if norm != r["ocr_text"].casefold():
            changed_beyond_case += 1
        out.append({**r, "text_norm": norm})   # the original fields first and untouched, the new one last

    checks = {
        "rows in = rows out": len(rows) == len(out),
        "same ids in the same order": [r["detection_id"] for r in rows] == [o["detection_id"] for o in out],
        "every original field unchanged": all({k: v for k, v in o.items() if k != "text_norm"} == r
                                              for r, o in zip(rows, out)),
        "text_norm never empty when the read had text": all(o["text_norm"] or not o["ocr_text"].strip() for o in out),
    }
    distinct_in = len({r["ocr_text"] for r in rows})
    distinct_out = len({o["text_norm"] for o in out})

    print(f"STAGE 1  NORMALIZE   {IN.relative_to(ROOT)} -> {OUT.relative_to(ROOT)}")
    print(f"  rows in             {len(rows):>6}")
    print(f"  rows out            {len(out):>6}   one field added: text_norm")
    print(f"  text changed        {changed:>6}   beyond case alone {changed_beyond_case}")
    print(f"  distinct texts      {distinct_in:>6} -> {distinct_out}")
    print("  rules fired         " + " | ".join(f"{k} {v}" for k, v in sorted(fired.items(), key=lambda kv: -kv[1])))
    for name, ok in checks.items():
        print(f"  CHECK    {name}: {ok}")
    print("\n  one example per rule")
    shown = set()
    for r, o in zip(rows, out):
        for rule in which_rules(r["ocr_text"], o["text_norm"]):
            if rule not in shown:
                shown.add(rule)
                print(f"  {rule:<14} {r['ocr_text']!r} -> {o['text_norm']!r}")

    if not all(checks.values()):
        sys.exit("STOP: a check failed. Nothing was written.")
    OUT.write_text("".join(json.dumps(o, ensure_ascii=False) + "\n" for o in out), encoding="utf-8", newline="\n")
    manifest = {
        "stage": 1, "name": "normalize",
        "input": {"path": IN.relative_to(ROOT).as_posix(), "rows": len(rows)},
        "output": {"path": OUT.relative_to(ROOT).as_posix(), "rows": len(out), "field_added": "text_norm"},
        "text_changed": changed, "text_changed_beyond_case": changed_beyond_case,
        "distinct_texts": {"in": distinct_in, "out": distinct_out},
        "rules_fired": fired,
        "checks": checks,
    }
    MANIFEST.write_text(json.dumps(manifest, indent=1) + "\n", encoding="utf-8", newline="\n")
    print(f"\nWrote {OUT.relative_to(ROOT)} ({len(out)} rows) and {MANIFEST.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
