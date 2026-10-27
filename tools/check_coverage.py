#!/usr/bin/env python3
"""Check that a region set leaves no gaps on the map.

Usage: check_coverage.py SET.json              checks a list of well-known UK places
       check_coverage.py SET.json --tiles DIR  also checks every routing tile in a built Valhalla tile tree
Exits 1 and lists what is not inside any region's bounding box.
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from split_tiles import intersects, tile_bbox, tiles  # noqa: E402

# (name, lon, lat)
PLACES = [
    ("London", -0.1276, 51.5072), ("Canterbury", 1.0802, 51.2802), ("Brighton", -0.1372, 50.8225),
    ("Southampton", -1.4044, 50.9097), ("Oxford", -1.2577, 51.7520), ("Bristol", -2.5879, 51.4545),
    ("Plymouth", -4.1427, 50.3755), ("Penzance", -5.5371, 50.1186), ("Cardiff", -3.1791, 51.4816),
    ("Swansea", -3.9436, 51.6214), ("Holyhead", -4.6317, 53.3085), ("Birmingham", -1.8904, 52.4862),
    ("Leicester", -1.1332, 52.6369), ("Cambridge", 0.1218, 52.2053), ("Norwich", 1.2974, 52.6309),
    ("Lowestoft", 1.7508, 52.4752), ("Liverpool", -2.9916, 53.4084), ("Manchester", -2.2426, 53.4808),
    ("Carlisle", -2.9329, 54.8925), ("Leeds", -1.5491, 53.8008), ("Hull", -0.3367, 53.7457),
    ("Newcastle", -1.6178, 54.9783), ("Berwick-upon-Tweed", -2.0, 55.7714), ("Dumfries", -3.6118, 55.0701),
    ("Glasgow", -4.2518, 55.8642), ("Edinburgh", -3.1883, 55.9533), ("Aberdeen", -2.0943, 57.1497),
    ("Inverness", -4.2247, 57.4778), ("Ullapool", -5.1597, 57.8957), ("Stornoway", -6.3866, 58.2093),
    ("Kirkwall", -2.9606, 58.9809), ("Lerwick", -1.1494, 60.1539), ("Belfast", -5.9301, 54.5973),
    ("Derry", -7.3086, 54.9966), ("Isle of Wight (Newport)", -1.2943, 50.7005),
]


def parse_box(text):
    min_lon, min_lat, max_lon, max_lat = (float(x) for x in text.split(","))
    return (min_lon, min_lat, max_lon, max_lat)


def uncovered(boxes, points):
    """Names of points that sit outside every box."""
    return [name for name, lon, lat in points
            if not any(b[0] <= lon <= b[2] and b[1] <= lat <= b[3] for b in boxes)]


def uncovered_tiles(boxes, tile_dir):
    """Routing tiles that touch no region. Only the finest level matters: coarse tiles are shared."""
    missing = []
    for _, rel, level, tid in tiles(tile_dir):
        if level != 2:
            continue
        if not any(intersects(tile_bbox(level, tid), b) for b in boxes):
            missing.append(rel)
    return missing


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("set")
    ap.add_argument("--tiles")
    args = ap.parse_args()

    with open(args.set) as f:
        boxes = [parse_box(r["bbox"]) for r in json.load(f)["regions"]]

    bad = uncovered(boxes, PLACES)
    for name in bad:
        print(f"not covered: {name}")
    if args.tiles:
        missing = uncovered_tiles(boxes, args.tiles)
        for rel in missing[:50]:
            print(f"tile outside every region: {rel}")
        if len(missing) > 50:
            print(f"... and {len(missing) - 50} more tiles")
        bad += missing
    if bad:
        sys.exit(1)
    print("ok: every checked place and tile is inside a region")


if __name__ == "__main__":
    main()
