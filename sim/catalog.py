"""Step 1.1 - pull a real product catalog slice and count how messy it is.

Seven US product groups in one catalog, about 300 products: sodas, breakfast cereals,
snacks, dairy, supplements, personal care, household.

    python sim/catalog.py            # pull once, then always read the saved snapshot
    python sim/catalog.py --refresh  # pull again and overwrite the snapshot

Every row is saved exactly as the source returned it. Nothing is cleaned here.
A row with no name or no brand is skipped, and counted.

Data, all under the Open Database Licence (ODbL), contents under the Database Contents Licence:
  Open Food Facts      https://world.openfoodfacts.org      food, dairy, supplements
  Open Beauty Facts    https://world.openbeautyfacts.org    personal care
  Open Products Facts  https://world.openproductsfacts.org  household
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

SITES = {
    "openfoodfacts": "https://world.openfoodfacts.org/api/v2/search",
    "openbeautyfacts": "https://world.openbeautyfacts.org/api/v2/search",
    "openproductsfacts": "https://world.openproductsfacts.org/api/v2/search",
}
BASE_PARAMS = {
    "countries_tags_en": "united-states",
    "fields": "code,product_name,brands,quantity,categories_tags,countries_tags,"
              "image_front_url,unique_scans_n,last_modified_t",
    "page_size": 100,
}
# (group, rows wanted, [(site, category), ...]). A group becomes one aisle in the store.
# Order matters: a product listed in two groups is kept by the first group that returns it.
# A group with two sources takes about half its rows from each.
GROUPS = [
    ("sodas", 60, [("openfoodfacts", "sodas")]),
    ("breakfast-cereals", 50, [("openfoodfacts", "breakfast-cereals")]),
    ("snacks", 50, [("openfoodfacts", "snacks")]),
    ("dairy", 60, [("openfoodfacts", "dairies")]),
    ("supplements", 30, [("openfoodfacts", "dietary-supplements")]),
    ("personal-care", 25, [("openbeautyfacts", "toothpastes"), ("openbeautyfacts", "shampoos")]),
    ("household", 25, [("openproductsfacts", "household-supplies")]),
]
MAX_PAGES = 4   # pages of 100 rows asked from one source before giving up on filling its share
HEADERS = {"User-Agent": "shelf-conflation-engine/0.1"}


def pull(site, category, page=1):
    """One request: one page of one category from one site. The API often answers 503, so retry."""
    params = dict(BASE_PARAMS, categories_tags_en=category, page=page)
    for attempt in range(1, 9):
        r = requests.get(SITES[site], params=params, headers=HEADERS, timeout=90)
        is_json = "json" in r.headers.get("content-type", "")
        if r.status_code == 200 and is_json:
            body = r.json()
            time.sleep(7)  # stay under the API's search rate limit
            return body["products"], body.get("count")
        print(f"  {site} {category} page {page} attempt {attempt}: HTTP {r.status_code}, waiting 15 s")
        time.sleep(15)
    sys.exit(f"{site} did not answer for {category!r} after 8 attempts. Nothing was written.")


def usable(p):
    """Rule: a product needs a code, a name and a brand. Anything else is skipped."""
    return bool(p.get("code") and (p.get("product_name") or "").strip() and (p.get("brands") or "").strip())


def pull_all():
    """Pull every group, keep each product code once, skip rows with no name or no brand."""
    products, infos, seen = [], [], set()
    for group, wanted, sources in GROUPS:
        kept, already_taken, skipped, source_infos = [], [], [], []
        for n, (site, category) in enumerate(sources):
            share = -(-(wanted - len(kept)) // (len(sources) - n))   # what is left, split over the sources left
            mine, reported = [], None
            for page in range(1, MAX_PAGES + 1):
                rows, reported = pull(site, category, page)
                for p in rows:
                    code = p.get("code")
                    if code in seen:
                        already_taken.append(code)
                    elif not usable(p):
                        skipped.append(code)
                    elif len(mine) < share:
                        mine.append(p)
                        seen.add(code)
                if len(mine) >= share or len(rows) < BASE_PARAMS["page_size"]:
                    break
            kept += mine
            source_infos.append({"site": site, "category": category, "url": SITES[site],
                                 "matching_records_reported": reported, "pages": page,
                                 "codes": [p["code"] for p in mine]})
        print(f"  {group:<18} kept {len(kept)} of {wanted} wanted   skipped, no name or brand: {len(skipped)}"
              f"   already taken by an earlier group: {len(already_taken)}")
        products += kept
        infos.append({
            "category": group,
            "wanted": wanted,
            "sources": source_infos,
            "matching_records_reported": sum(s["matching_records_reported"] or 0 for s in source_infos),
            "codes": [p["code"] for p in kept],
            "already_taken_by_earlier_category": already_taken,
            "skipped_no_name_or_brand": skipped,
        })
    meta = {
        "source": "Open Food Facts, Open Beauty Facts and Open Products Facts, search API v2",
        "licence": "ODbL (database), DbCL (contents)",
        "attribution": "Contains data from Open Food Facts, Open Beauty Facts and Open Products Facts, "
                       "available under the Open Database License.",
        "params": BASE_PARAMS,
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


def mess_report(products, meta=None, full=False):
    """full=False prints the 15 commonest size shapes and the first 10 repeated names."""
    n = len(products)
    print(f"\nCATALOG MESS COUNT - {n} rows")

    # 0. where the rows came from
    if meta:
        print("\n0. Rows by group")
        for info in meta["categories"]:
            print(f"   {info['category']:<18} {len(info['codes']):>3} of {info['wanted']} wanted"
                  f"   skipped, no name or brand: {len(info['skipped_no_name_or_brand'])}"
                  f"   also in an earlier group: {len(info['already_taken_by_earlier_category'])}")
            for s in info["sources"]:
                print(f"      {len(s['codes']):>3} from {s['site']} / {s['category']}"
                      f"   ({s['matching_records_reported']} US records exist)")
        wanted = sum(info["wanted"] for info in meta["categories"])
        kept = sum(len(info["codes"]) for info in meta["categories"])
        print(f"   CHECK    rows kept, summed over the groups = {kept}, rows in the file = {n}, wanted = {wanted}")
        print(f"   CHECK    every row has a code, a name and a brand: {all(usable(p) for p in products)}")
        print(f"   CHECK    every code appears once: {len({p['code'] for p in products}) == n}")
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
    shown = shapes.most_common() if full else shapes.most_common(15)
    for shape, count in shown:
        example = next(s for s in sizes if s.strip() and size_shape(s) == shape)
        print(f"   {count:>3} x  {shape:<28} e.g. {example}")
    if len(shown) < len(shapes):
        print(f"   ... and {len(shapes) - len(shown)} more shapes (python sim/catalog.py --full)")

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
    shown = list(repeated.values()) if full else list(repeated.values())[:10]
    for rows in shown:
        print(f"   {rows[0].get('product_name')!r}")
        for p in rows:
            print(f"      {p.get('code'):<14} brand={p.get('brands')!r}  size={p.get('quantity')!r}")
    if len(shown) < len(repeated):
        print(f"   ... and {len(repeated) - len(shown)} more repeated names (python sim/catalog.py --full)")

    # 6. same brand in more than one group
    if meta:
        group_of = {code: info["category"] for info in meta["categories"] for code in info["codes"]}
        groups_of_brand = collections.defaultdict(set)
        for p in products:
            groups_of_brand[(p.get("brands") or "").strip().casefold()].add(group_of[p["code"]])
        shared = {b: g for b, g in groups_of_brand.items() if len(g) > 1}
        print("\n6. Same brand in more than one group")
        print(f"   brands in 2 or more groups: {len(shared)}")
        for brand, groups in sorted(shared.items())[:(None if full else 10)]:
            print(f"   {brand!r}: {sorted(groups)}")


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    if "--refresh" in sys.argv or not RAW.exists():
        print("Pulling from Open Food Facts, Open Beauty Facts and Open Products Facts ...")
        products, meta = pull_all()
        save(products, meta)
        print(f"Saved {meta['rows']} rows to {RAW.relative_to(ROOT)}")
    else:
        print(f"Reading saved snapshot {RAW.relative_to(ROOT)} (use --refresh to pull again)")
    mess_report(load(), json.loads(META.read_text(encoding="utf-8")), full="--full" in sys.argv)


if __name__ == "__main__":
    main()
