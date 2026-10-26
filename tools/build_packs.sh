#!/usr/bin/env bash
# Build the three pack files for one region.
#
#   tools/build_packs.sh <id> "<pbf-url> [<pbf-url> ...]" <minLon,minLat,maxLon,maxLat> <out-dir> [map-maxzoom]
#   e.g. tools/build_packs.sh isle-of-man https://download.geofabrik.de/europe/isle-of-man-latest.osm.pbf \
#            -4.9,54.0,-4.2,54.5 out/isle-of-man
#
# Output: <out-dir>/routing.tar  Valhalla tiles (tile-dir layout, version-matched to valhalla-mobile)
#         <out-dir>/map.pmtiles  Protomaps basemap for the bbox
#         <out-dir>/index.sqlite cameras, speed limits, lit roads and search index
#
# Needs: docker, curl, python3 with `pip install osmium`, and the `pmtiles` CLI (go-pmtiles) on PATH.
set -euo pipefail

ID="$1"; PBF_URL="$2"; BBOX="$3"; OUT="$4"; MAXZOOM="${5:-14}"
# Must match the Valhalla version inside valhalla-mobile, because tile formats are version specific.
VALHALLA_IMAGE="${VALHALLA_IMAGE:-ghcr.io/valhalla/valhalla-scripted:3.9.0}"
HERE="$(cd "$(dirname "$0")" && pwd)"

mkdir -p "$OUT"
OUT="$(cd "$OUT" && pwd)"
WORK="$OUT/work"
mkdir -p "$WORK"

echo "== 1/4 download OSM extract(s)"
rm -f "$WORK"/part-*.osm.pbf
n=0
PBFS=()
for url in $PBF_URL; do
  n=$((n + 1))
  curl -fL --retry 3 -o "$WORK/part-$n.osm.pbf" "$url"
  PBFS+=("$WORK/part-$n.osm.pbf")
done
DATE="$(date -u +%Y-%m-%d)"

echo "== 2/4 Valhalla routing tiles"
rm -rf "$WORK/valhalla_tiles"
docker run --rm -v "$WORK:/data" --entrypoint bash "$VALHALLA_IMAGE" -c '
  set -euo pipefail
  cd /data
  valhalla_build_config \
      --mjolnir-tile-dir /data/valhalla_tiles \
      --mjolnir-admin /data/admin.sqlite \
      --mjolnir-concurrency "$(nproc)" > config.json
  valhalla_build_admins -c config.json part-*.osm.pbf
  valhalla_build_tiles -c config.json part-*.osm.pbf
'
# The app extracts this into its tile directory, so paths are relative to the tile dir root.
tar -cf "$OUT/routing.tar" -C "$WORK/valhalla_tiles" .

echo "== 3/4 basemap (zoom <= $MAXZOOM)"
# Find the newest Protomaps daily build (they are named YYYYMMDD.pmtiles).
BUILD=""
for back in 0 1 2 3 4 5 6; do
  d="$(date -u -d "-$back day" +%Y%m%d 2>/dev/null || date -u -v-"$back"d +%Y%m%d)"
  if curl -fsI "https://build.protomaps.com/$d.pmtiles" >/dev/null; then BUILD="https://build.protomaps.com/$d.pmtiles"; break; fi
done
[ -n "$BUILD" ] || { echo "no Protomaps build found" >&2; exit 1; }
pmtiles extract "$BUILD" "$OUT/map.pmtiles" --bbox="$BBOX" --maxzoom="$MAXZOOM"

echo "== 4/4 cameras, limits, lit roads, search index"
python3 "$HERE/build_index.py" --out "$OUT/index.sqlite" --date "$DATE" "${PBFS[@]}"

echo "$DATE" > "$OUT/DATE"
ls -l "$OUT"
