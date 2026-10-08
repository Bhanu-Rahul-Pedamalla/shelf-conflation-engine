"""Stage 0, ingest: the reads file in; every usable read once, in time order, out; a reason for every row left out.

    python pipeline/ingest.py

Reads   data/detections.jsonl               the badge's reads, as they arrived
Writes  data/pipeline/00_reads.jsonl        every usable read once, ordered by time then id, rows unchanged
        data/pipeline/00_quarantine.jsonl   every row that could not be used: its line, the reason, the raw line
        data/pipeline/00_manifest.json      the counts, and the checksum of the input

The rule of this stage: lines in = rows out + duplicates dropped + rows quarantined. It is checked
before anything is written, and nothing is written if it does not hold. Nothing is dropped without
a written reason.
"""
import datetime
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
READS = ROOT / "data" / "detections.jsonl"
OUT_DIR = ROOT / "data" / "pipeline"
OUT = OUT_DIR / "00_reads.jsonl"
QUARANTINE = OUT_DIR / "00_quarantine.jsonl"
MANIFEST = OUT_DIR / "00_manifest.json"

# What a read must carry to be usable, and the type of each field. A field the badge adds later
# passes through untouched: the record is checked for what this pipeline needs, not frozen to
# what it knows today. bool is checked separately below, because in Python a bool is an int.
NUMBER = (int, float)
FIELDS = {
    "detection_id": str, "badge_id": str, "store_id": str, "walk_id": str, "ts": str, "aisle": str,
    "shelf": int, "position_x": NUMBER, "x": NUMBER, "y": NUMBER, "z": NUMBER, "bbox": list,
    "ocr_text": str, "read_barcode": (str, type(None)), "confidence": NUMBER, "image_ref": str,
}
MUST_NOT_BE_BLANK = ("detection_id", "badge_id", "store_id", "walk_id", "aisle", "image_ref")
TS_FORMAT = "%Y-%m-%dT%H:%M:%S.%fZ"   # the one form the badge writes. Another form is quarantined, not guessed at.
REASONS = ("unreadable", "missing_field", "wrong_type", "bad_timestamp", "out_of_range", "empty_read", "id_conflict")


def sha256_of(path):
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def why_unusable(row):
    """The reason this row cannot be used, as (reason, detail), or None when it can."""
    if not isinstance(row, dict):
        return "unreadable", "not a record"
    for field, kind in FIELDS.items():
        if field not in row:
            return "missing_field", f"no {field}"
        value = row[field]
        if isinstance(value, bool) or not isinstance(value, kind):
            return "wrong_type", f"{field} is {type(value).__name__}: {value!r}"
    for field in MUST_NOT_BE_BLANK:
        if not row[field].strip():
            return "missing_field", f"{field} is blank"
    box = row["bbox"]
    if len(box) != 4 or not all(isinstance(v, int) and not isinstance(v, bool) for v in box):
        return "wrong_type", f"bbox is not four whole numbers: {box!r}"
    try:
        datetime.datetime.strptime(row["ts"], TS_FORMAT)
    except ValueError:
        return "bad_timestamp", row["ts"]
    if not 0 <= row["confidence"] <= 1:
        return "out_of_range", f"confidence {row['confidence']}"
    if row["shelf"] < 1:
        return "out_of_range", f"shelf {row['shelf']}"
    # store coordinates start at the store's corner, so a negative value is outside the store
    for field in ("x", "y", "z"):
        if row[field] < 0:
            return "out_of_range", f"{field} {row[field]}"
    if row["read_barcode"] is not None and not row["read_barcode"].isdigit():
        return "wrong_type", f"read_barcode is not digits: {row['read_barcode']!r}"
    if not row["ocr_text"].strip() and row["read_barcode"] is None:
        return "empty_read", "no text and no barcode"
    return None


def ingest(path):
    """Read every line once. Returns the usable rows in time order, the quarantine, and the counts."""
    rows, quarantine = [], []
    first = {}           # detection_id -> (line number, row), the first usable row with that id
    duplicates = 0
    by_walk = {}         # (store, walk) -> counts; a line without a store or walk is counted under "?"

    def tally(store, walk, key):
        counts = by_walk.setdefault((store, walk), {"lines": 0, "rows_out": 0, "duplicates": 0, "quarantined": 0})
        counts["lines"] += 1
        counts[key] += 1

    n = 0
    with path.open(encoding="utf-8") as f:
        for n, raw in enumerate(f, start=1):
            line = raw.rstrip("\n")
            try:
                row = json.loads(line)
            except ValueError as e:
                quarantine.append({"line": n, "reason": "unreadable", "detail": str(e), "raw": line})
                tally("?", "?", "quarantined")
                continue
            store = row.get("store_id") if isinstance(row, dict) else None
            walk = row.get("walk_id") if isinstance(row, dict) else None
            store = store if isinstance(store, str) and store else "?"
            walk = walk if isinstance(walk, str) and walk else "?"
            why = why_unusable(row)
            if why:
                quarantine.append({"line": n, "reason": why[0], "detail": why[1], "raw": line})
                tally(store, walk, "quarantined")
                continue
            seen = first.get(row["detection_id"])
            if seen is None:
                first[row["detection_id"]] = (n, row)
                rows.append(row)
                tally(store, walk, "rows_out")
            elif seen[1] == row:
                # the same record again: a frame sent twice. Compared as parsed records, not as
                # bytes, so that the same content in another key order is still the same record.
                duplicates += 1
                tally(store, walk, "duplicates")
            else:
                # the same id with different content. The first arrival is kept: it is the one the
                # badge sent first, and a later variant cannot be told from a corrupted re-send.
                quarantine.append({"line": n, "reason": "id_conflict",
                                   "detail": f"id first seen on line {seen[0]} with different content", "raw": line})
                tally(store, walk, "quarantined")

    # ts is written in one fixed-width form, so text order is time order. Ties keep id order.
    rows.sort(key=lambda r: (r["ts"], r["detection_id"]))
    return rows, quarantine, duplicates, n, by_walk


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    if not READS.exists():
        sys.exit(f"STOP: {READS.relative_to(ROOT)} not found.")
    rows, quarantine, duplicates, lines_in, by_walk = ingest(READS)
    by_reason = {reason: sum(1 for q in quarantine if q["reason"] == reason) for reason in REASONS}
    ids = [r["detection_id"] for r in rows]
    checks = {
        "lines_in = rows_out + duplicates + quarantined": lines_in == len(rows) + duplicates + len(quarantine),
        "every id out is distinct": len(ids) == len(set(ids)),
        "rows out are in time order": all(a["ts"] <= b["ts"] for a, b in zip(rows, rows[1:])),
        "every quarantined row has a reason from the list": all(q["reason"] in REASONS for q in quarantine),
    }

    print(f"STAGE 0  INGEST   {READS.relative_to(ROOT)} -> {OUT.relative_to(ROOT)}")
    print(f"  lines in            {lines_in:>6}   sha256 {sha256_of(READS)[:16]}")
    print(f"  rows out            {len(rows):>6}   every usable read once, in time order")
    print(f"  duplicates dropped  {duplicates:>6}   same id, same content: the frame was sent twice")
    print(f"  quarantined         {len(quarantine):>6}   "
          + " | ".join(f"{reason} {count}" for reason, count in by_reason.items()))
    for name, ok in checks.items():
        print(f"  CHECK    {name}: {ok}")
    print(f"  CHECK    {lines_in} = {len(rows)} + {duplicates} + {len(quarantine)}")
    print("\n  store  walk    lines    out    dup   quar")
    for (store, walk), c in sorted(by_walk.items()):
        print(f"  {store:<6} {walk:<6} {c['lines']:>6} {c['rows_out']:>6} {c['duplicates']:>6} {c['quarantined']:>6}")
    print("\n  one quarantined row per reason")
    shown = set()
    for q in quarantine:
        if q["reason"] not in shown:
            shown.add(q["reason"])
            print(f"  line {q['line']:>5}  {q['reason']:<14} {q['detail'][:90]}")

    if not all(checks.values()):
        sys.exit("STOP: a check failed. Nothing was written.")

    OUT_DIR.mkdir(exist_ok=True)
    # rows are written back exactly as they were read, in the new order: this stage changes nothing inside a record
    OUT.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8", newline="\n")
    QUARANTINE.write_text("".join(json.dumps(q, ensure_ascii=False) + "\n" for q in quarantine),
                          encoding="utf-8", newline="\n")
    manifest = {
        "stage": 0,
        "name": "ingest",
        "input": {"path": READS.relative_to(ROOT).as_posix(), "bytes": READS.stat().st_size,
                  "sha256": sha256_of(READS), "lines": lines_in},
        "output": {"path": OUT.relative_to(ROOT).as_posix(), "rows": len(rows), "sha256": sha256_of(OUT)},
        "quarantine": {"path": QUARANTINE.relative_to(ROOT).as_posix(), "rows": len(quarantine),
                       "by_reason": by_reason},
        "duplicates_dropped": duplicates,
        "by_walk": [{"store_id": store, "walk_id": walk, **c} for (store, walk), c in sorted(by_walk.items())],
        "checks": checks,
    }
    MANIFEST.write_text(json.dumps(manifest, indent=1) + "\n", encoding="utf-8", newline="\n")
    print(f"\nWrote {OUT.relative_to(ROOT)} ({len(rows)} rows), {QUARANTINE.relative_to(ROOT)} "
          f"({len(quarantine)} rows) and {MANIFEST.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
