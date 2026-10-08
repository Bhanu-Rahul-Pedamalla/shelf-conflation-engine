"""Stage 6, the golden record: one record per shelf spot per store, built from every read of that spot
on every day. The record says which product stands there, how that was decided, which reads support
it, which reads disagreed and lost, when the product was first and last seen, and which catalog
version was in effect. This is the realogram the pipeline hands on.

    python pipeline/golden.py

Reads   data/pipeline/05_reads.jsonl         stage 5's output: every read with its answer or its open note
        data/catalog_raw.jsonl, data/catalog_v2.jsonl, data/catalog_v2.meta.json
Writes  data/pipeline/06_golden.jsonl        one record per spot, ordered by store, aisle, shelf, slot
        data/pipeline/06_manifest.json       the counts

A spot is a store, aisle, shelf and a 0.20 m slot along the shelf, the width of one facing. Measured on
this feed against the truth: slots give 1,763 spots for the 1,763 facings that were ever read; 4 spots
hold one read that jittered in from the neighbouring facing. Clustering reads by gaps instead gives
1,637 spots at 0.10 m (126 of them mixing two facings) and 1,784 at 0.08 m (55 facings split in two).

Survivorship, measured on this feed before it was written. The reads of a spot vote. A read's weight
is its method's rank: barcode, then exact text, then close spelling, then same spot, then other day,
then language model. The product is the code named by the best-ranked reads; among reads of the same
rank, the most reads; an even split is contested and the record says so with both candidates. Before
the vote, a read whose code is no longer in the catalog version in effect on the spot's latest day
steps aside when a current code competes: it becomes history, the product that stood here before the
catalog moved on. When nothing current competes, the old code stands and the record says it is not in
the current catalog.

Why this rule and not the obvious ones, on the same 1,763 spots against the shelf truth on the day the
product was last seen: "the latest day wins" is right 1,735 times and wrong 2, because one wrong read
on a later day overturns a right read on an earlier one. "All reads vote, no history" is wrong once
and leaves 22 spots without an answer, because the 8 spots whose product changed barcode between the
days split their votes between the old code and the new. "A change needs two reads or a better rank"
is wrong 5 times, because every real barcode change on this feed was read once per day. The rule
above is right 1,744 times, wrong 0, contested 1, no answer 18.

The three wrong reads the matching stages let through sit in spots whose other days read the right
product: 'psi 12 foz' (close spelling) is outranked by two exact-text reads; "nature's bakery fig bar"
(exact text) is outvoted two to one by exact-text reads; 'OLIPOP Strawberry Vanilla' (exact text)
meets one exact-text read of the other code, and that record is the one contested.
"""
import json
import statistics
import sys
from pathlib import Path

from match_exact import CATALOGS, catalog_text   # noqa: F401  (the catalog files, as every stage names them)
from match_crossday import load_catalog, version_on

ROOT = Path(__file__).resolve().parents[1]
PIPELINE = ROOT / "data" / "pipeline"
IN = PIPELINE / "05_reads.jsonl"
OUT = PIPELINE / "06_golden.jsonl"
MANIFEST = PIPELINE / "06_manifest.json"

SLOT_M = 0.20          # one facing's width along the shelf
RANK = {"barcode": 1, "exact text": 2, "close spelling": 3, "same spot": 4, "other day": 5, "language model": 6}


def load_labels():
    """code -> 'brand · name · quantity', the catalog row as a person would say it."""
    labels = {}
    for path in CATALOGS.values():
        for line in path.open(encoding="utf-8"):
            if line.strip():
                p = json.loads(line)
                labels.setdefault(p["code"], " · ".join(x for x in ((p.get("brands") or "").strip(), (p.get("product_name") or "").strip(),
                                                                    (p.get("quantity") or "").strip()) if x))
    return labels


def spot_key(r):
    return (r["store_id"], r["aisle"], int(r["shelf"]), int(r["position_x"] // SLOT_M) + 1)


def spot_id(key):
    store, aisle, shelf, slot = key
    return f"{store}-{aisle}-S{shelf}-{slot:03d}"


def vote(reads):
    """The best-ranked reads' majority code, or None with the tied candidates."""
    best = min(RANK[r["match_method"]] for r in reads)
    counts = {}
    for r in reads:
        if RANK[r["match_method"]] == best:
            counts[r["match_code"]] = counts.get(r["match_code"], 0) + 1
    top = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    if len(top) > 1 and top[0][1] == top[1][1]:
        return None, best, [c for c, n in top if n == top[0][1]]
    return top[0][0], best, []


def seen(reads):
    ts = sorted(r["ts"] for r in reads)
    return ts[0], ts[-1], sorted({t[:10] for t in ts})


def build_record(key, reads, versions_of, labels, effective_from):
    reads = sorted(reads, key=lambda r: (r["ts"], r["detection_id"]))
    answered = [r for r in reads if r["match_code"]]
    open_reads = [r["detection_id"] for r in reads if not r["match_code"]]
    first_read, last_read, days_read = seen(reads)
    rec = {"spot_id": spot_id(key), "store_id": key[0], "aisle": key[1], "shelf": key[2], "slot": key[3],
           "position_m": round((key[3] - 0.5) * SLOT_M, 2), "x_median": round(statistics.median(r["position_x"] for r in reads), 3),
           "product_code": None, "product_label": None, "status": "no answer", "method": None, "confidence_rank": None,
           "catalog_version": version_on(last_read[:10], effective_from), "in_catalog_version": None,
           "first_seen": None, "last_seen": None, "days_seen": [],
           "first_read": first_read, "last_read": last_read, "days_read": days_read,
           "reads": len(reads), "supporting_reads": [], "dissenting_reads": [], "history": [], "candidates": [],
           "open_reads": open_reads, "source_reads": [r["detection_id"] for r in reads]}
    if not answered:
        return rec
    version = version_on(max(r["ts"][:10] for r in answered), effective_from)
    rec["catalog_version"] = version
    live = [r for r in answered if version in versions_of[r["match_code"]]]
    retired = [r for r in answered if version not in versions_of[r["match_code"]]]
    voters = live if live else answered                      # nothing current competes: the old code stands
    code, best, tied = vote(voters)
    if live:
        for old in sorted({r["match_code"] for r in retired}):
            rs = [r for r in retired if r["match_code"] == old]
            f, l, days = seen(rs)
            rec["history"].append({"product_code": old, "product_label": labels.get(old), "first_seen": f, "last_seen": l,
                                   "days_seen": days, "reads": [r["detection_id"] for r in rs],
                                   "why": f"not in catalog {version}, in effect from {effective_from}"})
    if code is None:
        rec["status"] = "contested"
        rec["confidence_rank"] = best
        for c in tied:
            rs = [r for r in voters if r["match_code"] == c]
            rec["candidates"].append({"product_code": c, "product_label": labels.get(c), "reads": [r["detection_id"] for r in rs],
                                      "methods": sorted({r["match_method"] for r in rs})})
        rec["dissenting_reads"] = [{"detection_id": r["detection_id"], "product_code": r["match_code"], "method": r["match_method"]}
                                   for r in voters]
        return rec
    support = [r for r in voters if r["match_code"] == code]
    dissent = [r for r in voters if r["match_code"] != code]
    f, l, days = seen(support)
    rec.update(product_code=code, product_label=labels.get(code), status="dissent outvoted" if dissent else "one product",
               method=min(support, key=lambda r: RANK[r["match_method"]])["match_method"], confidence_rank=best,
               in_catalog_version=version in versions_of[code], first_seen=f, last_seen=l, days_seen=days,
               supporting_reads=[r["detection_id"] for r in support],
               dissenting_reads=[{"detection_id": r["detection_id"], "product_code": r["match_code"], "method": r["match_method"]}
                                 for r in dissent])
    return rec


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    if not IN.exists():
        sys.exit(f"STOP: {IN.relative_to(ROOT)} not found. Run pipeline/match_llm.py first.")
    rows = [json.loads(line) for line in IN.open(encoding="utf-8") if line.strip()]
    versions_of, _, effective_from = load_catalog()
    labels = load_labels()

    spots = {}
    for r in rows:
        spots.setdefault(spot_key(r), []).append(r)
    records = [build_record(key, spots[key], versions_of, labels, effective_from) for key in sorted(spots)]

    by_status, by_method, by_version, by_store = {}, {}, {}, {}
    for rec in records:
        by_status[rec["status"]] = by_status.get(rec["status"], 0) + 1
        by_store[rec["store_id"]] = by_store.get(rec["store_id"], 0) + 1
        if rec["product_code"]:
            by_method[rec["method"]] = by_method.get(rec["method"], 0) + 1
            by_version[rec["catalog_version"]] = by_version.get(rec["catalog_version"], 0) + 1
    with_history = [rec for rec in records if rec["history"]]
    not_in_version = [rec for rec in records if rec["product_code"] and not rec["in_catalog_version"]]
    accounted = [rid for rec in records for rid in rec["source_reads"]]
    checks = {
        "every read in exactly one record": sorted(accounted) == sorted(r["detection_id"] for r in rows),
        "one record per store, aisle, shelf and slot": len({rec["spot_id"] for rec in records}) == len(records) == len(spots),
        "supporting + dissenting + history + open = reads, in every record":
            all(len(rec["supporting_reads"]) + len(rec["dissenting_reads"]) + sum(len(h["reads"]) for h in rec["history"])
                + len(rec["open_reads"]) == rec["reads"] for rec in records),
        "every product is a catalog code": all(rec["product_code"] in versions_of for rec in records if rec["product_code"]),
        "every product has a supporting read, first seen on or before last seen":
            all(rec["supporting_reads"] and rec["first_seen"] <= rec["last_seen"] for rec in records if rec["product_code"]),
        "every history product is absent from the catalog version in effect":
            all(rec["catalog_version"] not in versions_of[h["product_code"]] for rec in records for h in rec["history"]),
        "a contested record names two or more candidates and no product":
            all(len(rec["candidates"]) >= 2 and not rec["product_code"] for rec in records if rec["status"] == "contested"),
    }

    print(f"STAGE 6  GOLDEN RECORD   {IN.relative_to(ROOT)} -> {OUT.relative_to(ROOT)}")
    print(f"  reads in            {len(rows):>6}   in {len(records)} spots (store, aisle, shelf, {SLOT_M:.2f} m slot); "
          + ", ".join(f"{k} {v}" for k, v in sorted(by_store.items())))
    print(f"  records             {len(records):>6}   " + " | ".join(f"{k} {v}" for k, v in sorted(by_status.items(), key=lambda kv: -kv[1])))
    print(f"  decided by          " + " | ".join(f"{k} {v}" for k, v in sorted(by_method.items(), key=lambda kv: RANK[kv[0]])))
    print(f"  catalog, last seen  " + " | ".join(f"{k} {v}" for k, v in sorted(by_version.items()))
          + f"   product not in that version {len(not_in_version)}")
    print(f"  history             {len(with_history):>6}   records also carry a product the catalog has since replaced "
          f"({sum(len(h['reads']) for rec in with_history for h in rec['history'])} reads)")
    for name, ok in checks.items():
        print(f"  CHECK    {name}: {ok}")
    print("\n  the contested record, and one where a dissenting read lost")
    for status in ("contested", "dissent outvoted"):
        rec = next((rec for rec in records if rec["status"] == status), None)
        if rec is None:
            continue
        if status == "contested":
            cands = " vs ".join(f"{c['product_label']} ({len(c['reads'])} read, {', '.join(c['methods'])})" for c in rec["candidates"])
            print(f"  {rec['spot_id']}  {cands}")
        else:
            lost = ", ".join(f"{d['detection_id']} said {labels.get(d['product_code'])} by {d['method']}" for d in rec["dissenting_reads"])
            print(f"  {rec['spot_id']}  {rec['product_label']} by {rec['method']} ({len(rec['supporting_reads'])} reads); lost: {lost}")

    if not all(checks.values()):
        sys.exit("STOP: a check failed. Nothing was written.")
    OUT.write_text("".join(json.dumps(rec, ensure_ascii=False) + "\n" for rec in records), encoding="utf-8", newline="\n")
    manifest = {
        "stage": 6, "name": "golden record",
        "input": {"path": IN.relative_to(ROOT).as_posix(), "rows": len(rows)},
        "output": {"path": OUT.relative_to(ROOT).as_posix(), "records": len(records)},
        "rules": {"slot_m": SLOT_M, "method_rank": RANK,
                  "survivorship": "best method rank, then most reads; even split contested; a code absent from the catalog version "
                                  "in effect retires to history when a current code competes",
                  "catalog_v2_effective_from": effective_from},
        "records_by_status": by_status, "records_by_method": by_method, "records_by_catalog_version": by_version,
        "records_by_store": by_store, "records_with_history": len(with_history), "products_not_in_version": len(not_in_version),
        "checks": checks,
    }
    MANIFEST.write_text(json.dumps(manifest, indent=1) + "\n", encoding="utf-8", newline="\n")
    print(f"\nWrote {OUT.relative_to(ROOT)} ({len(records)} records) and {MANIFEST.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
