"""Badge walks past the real shelf. Three walks per store, on three days.

    python sim/walk.py           # all walks: one line each, totals, re-scans
    python sim/walk.py --legs    # the same, plus every walk aisle by aisle

Reads   data/shelf_truth_dayN.csv   what is really on the shelf on each day
Writes  data/detections.jsonl       what the badge reports. This is the pipeline's input.
        data/detections_truth.csv   the right answer for each detection.
                                    The pipeline never reads this file.
        data/malformed_truth.csv    which lines of the reads file arrived damaged, and how.
                                    The pipeline never reads this file either.

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
import drift      # sim/drift.py
import planogram  # sim/planogram.py

DETECTIONS = catalog.ROOT / "data" / "detections.jsonl"
DET_TRUTH = catalog.ROOT / "data" / "detections_truth.csv"
MALFORMED_TRUTH = catalog.ROOT / "data" / "malformed_truth.csv"
WALKS_FILE = catalog.ROOT / "data" / "walks.json"   # each walk's schedule and path, and the assumed numbers

# ---- ASSUMED: who walks, when, and how fast. Every store gets every walk in this list.
# ---- Each walk in a store is a different associate with their own badge.
# ---- Each walk goes past the shelf as it was on that day (shelf_truth_day1/2/3.csv),
# ---- so later walks re-scan the same facings and also see what was restocked or ran out.
DAY_ONE = drift.DAY_ONE     # the walks start on the catalog's day one
WALKS = [
    # walk,  day, start (UTC), speed in metres per second
    ("W001", 0, "09:00", 1.0),
    ("W002", 1, "14:30", 0.9),
    ("W003", 2, "11:15", 1.1),
]

# ---- ASSUMED: three ways the same facing reaches the pipeline more than once.
P_DOUBLE = 0.05            # a detection is reported twice in the same frame: second box, new id
DOUBLE_BOX_SHIFT = 12      # the second box sits up to this many pixels away from the first
SECOND_BADGE_WALK = "W004" # a second associate walks one aisle of each store on day 1 ...
SECOND_BADGE_GAP_S = 120   # ... starting this many seconds after walk W001
P_RESEND_FRAME = 0.03      # a frame's detections are sent again, byte for byte, same ids
DUPLICATE_SEED = 5000      # a store's duplicate seed = its seed + 5000, so nothing above is disturbed

# ---- ASSUMED: about one row in a hundred reaches the pipeline damaged on the way: the line cut short,
# ---- a field missing, a value of the wrong type, the timestamp written another way, a value outside
# ---- its range, a read with no text and no barcode, or an id already used, carrying different content.
# ---- A damaged row is an extra row placed right after the clean row it was copied from. No clean row changes.
P_MALFORMED = 0.01
MALFORMED_KINDS = ("cut_line", "missing_field", "wrong_type", "bad_timestamp",
                   "out_of_range", "empty_read", "id_conflict")
MALFORMED_SEED = 7000      # a store's damage seed = its seed + 7000, so nothing above is disturbed

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


def label_sources():
    """Which catalog the labels on the shelf come from, per day: {day: (products, {code: version})}.

    Before catalog v2 takes effect every label is a v1 label. From that day on a label is
    the v2 one, except for a product that v2 no longer lists: its leftover stock keeps the v1 label.
    """
    v1, v2 = catalog.load(), drift.load_v2()
    in_v2 = {p["code"] for p in v2}
    later = v2 + [p for p in v1 if p["code"] not in in_v2]
    early_version = {p["code"]: "v1" for p in v1}
    later_version = {p["code"]: "v2" if p["code"] in in_v2 else "v1" for p in later}
    return {day: (v1, early_version) if day < drift.EFFECTIVE_DAY else (later, later_version)
            for day in range(1, planogram.DAYS + 1)}


def build_walk(shelf_rows, products, rng, store, badge, walk, start, speed, extra="", versions=None):
    """Walk the aisles in shelf_rows in a serpentine: first forward, next back, and so on."""
    by_code = {p["code"]: p for p in products}
    all_aisles = sorted(planogram.AISLES.values())
    seen, legs = [], []
    clock, prev_end_x = 0.0, None

    for n, aisle in enumerate(sorted({r["aisle"] for r in shelf_rows})):
        rows = [r for r in shelf_rows if r["aisle"] == aisle]
        aisle_y = AISLE_Y0 + all_aisles.index(aisle) * AISLE_PITCH   # where this aisle is in the store
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
                "true_xyz": (AISLE_X0 + row["along"], aisle_y, SHELF_Z[row["shelf"]]),
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
            "label_catalog": versions[row["actual_code"]] if versions else "v1",   # catalog the label came from
            "extra": extra,         # "", "second_badge" or "double_in_frame"
            "duplicate_of": "",     # for a double: the id of the first box
            "times_sent": 1,        # 2 when the frame was re-sent
        })
    return detections, truth, legs


def add_doubles(detections, truth, rng):
    """Some detections are reported twice in the same frame: a second, overlapping box.

    The second box has a new id, numbered after the walk's last detection, and sits
    right after the first in the file. Its text is read again, so it can differ.
    """
    out_d, out_t = [], []
    prefix = detections[0]["detection_id"].rsplit("-", 1)[0]
    number = len(detections)
    for d, t in zip(detections, truth):
        out_d.append(d)
        out_t.append(t)
        if rng.random() < P_DOUBLE:
            number += 1
            x, y, z = (round(v + rng.gauss(0, POSITION_JITTER), 3) for v in (t["true_x"], t["true_y"], t["true_z"]))
            text, kinds = damage(t["clean_text"], rng)
            out_d.append({
                **d,
                "detection_id": f"{prefix}-{number:05d}",
                "position_x": round(x - AISLE_X0, 3),
                "x": x, "y": y, "z": z,
                "bbox": [d["bbox"][0] + rng.randint(-DOUBLE_BOX_SHIFT, DOUBLE_BOX_SHIFT),
                         d["bbox"][1] + rng.randint(-DOUBLE_BOX_SHIFT, DOUBLE_BOX_SHIFT),
                         d["bbox"][2], d["bbox"][3]],
                "ocr_text": text,
                "read_barcode": t["true_code"] if rng.random() < P_BARCODE else None,
                "confidence": max(0.30, round(0.95 - 0.12 * len(kinds) - rng.uniform(0, 0.05), 2)),
            })
            out_t.append({**t, "detection_id": f"{prefix}-{number:05d}", "damage": "+".join(kinds) or "none",
                          "extra": "double_in_frame", "duplicate_of": d["detection_id"]})
    return out_d, out_t


def add_resends(detections, truth, rng):
    """Some frames are sent twice. The copies arrive after the walk's other rows, unchanged."""
    frames = list(dict.fromkeys(d["image_ref"] for d in detections))
    resent = {frame for frame in frames if rng.random() < P_RESEND_FRAME}
    copies = [dict(d) for d in detections if d["image_ref"] in resent]
    ids = {d["detection_id"] for d in copies}
    for t in truth:
        if t["detection_id"] in ids:
            t["times_sent"] = 2
    return detections + copies


def damage_row(m, kind, rng):
    """One damaged copy of a read: the line that reaches the pipeline, and what was done to it."""
    if kind == "cut_line":                                  # the transmission stopped partway
        line = json.dumps(m, ensure_ascii=False)
        cut = rng.randint(len(line) // 4, len(line) - 3)
        return line[:cut], f"cut at {cut} of {len(line)} characters"
    if kind == "missing_field":
        field = rng.choice(["ts", "store_id", "aisle", "x", "ocr_text", "confidence", "bbox"])
        del m[field]
        what = f"no {field}"
    elif kind == "wrong_type":                              # a number sent as text, or a box with three numbers
        field = rng.choice(["confidence", "shelf", "x", "bbox"])
        m[field] = m[field][:3] if field == "bbox" else str(m[field])
        what = f"{field} is {m[field]!r}"
    elif kind == "bad_timestamp":                           # the same moment, written another way
        ts = datetime.datetime.strptime(m["ts"], "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=datetime.timezone.utc)
        m["ts"] = rng.choice([ts.strftime("%Y-%m-%d %H:%M:%S"), ts.strftime("%d/%m/%Y %H:%M:%S"),
                              str(int(ts.timestamp() * 1000))])
        what = f"ts is {m['ts']!r}"
    elif kind == "out_of_range":
        field = rng.choice(["confidence", "shelf", "z"])
        m[field] = {"confidence": round(1 + rng.uniform(0.05, 0.9), 2), "shelf": rng.choice([0, -1]),
                    "z": -m["z"]}[field]
        what = f"{field} is {m[field]}"
    elif kind == "empty_read":
        m["ocr_text"], m["read_barcode"] = "", None
        m["confidence"] = round(rng.uniform(0.30, 0.45), 2)
        what = "no text and no barcode"
    elif kind == "id_conflict":                             # the same id again, with the text read differently
        text, _ = damage(m["ocr_text"], rng)
        m["ocr_text"] = text if text != m["ocr_text"] else m["ocr_text"].swapcase()
        m["confidence"] = max(0.30, round(m["confidence"] - rng.uniform(0.05, 0.20), 2))
        what = f"same id, text read as {m['ocr_text']!r}"
    return json.dumps(m, ensure_ascii=False), what


def add_malformed(rows, rng):
    """Some rows arrive damaged. Each damaged row is an extra row placed right after the clean row it was
    copied from, with its own id numbered after the walk's last one, so no clean row changes.

    The kinds are taken in turn, so every kind is in the file. Returns the file's items in order: a clean
    row as a dict, a damaged row as (line, note), where line is the text as it will sit in the file.
    """
    last = {}   # walk prefix -> highest id number so far, doubles included
    for d in rows:
        prefix, number = d["detection_id"].rsplit("-", 1)
        last[prefix] = max(last.get(prefix, 0), int(number))
    out, k = [], 0
    for d in rows:
        out.append(d)
        if rng.random() >= P_MALFORMED:
            continue
        kind = MALFORMED_KINDS[k % len(MALFORMED_KINDS)]
        k += 1
        prefix = d["detection_id"].rsplit("-", 1)[0]
        m = dict(d)
        if kind != "id_conflict":                           # a conflict keeps the id it collides with
            last[prefix] += 1
            m["detection_id"] = f"{prefix}-{last[prefix]:05d}"
        line, what = damage_row(m, kind, rng)
        out.append((line, {"store_id": d["store_id"], "walk_id": d["walk_id"], "kind": kind, "what": what,
                           "detection_id": m["detection_id"], "copied_from": d["detection_id"]}))
    return out


def report_malformed(ledger, lines, clean_rows):
    """The damaged rows, counted by kind, with one example of each."""
    print(f"\nDAMAGED ROWS - rows that reach the pipeline unusable, about {P_MALFORMED:.0%} of rows, kinds taken in turn")
    for kind in MALFORMED_KINDS:
        print(f"  {kind:<14} {sum(1 for r in ledger if r['kind'] == kind):>3}")
    print(f"  CHECK    lines in the file = clean rows + damaged rows: {len(lines)} = {clean_rows} + {len(ledger)}: "
          f"{len(lines) == clean_rows + len(ledger)}")
    print("\nONE EXAMPLE OF EACH KIND")
    shown = set()
    for r in ledger:
        if r["kind"] not in shown:
            shown.add(r["kind"])
            print(f"  line {r['line']:>5}  {r['kind']:<14} copied from {r['copied_from']}: {r['what']}")


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


def report_walks(shelves, detections, truth, walks):
    """One line per walk: who, when, how fast, and that day's stocked facings against detections.

    Doubles and re-sent rows are left out of the count here. They are counted under DUPLICATES.
    """
    doubles = {t["detection_id"] for t in truth if t["extra"] == "double_in_frame"}
    print("\nTHE WALKS")
    print("  store  walk  badge  start (UTC)       shelf  aisles  speed    duration  frames  CHECK stocked = detections")
    for (store, walk), info in walks.items():
        mine = {d["detection_id"]: d for d in detections
                if d["store_id"] == store and d["walk_id"] == walk and d["detection_id"] not in doubles}
        stocked = sum(1 for r in shelves[info["day"]] if r["store_id"] == store and r["actual_code"]
                      and info["aisle"] in (None, r["aisle"]))
        print(f"  {store}   {walk}  {info['badge']}   {info['start']:%Y-%m-%d %H:%M}  day {info['day']}"
              f"  {info['aisle'] or 'all':<6}  {info['speed']:.1f} m/s  {info['legs'][-1]['end_s']:5.1f} s   "
              f"{len({d['image_ref'] for d in mine.values()}):>5}   {stocked} = {len(mine)}")


def print_reads(facing, reads, label):
    print(f"  {facing[0]} {facing[1]} shelf {facing[2]} pos {facing[3]} facing {facing[4]}   {label}")
    for d, _ in reads:
        print(f"      {d['walk_id']} {d['ts'][:16]}  conf {d['confidence']:.2f}"
              f"  barcode {d['read_barcode'] or '-':<14} read: {d['ocr_text']}")


def report_duplicates(file_rows, pairs, base, line_of):
    """The three kinds of duplicate, counted, with one example of each per store.

    file_rows are the clean rows; line_of gives each id's line numbers in the file as written,
    damaged rows included, so the lines printed here are the real ones.
    """
    doubles = [(d, t) for d, t in pairs if t["extra"] == "double_in_frame"]
    second = [(d, t) for d, t in pairs if t["extra"] == "second_badge"]
    resent_rows = len(file_rows) - len(pairs)
    resent_frames = {d["image_ref"] for d, t in pairs if t["times_sent"] == 2}
    print("\nDUPLICATES - the same facing reaching the pipeline more than once")
    print(f"  base detections         {len(base):>5}")
    print(f"  twice in one frame     +{len(doubles):>5}   new id, same frame, text read again")
    print(f"  second badge           +{len(second):>5}   new id, another badge, walk {SECOND_BADGE_WALK}, "
          f"{SECOND_BADGE_GAP_S} s after W001")
    print(f"  re-sent rows           +{resent_rows:>5}   {len(resent_frames)} frames, same ids, sent again")
    print(f"  CHECK    {len(base)} + {len(doubles)} + {len(second)} + {resent_rows} = "
          f"{len(base) + len(doubles) + len(second) + resent_rows}, rows in the file = {len(file_rows)}")
    print(f"  CHECK    distinct ids in the file = {len({d['detection_id'] for d in file_rows})}, "
          f"truth rows = {len(pairs)}")
    first = {}
    for d in file_rows:
        first.setdefault(d["detection_id"], d)
    print(f"  CHECK    every re-sent row is an exact copy of its first sending: "
          f"{all(d == first[d['detection_id']] for d in file_rows)}")
    print(f"  CHECK    rows sent twice in truth = {sum(1 for _, t in pairs if t['times_sent'] == 2)}, "
          f"re-sent rows in the file = {resent_rows}")

    by_id = {d["detection_id"]: d for d, _ in pairs}
    print("\nONE EXAMPLE OF EACH KIND PER STORE")
    for store in sorted({d["store_id"] for d in file_rows}):
        d, t = next(((d, t) for d, t in doubles if d["store_id"] == store), (None, None))
        if d:
            o = by_id[t["duplicate_of"]]
            print(f"  {store} twice in one frame   {d['image_ref']}   label: {t['clean_text']}")
            print(f"      {o['detection_id']}  box {o['bbox']}  read: {o['ocr_text']}")
            print(f"      {d['detection_id']}  box {d['bbox']}  read: {d['ocr_text']}")
        else:
            print(f"  {store} twice in one frame   none in this store")
        d, t = second[[t["store_id"] for _, t in second].index(store)]
        o = next(bd for bd, bt in base if facing_of(bt) == facing_of(t) and bd["walk_id"] == WALKS[0][0])
        print(f"  {store} two badges           {t['aisle']} shelf {t['shelf']} pos {t['position']} "
              f"facing {t['facing']}   label: {t['clean_text']}")
        print(f"      {o['detection_id']}  {o['badge_id']}  {o['ts'][11:19]}  read: {o['ocr_text']}")
        print(f"      {d['detection_id']}  {d['badge_id']}  {d['ts'][11:19]}  read: {d['ocr_text']}")
        frame = next((f for f in sorted(resent_frames) if f.split("/")[1] == store), None)
        if frame:
            ids = [d["detection_id"] for d, _ in pairs if d["image_ref"] == frame]
            print(f"  {store} re-sent frame        {frame}   {len(ids)} rows")
            print(f"      first sent at file lines {line_of[ids[0]][0]}-{line_of[ids[-1]][0]}, "
                  f"sent again at lines {line_of[ids[0]][1]}-{line_of[ids[-1]][1]}")
        else:
            print(f"  {store} re-sent frame        none in this store")


def report_drift(base, walks):
    """Catalog v1 against v2, as it shows up in the base detections."""
    day_of = {key: info["day"] for key, info in walks.items()}
    early = [(d, t) for d, t in base if day_of[(d["store_id"], d["walk_id"])] < drift.EFFECTIVE_DAY]
    later = [(d, t) for d, t in base if day_of[(d["store_id"], d["walk_id"])] >= drift.EFFECTIVE_DAY]
    changes = drift.load_changes()
    print(f"\nCATALOG DRIFT, AS THE BADGE SAW IT - v1 labels before day {drift.EFFECTIVE_DAY}, v2 labels from then on")
    for c in changes:
        if c["change"] == "new":
            seen = sum(1 for _, t in base if t["true_code"] == c["v2_code"])
            print(f"  new           {c['v2_code']:<14} in catalog v2 only, on no shelf. Detections: {seen}")
            continue
        before = [(d, t) for d, t in early if t["true_code"] == c["v1_code"]]
        after = [(d, t) for d, t in later if t["true_code"] == (c["v2_code"] or c["v1_code"])]
        print(f"  {c['change'].replace('_', ' '):<13} {c['v1_code']:<14} "
              f"{len(before)} detections before, {len(after)} from day {drift.EFFECTIVE_DAY}"
              f"{' under barcode ' + c['v2_code'] if c['change'] == 'new_size' else ''}")
        for when, found in (("before", before), ("after ", after)):
            if found:
                d, t = found[0]
                print(f"      {when}  label: {t['clean_text']:<58} read: {d['ocr_text']}")
    gone = {c["v1_code"] for c in changes if c["change"] == "discontinued"}
    old_size = {c["v1_code"] for c in changes if c["change"] == "new_size"}
    new_size = {c["v2_code"] for c in changes if c["change"] == "new_size"}
    v1_later = [t for _, t in later if t["label_catalog"] == "v1"]
    print(f"  CHECK    before day {drift.EFFECTIVE_DAY}: {len(early)} detections, labelled from v1: "
          f"{sum(1 for _, t in early if t['label_catalog'] == 'v1')}")
    print(f"  CHECK    from day {drift.EFFECTIVE_DAY}: {len(later)} detections, labelled from v2: "
          f"{len(later) - len(v1_later)}, from v1: {len(v1_later)}, "
          f"all of those the discontinued product: {all(t['true_code'] in gone for t in v1_later)}")
    print(f"  CHECK    new-size products from day {drift.EFFECTIVE_DAY}: under the new barcode "
          f"{sum(1 for _, t in later if t['true_code'] in new_size)}, under the old barcode "
          f"{sum(1 for _, t in later if t['true_code'] in old_size)}")


def report(shelves, file_rows, truth, walks, line_of):
    """All stores and walks together: totals, re-scans, duplicates, drift, damage, barcodes, samples.

    file_rows are the clean rows. The damaged rows have their own report.
    """
    by_truth = {t["detection_id"]: t for t in truth}
    first = {}
    for d in file_rows:
        first.setdefault(d["detection_id"], d)
    pairs = [(d, by_truth[i]) for i, d in first.items()]    # every distinct detection, once
    base = [(d, t) for d, t in pairs if t["extra"] == ""]    # without doubles and the second badge
    detections = [d for d, _ in base]
    stores = sorted({d["store_id"] for d in detections})
    print(f"\nALL STORES  {len(stores)} stores, {len(WALKS)} full walks each, "
          f"{len({d['badge_id'] for d in detections})} badges on full walks")
    for day, rows in shelves.items():
        empty = sum(1 for r in rows if not r["actual_code"])
        print(f"  SHELF    day {day}: {len(rows)} facings, {len(rows) - empty} stocked, {empty} empty")
    full_walks = {key: info for key, info in walks.items() if info["aisle"] is None}
    expected = sum(1 for (store, _), info in full_walks.items()
                   for r in shelves[info["day"]] if r["store_id"] == store and r["actual_code"])
    print(f"  BADGE    {len(detections)} base detections in {len({d['image_ref'] for d in detections})} frames")
    print(f"  CHECK    stocked facings on each walk's day, summed = {expected}, base detections = {len(detections)}")

    # Re-scans: a facing is seen once by every full walk on a day when it was stocked.
    reads = {}
    for d, t in base:
        reads.setdefault(facing_of(t), []).append((d, t))
    stocked_days = {}
    for (store, _), info in full_walks.items():
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

    report_duplicates(file_rows, pairs, base, line_of)
    report_drift(base, walks)

    print(f"\nDAMAGE TO THE TEXT - all {len(pairs)} distinct detections")
    for kind in ("unit", "truncate", "drop", "case"):
        print(f"  {kind:<9} {sum(1 for _, t in pairs if kind in t['damage'].split('+')):>3}")
    per_detection = [0 if t["damage"] == "none" else len(t["damage"].split("+")) for _, t in pairs]
    for k in sorted(set(per_detection)):
        print(f"  {k} kinds of damage: {per_detection.count(k)} detections")
    changed = sum(1 for d, t in pairs if d["ocr_text"] != t["clean_text"])
    print(f"  text differs from the clean label: {changed} of {len(pairs)}")

    barcodes = sum(1 for d, _ in pairs if d["read_barcode"])
    conf = [d["confidence"] for d, _ in pairs]
    print(f"\nBARCODE READ   {barcodes} of {len(pairs)}")
    print(f"CONFIDENCE     min {min(conf):.2f}   median {statistics.median(conf):.2f}   max {max(conf):.2f}")

    print("\nSAMPLE - clean label, then what the badge read")
    step = max(1, len(pairs) // 10)
    for d, t in pairs[::step][:10]:
        print(f"  {d['detection_id']}  {d['aisle']} shelf {d['shelf']}  x={d['x']:.3f} y={d['y']:.3f} z={d['z']:.3f}"
              f"  conf {d['confidence']:.2f}  barcode {d['read_barcode'] or '-'}")
        print(f"      label: {t['clean_text']}")
        print(f"      read : {d['ocr_text']}")


def read_shelves():
    """The real shelf on every day: {1: rows, 2: rows, 3: rows}."""
    return {day: read_shelf(day) for day in range(1, planogram.DAYS + 1)}


def build_all(shelves, labels):
    """Every walk in every store, each past the shelf and the labels as they were on the
    walk's day, then the duplicates on top.

    Returns the file's items in order (a clean row as a dict, a damaged row as (line, note)),
    one truth row per detection id, and each walk's details.
    """
    file_rows, truth, walks = [], [], {}
    for s, (store, store_seed) in enumerate(planogram.STORES.items()):
        blocks = []   # one (detections, truth) block per walk in this store
        for w, (walk, day, clock_time, speed) in enumerate(WALKS):
            rows = [r for r in shelves[day + 1] if r["store_id"] == store]
            products, versions = labels[day + 1]
            badge = f"B{s * len(WALKS) + w + 1:03d}"   # S001: B001-B003, S002: B004-B006, ...
            seed = store_seed + 100 * w                # walk 1 keeps the store's seed: 1, 101, 201
            hour, minute = map(int, clock_time.split(":"))
            start = datetime.datetime.combine(DAY_ONE + datetime.timedelta(days=day),
                                              datetime.time(hour, minute), datetime.timezone.utc)
            d, t, legs = build_walk(rows, products, random.Random(seed), store, badge, walk, start, speed,
                                    versions=versions)
            blocks.append((d, t))
            walks[(store, walk)] = {"badge": badge, "seed": seed, "start": start, "speed": speed,
                                    "day": day + 1, "aisle": None, "legs": legs}

        # Duplicates. One generator per store, separate from the walks' own.
        dup_rng = random.Random(store_seed + DUPLICATE_SEED)

        # A second associate walks one aisle on day 1, shortly after the first walk starts.
        first = walks[(store, WALKS[0][0])]
        aisle = dup_rng.choice(sorted(planogram.AISLES.values()))
        rows = [r for r in shelves[first["day"]] if r["store_id"] == store and r["aisle"] == aisle]
        badge = f"B{len(planogram.STORES) * len(WALKS) + s + 1:03d}"   # B010, B011, B012
        seed = store_seed + 100 * len(WALKS)                            # 301, 302, 303
        start = first["start"] + datetime.timedelta(seconds=SECOND_BADGE_GAP_S)
        products, versions = labels[first["day"]]
        d, t, legs = build_walk(rows, products, random.Random(seed), store, badge, SECOND_BADGE_WALK,
                                start, first["speed"], extra="second_badge", versions=versions)
        blocks.append((d, t))
        walks[(store, SECOND_BADGE_WALK)] = {"badge": badge, "seed": seed, "start": start,
                                             "speed": first["speed"], "day": first["day"],
                                             "aisle": aisle, "legs": legs}

        store_rows = []
        for d, t in blocks:
            d, t = add_doubles(d, t, dup_rng)           # twice in one frame
            store_rows += add_resends(d, t, dup_rng)    # frames sent again
            truth += t
        # Damage on the way. One generator per store, separate from the duplicates' own.
        file_rows += add_malformed(store_rows, random.Random(store_seed + MALFORMED_SEED))
    return file_rows, truth, walks


def walks_record(walks):
    """Every walk's schedule and path, plus the assumed numbers behind them. Read by the replay page."""
    return {
        "note": "Every value under 'assumed' is a guess written in sim/walk.py, not a measurement.",
        "assumed": {
            "facing_width_m": FACING_WIDTH, "aisle_x0_m": AISLE_X0, "aisle_y0_m": AISLE_Y0,
            "aisle_pitch_m": AISLE_PITCH, "shelf_height_m": SHELF_Z, "position_jitter_m": POSITION_JITTER,
            "frames_per_second": FRAME_HZ, "camera_height_m": CAMERA_Z, "image_px": [IMAGE_W, IMAGE_H],
            "p_unit": P_UNIT, "p_truncate": P_TRUNCATE, "p_drop": P_DROP, "p_case": P_CASE,
            "p_barcode": P_BARCODE, "p_double": P_DOUBLE, "p_resend_frame": P_RESEND_FRAME,
            "second_badge_gap_s": SECOND_BADGE_GAP_S, "p_malformed": P_MALFORMED,
            "catalog_v2_effective_day": drift.EFFECTIVE_DAY,
        },
        "walks": [{
            "store_id": store, "walk_id": walk, "badge_id": info["badge"], "day": info["day"],
            "start_utc": info["start"].isoformat(timespec="seconds").replace("+00:00", "Z"),
            "speed_m_per_s": info["speed"], "only_aisle": info["aisle"], "seed": info["seed"],
            "legs": [{**leg, "length_m": round(leg["length_m"], 3), "start_s": round(leg["start_s"], 3),
                      "end_s": round(leg["end_s"], 3)} for leg in info["legs"]],
        } for (store, walk), info in walks.items()],
    }


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    shelves = read_shelves()
    items, truth, walks = build_all(shelves, label_sources())
    lines, clean, ledger, line_of = [], [], [], {}
    for n, item in enumerate(items, start=1):
        if isinstance(item, dict):
            lines.append(json.dumps(item, ensure_ascii=False))
            clean.append(item)
            line_of.setdefault(item["detection_id"], []).append(n)
        else:
            line, note = item
            lines.append(line)
            ledger.append({"line": n, **note})
    DETECTIONS.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    planogram.write_csv(DET_TRUTH, truth)
    planogram.write_csv(MALFORMED_TRUTH, ledger)
    WALKS_FILE.write_text(json.dumps(walks_record(walks), indent=1) + "\n", encoding="utf-8", newline="\n")
    print(f"Wrote {DETECTIONS.relative_to(catalog.ROOT)} ({len(lines)} rows, {len(ledger)} of them damaged), "
          f"{DET_TRUTH.relative_to(catalog.ROOT)} ({len(truth)} rows), "
          f"{MALFORMED_TRUTH.relative_to(catalog.ROOT)} ({len(ledger)} rows), "
          f"{len(planogram.STORES)} stores, {len(walks)} walks, and {WALKS_FILE.relative_to(catalog.ROOT)}")
    report_walks(shelves, clean, truth, walks)
    if "--legs" in sys.argv:   # every walk aisle by aisle
        doubles = {t["detection_id"] for t in truth if t["extra"] == "double_in_frame"}
        for (store, walk), info in walks.items():
            mine = {d["detection_id"]: d for d in clean
                    if d["store_id"] == store and d["walk_id"] == walk and d["detection_id"] not in doubles}
            report_walk(store, info["badge"], walk,
                        [r for r in shelves[info["day"]] if r["store_id"] == store
                         and info["aisle"] in (None, r["aisle"])],
                        list(mine.values()), info["legs"])
    report(shelves, clean, truth, walks, line_of)
    report_malformed(ledger, lines, len(clean))


if __name__ == "__main__":
    main()
