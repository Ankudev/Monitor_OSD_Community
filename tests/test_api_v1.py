from policy_helpers import create_test_app
import json
from copy import deepcopy
import asyncio
import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch
from urllib.parse import quote

from aiohttp.test_utils import AioHTTPTestCase

import app
import osd_sources
from engine_store import history_record


class PublicApiV1Tests(AioHTTPTestCase):
    async def get_application(self):
        self.env_patcher = patch.dict(os.environ, {"OSD_ADMIN_TOKEN": "test-admin-token-" + "x" * 32})
        self.env_patcher.start()
        self.temp_directory = tempfile.TemporaryDirectory()
        base = Path(self.temp_directory.name)
        self.base = base
        config_path = base / "config.json"
        state_path = base / "state.json"
        objects_path = base / "objects.json"
        data = base / "data"
        data.mkdir()
        database = sqlite3.connect(data / "teryt.db")
        database.executescript(
            """
            CREATE TABLE simc (sym TEXT PRIMARY KEY, nazwa TEXT, woj TEXT, pow TEXT, gmi TEXT);
            CREATE TABLE ulic (sym_ul TEXT, sym TEXT, cecha TEXT, nazwa_1 TEXT, nazwa_2 TEXT, PRIMARY KEY (sym_ul, sym));
            INSERT INTO simc VALUES ('0123456', 'Miasto Testowe', '14', '01', '01');
            INSERT INTO ulic VALUES ('11111', '0123456', 'ul.', 'Energetyczna', '');
            """
        )
        database.commit()
        database.close()
        config_path.write_text(
            json.dumps(
                {
                    "scan_interval_seconds": 300,
                    "enabled_sources": ["pge"],
                    "source_scan_intervals_seconds": {"pge": 300},
                    "our_api_url": "",
                }
            ),
            encoding="utf-8",
        )
        objects_path.write_text(
            json.dumps(
                [
                    {
                        "id": "dom-1",
                        "name": "Dom",
                        "city_sym": "0123456",
                        "street_sym": "11111",
                        "keyword": "1",
                        "latitude": 52.1,
                        "longitude": 21.0,
                    },
                    {
                        "id": "warsztat-2",
                        "name": "Warsztat",
                        "city_sym": "0123456",
                        "street_sym": "11111",
                        "keyword": "2",
                    },
                ]
            ),
            encoding="utf-8",
        )
        state_path.write_text(json.dumps({"saved_at": None, "events": {}, "history": []}), encoding="utf-8")
        return create_test_app(
            config_path,
            state_path,
            objects_path=objects_path,
            teryt_db_path=data / "teryt.db",
            start_scanner=False,
        )

    async def test_geoportal_read_and_post_with_session(self):
        response = await self.client.get('/api/v1/objects/dom-1/location-review')
        self.assertEqual(response.status, 200)
        self.assertNotIn('Access-Control-Allow-Origin', response.headers)
        self.assertEqual((await response.json())['item']['status'], 'not_checked')
        obj = self.app[app.CONFIG_KEY]['teryt_locations'][0]
        from address_matching import address_binding
        report = {'object_id': obj['id'], 'binding': address_binding(obj), 'version': 1,
                  'checked_at': datetime.now(timezone.utc).isoformat(), 'status': 'inconclusive',
                  'commune': {'expected_code': app.expected_commune(self.base/'data/teryt.db', obj)}}
        with patch.object(self.app[app.GEOPORTAL_KEY], 'compare', AsyncMock(return_value=(report, []))) as compare:
            before = self.app[app.STATE_KEY].copy()
            response = await self.client.post('/api/location/check', json={'object_id': 'dom-1'})
            self.assertEqual(response.status, 200)
            response = await self.client.post('/api/location/check', json={'object_id': 'dom-1'})
            self.assertTrue((await response.json())['cached'])
            self.assertEqual(compare.await_count, 1)
            self.assertEqual(before, self.app[app.STATE_KEY])
        response = await self.client.options('/api/location/check')
        self.assertNotIn('Access-Control-Allow-Origin', response.headers)

    async def test_geoportal_binding_changed_during_request(self):
        obj = self.app[app.CONFIG_KEY]['teryt_locations'][0]
        from address_matching import address_binding
        report = {'object_id': obj['id'], 'binding': address_binding(obj), 'version': 1}
        async def changed(*args):
            obj['latitude'] = 53
            return report, []
        with patch.object(self.app[app.GEOPORTAL_KEY], 'compare', changed):
            response = await self.client.post('/api/location/check', json={'object_id': 'dom-1'})
            self.assertEqual(response.status, 409)
        response = await self.client.get('/api/v1/objects/dom-1/location-review')
        self.assertEqual((await response.json())['item']['status'], 'not_checked')

    async def test_geoportal_no_public_url_proxy(self):
        response = await self.client.post('/api/location/check', json={'object_id': 'dom-1', 'url': 'http://localhost'})
        self.assertEqual(response.status, 400)
        self.assertNotIn('Access-Control-Allow-Origin', response.headers)

    async def asyncSetUp(self):
        await super().asyncSetUp()
        from auth_helpers import login_admin
        await login_admin(self)
        now = datetime.now(timezone.utc)
        start = (now + timedelta(hours=1)).isoformat()
        stop = (now + timedelta(hours=3)).isoformat()
        event_id = "pge:evt-1:object:dom-1"
        event = {
            "event_key": event_id,
            "source": "pge",
            "operator": "PGE Dystrybucja",
            "source_event_id": "evt-1",
            "pge_uuid": "evt-1",
            "object_id": "dom-1",
            "type": "planned_outage",
            "status": "active",
            "description": "Planowane wyłączenie",
            "city_sym": "0123456",
            "city": "Miasto Testowe",
            "configured_name": "Dom",
            "street_sym": "11111",
            "start_at": start,
            "stop_at": stop,
            "periods": [{"startAt": start, "stopAt": stop}],
            "source_event": {"raw": {}},
            "pge_event": {"uuid": "evt-1", "privateShape": [1, 2, 3]},
        }
        runtime = self.app[app.RUNTIME_KEY]
        runtime["events"] = {event_id: event}
        success = now.isoformat()
        runtime["last_success_at"] = success
        runtime["sources"]["pge"].update(
            last_attempt_at=success,
            last_success_at=success,
            source_event_count=7,
            matched_event_count=1,
            error=None,
        )
        self.app[app.STATE_KEY].update(
            saved_at=success,
            events={event_id: event},
            history=[{"at": success, "action": "not_published", "event": event}],
            sources=runtime['sources'],
            object_fingerprints={'pge': app.object_fingerprints(self.app[app.CONFIG_KEY], 'pge')},
        )
        await app.persist_state(self.app, self.app[app.STATE_KEY], [history_record('not_published', event, None, success)])

    async def asyncTearDown(self):
        await super().asyncTearDown()
        self.temp_directory.cleanup()
        self.env_patcher.stop()

    async def test_index_summary_and_sources_are_public(self):
        for path in ("/api/v1/", "/api/v1/summary", "/api/v1/sources"):
            response = await self.client.get(path)
            self.assertEqual(response.status, 200)
            self.assertEqual(response.headers["Access-Control-Allow-Origin"], "*")
            self.assertEqual(response.headers["Cache-Control"], "public, no-cache")
            self.assertIn("ETag", response.headers)
        payload = await (await self.client.get("/api/v1/summary")).json()
        self.assertEqual(payload["data_status"], "fresh")
        self.assertEqual(payload["event_count"], 1)

    async def test_etag_head_and_options(self):
        first = await self.client.get("/api/v1/events")
        etag = first.headers["ETag"]
        cached = await self.client.get("/api/v1/events", headers={"If-None-Match": etag})
        self.assertEqual(cached.status, 304)
        self.assertEqual(await cached.read(), b"")
        head = await self.client.head("/api/v1/events")
        self.assertEqual(head.status, 200)
        self.assertEqual(await head.read(), b"")
        options = await self.client.options("/api/v1/events")
        self.assertEqual(options.status, 204)
        self.assertIn("If-None-Match", options.headers["Access-Control-Allow-Headers"])

    async def test_events_filters_detail_and_raw_scope(self):
        response = await self.client.get("/api/v1/events?source=pge&time_phase=upcoming&view=full")
        payload = await response.json()
        self.assertEqual(payload["total"], 1)
        self.assertNotIn("raw", payload["items"][0])
        event_id = quote(payload["items"][0]["event_id"], safe="")
        detail = await (await self.client.get(f"/api/v1/events/{event_id}?include_raw=1")).json()
        self.assertEqual(detail["item"]["raw_scope"], "full_record")
        self.assertEqual(detail["item"]["raw"]["uuid"], "evt-1")

    async def test_pagination_snapshot_and_projection(self):
        first = await (await self.client.get("/api/v1/objects?limit=1&fields=name")).json()
        self.assertEqual(first["count"], 1)
        self.assertEqual(set(first["items"][0]), {"object_id", "name"})
        self.assertIn("snapshot_id=", first["next"])
        second = await self.client.get(first["next"])
        self.assertEqual(second.status, 200)
        changed = await self.client.get("/api/v1/objects?limit=1&snapshot_id=wrong")
        self.assertEqual(changed.status, 409)

    async def test_locations_return_complete_catalog_without_pagination(self):
        response = await self.client.get(
            "/api/v1/locations",
            headers={"Origin": "null"},
        )
        self.assertEqual(response.status, 200)
        self.assertEqual(response.headers["Access-Control-Allow-Origin"], "*")
        payload = await response.json()
        self.assertEqual(payload["count"], 2)
        self.assertEqual(len(payload["items"]), 2)
        dom = next(item for item in payload["items"] if item["object_id"] == "dom-1")
        self.assertEqual(dom["city"], "Miasto Testowe")
        self.assertEqual(dom["street"], "ul. Energetyczna")
        self.assertNotIn("keyword", dom)
        self.assertTrue(dom["has_coordinates"])
        self.assertIn("destination=52.1,21.0", dom["map_url"])
        self.assertEqual(dom["upcoming_event_count"], 1)
        self.assertEqual(dom["events_url"], "/api/v1/events?object_id=dom-1")
        detail = await (await self.client.get("/api/v1/locations/dom-1")).json()
        self.assertEqual(detail["item"], dom)

    async def test_localities_group_locations_and_include_teryt_codes(self):
        response = await self.client.get("/api/v1/localities")
        payload = await response.json()
        self.assertEqual(payload["count"], 1)
        locality = payload["items"][0]
        self.assertEqual(locality["city_sym"], "0123456")
        self.assertEqual(locality["object_count"], 2)
        self.assertEqual(locality["object_ids"], ["dom-1", "warsztat-2"])
        self.assertEqual(locality["voivodeship_code"], "14")
        detail = await (await self.client.get("/api/v1/localities/0123456")).json()
        self.assertEqual(len(detail["item"]["locations"]), 2)

    async def test_map_is_one_public_snapshot_used_by_the_panels(self):
        blocked_until = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat()
        self.app[app.RUNTIME_KEY]["sources"]["pge"].update(
            consecutive_errors=3,
            blocked_until=blocked_until,
        )
        response = await self.client.get(
            "/api/v1/map",
            headers={"Origin": "https://example.invalid"},
        )
        self.assertEqual(response.status, 200)
        self.assertEqual(response.headers["Access-Control-Allow-Origin"], "*")
        payload = await response.json()
        self.assertEqual(payload["counts"]["locations"], 2)
        self.assertEqual(payload["counts"]["events"], 1)
        self.assertEqual(len(payload["locations"]), 2)
        self.assertEqual(len(payload["events"]), 1)
        self.assertEqual(payload["events"][0]["event_id"], "pge:evt-1:object:dom-1")
        self.assertEqual(payload["events"][0]["source_status"], "active")
        self.assertEqual(payload["sources"][0]["source"], "pge")
        self.assertEqual(payload["sources"][0]["consecutive_errors"], 3)
        self.assertTrue(payload["sources"][0]["blocked_until"].endswith("Z"))

    async def test_display_is_small_and_does_not_claim_power(self):
        response = await self.client.get("/api/v1/display/dom-1")
        self.assertEqual(response.status, 200)
        body = await response.read()
        self.assertLessEqual(len(body), 2048)
        payload = json.loads(body)
        self.assertEqual(payload["outage_count"], 0)
        self.assertEqual(payload["planned_future_count"], 0)
        self.assertEqual(payload["candidate_count"], 1)
        self.assertIsNone(payload["next_event_id"])
        self.assertIn("do sprawdzenia", payload["message"])
        self.assertNotIn("zasilanie działa", payload["message"].lower())

        empty = await (await self.client.get("/api/v1/display/warsztat-2")).json()
        self.assertEqual(empty["message"], "Brak komunikatów potwierdzających objęcie punktu")

        event = next(iter(self.app[app.RUNTIME_KEY]["events"].values()))
        event["match_evidence"] = {"version":2,"scope":"confirmed_point","point_confirmed":True}
        confirmed = await (await self.client.get("/api/v1/display/dom-1")).json()
        self.assertEqual(confirmed["planned_future_count"], 1)
        self.assertEqual(confirmed["candidate_count"], 0)

    async def test_display_message_declines_operator_notices(self):
        runtime = self.app[app.RUNTIME_KEY]
        original = deepcopy(runtime['events'])
        event = deepcopy(next(iter(original.values())))
        event['match_evidence'] = {'version': 2, 'scope': 'confirmed_point', 'point_confirmed': True}
        try:
            for number, noun in [(1, 'komunikat'), (2, 'komunikaty'), (5, 'komunikatów'),
                                 (12, 'komunikatów'), (22, 'komunikaty')]:
                with self.subTest(number=number):
                    runtime['events'] = {f'language-{i}': dict(event, source_event_id=f'language-{i}') for i in range(number)}
                    response = await self.client.get('/api/v1/display/dom-1')
                    self.assertEqual(response.status, 200)
                    data = await response.json()
                    self.assertEqual(data['message'], f'Potwierdzone objęcie punktu: {number} {noun}')
        finally:
            runtime['events'] = original

    async def test_point_filter_never_promotes_legacy_assignment(self):
        confirmed = await (await self.client.get('/api/v1/events?match_scope=confirmed')).json()
        candidates = await (await self.client.get('/api/v1/events?match_scope=candidate')).json()
        self.assertEqual(confirmed['count'],0)
        self.assertEqual(candidates['count'],1)
        self.assertEqual(candidates['items'][0]['match_evidence']['scope'],'unknown')
        self.assertEqual((await self.client.get('/api/v1/events?match_scope=invalid')).status,400)

    async def test_shared_polygon_is_atomic_durable_and_projected_once(self):
        config = self.app[app.CONFIG_KEY]
        config['enabled_sources'] = ['energa']
        config['source_scan_intervals_seconds']['energa'] = 3600
        self.app[app.SOURCE_LOCKS_KEY]['energa'] = asyncio.Lock()
        for location in config['teryt_locations']:
            location.update(latitude=50.25,longitude=20.25,
                            location_audit={'method':'gps_city','checked_at':'2026-10-02T00:00:00Z','source_url':'https://example.test/ref'})
            from test_matching_evidence import verified_audit
            location['location_audit']=verified_audit(location)
        self.app[app.RUNTIME_KEY]['sources']['energa'] = dict(self.app[app.RUNTIME_KEY]['sources']['pge'],
                                                            source='energa',last_attempt_at=None)
        raw={'guid':'shape','shutdownType':1,'polygon':{'poly':{'type':'Polygon','coordinates':[
            [[20,50],[22,50],[22,52],[20,52],[20,50]],
            [[20.5,50.5],[21.5,50.5],[21.5,51.5],[20.5,51.5],[20.5,50.5]]
        ]}}}
        with patch('app.fetch_source',new=AsyncMock(return_value=[osd_sources.normalize_energa(raw)])):
            self.assertTrue((await app.scan_source(self.app,'energa'))['ok'])
        saved, _ = self.app[app.ENGINE_STORE_KEY].load_current()
        self.assertEqual(len(saved['source_geometries']),1)
        for event in saved['events'].values():
            self.assertNotIn('geometry',event['source_event'])
        payload = await (await self.client.get('/api/v1/map')).json()
        self.assertEqual(payload['counts']['confirmed_points'],2)
        self.assertEqual(len(payload['geometries']),1)
        self.assertEqual(len(next(iter(payload['geometries'].values()))['coordinates']),2)
        config['enabled_sources'] = ['pge']
        hidden = await (await self.client.get('/api/v1/map')).json()
        self.assertEqual(hidden['geometries'],{})

    async def test_history_exports_and_validation(self):
        history = await (await self.client.get("/api/v1/history")).json()
        self.assertFalse(history["complete"])
        self.assertEqual(history["history_scope"], "stored_event_changes")
        exported = await (await self.client.get("/api/v1/export.json")).json()
        self.assertEqual(len(exported["events"]), 1)
        csv_response = await self.client.get("/api/v1/export.csv?dataset=objects")
        self.assertEqual(csv_response.status, 200)
        self.assertTrue((await csv_response.read()).startswith(b"\xef\xbb\xbf"))
        invalid = await self.client.get("/api/v1/events?unknown=1")
        self.assertEqual(invalid.status, 400)
        duplicate = await self.client.get("/api/v1/events?source=pge&source=enea")
        self.assertEqual(duplicate.status, 400)

    async def test_reads_never_call_operator_adapters_and_errors_keep_cors(self):
        with patch("app.fetch_source") as fetch_source:
            for path in (
                "/api/v1/",
                "/api/v1/summary",
                "/api/v1/sources",
                "/api/v1/locations",
                "/api/v1/localities",
                "/api/v1/map",
                "/api/v1/objects",
                "/api/v1/events",
                "/api/v1/history",
                "/api/v1/export.json",
            ):
                self.assertEqual((await self.client.get(path)).status, 200)
            fetch_source.assert_not_called()
        missing = await self.client.get("/api/v1/does-not-exist")
        self.assertEqual(missing.status, 404)
        self.assertEqual(missing.headers["Access-Control-Allow-Origin"], "*")
        self.assertIn("error", await missing.json())
        method = await self.client.post("/api/v1/events")
        self.assertEqual(method.status, 405)
        self.assertNotIn("Access-Control-Allow-Origin", method.headers)

    async def test_docs_openapi_and_legacy_contract(self):
        openapi = await self.client.get("/api/v1/openapi.json")
        self.assertEqual(openapi.status, 200)
        self.assertEqual((await self.client.get("/api-docs/")).status, 200)
        self.assertEqual((await self.client.get("/examples/osd-display.html")).status, 200)
        self.assertEqual((await self.client.get("/examples/locations.html")).status, 200)
        legacy = await (await self.client.get("/api/events")).json()
        self.assertIn("events", legacy)
        self.assertNotIn("api_version", legacy)


if __name__ == "__main__":
    unittest.main()
