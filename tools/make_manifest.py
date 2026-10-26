#!/usr/bin/env python3
"""Add (or replace) one region in regions.json, computing size and sha256 of its pack files.

Usage: make_manifest.py regions.json --id iom --name "Isle of Man" --bbox minLon,minLat,maxLon,maxLat \
         --date 2026-10-01 --base-url https://github.com/OWNER/REPO/releases/download/TAG \
         routing=routing.tar map=map.pmtiles index=index.sqlite
The JSON matches RegionManifest in SatnavCore (schemaVersion 1).
"""
import argparse
import hashlib
import json
import os

SCHEMA_VERSION = 1


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("manifest")
    ap.add_argument("--id", required=True)
    ap.add_argument("--name", required=True)
    ap.add_argument("--bbox", required=True)
    ap.add_argument("--date", required=True)
    ap.add_argument("--base-url", required=True)
    ap.add_argument("files", nargs="+", help="kind=path")
    a = ap.parse_args()

    min_lon, min_lat, max_lon, max_lat = (float(x) for x in a.bbox.split(","))
    files = []
    for spec in a.files:
        kind, path = spec.split("=", 1)
        files.append({
            "kind": kind,
            "url": f"{a.base_url.rstrip('/')}/{os.path.basename(path)}",
            "bytes": os.path.getsize(path),
            "sha256": sha256(path),
        })
    region = {
        "id": a.id, "name": a.name, "dataDate": a.date, "files": files,
        "bbox": {"minLat": min_lat, "minLon": min_lon, "maxLat": max_lat, "maxLon": max_lon},
    }

    manifest = {"schemaVersion": SCHEMA_VERSION, "regions": []}
    if os.path.exists(a.manifest):
        with open(a.manifest) as f:
            manifest = json.load(f)
    manifest["regions"] = [r for r in manifest["regions"] if r["id"] != a.id] + [region]
    manifest["regions"].sort(key=lambda r: r["id"])
    with open(a.manifest, "w") as f:
        json.dump(manifest, f, indent=2)
        f.write("\n")


if __name__ == "__main__":
    main()
