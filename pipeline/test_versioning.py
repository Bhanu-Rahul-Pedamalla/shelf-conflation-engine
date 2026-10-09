"""The versioning test: the record survives the catalog change.

    python pipeline/test_versioning.py        # or: python -m pytest pipeline/test_versioning.py

Reads   data/pipeline/05_reads.jsonl, data/pipeline/06_golden.jsonl, data/pipeline/07_golden.jsonl
        data/catalog_raw.jsonl, data/catalog_v2.jsonl, data/catalog_v2.meta.json
Writes  nothing. No truth file is read here; that is the scorer's job.

The realogram as it stood before catalog v2 took effect is rebuilt from the reads of the days before,
with the catalog pinned to v1: a record built in a world that has never heard of v2. The change set is
then applied to it. Every record that named a product still names one: the same code when v2 kept it,
the successor code when v2 gave the product a new size and barcode, or the old code with the record
saying v2 dropped it. Where the migration named a successor, the later days' reads of the same spot
are checked against it. The records the catalog dropped are checked to be still there, unchanged but
for the status. Each test prints the counts it asserted on, so a pass is a number and not a word.
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from golden import build_record, load_labels, spot_key                                   # noqa: E402
from match_crossday import load_catalog                                                   # noqa: E402
from versioning import (IN, OUT, PIPELINE, STATUS_DISCONTINUED, STATUS_NEW_SIZE, STATUS_OLD_SIZE,   # noqa: E402
                        VERSIONS, apply_changes, catalog_changes, label, load_versions, migrate, view)

READS = PIPELINE / "05_reads.jsonl"
_world = {}


def world():
    """The inputs, loaded once: both catalogs, the change set, the reads and the stage 6 and 7 records."""
    if not _world:
        catalogs = load_versions()
        _, _, effective_from = load_catalog()
        _world.update(catalogs=catalogs, effective_from=effective_from,
                      changes=catalog_changes(catalogs["v1"], catalogs["v2"]),
                      reads=[json.loads(l) for l in READS.open(encoding="utf-8") if l.strip()],
                      before=[json.loads(l) for l in IN.open(encoding="utf-8") if l.strip()],
                      after=[json.loads(l) for l in OUT.open(encoding="utf-8") if l.strip()])
    return _world


def records_before_the_change():
    """The golden record built from the reads before v2 took effect, with a catalog that knows only v1."""
    w = world()
    pre = [r for r in w["reads"] if r["ts"][:10] < w["effective_from"]]
    codes = set(w["catalogs"]["v1"]) | set(w["catalogs"]["v2"])
    v1_only_world = {code: ({"v1"} if code in w["catalogs"]["v1"] else set()) for code in codes}
    spots = {}
    for r in pre:
        spots.setdefault(spot_key(r), []).append(r)
    records = [build_record(key, spots[key], v1_only_world, load_labels(), w["effective_from"]) for key in sorted(spots)]
    return pre, records


def test_the_changes_rebuild_v2():
    """The change set found from the two files is complete: applied to v1 it gives v2, code for code and label for label."""
    w = world()
    rebuilt = apply_changes(w["catalogs"]["v1"], w["catalogs"]["v2"], w["changes"])
    assert set(rebuilt) == set(w["catalogs"]["v2"])
    assert all(label(rebuilt[c]) == label(w["catalogs"]["v2"][c]) for c in rebuilt)
    kinds = {}
    for c in w["changes"]:
        kinds[c["change"]] = kinds.get(c["change"], 0) + 1
    return f"{len(w['changes'])} changes ({', '.join(f'{k} {v}' for k, v in kinds.items())}) rebuild v2's {len(rebuilt)} rows from v1's {len(w['catalogs']['v1'])}"


def test_the_record_built_before_the_change_survives_it():
    """Every record built before v2 that names a product still names one after the change set is applied."""
    w = world()
    pre, before = records_before_the_change()
    after = [migrate(rec, w["catalogs"], w["changes"]) for rec in before]
    outcome = {"same code in v2": 0, "successor code in v2": 0, "dropped by v2, record kept": 0, "lost": 0}
    for rec in after:
        if not rec["product_code"]:
            continue
        v2 = view(rec, "v2")
        if v2 == rec["product_code"]:
            outcome["same code in v2"] += 1
        elif v2 in w["catalogs"]["v2"]:
            outcome["successor code in v2"] += 1
        elif rec["catalog_status"] == STATUS_DISCONTINUED and rec["product_code"] in w["catalogs"]["v1"]:
            outcome["dropped by v2, record kept"] += 1
        else:
            outcome["lost"] += 1
    with_product = sum(1 for rec in before if rec["product_code"])
    assert len(after) == len(before)
    assert outcome["lost"] == 0
    assert sum(outcome.values()) == with_product
    assert not any(d >= w["effective_from"] for rec in before for d in rec["days_seen"])   # built from the days before only
    return (f"{len(pre)} reads before {w['effective_from']} -> {len(before)} records, {with_product} with a product: "
            + ", ".join(f"{k} {v}" for k, v in outcome.items()))


def test_the_migration_predicts_what_the_later_days_read():
    """Where the migration gave a pre-change record a successor code, the full record built from all days names that code."""
    w = world()
    _, before = records_before_the_change()
    full = {rec["spot_id"]: rec for rec in w["after"]}
    moved = [migrate(rec, w["catalogs"], w["changes"]) for rec in before]
    moved = [rec for rec in moved if rec["catalog_status"] == STATUS_OLD_SIZE]
    predicted, not_read_later = 0, 0
    for rec in moved:
        later = full[rec["spot_id"]]
        if any(d >= w["effective_from"] for d in later["days_seen"]):
            assert later["product_code"] == view(rec, "v2"), (rec["spot_id"], later["product_code"], view(rec, "v2"))
            assert later["catalog_status"] == STATUS_NEW_SIZE
            assert rec["product_code"] in {h["product_code"] for h in later["history"]}
            predicted += 1
        else:
            assert later["product_code"] == rec["product_code"] and later["catalog_status"] == STATUS_OLD_SIZE
            not_read_later += 1
    assert predicted + not_read_later == len(moved)
    return f"{len(moved)} pre-change records moved to a successor code: {predicted} confirmed by the later days' reads, {not_read_later} not read again"


def test_the_dropped_products_stay_on_the_record():
    """The records whose product v2 dropped are all still there, with the product, the reads and the status that says so."""
    w = world()
    before = {rec["spot_id"]: rec for rec in w["before"]}
    leftovers = [rec for rec in w["after"] if rec["catalog_status"] == STATUS_DISCONTINUED]
    flagged_by_stage_6 = {rec["spot_id"] for rec in w["before"] if rec["product_code"] and not rec["in_catalog_version"]}
    assert {rec["spot_id"] for rec in leftovers} == flagged_by_stage_6
    for rec in leftovers:
        old = before[rec["spot_id"]]
        assert rec["product_code"] == old["product_code"] and rec["product_code"] not in w["catalogs"]["v2"]
        assert rec["source_reads"] == old["source_reads"] and rec["supporting_reads"] == old["supporting_reads"]
        assert rec["status"] == old["status"] and rec["in_catalog_version"] is False
        assert view(rec, "v2") is None and view(rec, "v1") == rec["product_code"]
        assert any(d >= w["effective_from"] for d in rec["days_seen"])
    return (f"{len(leftovers)} records keep a product v2 dropped, every one read after {w['effective_from']}: "
            + ", ".join(f"{rec['spot_id']} ({len(rec['supporting_reads'])} reads)" for rec in leftovers))


def test_every_version_view_is_that_version_s_code():
    """Each record's product under v1 is a v1 code or none, under v2 a v2 code or none; none only for a dropped or a new product."""
    w = world()
    none_v1, none_v2 = 0, 0
    for rec in w["after"]:
        if not rec["product_code"]:
            assert rec["lineage"] == [] and rec["catalog_status"] is None
            continue
        assert [s["version"] for s in rec["lineage"]] == list(VERSIONS)
        for version in VERSIONS:
            code = view(rec, version)
            assert code is None or code in w["catalogs"][version]
        none_v1 += view(rec, "v1") is None
        none_v2 += view(rec, "v2") is None
        if view(rec, "v2") is None:
            assert rec["catalog_status"] == STATUS_DISCONTINUED
    with_product = sum(1 for rec in w["after"] if rec["product_code"])
    return f"{with_product} records with a product: {with_product - none_v1} known to v1, {with_product - none_v2} known to v2, {none_v2} dropped by v2, {none_v1} new in v2"


def test_the_migration_is_idempotent_and_is_what_was_written():
    """Migrating the stage 6 file gives the stage 7 file; migrating the stage 7 file changes nothing."""
    w = world()
    once = [migrate(rec, w["catalogs"], w["changes"]) for rec in w["before"]]
    assert once == w["after"]
    assert [migrate(rec, w["catalogs"], w["changes"]) for rec in w["after"]] == w["after"]
    return f"{len(once)} records: migrate(stage 6) == stage 7 file, migrate(stage 7) == stage 7"


TESTS = (test_the_changes_rebuild_v2, test_the_record_built_before_the_change_survives_it,
         test_the_migration_predicts_what_the_later_days_read, test_the_dropped_products_stay_on_the_record,
         test_every_version_view_is_that_version_s_code, test_the_migration_is_idempotent_and_is_what_was_written)


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    print("TEST   stage 7 versioning: the record survives the catalog change")
    failed = 0
    for test in TESTS:
        try:
            note = test()
            print(f"  PASS   {test.__name__}: {note}")
        except AssertionError as e:
            failed += 1
            print(f"  FAIL   {test.__name__}: {e!r}")
    print(f"  RESULT   {len(TESTS) - failed} of {len(TESTS)} passed" + (", FAILED" if failed else ""))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
