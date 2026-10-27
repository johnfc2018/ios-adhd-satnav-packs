import json
import os
import subprocess
import sys
import tempfile
import unittest

TOOLS = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, TOOLS)

import check_coverage  # noqa: E402
import make_manifest  # noqa: E402
import split_tiles  # noqa: E402


class SplitTilesTests(unittest.TestCase):
    def test_parse_tile_path(self):
        self.assertEqual(split_tiles.parse_tile_path("2/000/753/001.gph"), (2, 753001))
        self.assertEqual(split_tiles.parse_tile_path("0/000/123.gph"), (0, 123))
        with self.assertRaises(ValueError):
            split_tiles.parse_tile_path("2/000/753/001.txt")

    def test_tile_bbox_level2_is_quarter_degree(self):
        # Row 0, column 0 is the bottom-left corner of the world.
        self.assertEqual(split_tiles.tile_bbox(2, 0), (-180.0, -90.0, -179.75, -89.75))
        box = split_tiles.tile_bbox(2, 1440 * 4 + 5)
        self.assertAlmostEqual(box[0], -180 + 5 * 0.25)
        self.assertAlmostEqual(box[1], -90 + 4 * 0.25)

    def test_intersects(self):
        self.assertTrue(split_tiles.intersects((0, 0, 1, 1), (0.5, 0.5, 2, 2)))
        self.assertFalse(split_tiles.intersects((0, 0, 1, 1), (2, 2, 3, 3)))


class ManifestTests(unittest.TestCase):
    def test_adds_and_replaces_regions(self):
        with tempfile.TemporaryDirectory() as d:
            pack = os.path.join(d, "index.sqlite")
            with open(pack, "wb") as f:
                f.write(b"hello")
            manifest = os.path.join(d, "regions.json")
            args = [sys.executable, os.path.join(TOOLS, "make_manifest.py"), manifest, "--id", "a", "--name", "A",
                    "--bbox=-1,50,0,51", "--date", "2026-10-01", "--base-url", "https://example.com/r", f"index={pack}"]
            subprocess.run(args, check=True)
            subprocess.run(args, check=True)   # same id again replaces
            data = json.load(open(manifest))
            self.assertEqual(data["schemaVersion"], make_manifest.SCHEMA_VERSION)
            self.assertEqual(len(data["regions"]), 1)
            f0 = data["regions"][0]["files"][0]
            self.assertEqual(f0["bytes"], 5)
            self.assertEqual(f0["sha256"], make_manifest.sha256(pack))
            self.assertEqual(f0["url"], "https://example.com/r/index.sqlite")


class CoverageTests(unittest.TestCase):
    def test_uk_set_has_no_gaps(self):
        with open(os.path.join(TOOLS, "sets", "uk.json")) as f:
            boxes = [check_coverage.parse_box(r["bbox"]) for r in json.load(f)["regions"]]
        self.assertEqual(check_coverage.uncovered(boxes, check_coverage.PLACES), [])

    def test_reports_gaps(self):
        boxes = [(-1.0, 50.0, 0.0, 51.0)]
        self.assertEqual(check_coverage.uncovered(boxes, [("in", -0.5, 50.5), ("out", 3.0, 50.5)]), ["out"])

    def test_every_region_id_is_unique_and_has_bbox(self):
        with open(os.path.join(TOOLS, "sets", "uk.json")) as f:
            regions = json.load(f)["regions"]
        ids = [r["id"] for r in regions]
        self.assertEqual(len(ids), len(set(ids)))
        for r in regions:
            lon0, lat0, lon1, lat1 = check_coverage.parse_box(r["bbox"])
            self.assertLess(lon0, lon1)
            self.assertLess(lat0, lat1)


if __name__ == "__main__":
    unittest.main()
