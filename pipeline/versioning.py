"""Stage 7, versioning: the catalog changed between the days, and the record has to say what that did
to it. The two catalog versions are compared, the changes between them are found from the files alone,
and every golden record gets its product as each version knows it, the change that touched it, and a
label from the catalog version in effect on the day the product was last seen.

    python pipeline/versioning.py

Reads   data/pipeline/06_golden.jsonl         stage 6's output: one record per shelf spot
        data/catalog_raw.jsonl, data/catalog_v2.jsonl, data/catalog_v2.meta.json
Writes  data/pipeline/07_catalog_changes.jsonl one row per change between the two catalog versions
        data/pipeline/07_golden.jsonl         every record, with its lineage across the versions
        data/pipeline/07_manifest.json        the counts

The changes, found by comparing the two files. A code in both versions whose brand, name or quantity
differs is renamed: 3 codes on this catalog, all three in the name. A code in one version only is
paired with a code in the other version only when the brand is the same and the name without its
numbers is the same: that is a new size, and the GS1 rule gives a new size a new barcode. Measured on
the 3 codes only in v1 and the 3 only in v2: 2 of the 9 pairings meet the rule, and those two score 91
and 95 on text similarity where the next best pairing scores 52. A code only in v1 with no pair is
discontinued; a code only in v2 with no pair is new. Applying the changes to v1 rebuilds v2 exactly,
code for code and label for label; pipeline/test_versioning.py checks that.

What the changes do to a record. Each record gains a lineage, the product under v1 and under v2 with
the label each version gives it, and a catalog status: current; renamed in v2; new size since v2, the
record's product is the successor of a v1 code and the old code sits in its history; old size, the
successor was never read here; or discontinued in v2 and still on the shelf. The record's label is
re-read from the catalog version in effect on the day its product was last seen, so a renamed product
carries its new name once v2 is in effect and its old name in the lineage.

A record whose product the catalog has dropped is not dropped. The reads of those spots named that
product on days after the catalog moved on: the stock is still on the shelf, the record says what it is
and that the catalog no longer carries it, and the serving stage can list it. Removing the record would
report an empty spot that is not empty.
"""
import json
import sys
from pathlib import Path

import normalize
from match_exact import CATALOGS

ROOT = Path(__file__).resolve().parents[1]
PIPELINE = ROOT / "data" / "pipeline"
V2_META = ROOT / "data" / "catalog_v2.meta.json"
IN = PIPELINE / "06_golden.jsonl"
OUT_CHANGES = PIPELINE / "07_catalog_changes.jsonl"
OUT = PIPELINE / "07_golden.jsonl"
MANIFEST = PIPELINE / "07_manifest.json"

VERSIONS = ("v1", "v2")
FIELDS = ("brands", "product_name", "quantity")          # what a label prints; the other fields are bookkeeping
RENAME_KIND = {"brands": "rebranded", "product_name": "renamed", "quantity": "quantity changed, same barcode"}
NEW_SIZE = "new size, new barcode"

STATUS_CURRENT = "current"
STATUS_RENAMED = "renamed in v2"
STATUS_NEW_SIZE = "new size since v2"
STATUS_OLD_SIZE = "old size, successor not read here"
STATUS_DISCONTINUED = "discontinued in v2, still on the shelf"

MIGRATION_FIELDS = ("product_label", "lineage", "catalog_status", "history")   # what this stage may change on a record


def load_versions():
    """version -> {code: catalog row}."""
    catalogs = {}
    for version, path in CATALOGS.items():
        catalogs[version] = {p["code"]: p for p in (json.loads(l) for l in path.open(encoding="utf-8") if l.strip())}
    return catalogs


def label(p):
    return " · ".join(x for x in ((p.get(f) or "").strip() for f in FIELDS) if x)


def name_without_numbers(p):
    """Brand and name, normalized, every token with a digit removed: the part of a label a new size keeps."""
    text = normalize.normalize_text(" ".join(x for x in ((p.get("brands") or ""), (p.get("product_name") or "")) if x))
    return " ".join(t for t in text.split() if not any(ch.isdigit() for ch in t))


def same_brand(a, b):
    return normalize.normalize_text(a.get("brands") or "") == normalize.normalize_text(b.get("brands") or "")


def catalog_changes(v1, v2):
    """Every change between two catalog versions, found from the rows: renamed, new size, discontinued, new."""
    changes = []
    for code in sorted(set(v1) & set(v2)):
        for f in FIELDS:
            old, new = (v1[code].get(f) or "").strip(), (v2[code].get(f) or "").strip()
            if old != new:
                changes.append({"change": RENAME_KIND[f], "v1_code": code, "v2_code": code, "field": f, "old": old, "new": new,
                                "v1_label": label(v1[code]), "v2_label": label(v2[code])})
    only1, only2 = sorted(set(v1) - set(v2)), sorted(set(v2) - set(v1))
    paired2 = set()
    for a in only1:
        matches = [b for b in only2 if b not in paired2 and same_brand(v1[a], v2[b])
                   and name_without_numbers(v1[a]) == name_without_numbers(v2[b])]
        if len(matches) == 1:
            b = matches[0]
            paired2.add(b)
            changes.append({"change": NEW_SIZE, "v1_code": a, "v2_code": b, "field": "quantity",
                            "old": (v1[a].get("quantity") or "").strip(), "new": (v2[b].get("quantity") or "").strip(),
                            "v1_label": label(v1[a]), "v2_label": label(v2[b])})
        else:
            changes.append({"change": "discontinued", "v1_code": a, "v2_code": None, "field": None, "old": None, "new": None,
                            "v1_label": label(v1[a]), "v2_label": None})
    for b in only2:
        if b not in paired2:
            changes.append({"change": "new", "v1_code": None, "v2_code": b, "field": None, "old": None, "new": None,
                            "v1_label": None, "v2_label": label(v2[b])})
    return changes


def apply_changes(v1, v2, changes):
    """v1 with the changes applied: the codes and labels v2 should have. The test rebuilds v2 with this.
    A new product has no v1 row to carry forward, so its row is taken from v2 by code alone."""
    out = {code: dict(p) for code, p in v1.items()}
    for c in changes:
        if c["change"] in RENAME_KIND.values():
            out[c["v1_code"]][c["field"]] = c["new"]
        elif c["change"] == NEW_SIZE:
            p = dict(out.pop(c["v1_code"]))
            p["code"], p["quantity"] = c["v2_code"], c["new"]
            if c["field"] == "quantity" and p.get("product_name") and c["old"] and c["old"] in p["product_name"]:
                p["product_name"] = p["product_name"].replace(c["old"], c["new"])   # the size printed inside the name moves too
            out[c["v2_code"]] = p
        elif c["change"] == "discontinued":
            out.pop(c["v1_code"])
        elif c["change"] == "new":
            out[c["v2_code"]] = dict(v2[c["v2_code"]])
    return out


def lineage_of(code, catalogs, changes):
    """The product under each version: [{version, code, label, change}], following the change set."""
    successor = {c["v1_code"]: c for c in changes if c["change"] == NEW_SIZE}
    predecessor = {c["v2_code"]: c for c in changes if c["change"] == NEW_SIZE}
    renamed = {c["v1_code"]: c for c in changes if c["change"] in RENAME_KIND.values()}
    dropped = {c["v1_code"] for c in changes if c["change"] == "discontinued"}
    v1, v2 = catalogs["v1"], catalogs["v2"]
    if code in v1 and code in v2:
        change = renamed[code]["change"] if code in renamed else None
        return [{"version": "v1", "code": code, "label": label(v1[code]), "change": None},
                {"version": "v2", "code": code, "label": label(v2[code]), "change": change}]
    if code in successor:
        new = successor[code]["v2_code"]
        return [{"version": "v1", "code": code, "label": label(v1[code]), "change": None},
                {"version": "v2", "code": new, "label": label(v2[new]), "change": NEW_SIZE}]
    if code in predecessor:
        old = predecessor[code]["v1_code"]
        return [{"version": "v1", "code": old, "label": label(v1[old]), "change": None},
                {"version": "v2", "code": code, "label": label(v2[code]), "change": NEW_SIZE}]
    if code in dropped:
        return [{"version": "v1", "code": code, "label": label(v1[code]), "change": None},
                {"version": "v2", "code": None, "label": None, "change": "discontinued"}]
    return [{"version": "v1", "code": None, "label": None, "change": None},
            {"version": "v2", "code": code, "label": label(v2[code]), "change": "new"}]


def view(rec, version):
    """The record's product code as one catalog version knows it, or None when that version has no such product."""
    return next((step["code"] for step in rec.get("lineage", []) if step["version"] == version), None)


def label_in_effect(code, version, catalogs):
    """The label of the catalog version in effect; v1's label when that version no longer carries the code."""
    return label(catalogs[version][code]) if code in catalogs[version] else label(catalogs["v1"][code])


def migrate(rec, catalogs, changes):
    """The record with its lineage, catalog status, explained history and the label of the version in effect. Idempotent."""
    rec = dict(rec)
    rec["lineage"], rec["catalog_status"] = [], None
    if not rec["product_code"]:
        return rec
    code = rec["product_code"]
    rec["lineage"] = lineage_of(code, catalogs, changes)
    rec["product_label"] = label_in_effect(code, rec["catalog_version"], catalogs)
    v2 = rec["lineage"][1]
    if v2["change"] == "discontinued":
        rec["catalog_status"] = STATUS_DISCONTINUED
    elif v2["change"] == NEW_SIZE:
        rec["catalog_status"] = STATUS_NEW_SIZE if code == v2["code"] else STATUS_OLD_SIZE
    elif v2["change"] in RENAME_KIND.values() and rec["catalog_version"] == "v2":
        rec["catalog_status"] = STATUS_RENAMED
    else:
        rec["catalog_status"] = STATUS_CURRENT
    history = []
    for h in rec["history"]:
        steps = lineage_of(h["product_code"], catalogs, changes)
        successor = steps[1]["code"] if steps[1]["change"] == NEW_SIZE else None
        history.append({**h, "change": steps[1]["change"], "successor": successor, "explains_product": successor == code})
    rec["history"] = history
    return rec


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    if not IN.exists():
        sys.exit(f"STOP: {IN.relative_to(ROOT)} not found. Run pipeline/golden.py first.")
    records = [json.loads(line) for line in IN.open(encoding="utf-8") if line.strip()]
    catalogs = load_versions()
    effective_from = json.loads(V2_META.read_text(encoding="utf-8"))["effective_from"]
    changes = catalog_changes(catalogs["v1"], catalogs["v2"])
    rebuilt = apply_changes(catalogs["v1"], catalogs["v2"], changes)
    migrated = [migrate(rec, catalogs, changes) for rec in records]

    by_change, by_status, relabelled, history_explained, history_other = {}, {}, 0, 0, 0
    for c in changes:
        by_change[c["change"]] = by_change.get(c["change"], 0) + 1
    for before, after in zip(records, migrated):
        if after["catalog_status"]:
            by_status[after["catalog_status"]] = by_status.get(after["catalog_status"], 0) + 1
        relabelled += before["product_label"] != after["product_label"]
        for h in after["history"]:
            if h["explains_product"]:
                history_explained += 1
            else:
                history_other += 1
    leftovers = [rec for rec in migrated if rec["catalog_status"] == STATUS_DISCONTINUED]
    only1, only2 = set(catalogs["v1"]) - set(catalogs["v2"]), set(catalogs["v2"]) - set(catalogs["v1"])
    checks = {
        "the changes applied to v1 rebuild v2, code for code and label for label":
            set(rebuilt) == set(catalogs["v2"]) and all(label(rebuilt[c]) == label(catalogs["v2"][c]) for c in catalogs["v2"]),
        "every code only in v1 and every code only in v2 appears in exactly one change":
            sorted(c["v1_code"] for c in changes if c["v1_code"] in only1) == sorted(only1)
            and sorted(c["v2_code"] for c in changes if c["v2_code"] in only2) == sorted(only2),
        "every record with a product has a lineage with one step per version, and its product is one of the steps":
            all([s["version"] for s in rec["lineage"]] == list(VERSIONS) and rec["product_code"] in {s["code"] for s in rec["lineage"]}
                for rec in migrated if rec["product_code"]),
        "every record's label is the label of the catalog version in effect on the day last seen, or v1's when that version lacks the code":
            all(rec["product_label"] == label_in_effect(rec["product_code"], rec["catalog_version"], catalogs) for rec in migrated if rec["product_code"]),
        "no record was dropped, and every stage 6 field but the label and the history is unchanged":
            len(migrated) == len(records) and all({k: v for k, v in a.items() if k not in MIGRATION_FIELDS}
                                                   == {k: v for k, v in b.items() if k not in MIGRATION_FIELDS} for a, b in zip(migrated, records)),
        "every discontinued leftover was read after the catalog moved on":
            all(any(d >= effective_from for d in rec["days_seen"]) for rec in leftovers),
        "migrating twice changes nothing":
            all(migrate(rec, catalogs, changes) == rec for rec in migrated),
    }

    print(f"STAGE 7  VERSIONING   {IN.relative_to(ROOT)} -> {OUT.relative_to(ROOT)}")
    print(f"  catalog v1 {len(catalogs['v1'])} rows, v2 {len(catalogs['v2'])} rows, v2 in effect from {effective_from}")
    print(f"  changes found       {len(changes):>6}   " + " | ".join(f"{k} {v}" for k, v in sorted(by_change.items(), key=lambda kv: -kv[1])))
    for c in changes:
        if c["change"] == NEW_SIZE:
            print(f"      {c['v1_code']} -> {c['v2_code']}  {c['v1_label']}  ->  {c['new']}")
        elif c["change"] in RENAME_KIND.values():
            print(f"      {c['v1_code']}  {c['field']}: {c['old']!r} -> {c['new']!r}")
        else:
            print(f"      {c['v1_code'] or c['v2_code']}  {c['change']}: {c['v1_label'] or c['v2_label']}")
    print(f"  records             {len(migrated):>6}   " + " | ".join(f"{k} {v}" for k, v in sorted(by_status.items(), key=lambda kv: -kv[1])))
    print(f"  relabelled          {relabelled:>6}   records now carry the name of the catalog version in effect")
    print(f"  history             {history_explained:>6}   entries explained by a change (the record's product is the old code's successor); "
          f"{history_other} not explained")
    print(f"  leftovers           {len(leftovers):>6}   records keep a product v2 dropped: "
          + "; ".join(f"{rec['spot_id']} read on {', '.join(rec['days_seen'])}" for rec in leftovers))
    for name, ok in checks.items():
        print(f"  CHECK    {name}: {ok}")
    if not all(checks.values()):
        sys.exit("STOP: a check failed. Nothing was written.")
    OUT_CHANGES.write_text("".join(json.dumps(c, ensure_ascii=False) + "\n" for c in changes), encoding="utf-8", newline="\n")
    OUT.write_text("".join(json.dumps(rec, ensure_ascii=False) + "\n" for rec in migrated), encoding="utf-8", newline="\n")
    manifest = {
        "stage": 7, "name": "versioning",
        "input": {"path": IN.relative_to(ROOT).as_posix(), "records": len(records),
                  "catalogs": {v: p.relative_to(ROOT).as_posix() for v, p in CATALOGS.items()}, "catalog_v2_effective_from": effective_from},
        "output": {"changes": {"path": OUT_CHANGES.relative_to(ROOT).as_posix(), "rows": len(changes)},
                   "records": {"path": OUT.relative_to(ROOT).as_posix(), "records": len(migrated)}},
        "rules": {"compared_fields": list(FIELDS),
                  "new_size": "a code only in v1 and a code only in v2, same brand, same name once every token with a digit is removed",
                  "unpaired": "only in v1: discontinued; only in v2: new",
                  "label": "from the catalog version in effect on the day the product was last seen",
                  "dropped_products": "kept, with status '" + STATUS_DISCONTINUED + "'"},
        "changes_by_kind": by_change, "records_by_catalog_status": by_status, "records_relabelled": relabelled,
        "history_explained_by_a_change": history_explained, "history_not_explained": history_other,
        "leftover_records": [rec["spot_id"] for rec in leftovers],
        "checks": checks,
    }
    MANIFEST.write_text(json.dumps(manifest, indent=1) + "\n", encoding="utf-8", newline="\n")
    print(f"\nWrote {OUT_CHANGES.relative_to(ROOT)} ({len(changes)} changes), {OUT.relative_to(ROOT)} ({len(migrated)} records) "
          f"and {MANIFEST.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
