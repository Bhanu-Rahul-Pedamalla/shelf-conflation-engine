"""Step 1.3 - badge walks past the real shelf. Three walks per store, on three days.

    python sim/walk.py           # all walks: one line each, totals, re-scans
    python sim/walk.py --legs    # the same, plus every walk aisle by aisle

Reads   data/shelf_truth_dayN.csv   what is really on the shelf on each day
Writes  data/detections.jsonl       what the badge reports. This is the pipeline's input.
        data/detections_truth.csv   the right answer for each detection.
                                    The pipeline never reads this file.

Every number in the ASSUMED blocks is a guess. None is measured from a real badge or store.
The label text is built from the catalog row and then damaged. Real labels differ more.
"""
import csv
import datetime
import json
import random
import re
import statistics
import sys

import catalog    # sim/catalog.py
import planogram  # sim/planogram.py

DETECTIONS = catalog.ROOT / "data" / "detections.jsonl"
DET_TRUTH = catalog.ROOT / "data" / "detections_truth.csv"

# ---- ASSUMED: who walks, when, and how fast. Every store gets every walk in this list.
# ---- Each walk in a store is a different associate with their own badge.
# ---- Each walk goes past the shelf as it was on that day (shelf_truth_day1/2/3.csv),
# ---- so later walks re-scan the same facings and also see what was restocked or ran out.
DAY_ONE = datetime.date(2026, 10, 2)
WALKS = [
    # walk,  day, start (UTC), speed in metres per second
    ("W001", 0, "09:00", 1.0),
    ("W002", 1, "14:30", 0.9),
    ("W003", 2, "11:15", 1.1),
]

# ---- ASSUMED: store geometry, in metres. x runs along the aisle, y across the
# ---- store from aisle to aisle, z up from the floor.
AISLE_X0 = 2.0          # every aisle starts 2 m from the store's front wall
AISLE_Y0 = 1.0          # the first aisle's shelf face is 1 m from the side wall
AISLE_PITCH = 3.0       # distance from one aisle's shelf face to the next
FACING_WIDTH = 0.20     # every facing is 20 cm wide
SHELF_Z = {1: 0.30, 2: 0.70, 3: 1.10, 4: 1.50}   # height of the products on each shelf
POSITION_JITTER = 0.03  # camera's 3D position error, standard deviation per axis

# ---- ASSUMED: the camera.
FRAME_HZ = 5            # frames per second, so one frame every 20 cm at 1 m/s
CAMERA_Z = 1.30         # badge height on the chest
IMAGE_W, IMAGE_H = 3840, 2160
PX_PER_M = 800          # flat projection, no lens distortion
BOX_W, BOX_H = 0.20, 0.30   # size of one product face, metres

# ---- ASSUMED: how often the camera damages the text it reads, and reads a barcode.
P_UNIT = 0.30           # a unit is written another way: "oz" -> "OZ", "ounces"
P_TRUNCATE = 0.30       # the text is cut short
P_DROP = 0.35           # 1 to 3 characters are lost
P_CASE = 0.40           # everything becomes upper or lower case
P_BARCODE = 0.15        # the barcode is visible and read correctly

UNIT_RE = re.compile(r"(?<![a-z])(fl\.?\s?oz|ounces|oz|ml|kg|grams|g|lbs?|liters?)(?![a-z])", re.I)
UNIT_SPELLINGS = {
    "floz": ["fl oz", "FL OZ", "fl. oz", "floz"],
    "oz": ["oz", "OZ", "ounces", "oz."],
    "ml": ["ml", "mL", "ML"],
    "g": ["g", "G", "grams"],
    "kg": ["kg", "KG"],
    "lb": ["lb", "lbs", "LB"],
    "l": ["liter", "L", "litre"],
}
UNIT_KEY = {"ounces": "oz", "grams": "g", "lbs": "lb", "liter": "l", "liters": "l"}


def label_text(p):
    """What a clean camera would read: brand, name, size, as the catalog writes them."""
    brand = (p.get("brands") or "").strip()
    name = (p.get("product_name") or "").strip()
    size = (p.get("quantity") or "").strip()
    parts = [name] if brand.casefold() in name.casefold() else [brand, name]
    return " ".join(part for part in parts + [size] if part)


def damage(text, rng):
    """Apply up to four kinds of damage. Returns the new text and which kinds were applied."""
    kinds = []

    if rng.random() < P_UNIT:
        def respell(match):
            key = re.sub(r"[.\s]", "", match.group(0).lower())
            key = UNIT_KEY.get(key, key)
            return rng.choice([s for s in UNIT_SPELLINGS[key] if s != match.group(0)])
        new = UNIT_RE.sub(respell, text)
        if new != text:
            text = new
            kinds.append("unit")

    if rng.random() < P_TRUNCATE and len(text) > 8:
        keep = max(6, int(len(text) * rng.uniform(0.4, 0.8)))
        text = text[:keep].rstrip()
        kinds.append("truncate")

    if rng.random() < P_DROP:
        for _ in range(rng.randint(1, 3)):
            i = rng.choice([i for i, c in enumerate(text) if c != " "])
            text = text[:i] + text[i + 1:]
        kinds.append("drop")

    if rng.random() < P_CASE:
        new = text.upper() if rng.random() < 0.5 else text.lower()
        if new != text:
            text = new
            kinds.append("case")

    return text, kinds


def read_shelf(day):
    """The real shelf on one day, with each facing's distance along its shelf added."""
    with planogram.truth_path(day).open(encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    for row in rows:
        for field in ("shelf", "position", "facing"):
            row[field] = int(row[field])
    rows.sort(key=lambda r: (r["store_id"], r["aisle"], r["shelf"], r["position"], r["facing"]))
    slot = {}
    for row in rows:
        shelf = (row["store_id"], row["aisle"], row["shelf"])
        i = slot.get(shelf, 0)
        row["along"] = (i + 0.5) * FACING_WIDTH   # centre of the facing, metres from shelf start
        slot[shelf] = i + 1
    return rows


def build_walk(shelf_rows, products, rng, store, badge, walk, start, speed):
    """Walk one store's aisles in a serpentine: A01 forward, A02 back, A03 forward."""
    by_code = {p["code"]: p for p in products}
    seen, legs = [], []
    clock, prev_end_x = 0.0, None

    for n, aisle in enumerate(sorted({r["aisle"] for r in shelf_rows})):
        rows = [r for r in shelf_rows if r["aisle"] == aisle]
        length = max(r["along"] for r in rows) + FACING_WIDTH / 2
        forward = n % 2 == 0
        start_x = AISLE_X0 if forward else AISLE_X0 + length
        end_x = AISLE_X0 + length if forward else AISLE_X0
        if prev_end_x is not None:   # walk around the end of the aisle to the next one
            clock += (abs(prev_end_x - start_x) + AISLE_PITCH) / speed
        aisle_start = clock

        stocked = [r for r in rows if r["actual_code"]]   # an empty facing gives no detection
        for row in stocked:
            walked = row["along"] if forward else length - row["along"]
            frame = round(walked / speed * FRAME_HZ)   # the frame taken closest to this facing
            seen.append({
                "row": row,
                "t": aisle_start + frame / FRAME_HZ,
                "frame": frame,
                "camera_x": start_x + (1 if forward else -1) * speed * frame / FRAME_HZ,
                "true_xyz": (AISLE_X0 + row["along"], AISLE_Y0 + n * AISLE_PITCH, SHELF_Z[row["shelf"]]),
            })

        clock = aisle_start + length / speed
        prev_end_x = end_x
        legs.append({"aisle": aisle, "direction": "forward" if forward else "back",
                     "length_m": length, "start_s": aisle_start, "end_s": clock,
                     "facings": len(rows), "detections": len(stocked)})

    seen.sort(key=lambda s: (s["t"], s["row"]["aisle"], -s["row"]["shelf"], s["row"]["along"]))

    detections, truth = [], []
    for i, s in enumerate(seen, start=1):
        row = s["row"]
        x, y, z = (round(v + rng.gauss(0, POSITION_JITTER), 3) for v in s["true_xyz"])
        box_w, box_h = BOX_W * PX_PER_M, BOX_H * PX_PER_M
        centre_x = IMAGE_W / 2 + (x - s["camera_x"]) * PX_PER_M
        centre_y = IMAGE_H / 2 - (z - CAMERA_Z) * PX_PER_M
        clean = label_text(by_code[row["actual_code"]])
        text, kinds = damage(clean, rng)
        barcode = row["actual_code"] if rng.random() < P_BARCODE else None
        confidence = max(0.30, round(0.95 - 0.12 * len(kinds) - rng.uniform(0, 0.05), 2))
        ts = start + datetime.timedelta(seconds=s["t"])
        detection_id = f"D-{store}-{walk}-{i:05d}"

        detections.append({
            "detection_id": detection_id,
            "badge_id": badge,
            "store_id": store,
            "walk_id": walk,
            "ts": ts.isoformat(timespec="milliseconds").replace("+00:00", "Z"),
            "aisle": row["aisle"],
            "shelf": row["shelf"],
            "position_x": round(x - AISLE_X0, 3),   # metres along the shelf
            "x": x, "y": y, "z": z,                 # metres, store coordinates
            "bbox": [round(centre_x - box_w / 2), round(centre_y - box_h / 2), round(box_w), round(box_h)],
            "ocr_text": text,
            "read_barcode": barcode,
            "confidence": confidence,
            "image_ref": f"frames/{store}/{badge}/{walk}/{row['aisle']}-{s['frame']:04d}.jpg",
        })
        truth.append({
            "detection_id": detection_id,
            "store_id": store,
            "walk_id": walk,
            "true_code": row["actual_code"],
            "aisle": row["aisle"],
            "shelf": row["shelf"],
            "position": row["position"],
            "facing": row["facing"],
            "shelf_status": row["status"],
            "true_x": round(s["true_xyz"][0], 3),
            "true_y": round(s["true_xyz"][1], 3),
            "true_z": round(s["true_xyz"][2], 3),
            "damage": "+".join(kinds) or "none",
            "clean_text": clean,
        })
    return detections, truth, legs


def report_walk(store, badge, walk, shelf_rows, detections, legs):
    """One walk in detail, aisle by aisle. Printed with: python sim/walk.py --legs"""
    print(f"\nSTORE {store}  badge {badge}  walk {walk}")
    for leg in legs:
        print(f"  {leg['aisle']}  {leg['direction']:<7}  {leg['length_m']:.1f} m   "
              f"clock {leg['start_s']:5.1f} s -> {leg['end_s']:5.1f} s   "
              f"{leg['facings']} facings, {leg['detections']} detections")
    print(f"  first detection {detections[0]['ts']}   last {detections[-1]['ts']}")
    empty = sum(1 for r in shelf_rows if not r["actual_code"])
    print(f"  SHELF    {len(shelf_rows)} facings: {len(shelf_rows) - empty} stocked, {empty} empty")
    print(f"  BADGE    {len(detections)} detections in {len({d['image_ref'] for d in detections})} frames")
    print(f"  CHECK    stocked facings = {len(shelf_rows) - empty}, detections = {len(detections)}")


def facing_of(t):
    """Which physical facing a truth row belongs to."""
    return (t["store_id"], t["aisle"], t["shelf"], t["position"], t["facing"])


def report_walks(shelves, detections, walks):
    """One line per walk: who, when, how fast, and that day's stocked facings against detections."""
    print("\nTHE WALKS")
    print("  store  walk  badge  start (UTC)       shelf  speed    duration  frames  CHECK stocked = detections")
    for (store, walk), info in walks.items():
        mine = [d for d in detections if d["store_id"] == store and d["walk_id"] == walk]
        stocked = sum(1 for r in shelves[info["day"]] if r["store_id"] == store and r["actual_code"])
        print(f"  {store}   {walk}  {info['badge']}   {info['start']:%Y-%m-%d %H:%M}  day {info['day']}"
              f"  {info['speed']:.1f} m/s  {info['legs'][-1]['end_s']:5.1f} s   "
              f"{len({d['image_ref'] for d in mine}):>5}   {stocked} = {len(mine)}")


def print_reads(facing, reads, label):
    print(f"  {facing[0]} {facing[1]} shelf {facing[2]} pos {facing[3]} facing {facing[4]}   {label}")
    for d, _ in reads:
        print(f"      {d['walk_id']} {d['ts'][:16]}  conf {d['confidence']:.2f}"
              f"  barcode {d['read_barcode'] or '-':<14} read: {d['ocr_text']}")


def report(shelves, detections, truth, walks):
    """All stores and walks together: totals, re-scans, damage, barcodes, confidence, samples."""
    stores = sorted({d["store_id"] for d in detections})
    print(f"\nALL STORES  {len(stores)} stores, {len(WALKS)} walks each, "
          f"{len({d['badge_id'] for d in detections})} badges")
    for day, rows in shelves.items():
        empty = sum(1 for r in rows if not r["actual_code"])
        print(f"  SHELF    day {day}: {len(rows)} facings, {len(rows) - empty} stocked, {empty} empty")
    expected = sum(1 for (store, _), info in walks.items()
                   for r in shelves[info["day"]] if r["store_id"] == store and r["actual_code"])
    print(f"  BADGE    {len(detections)} detections in {len({d['image_ref'] for d in detections})} frames")
    print(f"  CHECK    stocked facings on each walk's day, summed = {expected}, detections = {len(detections)}")
    print(f"  CHECK    detection ids are unique: "
          f"{len({d['detection_id'] for d in detections}) == len(detections)}")

    # Re-scans: a facing is seen once by every walk on a day when it was stocked.
    reads = {}
    for d, t in zip(detections, truth):
        reads.setdefault(facing_of(t), []).append((d, t))
    stocked_days = {}
    for (store, _), info in walks.items():
        for r in shelves[info["day"]]:
            if r["store_id"] == store and r["actual_code"]:
                stocked_days[facing_of(r)] = stocked_days.get(facing_of(r), 0) + 1
    print(f"\nRE-SCANS  {len(reads)} facings scanned at least once")
    for k in range(1, len(WALKS) + 1):
        print(f"  scanned {k} time{'s' if k > 1 else ' '}: {sum(1 for v in reads.values() if len(v) == k)}")
    print(f"  CHECK    every facing is scanned once for each walk that found it stocked: "
          f"{ {f: len(v) for f, v in reads.items()} == stocked_days}")
    for k in range(1, len(WALKS) + 1):
        n = sum(1 for v in reads.values() if len({d['ocr_text'] for d, _ in v}) == k)
        print(f"  facings read as {k} different text{'s' if k > 1 else ''}: {n}")
    with_barcode = sum(1 for v in reads.values() if any(d["read_barcode"] for d, _ in v))
    print(f"  facings with a barcode in at least one walk: {with_barcode} of {len(reads)}")

    print("\nONE FACING, EVERY READ - first facing in each store scanned by every walk, all reads different")
    for store in stores:
        for facing, v in reads.items():
            if facing[0] == store and len(v) == len(WALKS) and len({d["ocr_text"] for d, _ in v}) == len(v):
                print_reads(facing, v, f"label: {v[0][1]['clean_text']}")
                break

    print("\nA CHANGED SHELF, AS THE BADGE SAW IT - first restocked and first newly empty facing per store")
    change = {facing_of(r): r["change"] for rows in shelves.values() for r in rows if r["change"]}
    for store in stores:
        for wanted, note in (("restocked", "empty on day 1, restocked"), ("new_out_of_stock", "stocked on day 1, then empty")):
            for facing, v in reads.items():
                if facing[0] == store and change.get(facing) == wanted:
                    print_reads(facing, v, f"{note}   label: {v[0][1]['clean_text']}")
                    break

    print("\nDAMAGE TO THE TEXT")
    for kind in ("unit", "truncate", "drop", "case"):
        print(f"  {kind:<9} {sum(1 for t in truth if kind in t['damage'].split('+')):>3}")
    per_detection = [0 if t["damage"] == "none" else len(t["damage"].split("+")) for t in truth]
    for k in sorted(set(per_detection)):
        print(f"  {k} kinds of damage: {per_detection.count(k)} detections")
    changed = sum(1 for d, t in zip(detections, truth) if d["ocr_text"] != t["clean_text"])
    print(f"  text differs from the clean label: {changed} of {len(detections)}")

    barcodes = sum(1 for d in detections if d["read_barcode"])
    conf = [d["confidence"] for d in detections]
    print(f"\nBARCODE READ   {barcodes} of {len(detections)}")
    print(f"CONFIDENCE     min {min(conf):.2f}   median {statistics.median(conf):.2f}   max {max(conf):.2f}")

    print("\nSAMPLE - clean label, then what the badge read")
    step = max(1, len(detections) // 10)
    for d, t in list(zip(detections, truth))[::step][:10]:
        print(f"  {d['detection_id']}  {d['aisle']} shelf {d['shelf']}  x={d['x']:.3f} y={d['y']:.3f} z={d['z']:.3f}"
              f"  conf {d['confidence']:.2f}  barcode {d['read_barcode'] or '-'}")
        print(f"      label: {t['clean_text']}")
        print(f"      read : {d['ocr_text']}")


def read_shelves():
    """The real shelf on every day: {1: rows, 2: rows, 3: rows}."""
    return {day: read_shelf(day) for day in range(1, planogram.DAYS + 1)}


def build_all(shelves, products):
    """Every walk in every store, each past the shelf as it was on the walk's day.

    Returns all detections, all truth rows, and each walk's details.
    """
    detections, truth, walks = [], [], {}
    for s, (store, store_seed) in enumerate(planogram.STORES.items()):
        for w, (walk, day, clock_time, speed) in enumerate(WALKS):
            rows = [r for r in shelves[day + 1] if r["store_id"] == store]
            badge = f"B{s * len(WALKS) + w + 1:03d}"   # S001: B001-B003, S002: B004-B006, ...
            seed = store_seed + 100 * w                # walk 1 keeps the store's seed: 1, 101, 201
            hour, minute = map(int, clock_time.split(":"))
            start = datetime.datetime.combine(DAY_ONE + datetime.timedelta(days=day),
                                              datetime.time(hour, minute), datetime.timezone.utc)
            d, t, legs = build_walk(rows, products, random.Random(seed), store, badge, walk, start, speed)
            detections += d
            truth += t
            walks[(store, walk)] = {"badge": badge, "seed": seed, "start": start, "speed": speed,
                                    "day": day + 1, "legs": legs}
    return detections, truth, walks


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    shelves = read_shelves()
    detections, truth, walks = build_all(shelves, catalog.load())
    lines = [json.dumps(d, ensure_ascii=False) for d in detections]
    DETECTIONS.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    planogram.write_csv(DET_TRUTH, truth)
    print(f"Wrote {DETECTIONS.relative_to(catalog.ROOT)} ({len(detections)} rows) "
          f"and {DET_TRUTH.relative_to(catalog.ROOT)} ({len(truth)} rows), "
          f"{len(planogram.STORES)} stores, {len(walks)} walks")
    report_walks(shelves, detections, walks)
    if "--legs" in sys.argv:   # every walk aisle by aisle
        for (store, walk), info in walks.items():
            report_walk(store, info["badge"], walk,
                        [r for r in shelves[info["day"]] if r["store_id"] == store],
                        [d for d in detections if d["store_id"] == store and d["walk_id"] == walk],
                        info["legs"])
    report(shelves, detections, truth, walks)


if __name__ == "__main__":
    main()
