#!/usr/bin/env python3
"""Cut one region's routing tiles out of a larger Valhalla tile tree.

Valhalla tiles only join up if they come from the same build, so regions are cut from one tile tree
rather than built separately. A tile belongs to a region if its area touches the region's bounding box;
big, coarse tiles (levels 0 and 1) therefore end up in several regions, with identical bytes, which the
app handles when installing and removing regions.

Usage: split_tiles.py TILE_DIR --bbox minLon,minLat,maxLon,maxLat --out routing.tar
       split_tiles.py TILE_DIR --list     (prints the number of tiles per level)
"""
import argparse
import os
import sys
import tarfile

# level -> (tile size in degrees, tiles per row)
LEVELS = {0: (4.0, 90), 1: (1.0, 360), 2: (0.25, 1440)}


def tile_bbox(level, tile_id):
    size, ncols = LEVELS[level]
    row, col = divmod(tile_id, ncols)
    return (-180 + col * size, -90 + row * size, -180 + (col + 1) * size, -90 + (row + 1) * size)


def parse_tile_path(rel):
    """'2/000/753/001.gph' -> (2, 753001)."""
    parts = rel.replace("\\", "/").split("/")
    level = int(parts[0])
    digits = "".join(parts[1:])
    if not digits.endswith(".gph"):
        raise ValueError(rel)
    return level, int(digits[:-4])


def tiles(tile_dir):
    for dirpath, _, files in os.walk(tile_dir):
        for name in files:
            if not name.endswith(".gph"):
                continue
            full = os.path.join(dirpath, name)
            rel = os.path.relpath(full, tile_dir).replace("\\", "/")
            try:
                level, tid = parse_tile_path(rel)
            except ValueError:
                continue
            if level in LEVELS:
                yield full, rel, level, tid


def intersects(a, b):
    return a[0] <= b[2] and a[2] >= b[0] and a[1] <= b[3] and a[3] >= b[1]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("tile_dir")
    ap.add_argument("--bbox")
    ap.add_argument("--out")
    ap.add_argument("--list", action="store_true")
    args = ap.parse_args()

    if args.list:
        counts = {}
        for _, _, level, _ in tiles(args.tile_dir):
            counts[level] = counts.get(level, 0) + 1
        print(counts)
        return

    box = tuple(float(x) for x in args.bbox.split(","))
    chosen = sorted((rel, full, level) for full, rel, level, tid in tiles(args.tile_dir)
                    if intersects(tile_bbox(level, tid), box))
    if not chosen:
        sys.exit("no tiles intersect the bounding box")
    with tarfile.open(args.out, "w") as tar:
        for rel, full, _ in chosen:
            tar.add(full, arcname=rel)
    per_level = {}
    for _, _, level in chosen:
        per_level[level] = per_level.get(level, 0) + 1
    print(f"{args.out}: {len(chosen)} tiles {per_level}, {os.path.getsize(args.out) / 1e6:.1f} MB", file=sys.stderr)


if __name__ == "__main__":
    main()
