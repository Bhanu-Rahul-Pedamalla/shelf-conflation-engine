"""Stage 8, serve: the realogram against the planogram, in DuckDB. Every planned facing of every shelf
in every store gets a verdict from the record that stands at its spot, and every shelf gets a compliance
row. The spots the record could not settle are listed, not hidden: the open spots, the contested one,
and the ones holding a product the catalog has dropped.

    python pipeline/serve.py
    python pipeline/serve.py --sql "select store_id, aisle, verdict, count(*) from realogram group by all order by all"

Reads   data/pipeline/07_golden.jsonl         stage 7's output: the record with its lineage
        data/pipeline/05_reads.jsonl          for the text of the reads an open spot holds
        data/planogram.csv                    what should stand where: store, aisle, shelf, position, code, facings
        data/catalog_raw.jsonl, data/catalog_v2.jsonl
Writes  data/pipeline/08_realogram.jsonl      one row per planned facing: what was planned, what stands there, the verdict
        data/pipeline/08_compliance.csv       one row per store, aisle and shelf, then per store, then all
        data/pipeline/08_manifest.json        the counts, the lists, the SQL

The join. The planogram gives each position a product and a number of facings; the facings of a shelf
sit side by side from the shelf's start, 0.20 m each, so the n-th facing of a shelf is the record's
n-th slot. Expanding the planogram's 900 positions gives 1,812 facings; 1,763 of them have a record,
every record has a facing, 49 facings were never read.

The verdicts. "as planned": the record's product is the planned code. "as planned, new size": the
product is the planned code's successor, the planogram still carries the old barcode. "as planned,
catalog dropped it": planogram and shelf agree on a product the catalog no longer lists. "different
product": the record names another product. "contested", "open": the record could not decide, or had
nothing it could name. "never read": no walk produced a read here; the shelf is empty, or the badge
missed it. A record is also marked as seen on the latest walk or not: a product last seen on an earlier
day may have run out since, and the badge gives no read for an empty facing, so the record cannot tell.

The database is built in memory on every run, from the files; nothing binary is written. --sql runs
one query against it, for anyone who wants a different cut.
"""
import csv
import json
import sys
from pathlib import Path

import duckdb
import pandas as pd

from match_exact import CATALOGS

ROOT = Path(__file__).resolve().parents[1]
PIPELINE = ROOT / "data" / "pipeline"
IN = PIPELINE / "07_golden.jsonl"
READS = PIPELINE / "05_reads.jsonl"
PLANOGRAM = ROOT / "data" / "planogram.csv"
OUT = PIPELINE / "08_realogram.jsonl"
OUT_COMPLIANCE = PIPELINE / "08_compliance.csv"
MANIFEST = PIPELINE / "08_manifest.json"

VERDICTS = ("as planned", "as planned, new size", "as planned, catalog dropped it", "different product", "contested", "open", "never read")
COMPLIANT = VERDICTS[:3]

SQL = {
    "planned_facing": """
        select p.store_id, p.aisle, p.shelf, p.position, k as facing, p.before + k as slot, p.code as planned_code
        from (select *, sum(facings) over (partition by store_id, aisle, shelf order by position
                                            rows between unbounded preceding and current row) - facings as before
              from planogram) p, generate_series(1, p.facings) t(k)""",
    "realogram": """
        select f.store_id, f.aisle, f.shelf, f.position, f.facing, f.slot, f.planned_code,
               c1.label as planned_label, r.spot_id, r.product_code, r.product_label,
               case when r.spot_id is null then 'never read'
                    when r.status = 'contested' then 'contested'
                    when r.product_code is null then 'open'
                    when r.catalog_status = 'discontinued in v2, still on the shelf' and r.product_code = f.planned_code then 'as planned, catalog dropped it'
                    when r.product_code = f.planned_code then 'as planned'
                    when r.v1_code = f.planned_code then 'as planned, new size'
                    else 'different product' end as verdict,
               r.catalog_status, r.method, r.confidence_rank, r.reads, r.supporting, r.n_open,
               r.first_seen, r.last_seen, r.last_read,
               case when r.spot_id is null then null else substr(r.last_read, 1, 10) = l.latest end as read_on_latest_walk,
               case when r.product_code is null then null else substr(r.last_seen, 1, 10) = l.latest end as seen_on_latest_walk,
               r.open_reads, r.candidates
        from planned_facing f
        left join records r using (store_id, aisle, shelf, slot)
        left join (select store_id, max(substr(last_read, 1, 10)) as latest from records group by 1) l on l.store_id = f.store_id
        left join catalog c1 on c1.code = f.planned_code and c1.version = 'v1'
        order by f.store_id, f.aisle, f.shelf, f.slot""",
    "compliance": """
        select store_id, aisle, shelf, count(*) as planned_facings,
               count(*) filter (where verdict in ('as planned', 'as planned, new size', 'as planned, catalog dropped it')) as compliant,
               count(*) filter (where verdict = 'as planned') as as_planned,
               count(*) filter (where verdict = 'as planned, new size') as new_size,
               count(*) filter (where verdict = 'as planned, catalog dropped it') as catalog_dropped,
               count(*) filter (where verdict = 'different product') as different_product,
               count(*) filter (where verdict = 'contested') as contested,
               count(*) filter (where verdict = 'open') as open,
               count(*) filter (where verdict = 'never read') as never_read,
               count(*) filter (where seen_on_latest_walk = false) as not_seen_on_latest_walk
        from realogram group by all order by all""",
}


def load_catalog_table():
    rows = []
    for version, path in CATALOGS.items():
        for line in path.open(encoding="utf-8"):
            if line.strip():
                p = json.loads(line)
                rows.append({"version": version, "code": p["code"],
                             "label": " · ".join(x for x in ((p.get(f) or "").strip() for f in ("brands", "product_name", "quantity")) if x)})
    return pd.DataFrame(rows)


def load_records():
    rows = []
    for line in IN.open(encoding="utf-8"):
        if not line.strip():
            continue
        r = json.loads(line)
        steps = {s["version"]: s["code"] for s in r["lineage"]}
        rows.append({"spot_id": r["spot_id"], "store_id": r["store_id"], "aisle": r["aisle"], "shelf": r["shelf"], "slot": r["slot"],
                     "product_code": r["product_code"], "product_label": r["product_label"], "status": r["status"], "method": r["method"],
                     "confidence_rank": r["confidence_rank"], "catalog_status": r["catalog_status"],
                     "v1_code": steps.get("v1"), "v2_code": steps.get("v2"),
                     "first_seen": r["first_seen"], "last_seen": r["last_seen"], "last_read": r["last_read"],
                     "reads": r["reads"], "supporting": len(r["supporting_reads"]), "n_open": len(r["open_reads"]),
                     "open_reads": json.dumps(r["open_reads"]), "candidates": json.dumps(r["candidates"], ensure_ascii=False)})
    return pd.DataFrame(rows).astype({"confidence_rank": "Int64"})


def load_reads():
    rows = []
    for line in READS.open(encoding="utf-8"):
        if line.strip():
            r = json.loads(line)
            rows.append({"detection_id": r["detection_id"], "ts": r["ts"], "ocr_text": r["ocr_text"], "read_barcode": r.get("read_barcode"),
                         "match_code": r["match_code"], "match_method": r["match_method"]})
    return pd.DataFrame(rows)


def build(con):
    con.register("records", load_records())
    con.register("reads", load_reads())
    con.register("catalog", load_catalog_table())
    con.execute(f"create table planogram as select * from read_csv_auto('{PLANOGRAM.as_posix()}', types={{'code': 'VARCHAR'}})")
    con.execute("create table planned_facing as " + SQL["planned_facing"])
    con.execute("create table realogram as " + SQL["realogram"])
    con.execute("create table compliance as " + SQL["compliance"])


def rows_of(con, query):
    cur = con.execute(query)
    names = [d[0] for d in cur.description]
    return [dict(zip(names, row)) for row in cur.fetchall()]


def clean(row):
    """DuckDB's Python values back to plain JSON: numpy ints and NaN gone, the JSON text columns decoded."""
    out = {}
    for k, v in row.items():
        if hasattr(v, "item"):
            v = v.item()
        if isinstance(v, float) and v != v:
            v = None
        if k in ("open_reads", "candidates"):
            v = json.loads(v) if v else []
        out[k] = v
    return out


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    if not IN.exists():
        sys.exit(f"STOP: {IN.relative_to(ROOT)} not found. Run pipeline/versioning.py first.")
    con = duckdb.connect()
    build(con)
    if "--sql" in sys.argv:
        for row in rows_of(con, sys.argv[sys.argv.index("--sql") + 1]):
            print(json.dumps(clean(row), ensure_ascii=False, default=str))
        return

    realogram = [clean(r) for r in rows_of(con, "select * from realogram")]
    shelves = [clean(r) for r in rows_of(con, "select * from compliance")]
    totals = {"store": [clean(r) for r in rows_of(con, "select store_id, " + ", ".join(
                  f"sum({c}) as {c}" for c in shelves[0] if c not in ("store_id", "aisle", "shelf")) + " from compliance group by 1 order by 1")],
              "all": clean(rows_of(con, "select " + ", ".join(f"sum({c}) as {c}" for c in shelves[0] if c not in ("store_id", "aisle", "shelf")) + " from compliance")[0])}
    by_verdict = {v: sum(1 for r in realogram if r["verdict"] == v) for v in VERDICTS}
    records = len(load_records())

    open_spots = [clean(r) for r in rows_of(con, """
        select r.spot_id, r.reads, r.last_read, f.planned_label, list(d.ocr_text order by d.ts) as texts
        from realogram f join records r using (spot_id), unnest(from_json(r.open_reads, '["VARCHAR"]')) t(id) join reads d on d.detection_id = t.id
        where f.verdict = 'open' group by all order by 1""")]
    leftovers = [r for r in realogram if r["verdict"] == "as planned, catalog dropped it"]
    contested = [r for r in realogram if r["verdict"] == "contested"]
    different = [r for r in realogram if r["verdict"] == "different product"]

    checks = {
        "every planned facing has exactly one realogram row":
            len(realogram) == sum(int(r["facings"]) for r in csv.DictReader(PLANOGRAM.open(encoding="utf-8")))
            and len({(r["store_id"], r["aisle"], r["shelf"], r["slot"]) for r in realogram}) == len(realogram),
        "every record stands on exactly one planned facing":
            sorted(r["spot_id"] for r in realogram if r["spot_id"]) == sorted(load_records()["spot_id"]),
        "the verdicts add up to the planned facings": sum(by_verdict.values()) == len(realogram),
        "the shelf rows add up to the verdicts":
            all(totals["all"][c] == by_verdict[v] for c, v in (("as_planned", "as planned"), ("new_size", "as planned, new size"),
                                                               ("catalog_dropped", "as planned, catalog dropped it"), ("different_product", "different product"),
                                                               ("contested", "contested"), ("open", "open"), ("never_read", "never read")))
            and totals["all"]["planned_facings"] == len(realogram),
        "a compliant row's product is the planned code, or its successor, or the dropped product the planogram still plans":
            all(r["product_code"] == r["planned_code"] or r["verdict"] == "as planned, new size" for r in realogram if r["verdict"] in COMPLIANT),
        "an open row has no product and every one of its reads open":
            all(r["product_code"] is None and r["n_open"] == r["reads"] for r in realogram if r["verdict"] == "open"),
        "a never-read row has no record": all(r["spot_id"] is None for r in realogram if r["verdict"] == "never read"),
    }

    print(f"STAGE 8  SERVE   {IN.relative_to(ROOT)} + {PLANOGRAM.relative_to(ROOT)} -> {OUT.relative_to(ROOT)}, {OUT_COMPLIANCE.relative_to(ROOT)}")
    print(f"  planned facings     {len(realogram):>6}   from {sum(1 for _ in csv.DictReader(PLANOGRAM.open(encoding='utf-8')))} planogram positions; "
          f"records {records}; facings without a record {by_verdict['never read']}")
    print(f"  verdicts            " + " | ".join(f"{v} {n}" for v, n in by_verdict.items()))
    print(f"  {'compliance':<20} {'planned':>7} {'compliant':>9} {'%':>6}   different  contested  open  never read  not seen on latest walk")
    for t in totals["store"] + [{"store_id": "all", **totals["all"]}]:
        print(f"  {t['store_id']:<20} {t['planned_facings']:>7} {t['compliant']:>9} {100 * t['compliant'] / t['planned_facings']:>5.1f}%   "
              f"{t['different_product']:>9}  {t['contested']:>9}  {t['open']:>4}  {t['never_read']:>10}  {t['not_seen_on_latest_walk']:>23}")
    for name, ok in checks.items():
        print(f"  CHECK    {name}: {ok}")

    print(f"\n  the {len(open_spots)} open spots: read, nothing named")
    for s in open_spots:
        print(f"  {s['spot_id']}  planned {s['planned_label']}; {s['reads']} reads: " + " · ".join(repr(t) for t in s["texts"]))
    print(f"\n  the {len(leftovers)} spots holding a product the catalog dropped, and the planogram still plans")
    for r in leftovers:
        print(f"  {r['spot_id']}  {r['product_label']} by {r['method']}, {r['supporting']} reads, last seen {r['last_seen'][:10]}")
    print(f"\n  the {len(contested)} contested spot")
    for r in contested:
        print(f"  {r['spot_id']}  planned {r['planned_label']}; " + " vs ".join(f"{c['product_label']} ({len(c['reads'])} read)" for c in r["candidates"]))
    print(f"\n  the {len(different)} spots holding a different product than planned")
    for r in different:
        print(f"  {r['spot_id']}  planned {r['planned_label']}; stands {r['product_label']} by {r['method']}, {r['supporting']} reads")

    if not all(checks.values()):
        sys.exit("STOP: a check failed. Nothing was written.")
    OUT.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in realogram), encoding="utf-8", newline="\n")
    with OUT_COMPLIANCE.open("w", encoding="utf-8", newline="\n") as f:
        w = csv.writer(f, lineterminator="\n")
        cols = list(shelves[0])
        w.writerow(["level"] + cols + ["compliance_pct"])
        for s in shelves:
            w.writerow(["shelf"] + [s[c] for c in cols] + [round(100 * s["compliant"] / s["planned_facings"], 1)])
        for t in totals["store"]:
            w.writerow(["store", t["store_id"], "", ""] + [t[c] for c in cols[3:]] + [round(100 * t["compliant"] / t["planned_facings"], 1)])
        t = totals["all"]
        w.writerow(["all", "", "", ""] + [t[c] for c in cols[3:]] + [round(100 * t["compliant"] / t["planned_facings"], 1)])
    manifest = {
        "stage": 8, "name": "serve",
        "input": {"records": IN.relative_to(ROOT).as_posix(), "reads": READS.relative_to(ROOT).as_posix(), "planogram": PLANOGRAM.relative_to(ROOT).as_posix(),
                  "catalogs": {v: p.relative_to(ROOT).as_posix() for v, p in CATALOGS.items()}},
        "output": {"realogram": {"path": OUT.relative_to(ROOT).as_posix(), "rows": len(realogram)},
                   "compliance": {"path": OUT_COMPLIANCE.relative_to(ROOT).as_posix(), "shelf_rows": len(shelves), "store_rows": len(totals["store"])}},
        "engine": f"duckdb {duckdb.__version__}, in memory",
        "verdicts": by_verdict, "compliant_verdicts": list(COMPLIANT),
        "compliance_by_store": {t["store_id"]: {**{k: v for k, v in t.items() if k != "store_id"},
                                                "compliance_pct": round(100 * t["compliant"] / t["planned_facings"], 1)} for t in totals["store"]},
        "compliance_all": {**totals["all"], "compliance_pct": round(100 * totals["all"]["compliant"] / totals["all"]["planned_facings"], 1)},
        "open_spots": [{"spot_id": s["spot_id"], "reads": s["reads"], "texts": s["texts"]} for s in open_spots],
        "catalog_dropped_spots": [r["spot_id"] for r in leftovers],
        "contested_spots": [{"spot_id": r["spot_id"], "candidates": [c["product_code"] for c in r["candidates"]]} for r in contested],
        "different_product_spots": [{"spot_id": r["spot_id"], "planned": r["planned_code"], "stands": r["product_code"]} for r in different],
        "sql": {k: " ".join(v.split()) for k, v in SQL.items()},
        "checks": checks,
    }
    MANIFEST.write_text(json.dumps(manifest, indent=1, ensure_ascii=False) + "\n", encoding="utf-8", newline="\n")
    print(f"\nWrote {OUT.relative_to(ROOT)} ({len(realogram)} rows), {OUT_COMPLIANCE.relative_to(ROOT)} ({len(shelves)} shelf rows) and {MANIFEST.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
