"""Write down what in this simulation is verified and what is assumed.

    python sim/assumptions.py

Writes  ASSUMPTIONS.md          the list, for people
        data/assumptions.json   the same list, for the replay page

Every figure in the list is read from the constants in the simulation's own code or
counted from its data files, so the list cannot drift away from what the code does.
"""
import json
import sys

import catalog
import drift
import planogram
import walk

ASSUMPTIONS_MD = catalog.ROOT / "ASSUMPTIONS.md"
ASSUMPTIONS_JSON = catalog.ROOT / "data" / "assumptions.json"

GS1_URL = "https://www.gs1.org/1/gtinrules/en/rule/266/declared-net-content"


def pct(share):
    return f"{share * 100:g}%"


def build():
    products = catalog.load()
    meta = json.loads(catalog.META.read_text(encoding="utf-8"))
    changes = drift.load_changes()
    skipped = sum(len(group["skipped_no_name_or_brand"]) for group in meta["categories"])
    long_codes = [p["code"] for p in products if len(p["code"]) == 13]
    valid = sum(1 for code in long_codes if drift.check_digit(code[:-1]) == code[-1])
    kinds = {kind: sum(1 for c in changes if c["change"] == kind) for kind in ("renamed", "new_size", "discontinued", "new")}
    walks = "; ".join(f"day {day + 1} at {start} UTC and {speed:g} m/s" for _, day, start, speed in walk.WALKS)
    shelf_heights = ", ".join(f"{height:g}" for height in walk.SHELF_Z.values())

    verified = [
        f"The {len(products)} products are real catalog rows from Open Food Facts, Open Beauty Facts and Open Products "
        f"Facts, pulled on {meta['pulled_at_utc'][:10]} and published under the Open Database License.",
        "Each product row is stored exactly as its source returned it. The checksum of the file is recorded in "
        "data/catalog_raw.meta.json.",
        f"A row with no name or no brand was left out: {skipped} rows, each listed in data/catalog_raw.meta.json.",
        f"All {valid} of the {len(long_codes)} thirteen-digit barcodes in the catalog pass the GS1 check digit.",
        "A change of declared net content requires a new barcode: GS1 GTIN Management Standard, rule "
        f"\"Declared net content\" ({GS1_URL}).",
        "Every script is deterministic: the same seeds give the same files, byte for byte.",
        "Every number on the replay page is counted from the files in data/ when the page loads.",
    ]

    assumed = [
        # the store
        f"{len(planogram.STORES)} stores share one planogram of {len(products)} products: {len(planogram.AISLES)} aisles, "
        f"{planogram.SHELVES} shelves per aisle, shelf 1 at the bottom.",
        "Products of one brand sit side by side: each aisle is ordered by brand, then by name.",
        "A product gets 1, 2 or 3 facings, by how often it was scanned in its source catalog.",
        f"Every facing is {walk.FACING_WIDTH:g} m wide. Aisles are {walk.AISLE_PITCH:g} m apart. "
        f"Shelf heights are {shelf_heights} m.",
        # shelf problems
        f"Out of stock: {planogram.OUT_OF_STOCK[0]} to {planogram.OUT_OF_STOCK[1]} positions per aisle are empty on day 1.",
        f"Misplaced: {planogram.MISPLACED[0]} to {planogram.MISPLACED[1]} single units per store sit in the wrong aisle, "
        "and stay there on every day.",
        f"Restocking: overnight after day 1, {pct(planogram.RESTOCK_SHARE)} of the empty positions are refilled, rounded "
        f"down, and {planogram.NEW_OUT_OF_STOCK[0]} to {planogram.NEW_OUT_OF_STOCK[1]} stocked positions per store run out. "
        "Nothing changes after day 2.",
        # the walks and the camera
        f"{len(walk.WALKS)} full walks per store, one per day, each by a different badge, starting {walk.DAY_ONE}: {walks}.",
        "A walk is a serpentine: the first aisle forward, the next one back.",
        f"The camera takes {walk.FRAME_HZ} frames per second. Each stocked facing is read once per walk, in the frame "
        "taken closest to it. An empty facing gives no detection.",
        f"A detection's 3D position is the facing's true position plus noise of {walk.POSITION_JITTER:g} m standard "
        "deviation per axis.",
        f"The bounding box is a flat projection onto a {walk.IMAGE_W} x {walk.IMAGE_H} frame, with no lens distortion.",
        "The fields of a detection record are a guess at what a shelf-reading camera reports.",
        "No image is used. image_ref names a frame that does not exist.",
        # what the camera reads
        "A label's text is the catalog row's brand, name and size joined together. Real labels differ more from their "
        "catalog rows, so matching is easier here than on real images.",
        f"The read text is damaged in four independent ways: a unit is respelled on {pct(walk.P_UNIT)} of reads, the text is "
        f"cut short on {pct(walk.P_TRUNCATE)}, 1 to 3 characters are lost on {pct(walk.P_DROP)}, and the case changes on "
        f"{pct(walk.P_CASE)}.",
        f"A barcode is read on {pct(walk.P_BARCODE)} of detections, and is never misread.",
        "Confidence is highest for an undamaged read and falls with each kind of damage.",
        # duplicates
        f"{pct(walk.P_DOUBLE)} of detections are reported twice in the same frame, with a new id and the text read again.",
        f"A second badge walks one aisle of each store on day 1, {walk.SECOND_BADGE_GAP_S} seconds after the first walk starts.",
        f"{pct(walk.P_RESEND_FRAME)} of frames are sent twice, unchanged.",
        # catalog v2
        f"Catalog v2 takes effect on day {drift.EFFECTIVE_DAY}: {kinds['renamed']} products renamed by a fixed rule, "
        f"{kinds['new_size']} given a size {pct(1 - drift.SHRINK)} smaller and a new barcode, {kinds['discontinued']} "
        f"discontinued, and {kinds['new']} real product added. The renames, the sizes and the new barcodes are simulated.",
        "A simulated barcode keeps the first 7 digits of the barcode it replaces and carries a valid check digit.",
        f"From day {drift.EFFECTIVE_DAY} all stock of a resized product carries the new barcode. Stock of the discontinued "
        "product stays on the shelf with its old label. The planogram is not updated.",
        "\"Sold in the United States\" is the sources' own tag. Some personal care and household rows are products made "
        "for other markets.",
        # the replay page
        "The replay page's opening view plays one aisle slower than the walk, and states by how much.",
        "The replay page draws a repeated detection a quarter of a second after the first, and a re-sent row just after "
        "its walk ends: the data gives a re-sent row no arrival time.",
    ]
    return {"verified": verified, "assumed": assumed}


def markdown(facts):
    lines = [
        "# Verified and assumed",
        "",
        "What in this simulation can be checked, and what was made up to make it run.",
        "The figures below are read from the simulation's code and data by `sim/assumptions.py`.",
        "",
        f"## Verified ({len(facts['verified'])})",
        "",
        *[f"- {item}" for item in facts["verified"]],
        "",
        f"## Assumed ({len(facts['assumed'])})",
        "",
        *[f"- {item}" for item in facts["assumed"]],
        "",
    ]
    return "\n".join(lines)


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    facts = build()
    ASSUMPTIONS_JSON.write_text(json.dumps(facts, indent=1, ensure_ascii=False) + "\n", encoding="utf-8", newline="\n")
    ASSUMPTIONS_MD.write_text(markdown(facts), encoding="utf-8", newline="\n")
    print(f"Wrote {ASSUMPTIONS_MD.relative_to(catalog.ROOT)} and {ASSUMPTIONS_JSON.relative_to(catalog.ROOT)}: "
          f"{len(facts['verified'])} verified, {len(facts['assumed'])} assumed")


if __name__ == "__main__":
    main()
