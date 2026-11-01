from policy_helpers import create_test_app
import json
import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import aiohttp
from aiohttp import web
from aiohttp.test_utils import AioHTTPTestCase, TestServer

import app
import osd_sources


def sample_event(*, revoked=False, description="Planowane prace"):
    return {
        "description": description,
        "id": 123,
        "uuid": "ABC-123",
        "type": 2,
        "regionIdentifier": "region-1",
        "regionName": "Region testowy",
        "startAt": "2026-08-22T08:00:00+02:00",
        "stopAt": "2026-08-22T12:00:00+02:00",
        "dateRanges": [
            {
                "startAt": "2026-08-22T08:00:00+02:00",
                "stopAt": "2026-08-22T12:00:00+02:00",
            }
        ],
        "revoked": revoked,
        "revokedDescription": "Odwołane przez PGE" if revoked else None,
        "addresses": [
            {
                "numbers": ["1", "2"],
                "teryt": {
                    "voivodeshipName": "Mazowieckie",
                    "voivodeshipCode": "14",
                    "countyName": "Powiat testowy",
                    "countyCode": "1401",
                    "communeName": "Gmina testowa",
                    "communeCode": "1401011",
                    "cityName": "Miasto Testowe",
                    "citySym": "0123456",
                    "streetName": "Energetyczna",
                    "streetSymUl": "11111",
                },
            },
            {
                "numbers": [],
                "teryt": {
                    "cityName": "Inne Miasto",
                    "citySym": "9999999",
                    "streetName": None,
                    "streetSymUl": None,
                },
            },
        ],
        "coordinates": [
            {"longitude": 21.01, "latitude": 52.21},
            {"longitude": 21.02, "latitude": 52.22},
        ],
        "updatedAt": "2026-08-21T18:30:00+02:00",
        "createdAt": "2026-08-20T10:00:00+02:00",
        "futurePgeField": {"kept": True},
    }


def sample_config():
    return {
        "scan_interval_seconds": 300,
        "request_timeout_seconds": 30,
        "web_host": "127.0.0.1",
        "web_port": 8080,
        "our_api_url": "",
        "teryt_locations": [
            {
                "id": "test-object",
                "city_sym": "0123456",
                "name": "Nadajnik testowy",
                "street_sym": "",
                "latitude": 52.21,
                "longitude": 21.01,
            },
        ],
    }


class AppLogicTests(unittest.TestCase):
    def test_time_phase_keeps_source_status_separate(self):
        now = datetime(2026, 9, 2, 12, 0, tzinfo=timezone.utc)
        base = {"status": "active", "periods": []}
        upcoming = {**base, "start_at": "2026-09-03T10:00:00Z", "stop_at": "2026-09-03T12:00:00Z"}
        ongoing = {**base, "start_at": "2026-09-02T10:00:00Z", "stop_at": "2026-09-02T14:00:00Z"}
        overdue = {**base, "start_at": "2026-09-02T08:00:00Z", "stop_at": "2026-09-02T10:00:00Z"}

        self.assertEqual(app.event_time_phase(upcoming, now), "upcoming")
        self.assertEqual(app.event_time_phase(ongoing, now), "ongoing")
        self.assertEqual(app.event_time_phase(overdue, now), "overdue")
        self.assertEqual(app.event_time_phase({**ongoing, "status": "cancelled"}, now), "closed")

    def test_naive_operator_time_is_treated_as_polish_time(self):
        now = datetime(2026, 9, 2, 10, 30, tzinfo=timezone.utc)
        event = {"status": "active", "start_at": "2026-09-02T12:00", "stop_at": "2026-09-02T13:00"}
        self.assertEqual(app.event_time_phase(event, now), "ongoing")

    def test_time_windows_are_normalized_and_validate_ranges(self):
        event = {
            "status": "active",
            "periods": [
                {"startAt": "2026-10-25 02:30:00", "stopAt": "2026-10-25 03:15:00"},
                {"fromDate": "2026-09-11T08:00:00+02:00", "toDate": "2026-09-11T07:00:00+02:00"},
                {"start": "nie-data", "end": "2026-09-12T08:00:00+02:00"},
            ],
        }

        windows = app.event_time_windows(event)

        self.assertEqual(len(windows), 3)
        self.assertEqual(windows[0]["start_at"], "2026-10-25T02:30:00+02:00")
        self.assertTrue(windows[0]["valid"])
        self.assertFalse(windows[1]["valid"])
        self.assertIsNone(windows[2]["start_at"])
        self.assertFalse(windows[2]["valid"])

    def test_public_event_exposes_time_windows_without_changing_existing_fields(self):
        event = {
            "source_hash": "hidden",
            "source_event": {"raw": {"secret": True}},
            "status": "active",
            "start_at": "2026-09-09 08:00:00",
            "stop_at": "2026-09-09 10:00:00",
        }

        result = app.public_event(event)

        self.assertNotIn("source_hash", result)
        self.assertEqual(result["source_event"]["raw"], {})
        self.assertEqual(result["source_status"], "active")
        self.assertEqual(result["time_windows"][0]["start_at"], "2026-09-09T08:00:00+02:00")
        self.assertTrue(result["time_windows"][0]["valid"])

    def test_matching_version_changes_object_fingerprint(self):
        config = sample_config()
        current = app.object_fingerprints(config)
        old = {
            str(obj["id"]): app.event_hash({"matching_version": 1, "object": obj})
            for obj in config["teryt_locations"]
        }

        self.assertNotEqual(current, old)
        self.assertNotEqual(app.object_fingerprints(config, "enea"), current)
        self.assertEqual(app.object_fingerprints(config, "pge"), current)

    def test_new_object_id_uses_uuid_and_does_not_reuse_deleted_numeric_id(self):
        with patch("app.uuid.uuid4", side_effect=["2", "new-uuid"]):
            self.assertEqual(app.next_object_id([{"id": "1"}, {"id": "2"}]), "new-uuid")

        self.assertNotEqual(app.next_object_id([{"id": "1"}]), "2")

    def test_review_object_does_not_use_uncertain_text_or_teryt(self):
        location = {
            "id": "review",
            "city_sym": "0123456",
            "latitude": 52.21,
            "longitude": 21.01,
            "location_audit": {"requires_review": True},
        }
        profile = {"city": "Miasto Testowe", "county_code": "1401"}
        text_only = osd_sources.canonical_event(
            "energa", "one", "outage", description="Miasto Testowe"
        )
        self.assertFalse(app.canonical_matching_addresses(text_only, location, profile, 1000)[0])

        near = osd_sources.canonical_event(
            "energa", "two", "outage",
            coordinates=[{"latitude": 52.2102, "longitude": 21.0102}],
        )
        matched = app.canonical_matching_addresses(near, location, profile, 1000)
        self.assertEqual(matched[:2], (True, "spatial_nearby"))

        pge_without_gps = osd_sources.normalize_pge(sample_event())
        pge_without_gps["coordinates"] = []
        self.assertFalse(app.canonical_matching_addresses(pge_without_gps, location, profile, 1000)[0])

    def test_normalize_keeps_full_pge_record_and_matching_location(self):
        raw = sample_event()
        normalized = app.normalize_events([raw], sample_config())

        self.assertEqual(list(normalized), ["abc-123:object:test-object"])
        event = normalized["abc-123:object:test-object"]
        self.assertEqual(event["object_id"], "test-object")
        self.assertEqual(event["type"], "planned_outage")
        self.assertEqual(event["city"], "Miasto Testowe")
        self.assertEqual(len(event["matched_locations"]), 1)
        self.assertEqual(event["matched_locations"][0]["numbers"], ["1", "2"])
        self.assertEqual(len(event["pge_event"]["addresses"]), 2)
        self.assertEqual(len(event["pge_event"]["coordinates"]), 2)
        self.assertTrue(event["pge_event"]["futurePgeField"]["kept"])

    def test_new_updated_cancelled_and_resolved_changes(self):
        current = app.normalize_events([sample_event()], sample_config())
        self.assertEqual(app.compute_changes({}, current)[0][0], "new")

        updated = app.normalize_events(
            [sample_event(description="Zmieniony opis")], sample_config()
        )
        self.assertEqual(app.compute_changes(current, updated)[0][0], "updated")

        cancelled = app.normalize_events(
            [sample_event(revoked=True)], sample_config()
        )
        self.assertEqual(app.compute_changes(current, cancelled)[0][0], "cancelled")

        self.assertEqual(app.compute_changes(current, {})[0][0], "resolved")

    def test_unchanged_event_does_not_create_notification(self):
        current = app.normalize_events([sample_event()], sample_config())
        self.assertEqual(app.compute_changes(current, current), [])

    def test_notification_contains_full_pge_data(self):
        event = next(iter(app.normalize_events([sample_event()], sample_config()).values()))
        payload = app.notification_payload("new", event)

        self.assertEqual(payload["city_sym"], "0123456")
        self.assertEqual(payload["action"], "new")
        self.assertEqual(payload["status"], "active")
        self.assertEqual(payload["pge_event"]["dateRanges"][0]["startAt"], "2026-08-22T08:00:00+02:00")
        self.assertEqual(payload["pge_event"]["coordinates"][1]["longitude"], 21.02)
        self.assertEqual(payload["matched_locations"][0]["teryt"]["countyName"], "Powiat testowy")

    def test_mass_outage_requires_explicit_signal(self):
        self.assertEqual(app.event_type({"type": 1}), "outage")
        self.assertEqual(app.event_type({"type": 99}), "unknown")
        self.assertEqual(app.event_type({"type": 1, "isMassOutage": True}), "mass_outage")

    def test_keyword_can_match_when_pge_has_no_expected_street_code(self):
        matches = app.matching_addresses(
            sample_event(),
            "0123456",
            street_sym="99999",
            keyword="Energetyczna",
        )
        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0]["teryt"]["streetSymUl"], "11111")

    def test_load_config_rejects_duplicate_ids_instead_of_renaming_them(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text(
                json.dumps(
                    {
                        "scan_interval_seconds": 5,
                        "teryt_locations": [
                            "0123456",
                            "0123456",
                            {"city_sym": "0765432", "name": "Druga"},
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "0123456.*powtórzony"):
                app.load_config(path)

    def test_load_config_rejects_invalid_item_with_index(self):
        with self.assertRaisesRegex(ValueError, "Obiekt błędny.*city_sym"):
            app.normalize_locations(["0123456", "błędny"])

    def test_coordinates_are_read_from_gps_and_google_maps_url(self):
        self.assertEqual(app.coordinates_from_text("53.778, 20.480"), (53.778, 20.48))
        self.assertEqual(
            app.coordinates_from_text("https://maps.google.com/@52.1001,21.2002,16z"),
            (52.1001, 21.2002),
        )
        self.assertIsNone(app.coordinates_from_text("Olsztyn, Klonowa 14"))

    def test_maps_url_parser_accepts_only_full_allowed_gps_links(self):
        self.assertEqual(
            app.coordinates_from_maps_url("https://www.google.com/maps/@52.1001,21.2002,16z"),
            (52.1001, 21.2002),
        )
        self.assertEqual(
            app.coordinates_from_maps_url("maps.google.com/?q=52.1001,21.2002"),
            (52.1001, 21.2002),
        )
        for value in (
            "http://maps.google.com/?q=52.1,21.2",
            "https://google.com.evil.test/maps/@52.1,21.2,16z",
            "https://user@maps.google.com/maps/@52.1,21.2,16z",
            "https://maps.google.com:444/maps/@52.1,21.2,16z",
            "https://maps.app.goo.gl/test",
            "file:///etc/passwd",
        ):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    app.coordinates_from_maps_url(value)

    def test_public_error_message_never_returns_empty_text(self):
        self.assertEqual(app.public_error_message(TimeoutError()), "TimeoutError")
        wrapped = RuntimeError("")
        wrapped.__cause__ = TimeoutError()
        self.assertEqual(app.public_error_message(wrapped), "RuntimeError: TimeoutError")

    def test_address_is_matched_to_local_teryt_and_street(self):
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "teryt.db"
            database = sqlite3.connect(database_path)
            database.executescript(
                """
                CREATE TABLE simc (sym TEXT PRIMARY KEY, nazwa TEXT, woj TEXT, pow TEXT, gmi TEXT);
                CREATE TABLE ulic (sym_ul TEXT, sym TEXT, cecha TEXT, nazwa_1 TEXT, nazwa_2 TEXT);
                INSERT INTO simc VALUES ('0123456', 'Miasto Testowe', '14', '01', '01');
                INSERT INTO ulic VALUES ('11111', '0123456', 'ul.', 'Energetyczna', '');
                """
            )
            database.commit()
            database.close()

            result = app.teryt_from_address(
                database_path,
                {
                    "city": "Miasto Testowe",
                    "road": "ulica Energetyczna",
                    "ISO3166-2-lvl4": "PL-14",
                },
            )

        self.assertEqual(result["city_sym"], "0123456")
        self.assertEqual(result["street_sym"], "11111")

    def test_canonical_pge_keeps_teryt_and_uses_namespaced_key(self):
        config = sample_config()
        config["location_profiles"] = {
            "test-object": {
                "city": "Miasto Testowe",
                "street": "",
                "county_code": "1401",
            }
        }
        canonical = osd_sources.normalize_pge(sample_event())
        normalized = app.normalize_source_events([canonical], config)

        self.assertEqual(
            list(normalized), ["pge:abc-123:object:test-object"]
        )
        event = next(iter(normalized.values()))
        self.assertEqual(event["source"], "pge")
        self.assertEqual(event["match_confidence"], "exact_teryt")
        self.assertEqual(event["pge_event"]["uuid"], "ABC-123")

    def test_spatial_event_matches_object_coordinates(self):
        config = sample_config()
        config["location_profiles"] = {
            "test-object": {"city": "Warszawa", "street": "", "county_code": "1465"}
        }
        config["spatial_match_radius_m"] = 1000
        event = osd_sources.canonical_event(
            "tauron",
            "event-1",
            "outage",
            description="Awaria",
            coordinates=[{"latitude": 52.2102, "longitude": 21.0102}],
            raw={"OutageId": "event-1"},
        )

        normalized = app.normalize_source_events([event], config)

        matched = next(iter(normalized.values()))
        self.assertEqual(matched["match_confidence"], "spatial_nearby")
        self.assertEqual(matched["operator"], "TAURON Dystrybucja")

    def test_far_geometry_does_not_fall_back_to_same_city_name(self):
        location = {
            "id": "gora",
            "city_sym": "0596240",
            "latitude": 52.117332,
            "longitude": 16.984294,
        }
        event = osd_sources.canonical_event(
            "tauron",
            "far-event",
            "outage",
            description="Kamienna Góra, ul. Śląska",
            coordinates=[{"latitude": 50.783, "longitude": 16.033}],
        )

        matched = app.canonical_matching_addresses(
            event, location, {"city": "Góra", "street": ""}, 1000
        )

        self.assertEqual(matched, (False, "spatial_outside", []))

    def test_tauron_repeated_id_keeps_all_windows_for_the_same_object(self):
        first = osd_sources.canonical_event(
            "tauron", "shared-id", "planned_outage",
            start_at="2026-09-10T08:00:00Z",
            stop_at="2026-09-10T10:00:00Z",
            coordinates=[{"latitude": 52.2101, "longitude": 21.0101}],
        )
        second = osd_sources.canonical_event(
            "tauron", "shared-id", "planned_outage",
            start_at="2026-09-12T12:00:00Z",
            stop_at="2026-09-12T14:00:00Z",
            coordinates=[{"latitude": 52.2102, "longitude": 21.0102}],
        )
        outside = osd_sources.canonical_event(
            "tauron", "shared-id", "planned_outage",
            start_at="2026-09-14T12:00:00Z",
            stop_at="2026-09-14T14:00:00Z",
            coordinates=[{"latitude": 50.0, "longitude": 19.0}],
        )

        forward = app.normalize_source_events([first, second, outside], sample_config())
        reverse = app.normalize_source_events([outside, second, first], sample_config())

        self.assertEqual(len(forward), 1)
        event = next(iter(forward.values()))
        self.assertEqual(len(event["periods"]), 2)
        self.assertEqual(event["periods"][0]["startAt"], "2026-09-10T08:00:00Z")
        self.assertEqual(event["periods"][1]["startAt"], "2026-09-12T12:00:00Z")
        self.assertEqual(event["source_hash"], next(iter(reverse.values()))["source_hash"])

    def test_enea_same_city_name_requires_correct_gps_commune_area(self):
        location = {
            "id": "gora-srem",
            "city_sym": "0596240",
            "latitude": 52.117332,
            "longitude": 16.984294,
        }
        profile = {"city": "Góra", "street": "", "gps_commune": "Śrem"}
        wrong_area = osd_sources.canonical_event(
            "enea", "wrong", "planned_outage",
            description="Obszar Góra — Góra ul. Poznańska 34",
        )
        correct_area = osd_sources.canonical_event(
            "enea", "correct", "planned_outage",
            description="Obszar Śrem — Góra ul. Poznańska 34",
        )

        self.assertEqual(
            app.canonical_matching_addresses(wrong_area, location, profile, 1000)[1],
            "area_mismatch",
        )
        self.assertTrue(
            app.canonical_matching_addresses(correct_area, location, profile, 1000)[0]
        )

    def test_enea_uses_description_not_area_header_for_matching(self):
        location = {"id": "dobra-malopolska", "city_sym": "0423172"}
        profile = {
            "city": "Dobra",
            "street": "",
            "gps_commune": "Dobra",
            "voivodeship_code": "12",
        }
        szczecin_event = osd_sources.canonical_event(
            "enea",
            "b7e223ad-dcf3-43be-9857-c515406cbfc2",
            "planned_outage",
            description="Obszar Dobra (Szczecińska), Szczecin — Szczecin, ul. Waniliowa 1, 3 do 14.",
            raw={
                "title": "Obszar Dobra (Szczecińska), Szczecin",
                "description": "Szczecin, ul. Waniliowa 1, 3 do 14.",
            },
        )
        dobra_szczecinska_event = osd_sources.canonical_event(
            "enea",
            "0975628f-20c1-44a4-a17e-7a436743f2b2",
            "planned_outage",
            description="Obszar Dobra (Szczecińska) — Dobra, ul. Sasankowa 9 do 13 nieparzyste.",
            raw={
                "title": "Obszar Dobra (Szczecińska)",
                "description": "Dobra, ul. Sasankowa 9 do 13 nieparzyste.",
            },
        )

        self.assertFalse(
            app.canonical_matching_addresses(szczecin_event, location, profile, 1000)[0]
        )
        self.assertEqual(
            app.canonical_matching_addresses(dobra_szczecinska_event, location, profile, 1000)[1],
            "area_mismatch",
        )

        zachodniopomorskie_profile = {**profile, "voivodeship_code": "32"}
        self.assertTrue(
            app.canonical_matching_addresses(
                dobra_szczecinska_event,
                {"id": "dobra-szczecinska", "city_sym": "0771234"},
                zachodniopomorskie_profile,
                1000,
            )[0]
        )

    def test_enea_header_city_does_not_match_when_detail_has_other_city(self):
        event = osd_sources.canonical_event(
            "enea",
            "aecc066f-921e-40f7-a2b2-3cc56b9e69a0",
            "planned_outage",
            description="Obszar Gryfice, Szczecin — Szczecin, al. Wojska Polskiego 64.",
            raw={
                "title": "Obszar Gryfice, Szczecin",
                "description": "Szczecin, al. Wojska Polskiego 64.",
            },
        )
        self.assertFalse(
            app.canonical_matching_addresses(
                event,
                {"id": "gryfice", "city_sym": "0979053"},
                {"city": "Gryfice", "street": "", "gps_commune": "Gryfice"},
                1000,
            )[0]
        )

    def test_enea_city_name_in_cadastral_district_or_street_is_not_a_match(self):
        dobra = osd_sources.canonical_event(
            "enea",
            "763c575e-f4f8-4dc8-bb8c-11af883f1511",
            "planned_outage",
            description=(
                "Obszar Buk dzisiaj — Gmina Buk obręb ewidencyjny Dobra "
                "miejscowość Sznyfin od 2 do 9."
            ),
            raw={
                "title": "Obszar Buk dzisiaj",
                "description": (
                    "Gmina Buk obręb ewidencyjny Dobra miejscowość Sznyfin "
                    "od 2 do 9."
                ),
            },
        )
        lesna = osd_sources.canonical_event(
            "enea",
            "street-lesna",
            "planned_outage",
            description="Obszar Żagań — Tomaszowo: ul. Leśna od 1 do 3.",
            raw={
                "title": "Obszar Żagań",
                "description": "Tomaszowo: ul. Leśna od 1 do 3.",
            },
        )

        self.assertFalse(
            app.canonical_matching_addresses(
                dobra,
                {"id": "dobra-malopolska", "city_sym": "0423172"},
                {
                    "city": "Dobra",
                    "street": "",
                    "gps_commune": "Dobra",
                    "voivodeship_code": "12",
                },
                1000,
            )[0]
        )
        self.assertFalse(
            app.canonical_matching_addresses(
                lesna,
                {"id": "lesna", "city_sym": "0000001"},
                {"city": "Leśna", "street": "", "gps_commune": "Leśna"},
                1000,
            )[0]
        )

    def test_enea_explicit_locality_still_matches(self):
        event = osd_sources.canonical_event(
            "enea",
            "wilkanowo",
            "planned_outage",
            description=(
                "Obszar Świdnica — miejscowości Wilkanowo ul. Komarnickiej 7a."
            ),
            raw={
                "title": "Obszar Świdnica",
                "description": "miejscowości Wilkanowo ul. Komarnickiej 7a.",
            },
        )

        self.assertTrue(
            app.canonical_matching_addresses(
                event,
                {"id": "wilkanowo", "city_sym": "0000002"},
                {"city": "Wilkanowo", "street": "", "gps_commune": "Świdnica"},
                1000,
            )[0]
        )

    def test_enea_html_parser_reads_uuid_dates_and_description(self):
        page = """
        <div class="info block unpl">
          <h4 class="other title_main">Obszar Zielona Góra</h4>
          <p class="bold subtext"><!-- 2026-08-24, w godz. 2026-08-24 08:00 - 2026-08-24 15:00 --></p>
          <p class="lead description">miejscowości <span>Zielona Góra</span> ul. Leśna 1</p>
          <!-- 6bef872f-08a2-4159-991a-52af025a8651 -->
        </div>
        """

        events = osd_sources.parse_enea_events(
            page, "planned_outage", "https://example.test/?rejon=1"
        )

        self.assertEqual(len(events), 1)
        self.assertEqual(
            events[0]["source_event_id"], "6bef872f-08a2-4159-991a-52af025a8651"
        )
        self.assertEqual(events[0]["start_at"], "2026-08-24T08:00")
        self.assertIn("Leśna 1", events[0]["description"])

    def test_source_specific_type_mappings(self):
        tauron = osd_sources.normalize_tauron(
            {
                "OutageId": "T-1",
                "TypeId": 1,
                "IsActive": True,
                "Message": "Prace planowe",
                "Coordinates": [{"lat": 50.0, "lng": 19.0}],
            }
        )
        energa = osd_sources.normalize_energa(
            {
                "guid": "E-1",
                "shutdownType": 1,
                "message": "Awaria",
                "polygon": {
                    "poly": {"coordinates": [[[19.0, 50.0], [19.1, 50.0], [19.1, 50.1]]]},
                    "centroid": {"coordinates": [19.05, 50.03]},
                },
            }
        )

        self.assertEqual(tauron["type"], "planned_outage")
        self.assertEqual(energa["type"], "outage")
        self.assertEqual(energa["geometry"]["type"], "Polygon")
        self.assertEqual(len(energa["geometry"]["coordinates"][0]), 4)
        self.assertEqual(
            osd_sources.normalize_energa({"guid": "E-2", "shutdownType": 2})["type"],
            "planned_outage",
        )
        self.assertEqual(
            osd_sources.normalize_energa({"guid": "E-3", "shutdownType": 3})["type"],
            "mass_outage",
        )

    def test_enea_index_selects_only_active_regions(self):
        index = """
        <a title="4 planowane wyłączenia" href="index.php?rejon=1">Zielona Góra</a>
        <a href="index.php?rejon=2" title="brak planowanych wyłączeń">Żary</a>
        """
        self.assertEqual(osd_sources.enea_active_regions(index), [1])


class NotificationSendTests(unittest.IsolatedAsyncioTestCase):
    async def test_send_notification_posts_complete_json(self):
        received = []

        async def receiver(request):
            received.append(await request.json())
            return web.json_response({"ok": True}, status=201)

        receiver_app = web.Application()
        receiver_app.router.add_post("/notifications", receiver)

        async with (
            TestServer(receiver_app) as server,
            aiohttp.ClientSession() as session,
        ):
            result = await app.send_notification(
                session,
                str(server.make_url("/notifications")),
                {"notification_id": "test-1", "pge_event": sample_event()},
            )

        self.assertEqual(result["result"], "sent")
        self.assertEqual(result["http_status"], 201)
        self.assertEqual(received[0]["pge_event"]["uuid"], "ABC-123")


class LocationResolveTests(unittest.IsolatedAsyncioTestCase):
    async def test_forbidden_url_is_rejected_before_any_request(self):
        class NoNetworkSession:
            def __init__(self):
                self.calls = 0

            def get(self, *_args, **_kwargs):
                self.calls += 1
                raise AssertionError("unexpected network request")

        session = NoNetworkSession()
        with self.assertRaises(ValueError):
            await app.coordinates_for_query(
                session,
                "https://google.com.evil.test/maps/@52.1001,21.2002,16z",
            )
        self.assertEqual(session.calls, 0)

        self.assertEqual(
            await app.coordinates_for_query(
                session,
                "https://maps.google.com/maps/@52.1001,21.2002,16z",
            ),
            (52.1001, 21.2002),
        )
        self.assertEqual(session.calls, 0)


class HttpHelperTests(unittest.IsolatedAsyncioTestCase):
    async def test_request_text_reports_empty_timeout_without_query_string(self):
        class TimeoutSession:
            def request(self, *_args, **_kwargs):
                raise TimeoutError()

        with self.assertRaises(RuntimeError) as context:
            await osd_sources._request_text(
                TimeoutSession(),
                "GET",
                "https://example.test/path?token=secret",
                retries=1,
            )
        message = str(context.exception)
        self.assertIn("TimeoutError", message)
        self.assertIn("https://example.test/path", message)
        self.assertNotIn("secret", message)

    async def test_request_text_does_not_retry_rate_limit(self):
        class Response:
            status = 429

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return False

            async def text(self):
                return "rate limited"

        class Session:
            calls = 0

            def request(self, *_args, **_kwargs):
                self.calls += 1
                return Response()

        session = Session()
        with self.assertRaisesRegex(osd_sources.SourceHttpError, "HTTP 429"):
            await osd_sources._request_text(
                session, "GET", "https://example.test/path", retries=3
            )
        self.assertEqual(session.calls, 1)


class ApiTests(AioHTTPTestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        from auth_helpers import login_admin
        await login_admin(self)

    async def get_application(self):
        self.temp_directory = tempfile.TemporaryDirectory()
        base = Path(self.temp_directory.name)
        config_path = base / "config.json"
        state_path = base / "state.json"
        data_directory = base / "data"
        data_directory.mkdir()
        database = sqlite3.connect(data_directory / "teryt.db")
        database.executescript(
            """
            CREATE TABLE simc (sym TEXT PRIMARY KEY, nazwa TEXT, woj TEXT, pow TEXT, gmi TEXT);
            CREATE TABLE ulic (
                sym_ul TEXT,
                sym TEXT,
                cecha TEXT,
                nazwa_1 TEXT,
                nazwa_2 TEXT,
                PRIMARY KEY (sym_ul, sym)
            );
            INSERT INTO simc VALUES ('0123456', 'Miasto Testowe', '14', '01', '01');
            INSERT INTO simc VALUES ('0765432', 'Drugie Miasto', '28', '02', '03');
            INSERT INTO ulic VALUES ('11111', '0123456', 'ul.', 'Energetyczna', '');
            """
        )
        database.commit()
        database.close()
        snut_directory = base / "snut"
        self.snut_directory = snut_directory
        (snut_directory / "resources").mkdir(parents=True)
        (snut_directory / "assets").mkdir()
        (snut_directory / "index.html").write_text(
            '<!doctype html><script src="/snut/resources/app-test.js"></script>',
            encoding="utf-8",
        )
        (snut_directory / "resources" / "app-test.js").write_text(
            "console.log('snut');", encoding="utf-8"
        )
        (snut_directory / "assets" / "config.yml").write_text(
            'title: "SNUT"\n', encoding="utf-8"
        )
        config_path.write_text(
            json.dumps(
                {
                    "teryt_locations": ["0123456"],
                    "scan_interval_seconds": 300,
                    "our_api_url": "",
                }
            ),
            encoding="utf-8",
        )
        return create_test_app(
            config_path,
            state_path,
            snut_dir=snut_directory,
            start_scanner=False,
        )

    async def asyncTearDown(self):
        await super().asyncTearDown()
        self.temp_directory.cleanup()

    def admin_headers(self, token=None):
        # Stare wywołania helpera pozostają zgodne, ale domyślnie testujemy bez klucza.
        return {"Authorization": f"Bearer {token}"} if token else {}

    async def test_status_endpoint(self):
        response = await self.client.get("/api/status")
        self.assertEqual(response.status, 200)
        payload = await response.json()
        self.assertEqual(payload["configured_locations"], 1)
        self.assertFalse(payload["notifications_enabled"])
        self.assertEqual(payload["storage"]["backend"], "sqlite")
        self.assertEqual(payload["storage"]["schema_version"], 3)
        self.assertGreaterEqual(payload["storage"]["revision"], 1)
        self.assertTrue(payload["storage"]["state_json_export_ok"])

    async def test_power_supply_write_preserves_events_audits_and_restart(self):
        from copy import deepcopy
        from unittest.mock import AsyncMock
        self.app[app.CONFIG_KEY]['enabled_sources'] = ['pge']
        with patch('app.fetch_source', AsyncMock(return_value=[osd_sources.normalize_pge(sample_event())])), patch('app.schedule_notifications'):
            self.assertTrue((await app.scan_source(self.app, 'pge'))['ok'])
        obj = self.app[app.CONFIG_KEY]['teryt_locations'][0]
        before = deepcopy(obj)
        visible_before = deepcopy(app.event_visibility(self.app))
        history_before = self.app[app.ENGINE_STORE_KEY].read_history()
        response = await self.client.patch(f"/api/admin/objects/{obj['id']}", json={'power_supply':{'generator':'szr','secondary_feed':'manual','notes':'Sprawdzić paliwo'}})
        self.assertEqual(response.status, 200)
        self.assertEqual(app.event_visibility(self.app), visible_before)
        self.assertEqual(self.app[app.ENGINE_STORE_KEY].read_history(), history_before)
        saved = app.load_objects(self.app[app.OBJECTS_PATH_KEY], [])
        self.assertEqual(saved[0]['power_supply']['generator'], 'szr')
        for field in ('id','latitude','longitude','city_sym','street_sym','location_audit','address_audit'):
            self.assertEqual(saved[0].get(field), before.get(field))
        response = await self.client.patch(f"/api/admin/objects/{obj['id']}", json={'power_supply':{'generator':'manual'}})
        self.assertEqual(response.status, 200)
        self.assertEqual((await response.json())['object']['power_supply']['secondary_feed'], 'manual')
        for path in ('/api/v1/objects?view=full', '/api/v1/locations', '/api/v1/map'):
            response = await self.client.get(path)
            payload = await response.json()
            rows = payload.get('items',payload.get('locations'))
            self.assertNotIn('power_supply', rows[0], path)

    async def test_power_supply_create_invalid_input_and_failed_save(self):
        from copy import deepcopy
        created = await self.client.post('/api/admin/objects', json={'city_sym':'0765432','power_supply':{'generator':'manual','notes':'Odpalić na miejscu'}})
        self.assertEqual(created.status, 201)
        object_id = (await created.json())['object']['id']
        for bad in (None, {'generator':'wrong'}, {'notes':True}, {'notes':'x'*1001}):
            response = await self.client.patch(f'/api/admin/objects/{object_id}',json={'power_supply':bad})
            self.assertEqual(response.status,400)
        before = deepcopy(self.app[app.CONFIG_KEY]['teryt_locations'])
        with patch('app.save_json',side_effect=OSError('disk full')):
            response = await self.client.patch(f'/api/admin/objects/{object_id}',json={'power_supply':{'generator':'szr'}})
        self.assertEqual(response.status,500)
        self.assertEqual(self.app[app.CONFIG_KEY]['teryt_locations'],before)

    async def test_power_supply_changes_public_snapshot_and_survives_location_tool(self):
        from tools.apply_location_corrections import prepare
        response = await self.client.get('/api/v1/objects?view=full&limit=1')
        old_snapshot = (await response.json())['snapshot_id']
        obj = self.app[app.CONFIG_KEY]['teryt_locations'][0]
        response = await self.client.patch(f"/api/admin/objects/{obj['id']}",json={'power_supply':{'generator':'szr','notes':'Sprawdzić paliwo'}})
        self.assertEqual(response.status,200)
        response = await self.client.get(f'/api/v1/objects?view=full&limit=1&snapshot_id={old_snapshot}')
        self.assertEqual(response.status,409)
        rows = self.app[app.CONFIG_KEY]['teryt_locations']
        corrected = prepare(rows,{'changes':[]},self.app[app.TERYT_DB_PATH_KEY])
        self.assertEqual(corrected[0]['power_supply'],rows[0]['power_supply'])

    async def test_snut_dashboard_and_assets(self):
        response = await self.client.get("/snut/")
        self.assertEqual(response.status, 200)
        self.assertIn("/snut/resources/app-test.js", await response.text())
        self.assertEqual(response.headers["Cache-Control"], "no-cache")

        asset = await self.client.get("/snut/resources/app-test.js")
        self.assertEqual(asset.status, 200)
        # Python 3.11 i nowsze wersje mają różne tablice MIME.
        self.assertIn(asset.content_type, {"text/javascript", "application/javascript"})
        self.assertIn("immutable", asset.headers["Cache-Control"])

        config = await self.client.get("/snut/assets/config.yml")
        self.assertEqual(config.status, 200)
        self.assertEqual(config.headers["Cache-Control"], "no-cache")

    async def test_snut_aliases_use_canonical_url(self):
        for path in ("/snut", "/SNUT", "/SNUT/"):
            response = await self.client.get(path, allow_redirects=False)
            self.assertEqual(response.status, 308)
            self.assertEqual(response.headers["Location"], "/snut/")

    async def test_readme_route_and_shared_assets(self):
        response = await self.client.get("/readme")
        self.assertEqual(response.status, 200)
        self.assertEqual(response.content_type, "text/html")
        text = await response.text()
        self.assertIn("Monitor OSD Community", text)
        self.assertIn("Monitor OSD Community", text)
        self.assertIn("/static/readme.css", text)
        self.assertNotIn("/static/script.js", text)

        redirect = await self.client.get("/readme/", allow_redirects=False)
        self.assertEqual(redirect.status, 308)
        self.assertEqual(redirect.headers["Location"], "/readme")

        readme_css = await self.client.get("/static/readme.css")
        self.assertEqual(readme_css.status, 200)
        self.assertIn(".readme-main", await readme_css.text())

        for path in ("/static/app-header.css", "/static/app-header.js"):
            asset = await self.client.get(path)
            self.assertEqual(asset.status, 200)
            self.assertIn("osd-navigation", await asset.text())

    async def test_snut_rejects_missing_and_traversal_paths(self):
        missing = await self.client.get("/snut/resources/missing.js")
        self.assertEqual(missing.status, 404)
        traversal = await self.client.get("/snut/%2e%2e/config.json")
        self.assertIn(traversal.status, {404, 400})

    async def test_missing_snut_build_does_not_break_api(self):
        self.snut_directory.rename(self.snut_directory.with_name("missing-snut"))
        response = await self.client.get("/snut/")
        self.assertEqual(response.status, 503)
        self.assertEqual(response.content_type, "text/html")
        self.assertIn("Brak zbudowanej wersji SNUT", await response.text())
        api_response = await self.client.get("/api/status")
        self.assertEqual(api_response.status, 200)
        readme_response = await self.client.get("/readme")
        self.assertEqual(readme_response.status, 200)

    async def test_events_endpoint_returns_full_record(self):
        self.app[app.CONFIG_KEY]["teryt_locations"] = app.normalize_locations(sample_config()["teryt_locations"])
        normalized = app.normalize_events([sample_event()], sample_config())
        self.app[app.RUNTIME_KEY]["events"] = normalized

        response = await self.client.get("/api/events?include_raw=1")
        self.assertEqual(response.status, 200)
        payload = await response.json()
        self.assertEqual(payload["count"], 1)
        self.assertEqual(payload["events"][0]["pge_event"]["uuid"], "ABC-123")
        self.assertNotIn("source_hash", payload["events"][0])

        compact_response = await self.client.get("/api/events")
        compact_payload = await compact_response.json()
        self.assertNotIn("pge_event", compact_payload["events"][0])

    async def test_teryt_search_and_object_crud(self):
        search = await self.client.get("/api/teryt/search?q=Drugie")
        self.assertEqual(search.status, 200)
        search_payload = await search.json()
        self.assertEqual(search_payload["results"][0]["city_sym"], "0765432")

        created = await self.client.post(
            "/api/objects",
            headers=self.admin_headers(),
            json={
                "name": "Nowy obiekt",
                "city_sym": "0765432",
                "latitude": 53.5,
                "longitude": 21.5,
            },
        )
        self.assertEqual(created.status, 201)
        created_payload = await created.json()

        listed = await self.client.get("/api/objects")
        listed_payload = await listed.json()
        self.assertEqual(listed_payload["count"], 2)
        self.assertEqual(listed_payload["objects"][1]["city"], "Drugie Miasto")

        deleted = await self.client.delete(
            f"/api/objects/{created_payload['object']['id']}",
            headers=self.admin_headers(),
        )
        self.assertEqual(deleted.status, 200)

    async def test_legacy_write_uses_authenticated_session(self):
        denied = await self.client.post(
            "/api/objects",
            json={"name": "Bez klucza", "city_sym": "0765432"},
        )
        self.assertEqual(denied.status, 201)

        wrong = await self.client.delete(
            "/api/objects/test-object",
            headers=self.admin_headers("wrong-token-" + ("x" * 32)),
        )
        self.assertEqual(wrong.status, 404)

        check = await self.client.get("/api/admin/check")
        self.assertEqual(check.status, 200)
        self.assertTrue((await check.json())["ok"])

    async def test_admin_routes_create_update_and_delete_without_changing_id(self):
        created = await self.client.post(
            "/api/admin/objects",
            headers=self.admin_headers(),
            json={"name": "Panel admina", "city_sym": "0765432"},
        )
        self.assertEqual(created.status, 201)
        object_id = (await created.json())["object"]["id"]

        updated = await self.client.patch(
            f"/api/admin/objects/{object_id}",
            headers=self.admin_headers(),
            json={"name": "Panel po zmianie", "keyword": "test"},
        )
        self.assertEqual(updated.status, 200)
        item = (await updated.json())["object"]
        self.assertEqual(item["id"], object_id)
        self.assertEqual(item["name"], "Panel po zmianie")
        self.assertEqual(item["keyword"], "test")

        denied = await self.client.patch(
            f"/api/admin/objects/{object_id}",
            json={"name": "Bez klucza"},
        )
        self.assertEqual(denied.status, 200)
        deleted = await self.client.delete(
            f"/api/admin/objects/{object_id}",
            headers=self.admin_headers(),
        )
        self.assertEqual(deleted.status, 200)

    async def test_write_uses_session_instead_of_bearer_header(self):
        denied = await self.client.post(
            "/api/objects",
            headers=self.admin_headers(),
            json={"name": "Bez sekretu", "city_sym": "0765432"},
        )
        self.assertEqual(denied.status, 201)

    async def test_public_write_still_validates_content_type(self):
        no_auth = await self.client.post(
            "/api/objects",
            data="{bad json",
            headers={"Content-Type": "text/plain"},
        )
        self.assertEqual(no_auth.status, 415)

        bad_type = await self.client.post(
            "/api/objects",
            data="{bad json",
            headers={**self.admin_headers(), "Content-Type": "text/plain"},
        )
        self.assertEqual(bad_type.status, 415)

    async def test_empty_objects_file_keeps_api_usable_and_allows_first_object(self):
        existing = await self.client.get("/api/objects")
        existing_payload = await existing.json()
        object_id = existing_payload["objects"][0]["id"]

        deleted = await self.client.delete(
            f"/api/objects/{object_id}",
            headers=self.admin_headers(),
        )
        self.assertEqual(deleted.status, 200)
        listed_empty = await self.client.get("/api/objects")
        listed_empty_payload = await listed_empty.json()
        self.assertEqual(listed_empty_payload["count"], 0)

        restarted = create_test_app(
            self.app[app.CONFIG_PATH_KEY],
            self.app[app.STATE_PATH_KEY],
            self.app[app.OBJECTS_PATH_KEY],
            self.app[app.TERYT_DB_PATH_KEY],
            snut_dir=self.snut_directory,
            start_scanner=False,
        )
        self.assertEqual(restarted[app.CONFIG_KEY]["teryt_locations"], [])
        await restarted[app.ENGINE_STORE_KEY].close()

        created = await self.client.post(
            "/api/objects",
            headers=self.admin_headers(),
            json={"name": "Pierwszy po pustej liście", "city_sym": "0765432"},
        )
        self.assertEqual(created.status, 201)
        created_payload = await created.json()
        self.assertEqual(created_payload["object"]["city_sym"], "0765432")

    async def test_failed_object_write_keeps_memory_and_disk_consistent(self):
        objects_path = self.app[app.OBJECTS_PATH_KEY]
        original_memory = list(self.app[app.CONFIG_KEY]["teryt_locations"])
        original_disk = (
            json.loads(objects_path.read_text(encoding="utf-8"))
            if objects_path.exists()
            else None
        )

        with patch.object(app, "save_json", side_effect=OSError("simulated disk full")):
            created = await self.client.post(
                "/api/objects",
                headers=self.admin_headers(),
                json={"name": "Nie zapisuj", "city_sym": "0765432"},
            )
        self.assertEqual(created.status, 500)
        self.assertEqual(self.app[app.CONFIG_KEY]["teryt_locations"], original_memory)
        if original_disk is None:
            self.assertFalse(objects_path.exists())
        else:
            self.assertEqual(json.loads(objects_path.read_text(encoding="utf-8")), original_disk)

        object_id = original_memory[0]["id"]
        with patch.object(app, "save_json", side_effect=OSError("simulated disk full")):
            updated = await self.client.patch(
                f"/api/admin/objects/{object_id}",
                headers=self.admin_headers(),
                json={"name": "Nie zapisuj zmiany"},
            )
        self.assertEqual(updated.status, 500)
        self.assertEqual(self.app[app.CONFIG_KEY]["teryt_locations"], original_memory)
        if original_disk is None:
            self.assertFalse(objects_path.exists())
        else:
            self.assertEqual(json.loads(objects_path.read_text(encoding="utf-8")), original_disk)

        with patch.object(app, "save_json", side_effect=OSError("simulated disk full")):
            deleted = await self.client.delete(
                f"/api/objects/{object_id}",
                headers=self.admin_headers(),
            )
        self.assertEqual(deleted.status, 500)
        self.assertEqual(self.app[app.CONFIG_KEY]["teryt_locations"], original_memory)
        if original_disk is None:
            self.assertFalse(objects_path.exists())
        else:
            self.assertEqual(json.loads(objects_path.read_text(encoding="utf-8")), original_disk)

    async def test_catalog_edit_during_fetch_rematches_without_second_http(self):
        self.app[app.CONFIG_KEY]["enabled_sources"] = ["pge"]
        async def edited_fetch(_session, _source, _config):
            async with self.app[app.OBJECTS_LOCK_KEY]:
                self.app[app.CONFIG_KEY]["teryt_locations"][0]["name"] = "Aktualna nazwa"
            return [osd_sources.normalize_pge(sample_event())]
        with patch("app.fetch_source", side_effect=edited_fetch) as fetch:
            result = await app.perform_scan(self.app)
        self.assertTrue(result["ok"])
        self.assertEqual(fetch.call_count, 1)
        event = next(iter(self.app[app.STATE_KEY]["events"].values()))
        self.assertEqual(event["configured_name"], "Aktualna nazwa")
        self.assertEqual(self.app[app.STATE_KEY]["object_fingerprints"]["pge"], app.object_fingerprints(self.app[app.CONFIG_KEY], "pge"))
        self.assertEqual(self.app[app.RUNTIME_KEY]["sources"]["pge"]["consecutive_errors"], 0)
        self.assertFalse(self.app[app.OBJECTS_LOCK_KEY].locked())

    async def test_continuous_catalog_edits_are_bounded_and_not_provider_backoff(self):
        self.app[app.CONFIG_KEY]["enabled_sources"] = ["pge"]
        previous = {"old": {"source": "pge", "object_id": "0123456"}}
        self.app[app.STATE_KEY]["events"] = previous.copy()
        normalized = app.normalize_source_events
        attempts = []
        def edited_match(events, config):
            attempts.append(1)
            self.app[app.CONFIG_KEY]["teryt_locations"][0]["name"] = f"Zmiana {len(attempts)}"
            return normalized(events, config)
        with patch("app.fetch_source", return_value=[osd_sources.normalize_pge(sample_event())]) as fetch, patch("app.normalize_source_events", side_effect=edited_match):
            result = await app.perform_scan(self.app)
        self.assertFalse(result["ok"])
        self.assertEqual(fetch.call_count, 1)
        self.assertEqual(len(attempts), 3)
        self.assertEqual(self.app[app.STATE_KEY]["events"], previous)
        health = self.app[app.RUNTIME_KEY]["sources"]["pge"]
        self.assertEqual(health["error_category"], "catalog_changed")
        self.assertEqual(health["consecutive_errors"], 0)
        self.assertTrue(health["last_attempt_at"])
        self.assertFalse(self.app[app.OBJECTS_LOCK_KEY].locked())

    async def test_failed_source_keeps_previous_events_and_reports_stale_state(self):
        self.app[app.CONFIG_KEY]["enabled_sources"] = ["pge"]
        previous = app.normalize_source_events(
            [osd_sources.normalize_pge(sample_event())],
            {
                **self.app[app.CONFIG_KEY],
                "location_profiles": {
                    "0123456": {
                        "city": "Miasto Testowe",
                        "street": "",
                        "county_code": "1401",
                    }
                },
            },
        )
        self.app[app.STATE_KEY]["events"] = previous
        self.app[app.RUNTIME_KEY]["events"] = previous

        async def failing_fetch(_session, _source, _config):
            raise RuntimeError("źródło testowo niedostępne")

        with patch("app.fetch_source", side_effect=failing_fetch):
            result = await app.perform_scan(self.app)

        self.assertFalse(result["ok"])
        self.assertEqual(self.app[app.RUNTIME_KEY]["events"], previous)
        self.assertIn("testowo niedostępne", self.app[app.RUNTIME_KEY]["sources"]["pge"]["error"])
        health = self.app[app.RUNTIME_KEY]["sources"]["pge"]
        self.assertEqual(health["consecutive_errors"], 1)
        self.assertTrue(health["last_error_at"])
        saved = json.loads(self.app[app.STATE_PATH_KEY].read_text(encoding="utf-8"))
        self.assertEqual(saved["sources"]["pge"]["consecutive_errors"], 1)

    async def test_provider_error_after_catalog_error_starts_its_own_counter(self):
        self.app[app.CONFIG_KEY]["enabled_sources"] = ["pge"]
        self.app[app.RUNTIME_KEY]["sources"]["pge"].update(error=app.LEGACY_CATALOG_CHANGE_ERROR, consecutive_errors=1)
        with patch("app.fetch_source", side_effect=TimeoutError):
            await app.perform_scan(self.app)
        health = self.app[app.RUNTIME_KEY]["sources"]["pge"]
        self.assertEqual(health["consecutive_errors"], 1)
        self.assertEqual(health["error_category"], "source")
        self.assertEqual(app.source_effective_interval("pge", health, 3600), 7200)

    async def test_empty_source_exception_is_reported_with_type(self):
        self.app[app.CONFIG_KEY]["enabled_sources"] = ["pge"]

        async def failing_fetch(_session, _source, _config):
            raise TimeoutError()

        with patch("app.fetch_source", side_effect=failing_fetch):
            result = await app.perform_scan(self.app)

        self.assertFalse(result["ok"])
        health = self.app[app.RUNTIME_KEY]["sources"]["pge"]
        self.assertEqual(health["error"], "TimeoutError")
        self.assertIn("PGE Dystrybucja: TimeoutError", self.app[app.RUNTIME_KEY]["error"])
        saved = json.loads(self.app[app.STATE_PATH_KEY].read_text(encoding="utf-8"))
        self.assertEqual(saved["sources"]["pge"]["error"], "TimeoutError")

    async def test_source_recovery_resets_counter_and_keeps_error_history(self):
        self.app[app.CONFIG_KEY]["enabled_sources"] = ["pge"]
        self.app[app.RUNTIME_KEY]["sources"]["pge"].update(
            consecutive_errors=2,
            last_error="poprzedni błąd",
            last_error_at="2026-09-02T10:00:00+02:00",
        )

        async def successful_fetch(_session, _source, _config):
            return [osd_sources.normalize_pge(sample_event())]

        with patch("app.fetch_source", side_effect=successful_fetch):
            result = await app.perform_scan(self.app)

        self.assertTrue(result["ok"])
        health = self.app[app.RUNTIME_KEY]["sources"]["pge"]
        self.assertEqual(health["consecutive_errors"], 0)
        self.assertEqual(health["last_error"], "poprzedni błąd")
        self.assertTrue(health["recovered_at"])
        saved = json.loads(self.app[app.STATE_PATH_KEY].read_text(encoding="utf-8"))
        self.assertEqual(saved["sources"]["pge"]["last_error"], "poprzedni błąd")

    async def test_events_endpoint_adds_time_phase_without_changing_source_status(self):
        normalized = app.normalize_source_events(
            [
                osd_sources.canonical_event(
                    "energa", "future", "outage",
                    start_at="2099-01-01T08:00:00Z",
                    stop_at="2099-01-01T10:00:00Z",
                    description="Miasto Testowe",
                )
            ],
            {
                **self.app[app.CONFIG_KEY],
                "location_profiles": {
                    "0123456": {"city": "Miasto Testowe", "street": ""}
                },
            },
        )
        self.app[app.RUNTIME_KEY]["events"] = normalized
        response = await self.client.get("/api/events")
        payload = await response.json()
        self.assertEqual(payload["events"][0]["source_status"], "active")
        self.assertEqual(payload["events"][0]["time_phase"], "upcoming")


if __name__ == "__main__":
    unittest.main()
