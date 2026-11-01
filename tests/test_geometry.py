import json
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

import app
import osd_sources
from geometry import normalize_geometry
import test_app as base_tests


OUTER = [[20, 50], [22, 50], [22, 52], [20, 52], [20, 50]]
HOLE = [[20.5, 50.5], [21.5, 50.5], [21.5, 51.5], [20.5, 51.5], [20.5, 50.5]]


def raw_event(geometry=None):
    return {"guid": "geometry-test", "shutdownType": 1,
            "polygon": {"poly": geometry or {"type": "Polygon", "coordinates": [OUTER, HOLE]},
                        "centroid": {"coordinates": [21, 51]}}}


def match(event, lon, lat):
    return app.canonical_matching_addresses(event, {"latitude": lat, "longitude": lon}, {}, 1000)


class GeometryTests(unittest.TestCase):
    def test_hole_and_boundaries_in_both_directions(self):
        for reverse in (False, True):
            rings = [list(reversed(r)) if reverse else r for r in [OUTER, HOLE]]
            event = osd_sources.normalize_energa(raw_event({"type": "Polygon", "coordinates": rings}))
            for lon, lat, expected in [(20.25, 50.25, True), (21, 51, False),
                                        (20, 51, True), (20, 50, True),
                                        (20.5, 51, False), (20.5, 50.5, False),
                                        (23, 53, False)]:
                with self.subTest(reverse=reverse, lon=lon, lat=lat):
                    self.assertEqual(match(event, lon, lat)[0], expected)

    def test_multipolygon_preserves_holes_and_separate_parts(self):
        second = [[24, 50], [25, 50], [25, 51], [24, 51], [24, 50]]
        event = osd_sources.normalize_energa(raw_event({"type": "MultiPolygon", "coordinates": [[OUTER, HOLE], [second]]}))
        for lon, lat, expected in [(20.1, 50.1, True), (21, 51, False), (24.5, 50.5, True), (23, 50.5, False)]:
            with self.subTest(lon=lon, lat=lat):
                self.assertEqual(match(event, lon, lat)[0], expected)

    def test_centroid_radius_and_text_do_not_override_polygon(self):
        event = osd_sources.normalize_energa(raw_event())
        event.update(radius_m=100000, description="Miasto Testowe")
        self.assertEqual(match(event, 21, 51)[:2], (False, "spatial_outside"))
        event["coordinates"] = [{"longitude": 23, "latitude": 53}]
        event["center"] = event["coordinates"][0]
        self.assertEqual(match(event, 23, 53)[:2], (False, "spatial_outside"))

    def test_missing_gps_cannot_fall_back_to_text_when_geometry_exists(self):
        event = osd_sources.normalize_energa(raw_event())
        event["description"] = "Miasto Testowe"
        result = app.canonical_matching_addresses(event, {}, {"city": "Miasto Testowe"}, 1000)
        self.assertEqual(result[:2], (False, "spatial_missing_gps"))


    def test_untyped_legacy_multi_and_holes_keep_their_structure(self):
        geometry = normalize_geometry({"coordinates": [[OUTER[:-1], HOLE[:-1]]]}, legacy_energa=True)
        self.assertEqual(geometry["type"], "MultiPolygon")
        self.assertEqual(geometry["coordinates"], [[OUTER, HOLE]])

    def test_malformed_declared_geometry_is_rejected(self):
        bad = [None, [], {}, {"type": [], "coordinates": [OUTER]}, {"type": "Point", "coordinates": [21, 51]},
               {"type": "Polygon", "coordinates": []},
               {"type": "MultiPolygon", "coordinates": [[]]},
               {"type": "Polygon", "coordinates": [OUTER[:-1]]},
               {"type": "Polygon", "coordinates": [OUTER, []]},
               {"type": "Polygon", "coordinates": [[[20, 50], [21, 51], [22, 52], [20, 50]]]}]
        for value in bad:
            with self.subTest(value=value), self.assertRaises(ValueError):
                normalize_geometry(value, legacy_energa=True)

    def test_invalid_points_never_silently_shrink_a_polygon(self):
        for coordinate in [True, "21", None, float("nan"), float("inf"), 181]:
            geometry = {"type": "Polygon", "coordinates": deepcopy([OUTER])}
            geometry["coordinates"][0][1][0] = coordinate
            with self.subTest(coordinate=coordinate), self.assertRaises(ValueError):
                osd_sources.normalize_energa(raw_event(geometry))

    def test_absent_geometry_allows_existing_point_matching(self):
        raw = raw_event()
        raw["polygon"].pop("poly")
        event = osd_sources.normalize_energa(raw)
        self.assertEqual(match(event, 21, 51)[:2], (True, "spatial_nearby"))
        self.assertNotIn("geometry", event)

    def test_legacy_independent_polygons_do_not_fall_back_to_centroid(self):
        ring = [{"longitude": x, "latitude": y} for x, y in OUTER]
        event = osd_sources.canonical_event("energa", "old", "outage", polygons=[ring],
                                           coordinates=[{"longitude": 23, "latitude": 53}])
        self.assertTrue(match(event, 20, 50)[0])
        self.assertEqual(match(event, 23, 53)[:2], (False, "spatial_outside"))

    def test_geometry_is_not_duplicated_per_matched_object(self):
        event = osd_sources.normalize_energa(raw_event())
        config = {"teryt_locations": [{"id": "site", "city_sym": "0123456", "latitude": 50.1, "longitude": 20.1}]}
        matched = app.normalize_source_events([event], config)
        self.assertEqual(list(matched), ["energa:geometry-test:object:site"])
        stored = next(iter(matched.values()))
        self.assertNotIn("geometry", stored["source_event"])
        self.assertNotIn("polygon", stored["source_event"]["raw"])
        self.assertIn("geometry", event)

    def test_only_energa_fingerprints_change_for_migration(self):
        config = {"teryt_locations": [{"id": "site", "city_sym": "0123456"}]}
        expected = {"site": app.event_hash({"matching_version": app.MATCHING_VERSION, "object": config["teryt_locations"][0]})}
        self.assertEqual(app.object_fingerprints(config, "pge"), expected)
        self.assertEqual(app.object_fingerprints(config, "tauron"), expected)
        self.assertNotEqual(app.object_fingerprints(config, "energa"), expected)


class GeometryScanTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.case = base_tests.ApiTests("test_status_endpoint")
        await self.case.asyncSetUp()

    async def asyncTearDown(self):
        await self.case.asyncTearDown()

    async def test_bad_geometry_keeps_previous_events_and_absence_counters(self):
        application = self.case.app
        old = {"old": {"source": "energa", "object_id": "0123456", "source_event_id": "old"}}
        application[app.STATE_KEY]["events"] = deepcopy(old)
        application[app.RUNTIME_KEY]["events"] = deepcopy(old)
        application[app.STATE_KEY]["missing"] = {"energa": {"old": {"count": 1, "reason": "not_published"}}}

        async def fetch(*args):
            return [osd_sources.normalize_energa(raw_event({"type": "Polygon", "coordinates": [OUTER, []]}))]

        with patch("app.fetch_source", side_effect=fetch):
            result = await app.scan_source(application, "energa")
        self.assertFalse(result["ok"])
        self.assertEqual(application[app.STATE_KEY]["events"], old)
        self.assertEqual(application[app.STATE_KEY]["missing"]["energa"]["old"]["count"], 1)
        self.assertIn("geometria", result["error"])
        saved = app.load_json(application[app.STATE_PATH_KEY], {})
        self.assertEqual(saved["events"], old)

    async def test_rule_migration_does_not_emit_false_departure(self):
        application = self.case.app
        application[app.CONFIG_KEY]["teryt_locations"][0].update(latitude=51, longitude=21)
        obj = application[app.CONFIG_KEY]["teryt_locations"][0]
        key = "energa:geometry-test:object:0123456"
        old = {key: {"event_key": key, "source": "energa", "object_id": "0123456", "source_event_id": "geometry-test", "status": "active", "source_hash": "old"}}
        application[app.STATE_KEY]["events"] = old
        application[app.STATE_KEY]["object_fingerprints"] = {"energa": {"0123456": app.event_hash({"matching_version": 2, "object": obj})}}
        with patch("app.fetch_source", return_value=[osd_sources.normalize_energa(raw_event())]), patch("app.schedule_notifications") as notify:
            result = await app.scan_source(application, "energa")
        self.assertTrue(result["ok"])
        self.assertEqual(application[app.STATE_KEY]["events"], {})
        self.assertEqual(application[app.STATE_KEY].get("history"), [])
        notify.assert_not_called()
