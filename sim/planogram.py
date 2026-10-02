"""Step 1.2 - the plan for the shelf, and what is really on the shelf.

    python sim/planogram.py

Writes two files:
  data/planogram.csv     the plan: which product belongs in which position
  data/shelf_truth.csv   the real shelf: the plan, plus planted problems

The plan has no problems in it. The problems live only in shelf_truth, because a
problem is a difference between the plan and the shelf.

Every rule in this file is ASSUMED. None of it is taken from a real store.
"""
import csv
import json
import math
import random
import sys

import catalog  # sim/catalog.py: the saved catalog snapshot

PLANOGRAM = catalog.ROOT / "data" / "planogram.csv"
TRUTH = catalog.ROOT / "data" / "shelf_truth.csv"

SEED = 1                 # same seed, same store
STORE = "S001"
AISLES = {"sodas": "A01", "breakfast-cereals": "A02", "snacks": "A03"}
SHELVES = 4              # per aisle. Shelf 1 is the bottom shelf.
OUT_OF_STOCK = (2, 3)    # positions left empty, per aisle
MISPLACED = (1, 2)       # single units sitting in the wrong aisle, whole store


def facings_by_scans(items):
    """More scans in the catalog -> more facings. Top third 3, middle third 2, rest 1."""
    ranked = sorted(items, key=lambda p: (-(p.get("unique_scans_n") or 0), p["code"]))
    n = len(ranked)
    return {p["code"]: 3 if i < n / 3 else 2 if i < 2 * n / 3 else 1 for i, p in enumerate(ranked)}


def build_planogram(products, meta):
    """One row per position. Same brand side by side: sorted by brand, then name."""
    by_code = {p["code"]: p for p in products}
    plan = []
    for info in meta["categories"]:
        items = [by_code[code] for code in info["codes"]]
        items.sort(key=lambda p: ((p.get("brands") or "").casefold(),
                                  (p.get("product_name") or "").casefold(),
                                  p["code"]))
        facings = facings_by_scans(items)
        per_shelf = math.ceil(len(items) / SHELVES)
        for i, p in enumerate(items):
            plan.append({
                "store_id": STORE,
                "aisle": AISLES[info["category"]],
                "category": info["category"],
                "shelf": i // per_shelf + 1,
                "position": i % per_shelf + 1,
                "code": p["code"],
                "facings": facings[p["code"]],
            })
    return plan


def place(row):
    return (row["aisle"], row["shelf"], row["position"])


def build_truth(plan, rng):
    """One row per facing. Starts as a copy of the plan, then problems are planted."""
    truth = []
    for row in plan:
        for facing in range(1, row["facings"] + 1):
            truth.append({
                "store_id": row["store_id"],
                "aisle": row["aisle"],
                "shelf": row["shelf"],
                "position": row["position"],
                "facing": facing,
                "planned_code": row["code"],
                "actual_code": row["code"],
                "status": "ok",
            })

    # Problem 1: out of stock. Every facing of the chosen positions is empty.
    empty = set()
    for aisle in sorted(AISLES.values()):
        in_aisle = [row for row in plan if row["aisle"] == aisle]
        for row in rng.sample(in_aisle, rng.randint(*OUT_OF_STOCK)):
            empty.add(place(row))
    for slot in truth:
        if place(slot) in empty:
            slot["actual_code"] = ""
            slot["status"] = "out_of_stock"

    # Problem 2: misplaced. One unit of a product sits in another aisle,
    # in the last facing of a position that has 2 or more facings.
    stocked = [row for row in plan if place(row) not in empty]
    hosts = [row for row in stocked if row["facings"] >= 2]
    for _ in range(rng.randint(*MISPLACED)):
        host = rng.choice(hosts)
        hosts.remove(host)
        stray = rng.choice([row for row in stocked if row["aisle"] != host["aisle"]])
        stocked.remove(stray)
        for slot in truth:
            if place(slot) == place(host) and slot["facing"] == host["facings"]:
                slot["actual_code"] = stray["code"]
                slot["status"] = "misplaced"
    return truth


def write_csv(path, rows):
    # newline="" + lineterminator="\n": same bytes on every operating system
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0]), lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def label(p):
    text = f"{p.get('brands')} | {p.get('product_name')} | {p.get('quantity') or '(no size)'}"
    return text if len(text) <= 62 else text[:59] + "..."


def report(plan, truth, products):
    by_code = {p["code"]: p for p in products}
    home_aisle = {row["code"]: row["aisle"] for row in plan}
    slots = {}
    for slot in truth:
        slots.setdefault(place(slot), []).append(slot)

    for category, aisle in AISLES.items():
        rows = [row for row in plan if row["aisle"] == aisle]
        print(f"\nAISLE {aisle}  {category}  -  {len(rows)} positions, "
              f"{sum(row['facings'] for row in rows)} facings")
        for shelf in range(SHELVES, 0, -1):
            print(f"  shelf {shelf}")
            for row in (r for r in rows if r["shelf"] == shelf):
                print(f"    pos {row['position']}  x{row['facings']}  {row['code']:<14} {label(by_code[row['code']])}")
                for slot in slots[place(row)]:
                    if slot["status"] == "misplaced":
                        stray = slot["actual_code"]
                        print(f"           >> REALITY: facing {slot['facing']} holds {stray} "
                              f"from {home_aisle[stray]}: {label(by_code[stray])}")
                if slots[place(row)][0]["status"] == "out_of_stock":
                    print("           >> REALITY: empty, out of stock")

    counts = {status: sum(1 for s in truth if s["status"] == status)
              for status in ("ok", "out_of_stock", "misplaced")}
    empty_positions = len({place(s) for s in truth if s["status"] == "out_of_stock"})
    print(f"\nPLAN     {len(plan)} positions, {sum(row['facings'] for row in plan)} facings")
    print(f"REALITY  {len(truth)} facings: {counts['ok']} as planned, "
          f"{counts['out_of_stock']} empty ({empty_positions} positions out of stock), "
          f"{counts['misplaced']} misplaced")
    print(f"CHECK    as planned + empty + misplaced = {sum(counts.values())}, "
          f"plan facings = {sum(row['facings'] for row in plan)}")


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    products = catalog.load()
    meta = json.loads(catalog.META.read_text(encoding="utf-8"))
    plan = build_planogram(products, meta)
    truth = build_truth(plan, random.Random(SEED))
    write_csv(PLANOGRAM, plan)
    write_csv(TRUTH, truth)
    print(f"Wrote {PLANOGRAM.relative_to(catalog.ROOT)} ({len(plan)} rows) "
          f"and {TRUTH.relative_to(catalog.ROOT)} ({len(truth)} rows), seed {SEED}")
    report(plan, truth, products)


if __name__ == "__main__":
    main()
