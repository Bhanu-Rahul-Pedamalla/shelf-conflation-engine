"""Step 1.2 - the plan for the shelf, and what is really on the shelf.

    python sim/planogram.py                 # all stores: problems, restocking, totals
    python sim/planogram.py --map S002 2    # the same, plus the full shelf map of one store on one day

Three stores share one catalog and one plan. Each store has its own planted problems.
Overnight between day 1 and day 2 some empty positions are refilled and new ones run out.

Writes:
  data/planogram.csv           the plan: which product belongs in which position
  data/shelf_truth_day1.csv    the real shelf on day 1: the plan, plus planted problems
  data/shelf_truth_day2.csv    the real shelf on day 2, after restocking
  data/shelf_truth_day3.csv    the real shelf on day 3, same as day 2

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
DAYS = 3                 # the shelf is recorded once per day


def truth_path(day):
    return catalog.ROOT / "data" / f"shelf_truth_day{day}.csv"


STORES = {"S001": 1, "S002": 2, "S003": 3}   # store -> seed for its planted problems.
                                             # Same seed, same problems. One plan for all stores.
AISLES = {"sodas": "A01", "breakfast-cereals": "A02", "snacks": "A03"}
SHELVES = 4              # per aisle. Shelf 1 is the bottom shelf.
OUT_OF_STOCK = (2, 3)    # positions left empty, per aisle
MISPLACED = (1, 2)       # single units sitting in the wrong aisle, whole store

# What changes overnight between day 1 and day 2. Day 3 is the same shelf as day 2.
RESTOCK_SHARE = 0.5          # this share of the out-of-stock positions is refilled, rounded down
NEW_OUT_OF_STOCK = (1, 2)    # stocked positions that newly run out, per store
RESTOCK_SEED = 1000          # a store's restock seed = its seed + 1000, so day 1 is not disturbed


def facings_by_scans(items):
    """More scans in the catalog -> more facings. Top third 3, middle third 2, rest 1."""
    ranked = sorted(items, key=lambda p: (-(p.get("unique_scans_n") or 0), p["code"]))
    n = len(ranked)
    return {p["code"]: 3 if i < n / 3 else 2 if i < 2 * n / 3 else 1 for i, p in enumerate(ranked)}


def build_planogram(products, meta, store):
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
                "store_id": store,
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


def restock(day1, rng):
    """One store's shelf on day 2: day 1, with some empty positions refilled and new ones emptied.

    The misplaced unit stays where it is.
    """
    truth = [dict(slot, change="") for slot in day1]
    empty = sorted({place(s) for s in truth if s["status"] == "out_of_stock"})
    refilled = set(rng.sample(empty, int(len(empty) * RESTOCK_SHARE)))
    hosting_a_stray = {place(s) for s in truth if s["status"] == "misplaced"}
    stocked = sorted({place(s) for s in truth} - set(empty) - hosting_a_stray)
    emptied = set(rng.sample(stocked, rng.randint(*NEW_OUT_OF_STOCK)))
    for slot in truth:
        if place(slot) in refilled:
            slot.update(actual_code=slot["planned_code"], status="ok", change="restocked")
        elif place(slot) in emptied:
            slot.update(actual_code="", status="out_of_stock", change="new_out_of_stock")
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


def problems(truth):
    """The planted problems of one store, as a set that can be compared with another store's."""
    return {(place(s), s["facing"], s["status"], s["actual_code"]) for s in truth if s["status"] != "ok"}


def summary(plan, truth, products):
    """Every store's planted problems, then the totals. Use --map S001 for a full shelf map."""
    by_code = {p["code"]: p for p in products}
    home_aisle = {row["code"]: row["aisle"] for row in plan}
    total = {"ok": 0, "out_of_stock": 0, "misplaced": 0}

    for store, seed in STORES.items():
        store_plan = [row for row in plan if row["store_id"] == store]
        store_truth = [s for s in truth if s["store_id"] == store]
        print(f"\nSTORE {store}  seed {seed}")
        for row in store_plan:
            slots = [s for s in store_truth if place(s) == place(row)]
            where = f"{row['aisle']} shelf {row['shelf']} pos {row['position']}"
            if slots[0]["status"] == "out_of_stock":
                print(f"  out of stock  {where}  {row['code']:<14} {label(by_code[row['code']])}")
            for s in slots:
                if s["status"] == "misplaced":
                    stray = s["actual_code"]
                    print(f"  misplaced     {where} facing {s['facing']} holds {stray} "
                          f"from {home_aisle[stray]}: {label(by_code[stray])}")
        counts = {status: sum(1 for s in store_truth if s["status"] == status) for status in total}
        facings = sum(row["facings"] for row in store_plan)
        empty_positions = len({place(s) for s in store_truth if s["status"] == "out_of_stock"})
        print(f"  PLAN     {len(store_plan)} positions, {facings} facings")
        print(f"  REALITY  {counts['ok']} as planned, {counts['out_of_stock']} empty "
              f"({empty_positions} positions out of stock), {counts['misplaced']} misplaced")
        print(f"  CHECK    {counts['ok']} + {counts['out_of_stock']} + {counts['misplaced']} = "
              f"{sum(counts.values())}, plan facings = {facings}")
        for status in total:
            total[status] += counts[status]

    facings = sum(row["facings"] for row in plan)
    sets = [problems([s for s in truth if s["store_id"] == store]) for store in STORES]
    all_differ = all(a != b for i, a in enumerate(sets) for b in sets[i + 1:])
    print(f"\nALL STORES  {len(STORES)} stores, one catalog of {len(products)} products")
    print(f"  PLAN     {len(plan)} positions, {facings} facings")
    print(f"  REALITY  {total['ok']} as planned, {total['out_of_stock']} empty, {total['misplaced']} misplaced")
    print(f"  CHECK    as planned + empty + misplaced = {sum(total.values())}, plan facings = {facings}")
    print(f"  CHECK    every store has a different set of problems: {all_differ}")


def empty_positions(truth):
    return {place(s) for s in truth if s["status"] == "out_of_stock"}


def restock_summary(plan, days, products):
    """What changed overnight in each store, then the shelf totals for every day."""
    by_code = {p["code"]: p for p in products}
    print("\nRESTOCKING - overnight between day 1 and day 2")
    for store, seed in STORES.items():
        day1 = [s for s in days[1] if s["store_id"] == store]
        day2 = [s for s in days[2] if s["store_id"] == store]
        print(f"\nSTORE {store}  restock seed {seed + RESTOCK_SEED}")
        for row in (r for r in plan if r["store_id"] == store):
            change = next(s["change"] for s in day2 if place(s) == place(row))
            if change:
                print(f"  {change.replace('_', ' '):<17} {row['aisle']} shelf {row['shelf']} pos {row['position']}"
                      f"  {row['code']:<14} {label(by_code[row['code']])}")
        before, after = empty_positions(day1), empty_positions(day2)
        refilled, emptied = before - after, after - before
        print(f"  CHECK    day 1 out of stock {len(before)} - restocked {len(refilled)} + new {len(emptied)} = "
              f"{len(before) - len(refilled) + len(emptied)}, day 2 out of stock = {len(after)}")
        misplaced = [(place(s), s["facing"], s["actual_code"]) for s in day1 if s["status"] == "misplaced"]
        print(f"  CHECK    the misplaced unit did not move: "
              f"{misplaced == [(place(s), s['facing'], s['actual_code']) for s in day2 if s['status'] == 'misplaced']}")

    facings = sum(row["facings"] for row in plan)
    print(f"\nALL STORES, DAY BY DAY  plan facings = {facings}")
    for day in sorted(days):
        counts = {status: sum(1 for s in days[day] if s["status"] == status)
                  for status in ("ok", "out_of_stock", "misplaced")}
        print(f"  day {day}   {counts['ok']} as planned, {counts['out_of_stock']} empty "
              f"({len({(s['store_id'],) + place(s) for s in days[day] if s['status'] == 'out_of_stock'})} positions), "
              f"{counts['misplaced']} misplaced   CHECK sum = {sum(counts.values())}")
    same = [{k: v for k, v in s.items() if k != "change"} for s in days[2]] == \
           [{k: v for k, v in s.items() if k != "change"} for s in days[3]]
    print(f"  CHECK    the day 3 shelf is the day 2 shelf: {same}")


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    products = catalog.load()
    meta = json.loads(catalog.META.read_text(encoding="utf-8"))
    plan, days = [], {day: [] for day in range(1, DAYS + 1)}
    for store, seed in STORES.items():
        store_plan = build_planogram(products, meta, store)
        plan += store_plan
        day1 = [dict(slot, change="") for slot in build_truth(store_plan, random.Random(seed))]
        day2 = restock(day1, random.Random(seed + RESTOCK_SEED))
        days[1] += day1
        days[2] += day2
        for day in range(3, DAYS + 1):   # nothing changes after day 2
            days[day] += [dict(slot, change="") for slot in day2]
    write_csv(PLANOGRAM, plan)
    for day, truth in days.items():
        write_csv(truth_path(day), truth)
    print(f"Wrote {PLANOGRAM.relative_to(catalog.ROOT)} ({len(plan)} rows) and "
          f"{', '.join(truth_path(day).name for day in days)} ({len(days[1])} rows each), {len(STORES)} stores")
    print("\nDAY 1 - the planted problems")
    summary(plan, days[1], products)
    restock_summary(plan, days, products)
    if "--map" in sys.argv:   # full shelf map of one store on one day: python sim/planogram.py --map S002 2
        i = sys.argv.index("--map")
        store = sys.argv[i + 1]
        day = int(sys.argv[i + 2]) if len(sys.argv) > i + 2 else 1
        print(f"\nFULL MAP OF {store}, DAY {day}")
        report([row for row in plan if row["store_id"] == store],
               [s for s in days[day] if s["store_id"] == store], products)


if __name__ == "__main__":
    main()
