#!/usr/bin/env python3
"""Build index.sqlite for one region from an OSM .pbf.

Tables
  meta(key, value)
  cameras(id, lat, lon, limit_kph, direction, kind)
  ways(id, kind, value, geom)        kind 'limit' (value = km/h) or 'lit' (value = 1); geom = polyline6
  way_rtree(id, min_lat, max_lat, min_lon, max_lon)
  localities(name, kind, lat, lon, importance)   towns/villages/suburbs, for "where am I"
  places                             FTS5 gazetteer: places, named streets, postcodes, POIs

Usage: build_index.py --out index.sqlite [--date YYYY-MM-DD] region.osm.pbf [more.osm.pbf ...]
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


class Handler(osmium.SimpleHandler):
    def __init__(self, db):
        super().__init__()
        self.db = db
        self.postcodes = {}   # postcode -> [sum_lat, sum_lon, n]
        self.streets = {}     # (name, lat cell, lon cell) -> (name, lat, lon, locality)
        self.grid = {}        # (lat*10, lon*10) -> [(lat, lon, name)] of towns/villages, to label streets
        self.counts = {"cameras": 0, "limit": 0, "lit": 0, "places": 0}

    # -- nodes: cameras, places, POIs, postcodes
    def node(self, n):
        if not n.location.valid():
            return
        t = n.tags
        lat, lon = n.location.lat, n.location.lon
        if t.get("highway") == "speed_camera" or t.get("enforcement") in ("maxspeed", "average_speed"):
            limit = parse_kph(t.get("maxspeed", ""))
            direction = parse_direction(t.get("direction") or t.get("camera:direction") or "")
            kind = "average" if t.get("enforcement") == "average_speed" else "fixed"
            self.db.execute("INSERT OR REPLACE INTO cameras VALUES (?,?,?,?,?,?)",
                            (n.id, lat, lon, limit, direction, kind))
            self.counts["cameras"] += 1
        name = t.get("name")
        if name:
            place = t.get("place")
            if place in PLACE_IMPORTANCE:
                self.add_place(name, "", place, lat, lon, PLACE_IMPORTANCE[place])
                self.db.execute("INSERT INTO localities VALUES (?,?,?,?,?)",
                                (name, place, lat, lon, PLACE_IMPORTANCE[place]))
                if place in ("city", "town", "village", "suburb", "hamlet"):
                    self.grid.setdefault((int(lat * 10), int(lon * 10)), []).append((lat, lon, name))
            elif any(k in t for k in POI_KEYS):
                kind = next(t[k] for k in POI_KEYS if k in t)
                self.add_place(name, t.get("addr:city", ""), kind, lat, lon, 20)
        self.note_postcode(t, lat, lon)

    # -- ways: limits, lit, named streets, postcodes
    def way(self, w):
        t = w.tags
        highway = t.get("highway")
        pts = []
        if highway in DRIVABLE or "addr:postcode" in t:
            try:
                pts = [(nd.lat, nd.lon) for nd in w.nodes if nd.location.valid()]
            except osmium.InvalidLocationError:
                return
        if not pts:
            return
        if highway in DRIVABLE:
            if len(pts) > 1:
                kph = parse_kph(t.get("maxspeed", ""), highway) if "maxspeed" in t else None
                if kph:
                    self.add_way("limit", kph, pts)
                    self.counts["limit"] += 1
                if t.get("lit", "").lower() in LIT_VALUES:
                    self.add_way("lit", 1.0, pts)
                    self.counts["lit"] += 1
            name = t.get("name")
            if name:
                mid = pts[len(pts) // 2]
                key = (name.lower(), round(mid[0], 2), round(mid[1], 2))
                if key not in self.streets:
                    self.streets[key] = (name, mid[0], mid[1], t.get("addr:city", ""))
        self.note_postcode(t, pts[0][0], pts[0][1])

    def add_way(self, kind, value, pts):
        cur = self.db.execute("INSERT INTO ways(kind, value, geom) VALUES (?,?,?)",
                              (kind, value, encode_polyline(pts)))
        lats = [p[0] for p in pts]
        lons = [p[1] for p in pts]
        self.db.execute("INSERT INTO way_rtree VALUES (?,?,?,?,?)",
                        (cur.lastrowid, min(lats), max(lats), min(lons), max(lons)))

    def add_place(self, name, locality, kind, lat, lon, importance):
        self.db.execute("INSERT INTO places(name, locality, kind, lat, lon, importance) VALUES (?,?,?,?,?,?)",
                        (name, locality, kind, lat, lon, importance))
        self.counts["places"] += 1

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
            locality = locality or self.nearest_locality(lat, lon)
            self.add_place(name, locality, "street", lat, lon, 30)
        for pc, (sl, so, n) in self.postcodes.items():
            self.add_place(pc, "", "postcode", sl / n, so / n, 35)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("pbf", nargs="+")
    ap.add_argument("--out", required=True)
    ap.add_argument("--date", default="")
    args = ap.parse_args()

    db = sqlite3.connect(args.out)
    db.executescript("""
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
    h = Handler(db)
    for pbf in args.pbf:
        h.apply_file(pbf, locations=True, idx="flex_mem")
    h.finish()
    db.execute("INSERT INTO meta VALUES ('schema', ?)", (str(SCHEMA_VERSION),))
    db.execute("INSERT INTO meta VALUES ('date', ?)", (args.date,))
    db.commit()
    db.execute("VACUUM")
    db.close()
    print("index built:", h.counts, "postcodes:", len(h.postcodes), "streets:", len(h.streets), file=sys.stderr)


if __name__ == "__main__":
    main()
