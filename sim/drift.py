"""Catalog drift: a second version of the catalog.

    python sim/drift.py            # build catalog v2 once, then always read the saved files
    python sim/drift.py --refresh  # build it again (pulls the one new product again)
    python sim/drift.py --rebuild  # apply the rules again, keeping the saved new product (no network)

Reads   data/catalog_raw.jsonl            catalog v1, the real snapshot. Never changed.
Writes  data/catalog_v2.jsonl             catalog v2: v1 with seven changes
        data/catalog_v2.meta.json         how v2 was made, and the day it takes effect
        data/catalog_changes_truth.csv    the seven changes. The pipeline never reads this file.

Real and simulated:
  The one new product in v2 is real, pulled from Open Food Facts.
  The renames, the new sizes and the new barcodes are SIMULATED. They are not real products.

A new size gets a new barcode. Source: GS1 GTIN Management Standard, rule "Declared net content":
  "Any change (increase or decrease) to the legally-required declared net content that is
  printed on the pack, requires assignment of a new GTIN."
  https://www.gs1.org/1/gtinrules/en/rule/266/declared-net-content
"""
import csv
import datetime
import hashlib
import json
import random
import re
import sys

import catalog  # sim/catalog.py

V2 = catalog.ROOT / "data" / "catalog_v2.jsonl"
V2_META = catalog.ROOT / "data" / "catalog_v2.meta.json"
CHANGES = catalog.ROOT / "data" / "catalog_changes_truth.csv"

GS1_RULE = ("GS1 GTIN Management Standard, rule 'Declared net content': any change to the declared "
            "net content printed on the pack requires a new GTIN. "
            "https://www.gs1.org/1/gtinrules/en/rule/266/declared-net-content")

# ---- ASSUMED: what changes between catalog v1 and v2, and when.
SEED = 7000              # which products change
EFFECTIVE_DAY = 2        # shelves and labels follow v2 from this day on
DAY_ONE = datetime.date(2026, 10, 2)   # the first day of the walks; day 2 is the day after
EFFECTIVE_FROM = DAY_ONE + datetime.timedelta(days=EFFECTIVE_DAY - 1)   # the date v2 took effect, as a catalog states it
RENAMED = 3              # same barcode, new name
NEW_SIZE = 2             # new size, and so a new barcode (GS1 rule above)
DISCONTINUED = 1         # gone from v2. Leftover stock stays on the shelf.
SHRINK = 0.9             # a new size is 10% smaller
NEW_FROM_SITE = "openfoodfacts"
NEW_FROM_CATEGORY = "sodas"   # the one new product is the next real soda not already in v1
RENAME_RULES = [
    ("'New ' in front", lambda name: "New " + name),
    ("' Classic' at the end", lambda name: name + " Classic"),
    ("last word dropped", lambda name: " ".join(name.split()[:-1])),
]

NUMBER = re.compile(r"\d+(?:\.\d+)?")


def check_digit(body):
    """GS1 check digit: from the right of the body, weights 3, 1, 3, 1 ..."""
    total = sum(int(ch) * (3 if i % 2 == 0 else 1) for i, ch in enumerate(reversed(body)))
    return str((10 - total % 10) % 10)


def new_barcode(code, rng, taken):
    """A simulated 13-digit barcode: same first 7 digits, 5 new digits, a valid check digit."""
    while True:
        body = code[:7] + "".join(str(rng.randint(0, 9)) for _ in range(5))
        barcode = body + check_digit(body)
        if barcode not in taken:
            return barcode


def shrink(quantity):
    """'20 fl oz' -> '18 fl oz'. The one number in the size, times SHRINK."""
    value = round(float(NUMBER.search(quantity).group()) * SHRINK, 1)
    return NUMBER.sub(f"{value:g}", quantity, count=1)


def resize_name(name, old_quantity, new_quantity):
    """A name that carries its size ('Diet Coke - 6pk/16.9 fl oz Bottles') carries the new size after a resize."""
    return name.replace(old_quantity, new_quantity) if old_quantity and old_quantity in name else name


def build(v1, new_product, rng):
    """Returns the v2 rows and the list of changes."""
    codes = {p["code"] for p in v1}

    can_resize = [p for p in v1 if len(p["code"]) == 13 and len(NUMBER.findall(p.get("quantity") or "")) == 1]
    resized = rng.sample(can_resize, NEW_SIZE)
    left = [p for p in v1 if p not in resized]
    can_rename = [p for p in left if len((p.get("product_name") or "").split()) >= 2]
    renamed = rng.sample(can_rename, RENAMED)
    left = [p for p in left if p not in renamed]
    discontinued = rng.sample(left, DISCONTINUED)

    new_name = {p["code"]: (rule(p["product_name"]), note) for p, (note, rule) in zip(renamed, RENAME_RULES)}
    successor = {}
    for p in resized:
        successor[p["code"]] = new_barcode(p["code"], rng, codes | set(successor.values()) | {new_product["code"]})

    v2, changes = [], []
    for p in v1:
        code = p["code"]
        if p in discontinued:
            changes.append({"change": "discontinued", "v1_code": code, "v2_code": "", "field": "",
                            "old_value": "", "new_value": "", "note": "leftover stock stays on the shelf"})
        elif code in successor:
            size = shrink(p["quantity"])
            name = resize_name(p.get("product_name") or "", p["quantity"], size)
            v2.append(dict(p, code=successor[code], quantity=size, product_name=name))
            changes.append({"change": "new_size", "v1_code": code, "v2_code": successor[code], "field": "quantity",
                            "old_value": p["quantity"], "new_value": size,
                            "note": "simulated: 10% smaller, new barcode per the GS1 net content rule"
                                    + ("; the size inside the name is rewritten too" if name != p.get("product_name") else "")})
        elif code in new_name:
            name, note = new_name[code]
            v2.append(dict(p, product_name=name))
            changes.append({"change": "renamed", "v1_code": code, "v2_code": code, "field": "product_name",
                            "old_value": p["product_name"], "new_value": name, "note": "simulated: " + note})
        else:
            v2.append(p)
    v2.append(new_product)
    changes.append({"change": "new", "v1_code": "", "v2_code": new_product["code"], "field": "",
                    "old_value": "", "new_value": "", "note": "real product, in the catalog but not on any shelf"})
    return v2, changes


def pull_new_product(v1):
    """One real product that is not in v1: the next usable row of the category's default order."""
    codes = {p["code"] for p in v1}
    for page in range(1, catalog.MAX_PAGES + 1):
        rows, reported = catalog.pull(NEW_FROM_SITE, NEW_FROM_CATEGORY, page)
        for p in rows:
            if catalog.usable(p) and p["code"] not in codes:
                return p, {"site": NEW_FROM_SITE, "category": NEW_FROM_CATEGORY, "page": page,
                           "matching_records_reported": reported}
    sys.exit(f"No product outside v1 found in {catalog.MAX_PAGES} pages of {NEW_FROM_CATEGORY}. Nothing was written.")


def save(v2, changes, info):
    lines = [json.dumps(p, ensure_ascii=False) for p in v2]
    V2.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    with CHANGES.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(changes[0]), lineterminator="\n")
        writer.writeheader()
        writer.writerows(changes)
    meta = {
        "derived_from": catalog.RAW.name,
        "derived_from_sha256": hashlib.sha256(catalog.RAW.read_bytes()).hexdigest(),
        "built_at_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
        "seed": SEED,
        "effective_day": EFFECTIVE_DAY,
        "effective_from": EFFECTIVE_FROM.isoformat(),
        "simulated": "renames, new sizes and new barcodes are simulated; the one new product is real",
        "new_barcode_rule": GS1_RULE,
        "new_product_pull": info,
        "rows": len(v2),
        "sha256": hashlib.sha256(V2.read_bytes()).hexdigest(),
    }
    V2_META.write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8", newline="\n")


def load_v2():
    with V2.open(encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def load_changes():
    if not CHANGES.exists():
        sys.exit("data/catalog_changes_truth.csv is missing. Run: python sim/drift.py")
    with CHANGES.open(encoding="utf-8") as f:
        return list(csv.DictReader(f))


def successors():
    """{old barcode: new barcode} for the products that got a new size."""
    return {c["v1_code"]: c["v2_code"] for c in load_changes() if c["change"] == "new_size"}


def report(v1, v2, changes):
    by1 = {p["code"]: p for p in v1}
    by2 = {p["code"]: p for p in v2}
    count = {kind: sum(1 for c in changes if c["change"] == kind) for kind in ("renamed", "new_size", "discontinued", "new")}
    print(f"\nCATALOG DRIFT - v1 has {len(v1)} products, v2 has {len(v2)}, v2 takes effect on day {EFFECTIVE_DAY}")
    for c in changes:
        if c["change"] == "renamed":
            print(f"  renamed       {c['v1_code']:<14} {c['old_value']!r} -> {c['new_value']!r}   ({c['note']})")
        elif c["change"] == "new_size":
            p = by1[c["v1_code"]]
            print(f"  new size      {c['v1_code']:<14} {p.get('brands')} | {p.get('product_name')}: "
                  f"{c['old_value']!r} -> {c['new_value']!r}, new barcode {c['v2_code']}")
        elif c["change"] == "discontinued":
            p = by1[c["v1_code"]]
            print(f"  discontinued  {c['v1_code']:<14} {p.get('brands')} | {p.get('product_name')} | {p.get('quantity')}")
        else:
            p = by2[c["v2_code"]]
            print(f"  new           {c['v2_code']:<14} {p.get('brands')} | {p.get('product_name')} | {p.get('quantity')}   (real)")

    unchanged = sum(1 for p in v1 if by2.get(p["code"]) == p)
    only1, only2 = set(by1) - set(by2), set(by2) - set(by1)
    print(f"\n  CHECK    v1 {len(v1)} - discontinued {count['discontinued']} - old-size barcodes {count['new_size']}"
          f" + new-size barcodes {count['new_size']} + new {count['new']} = "
          f"{len(v1) - count['discontinued'] + count['new']}, v2 rows = {len(v2)}")
    print(f"  CHECK    renamed {count['renamed']} + new size {count['new_size']} + discontinued {count['discontinued']}"
          f" + unchanged {unchanged} = {count['renamed'] + count['new_size'] + count['discontinued'] + unchanged},"
          f" v1 rows = {len(v1)}")
    print(f"  CHECK    barcodes only in v1 = {len(only1)} (old sizes {count['new_size']} + discontinued "
          f"{count['discontinued']}), barcodes only in v2 = {len(only2)} (new sizes {count['new_size']} + new {count['new']})")
    made = [c["v2_code"] for c in changes if c["change"] == "new_size"]
    print(f"  CHECK    the simulated barcodes have a valid GS1 check digit: "
          f"{all(check_digit(code[:-1]) == code[-1] for code in made)}")
    real13 = [p["code"] for p in v1 if len(p["code"]) == 13]
    print(f"  CHECK    the same check, on the real 13-digit barcodes in v1: "
          f"{sum(1 for code in real13 if check_digit(code[:-1]) == code[-1])} of {len(real13)} valid")
    print(f"  CHECK    catalog_raw.jsonl is untouched: "
          f"{hashlib.sha256(catalog.RAW.read_bytes()).hexdigest() == json.loads(catalog.META.read_text(encoding='utf-8'))['sha256']}")


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    v1 = catalog.load()
    if "--rebuild" in sys.argv and V2.exists():
        # the rules again, over v1, with the new product already saved: no network
        print("Rebuilding catalog v2 from the saved new product ...")
        new_code = next(c["v2_code"] for c in load_changes() if c["change"] == "new")
        new_product = next(p for p in load_v2() if p["code"] == new_code)
        info = json.loads(V2_META.read_text(encoding="utf-8"))["new_product_pull"]
        v2, changes = build(v1, new_product, random.Random(SEED))
        save(v2, changes, info)
        print(f"Saved {len(v2)} rows to {V2.relative_to(catalog.ROOT)} and "
              f"{len(changes)} changes to {CHANGES.relative_to(catalog.ROOT)}")
    elif "--refresh" in sys.argv or not V2.exists():
        print("Building catalog v2 ...")
        new_product, info = pull_new_product(v1)
        v2, changes = build(v1, new_product, random.Random(SEED))
        save(v2, changes, info)
        print(f"Saved {len(v2)} rows to {V2.relative_to(catalog.ROOT)} and "
              f"{len(changes)} changes to {CHANGES.relative_to(catalog.ROOT)}")
    else:
        print(f"Reading saved {V2.relative_to(catalog.ROOT)} (use --refresh to build it again)")
    report(v1, load_v2(), load_changes())


if __name__ == "__main__":
    main()
