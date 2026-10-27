#!/usr/bin/env python3
"""Build a whole set of regions from one OSM extract, so their routing tiles come from a single graph build.

    tools/build_set.py tools/sets/uk.json --out out [--publish-repo OWNER/REPO]

The set file lists the OSM extract(s) and the regions (id, name, bbox "minLon,minLat,maxLon,maxLat").
Steps: download -> Valhalla tiles for everything -> one pass for every region's index -> per region:
cut its routing tiles, extract its basemap, and (with --publish-repo) release it and update the manifest.
Needs docker, curl, `pmtiles` on PATH, `pip install osmium`, and `gh` with GH_TOKEN for publishing.
"""
import argparse
import datetime
import json
import os
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
# Must match the Valhalla version inside valhalla-mobile: tile formats are version specific.
VALHALLA_IMAGE = os.environ.get("VALHALLA_IMAGE", "ghcr.io/valhalla/valhalla-scripted:3.9.0")


def run(cmd, **kw):
    print("+", " ".join(str(c) for c in cmd), flush=True)
    subprocess.run([str(c) for c in cmd], check=True, **kw)


def step(text):
    print(f"\n== {text}", flush=True)


def newest_protomaps_build():
    for back in range(7):
        day = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=back)).strftime("%Y%m%d")
        url = f"https://build.protomaps.com/{day}.pmtiles"
        if subprocess.run(["curl", "-fsI", url], stdout=subprocess.DEVNULL).returncode == 0:
            return url
    sys.exit("no Protomaps build found")


def publish(repo, region, out, date, files):
    """Create the region's release and merge it into the published manifest."""
    rid = region["id"]
    tag = f"{rid}-{date}"
    manifest = os.path.join(out, "regions.json")
    if not os.path.exists(manifest):
        # Start from the published manifest so other regions are kept.
        subprocess.run(["curl", "-fsL", f"https://github.com/{repo}/releases/download/manifest/regions.json",
                        "-o", manifest], check=False)
    run([sys.executable, os.path.join(HERE, "make_manifest.py"), manifest, "--id", rid, "--name", region["name"],
         f"--bbox={region['bbox']}", "--date", date,
         "--base-url", f"https://github.com/{repo}/releases/download/{tag}",
         *[f"{kind}={path}" for kind, path in files.items()]])
    subprocess.run(["gh", "release", "delete", tag, "-R", repo, "--yes", "--cleanup-tag"],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    run(["gh", "release", "create", tag, "-R", repo, "--title", tag,
         "--notes", f"Offline packs for {region['name']}. Map data © OpenStreetMap contributors (ODbL); basemap by Protomaps.",
         *files.values()])
    if subprocess.run(["gh", "release", "view", "manifest", "-R", repo], stdout=subprocess.DEVNULL,
                      stderr=subprocess.DEVNULL).returncode != 0:
        run(["gh", "release", "create", "manifest", "-R", repo, "--title", "Region manifest",
             "--notes", "Latest regions.json read by the app."])
    run(["gh", "release", "upload", "manifest", manifest, "-R", repo, "--clobber"])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("set_file")
    ap.add_argument("--out", default="out")
    ap.add_argument("--publish-repo")
    ap.add_argument("--maxzoom", default="14")
    ap.add_argument("--only", help="comma-separated region ids to package (the whole set is still built)")
    args = ap.parse_args()

    cfg = json.load(open(args.set_file))
    regions = cfg["regions"]
    if args.only:
        wanted = set(args.only.split(","))
        regions = [r for r in regions if r["id"] in wanted]
    out = os.path.abspath(args.out)
    work = os.path.join(out, "work")
    os.makedirs(work, exist_ok=True)
    date = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")

    step("download OSM extract(s)")
    pbfs = []
    for i, url in enumerate(cfg["pbf"], 1):
        path = os.path.join(work, f"part-{i}.osm.pbf")
        run(["curl", "-fL", "--retry", "3", "-o", path, url])
        pbfs.append(path)

    step("Valhalla routing tiles (one build for the whole set)")
    shutil.rmtree(os.path.join(work, "valhalla_tiles"), ignore_errors=True)
    run(["docker", "run", "--rm", "-v", f"{work}:/data", "--entrypoint", "bash", VALHALLA_IMAGE, "-c", """
        set -euo pipefail
        cd /data
        valhalla_build_config --mjolnir-tile-dir /data/valhalla_tiles --mjolnir-admin /data/admin.sqlite \
            --mjolnir-concurrency "$(nproc)" > config.json
        valhalla_build_admins -c config.json part-*.osm.pbf
        valhalla_build_tiles -c config.json part-*.osm.pbf
        rm -f /data/admin.sqlite
    """])
    tiles = os.path.join(work, "valhalla_tiles")
    run([sys.executable, os.path.join(HERE, "split_tiles.py"), tiles, "--list"])

    step("search, speed limit, camera and lighting indexes (all regions, one pass)")
    for r in regions:
        os.makedirs(os.path.join(out, r["id"]), exist_ok=True)
    cmd = [sys.executable, os.path.join(HERE, "build_index.py"), "--date", date]
    for r in regions:
        cmd += ["--region", f"{r['id']}:{r['bbox']}:{os.path.join(out, r['id'], 'index.sqlite')}"]
    run(cmd + pbfs)
    for p in pbfs:
        os.remove(p)

    step("basemap source")
    basemap = newest_protomaps_build()
    print(basemap)

    for r in regions:
        step(f"package {r['id']}")
        d = os.path.join(out, r["id"])
        routing = os.path.join(d, "routing.tar")
        mapfile = os.path.join(d, "map.pmtiles")
        run([sys.executable, os.path.join(HERE, "split_tiles.py"), tiles, f"--bbox={r['bbox']}", "--out", routing])
        run(["pmtiles", "extract", basemap, mapfile, f"--bbox={r['bbox']}", f"--maxzoom={args.maxzoom}"])
        files = {"routing": routing, "map": mapfile, "index": os.path.join(d, "index.sqlite")}
        for kind, path in files.items():
            print(f"  {kind}: {os.path.getsize(path) / 1e6:.1f} MB")
        if args.publish_repo:
            publish(args.publish_repo, r, out, date, files)
            shutil.rmtree(d)   # free disk before the next region

    print("\nDone.")


if __name__ == "__main__":
    main()
