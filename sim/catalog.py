"""Step 1.1 - pull a real product catalog slice and count how messy it is.

Three US categories in one catalog: sodas, breakfast cereals, snacks.

    python sim/catalog.py            # pull once, then always read the saved snapshot
    python sim/catalog.py --refresh  # pull again and overwrite the snapshot

The snapshot is saved exactly as Open Food Facts returned it. Nothing is cleaned here.
Data: Open Food Facts (https://world.openfoodfacts.org), Open Database Licence (ODbL).
"""
import collections
import datetime
import hashlib
import json
import re
import sys
import time
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[1]
RAW = ROOT / "data" / "catalog_raw.jsonl"
META = ROOT / "data" / "catalog_raw.meta.json"

URL = "https://world.openfoodfacts.org/api/v2/search"
BASE_PARAMS = {
    "countries_tags_en": "united-states",
    "fields": "code,product_name,brands,quantity,categories_tags,countries_tags,"
              "image_front_url,unique_scans_n,last_modified_t",
    "page": 1,
}
# (Open Food Facts category, rows wanted). Order matters: a product listed in two
# categories is kept by the first category that returns it.
CATEGORIES = [("sodas", 20), ("breakfast-cereals", 15), ("snacks", 15)]
SPARE = 10  # extra rows asked for per category, to replace products already taken
HEADERS = {"User-Agent": "shelf-conflation-engine/0.1"}


def pull(category, wanted):
    """One request for one category. The API often answers 503, so retry."""
    params = dict(BASE_PARAMS, categories_tags_en=category, page_size=wanted + SPARE)
    for attempt in range(1, 9):
        r = requests.get(URL, params=params, headers=HEADERS, timeout=90)
        is_json = "json" in r.headers.get("content-type", "")
        if r.status_code == 200 and is_json:
            body = r.json()
            info = {
                "category": category,
                "wanted": wanted,
                "params": params,
                "attempts": attempt,
                "matching_records_reported": body.get("count"),
            }
            return body["products"], info
        print(f"  {category} attempt {attempt}: HTTP {r.status_code}, waiting 15 s")
        time.sleep(15)
    sys.exit(f"Open Food Facts did not answer for {category!r} after 8 attempts. Nothing was written.")


def pull_all():
    """Pull every category, keep each product code once."""
    products, infos, seen = [], [], set()
    for category, wanted in CATEGORIES:
        rows, info = pull(category, wanted)
        kept, already_taken = [], []
        for p in rows:
            code = p.get("code")
            if code in seen:
                already_taken.append(code)
            elif code and len(kept) < wanted:
                kept.append(p)
                seen.add(code)
        info["codes"] = [p["code"] for p in kept]
        info["already_taken_by_earlier_category"] = already_taken
        print(f"  {category:<18} kept {len(kept)} of {wanted} wanted"
              f"  (already taken by an earlier category: {len(already_taken)})")
        products += kept
        infos.append(info)
        time.sleep(6)  # stay under the API's search rate limit
    meta = {
        "source": "Open Food Facts search API v2",
        "licence": "ODbL",
        "url": URL,
        "pulled_at_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
        "rows": len(products),
        "categories": infos,
    }
    return products, meta


def save(products, meta):
    RAW.parent.mkdir(parents=True, exist_ok=True)
    lines = [json.dumps(p, ensure_ascii=False) for p in products]
    # newline="\n": same bytes on Windows, Mac and Linux, so the checksum holds everywhere
    RAW.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    meta["sha256"] = hashlib.sha256(RAW.read_bytes()).hexdigest()
    META.write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8", newline="\n")


def load():
    with RAW.open(encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def size_shape(quantity):
    """'12 FL OZ (355 mL)' -> 'N FL OZ (N mL)'. Every number becomes N, nothing else changes."""
    return re.sub(r"\d+(?:[.,]\d+)?", "N", quantity)


def mess_report(products, meta=None):
    n = len(products)
    print(f"\nCATALOG MESS COUNT - {n} rows")

    # 0. where the rows came from
    if meta:
        print("\n0. Rows by category")
        for info in meta["categories"]:
            print(f"   {info['category']:<18} {len(info['codes']):>3} rows"
                  f"   ({info['matching_records_reported']} US records exist)")
            for code in info["already_taken_by_earlier_category"]:
                print(f"      {code} also listed here, kept under an earlier category")
    us_only = sum(1 for p in products if p.get("countries_tags") == ["en:united-states"])
    print(f"   sold only in the US: {us_only}   also sold elsewhere: {n - us_only}")

    # 1. product codes
    lengths = collections.Counter(len(p.get("code") or "") for p in products)
    non_digit = sum(1 for p in products if not (p.get("code") or "").isdigit())
    print("\n1. Product codes")
    for length, count in sorted(lengths.items()):
        print(f"   {length:>2} digits: {count}")
    print(f"   not all digits: {non_digit}")

    # 2. missing fields
    print("\n2. Missing or empty fields")
    for field in ("product_name", "brands", "quantity"):
        missing = sum(1 for p in products if not (p.get(field) or "").strip())
        print(f"   {field:<13} {missing}")

    # 3. size formats
    sizes = [p.get("quantity") or "" for p in products]
    shapes = collections.Counter(size_shape(s) for s in sizes if s.strip())
    shapes_no_case = {shape.casefold() for shape in shapes}
    print("\n3. Ways a size is written")
    print(f"   distinct shapes: {len(shapes)}  (still {len(shapes_no_case)} after ignoring upper/lower case)")
    for shape, count in shapes.most_common():
        example = next(s for s in sizes if s.strip() and size_shape(s) == shape)
        print(f"   {count:>3} x  {shape:<28} e.g. {example}")

    # 4. brands
    brands = [(p.get("brands") or "").strip() for p in products]
    spellings = collections.defaultdict(set)
    for b in brands:
        if b:
            spellings[b.casefold()].add(b)
    multi = sum(1 for b in brands if "," in b)
    print("\n4. Brands")
    print(f"   distinct spellings: {len({b for b in brands if b})}")
    print(f"   rows with more than one brand in the field (comma): {multi}")
    for variants in spellings.values():
        if len(variants) > 1:
            print(f"   same brand, different case: {sorted(variants)}")

    # 5. repeated names
    by_name = collections.defaultdict(list)
    for p in products:
        name = (p.get("product_name") or "").strip()
        if name:
            by_name[name.casefold()].append(p)
    repeated = {k: v for k, v in by_name.items() if len(v) > 1}
    print("\n5. Same product name on more than one row")
    print(f"   names repeated: {len(repeated)}  covering {sum(len(v) for v in repeated.values())} rows")
    for rows in repeated.values():
        print(f"   {rows[0].get('product_name')!r}")
        for p in rows:
            print(f"      {p.get('code'):<14} brand={p.get('brands')!r}  size={p.get('quantity')!r}")


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    if "--refresh" in sys.argv or not RAW.exists():
        print("Pulling from Open Food Facts ...")
        products, meta = pull_all()
        save(products, meta)
        print(f"Saved {meta['rows']} rows to {RAW.relative_to(ROOT)}")
    else:
        print(f"Reading saved snapshot {RAW.relative_to(ROOT)} (use --refresh to pull again)")
    mess_report(load(), json.loads(META.read_text(encoding="utf-8")))


if __name__ == "__main__":
    main()
