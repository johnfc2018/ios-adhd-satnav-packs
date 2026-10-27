#!/usr/bin/env python3
"""Build one index.sqlite per region from OSM .pbf files, in a single pass over the data.

Tables (per region database)
  meta(key, value)
  cameras(id, lat, lon, limit_kph, direction, kind)
  ways(id, kind, value, geom)        kind 'limit' (value = km/h) or 'lit' (value = 1); geom = polyline6
  way_rtree(id, min_lat, max_lat, min_lon, max_lon)
  localities(name, kind, lat, lon, importance)   towns/villages/suburbs, for "where am I"
  places                             FTS5 gazetteer: places, named streets, postcodes, POIs

Usage: build_index.py --date YYYY-MM-DD --region ID:minLon,minLat,maxLon,maxLat:OUT.sqlite [--region ...] a.osm.pbf [b.osm.pbf ...]
       build_index.py --out OUT.sqlite [--date D] a.osm.pbf       (one region covering everything)
Requires: pip install osmium
"""
import argparse
import sqlite3
import sys

import osmium

SCHEMA_VERSION = 1
DRIVABLE = {
    "motorway", "motorway_link", "trunk", "trunk_link", "primary", "primary_link", "secondary",
    "secondary_link", "tertiary", "tertiary_link", "unclassified", "residential", "living_street",
    "service", "road",
}
LIT_VALUES = {"yes", "24/7", "automatic", "sunset-sunrise", "limited"}
PLACE_IMPORTANCE = {"city": 100, "town": 80, "suburb": 55, "village": 60, "hamlet": 40,
                    "neighbourhood": 35, "locality": 30}
SETTLEMENTS = ("city", "town", "village", "suburb", "hamlet")
POI_KEYS = ("amenity", "shop", "tourism", "leisure")
MPH = 1.609344


def parse_kph(raw, highway=None):
    """Mirror of SpeedTag.parseKph in SatnavCore."""
    first = raw.split(";")[0].strip().lower()
    if first in ("", "none", "signals", "variable"):
        return None
    if first in ("national_speed_limit", "gb:nsl_single"):
        return 70 * MPH if highway == "motorway" else 60 * MPH
    if first in ("gb:nsl_dual", "gb:motorway"):
        return 70 * MPH
    if first == "walk":
        return 7.0
    try:
        if first.endswith("mph"):
            return float(first[:-3].strip()) * MPH
        if first.endswith("km/h"):
            return float(first[:-4].strip())
        return float(first)
    except ValueError:
        return None


def parse_direction(raw):
    names = {"n": 0, "ne": 45, "e": 90, "se": 135, "s": 180, "sw": 225, "w": 270, "nw": 315,
             "north": 0, "east": 90, "south": 180, "west": 270}
    try:
        return float(raw)
    except ValueError:
        return names.get(raw.lower())


def encode_polyline(points, precision=6):
    factor = 10 ** precision
    out, prev_lat, prev_lon = [], 0, 0
    for lat, lon in points:
        la, lo = round(lat * factor), round(lon * factor)
        for delta in (la - prev_lat, lo - prev_lon):
            v = ~(delta << 1) if delta < 0 else (delta << 1)
            while v >= 0x20:
                out.append(chr((0x20 | (v & 0x1F)) + 63))
                v >>= 5
            out.append(chr(v + 63))
        prev_lat, prev_lon = la, lo
    return "".join(out)


class Sink:
    """Everything that goes into one region's database."""

    def __init__(self, region_id, bbox, path, date):
        self.id = region_id
        self.min_lon, self.min_lat, self.max_lon, self.max_lat = bbox
        self.path = path
        self.date = date
        self.db = sqlite3.connect(path)
        self.db.executescript("""
            PRAGMA journal_mode = OFF;
            PRAGMA synchronous = OFF;
            DROP TABLE IF EXISTS meta; DROP TABLE IF EXISTS cameras; DROP TABLE IF EXISTS ways;
            DROP TABLE IF EXISTS way_rtree; DROP TABLE IF EXISTS places; DROP TABLE IF EXISTS localities;
            CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT);
            CREATE TABLE cameras(id INTEGER PRIMARY KEY, lat REAL, lon REAL, limit_kph REAL, direction REAL, kind TEXT);
            CREATE TABLE ways(id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT, value REAL, geom TEXT);
            CREATE TABLE localities(name TEXT, kind TEXT, lat REAL, lon REAL, importance REAL);
            CREATE INDEX localities_lat ON localities(lat);
            CREATE VIRTUAL TABLE way_rtree USING rtree(id, min_lat, max_lat, min_lon, max_lon);
            CREATE VIRTUAL TABLE places USING fts5(
                name, locality, kind UNINDEXED, lat UNINDEXED, lon UNINDEXED, importance UNINDEXED,
                tokenize = 'unicode61 remove_diacritics 2', prefix = '2 3 4');
        """)
        self.postcodes = {}   # postcode -> [sum_lat, sum_lon, n]
        self.streets = {}     # (name, lat cell, lon cell) -> (name, lat, lon, locality)
        self.grid = {}        # (lat*10, lon*10) -> [(lat, lon, name)] of settlements, to label streets
        self.counts = {"cameras": 0, "limit": 0, "lit": 0, "places": 0}

    def has_point(self, lat, lon):
        return self.min_lat <= lat <= self.max_lat and self.min_lon <= lon <= self.max_lon

    def touches(self, min_lat, max_lat, min_lon, max_lon):
        return (min_lat <= self.max_lat and max_lat >= self.min_lat
                and min_lon <= self.max_lon and max_lon >= self.min_lon)

    def add_camera(self, osm_id, lat, lon, limit, direction, kind):
        self.db.execute("INSERT OR REPLACE INTO cameras VALUES (?,?,?,?,?,?)", (osm_id, lat, lon, limit, direction, kind))
        self.counts["cameras"] += 1

    def add_way(self, kind, value, pts, bounds):
        cur = self.db.execute("INSERT INTO ways(kind, value, geom) VALUES (?,?,?)", (kind, value, encode_polyline(pts)))
        self.db.execute("INSERT INTO way_rtree VALUES (?,?,?,?,?)", (cur.lastrowid, *bounds))
        self.counts[kind] += 1

    def add_place(self, name, locality, kind, lat, lon, importance):
        self.db.execute("INSERT INTO places(name, locality, kind, lat, lon, importance) VALUES (?,?,?,?,?,?)",
                        (name, locality, kind, lat, lon, importance))
        self.counts["places"] += 1

    def add_locality(self, name, kind, lat, lon):
        self.db.execute("INSERT INTO localities VALUES (?,?,?,?,?)", (name, kind, lat, lon, PLACE_IMPORTANCE[kind]))
        if kind in SETTLEMENTS:
            self.grid.setdefault((int(lat * 10), int(lon * 10)), []).append((lat, lon, name))

    def note_street(self, name, lat, lon, locality):
        key = (name.lower(), round(lat, 2), round(lon, 2))
        if key not in self.streets:
            self.streets[key] = (name, lat, lon, locality)

    def note_postcode(self, tags, lat, lon):
        pc = tags.get("addr:postcode")
        if pc:
            s = self.postcodes.setdefault(pc.strip().upper(), [0.0, 0.0, 0])
            s[0] += lat
            s[1] += lon
            s[2] += 1

    def nearest_locality(self, lat, lon):
        best, best_d = "", 1e9
        cx, cy = int(lat * 10), int(lon * 10)
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for plat, plon, pname in self.grid.get((cx + dx, cy + dy), ()):
                    d = (plat - lat) ** 2 + ((plon - lon) * 0.62) ** 2
                    if d < best_d:
                        best, best_d = pname, d
        return best

    def finish(self):
        for name, lat, lon, locality in self.streets.values():
            self.add_place(name, locality or self.nearest_locality(lat, lon), "street", lat, lon, 30)
        for pc, (sl, so, n) in self.postcodes.items():
            self.add_place(pc, "", "postcode", sl / n, so / n, 35)
        self.db.execute("INSERT INTO meta VALUES ('schema', ?)", (str(SCHEMA_VERSION),))
        self.db.execute("INSERT INTO meta VALUES ('date', ?)", (self.date,))
        self.db.commit()
        self.db.execute("VACUUM")
        self.db.close()
        print(f"{self.id}: {self.counts} postcodes={len(self.postcodes)} streets={len(self.streets)}", file=sys.stderr)


class Handler(osmium.SimpleHandler):
    def __init__(self, sinks):
        super().__init__()
        self.sinks = sinks

    def node(self, n):
        t = n.tags
        if not t or not n.location.valid():
            return
        lat, lon = n.location.lat, n.location.lon
        targets = [s for s in self.sinks if s.has_point(lat, lon)]
        if not targets:
            return
        name = t.get("name")
        is_camera = t.get("highway") == "speed_camera" or t.get("enforcement") in ("maxspeed", "average_speed")
        for s in targets:
            if is_camera:
                limit = parse_kph(t.get("maxspeed", ""))
                direction = parse_direction(t.get("direction") or t.get("camera:direction") or "")
                kind = "average" if t.get("enforcement") == "average_speed" else "fixed"
                s.add_camera(n.id, lat, lon, limit, direction, kind)
            if name:
                place = t.get("place")
                if place in PLACE_IMPORTANCE:
                    s.add_place(name, "", place, lat, lon, PLACE_IMPORTANCE[place])
                    s.add_locality(name, place, lat, lon)
                elif any(k in t for k in POI_KEYS):
                    kind = next(t[k] for k in POI_KEYS if k in t)
                    s.add_place(name, t.get("addr:city", ""), kind, lat, lon, 20)
            s.note_postcode(t, lat, lon)

    def way(self, w):
        t = w.tags
        highway = t.get("highway")
        if highway not in DRIVABLE and "addr:postcode" not in t:
            return
        try:
            pts = [(nd.lat, nd.lon) for nd in w.nodes if nd.location.valid()]
        except osmium.InvalidLocationError:
            return
        if not pts:
            return
        lats = [p[0] for p in pts]
        lons = [p[1] for p in pts]
        bounds = (min(lats), max(lats), min(lons), max(lons))
        targets = [s for s in self.sinks if s.touches(*bounds)]
        if not targets:
            return

        kph = parse_kph(t["maxspeed"], highway) if highway in DRIVABLE and "maxspeed" in t else None
        lit = highway in DRIVABLE and t.get("lit", "").lower() in LIT_VALUES
        name = t.get("name") if highway in DRIVABLE else None
        mid = pts[len(pts) // 2]
        for s in targets:
            if len(pts) > 1:
                if kph:
                    s.add_way("limit", kph, pts, bounds)
                if lit:
                    s.add_way("lit", 1.0, pts, bounds)
            if name and s.has_point(*mid):
                s.note_street(name, mid[0], mid[1], t.get("addr:city", ""))
            if s.has_point(*pts[0]):
                s.note_postcode(t, pts[0][0], pts[0][1])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("pbf", nargs="+")
    ap.add_argument("--out")
    ap.add_argument("--region", action="append", default=[], help="ID:minLon,minLat,maxLon,maxLat:OUT.sqlite")
    ap.add_argument("--date", default="")
    args = ap.parse_args()

    sinks = []
    for spec in args.region:
        rid, bbox, path = spec.split(":", 2)
        sinks.append(Sink(rid, tuple(float(x) for x in bbox.split(",")), path, args.date))
    if args.out:
        sinks.append(Sink("all", (-180, -90, 180, 90), args.out, args.date))
    if not sinks:
        sys.exit("give --region or --out")

    handler = Handler(sinks)
    for pbf in args.pbf:
        # A sparse in-memory node index suits country-sized extracts whose node ids are scattered.
        handler.apply_file(pbf, locations=True, idx="sparse_mem_array")
    for s in sinks:
        s.finish()


if __name__ == "__main__":
    main()
