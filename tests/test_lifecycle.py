import asyncio
import json
import unittest
from copy import deepcopy
from contextlib import closing
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

import app
import osd_sources
from event_lifecycle import reconcile
import test_app
from test_app import sample_event


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        self.old = {"event:osinki": {"object_id": "osinki", "source_event_id": "uuid",
                                      "status": "active", "source_hash": "one"}}

    def test_area_change_is_not_repaired_power(self):
        pending, counters, changes = reconcile(self.old, {}, {"uuid"}, {})
        self.assertTrue(pending["event:osinki"]["verification_pending"])
        self.assertEqual(changes, [])
        current, _, changes = reconcile(pending, {}, {"uuid"}, counters)
        self.assertEqual(current, {})
        self.assertEqual(changes[0][0], "out_of_scope")

    def test_absence_requires_two_complete_scans_and_is_not_physical_confirmation(self):
        pending, counters, _ = reconcile(self.old, {}, set(), {})
        current, _, changes = reconcile(pending, {}, set(), counters)
        self.assertEqual(current, {})
        self.assertEqual(changes[0][0], "not_published")

    def test_reappearance_clears_missing_counter(self):
        pending, counters, _ = reconcile(self.old, {}, set(), {})
        current, counters, changes = reconcile(pending, self.old, {"uuid"}, counters)
        self.assertEqual(current, self.old)
        self.assertEqual(counters, {})
        self.assertEqual(changes, [])

    def test_configuration_change_is_not_outage_resolution(self):
        current, _, changes = reconcile(self.old, {}, {"uuid"}, {}, {"osinki"})
        self.assertEqual((current, changes), ({}, []))

    def test_city_name_does_not_match_part_of_street_name(self):
        source = osd_sources.canonical_event("energa", "one", "outage",
                                              description="Turek, ulica Chełmońskiego")
        matched, _, _ = app.canonical_matching_addresses(
            source, {"city_sym": "0929902"}, {"city": "Chełm"}, 1000)
        self.assertFalse(matched)
        source["description"] = "Chełm, ulica Testowa"
        matched, _, _ = app.canonical_matching_addresses(
            source, {"city_sym": "0929902"}, {"city": "Chełm"}, 1000)
        self.assertTrue(matched)


class ScanTests(test_app.ApiTests):
    async def test_corrupted_operator_contract_preserves_sqlite_history_and_recovers(self):
        from test_parser_contracts import ENEA_BLOCK, ENEA_INDEX, ENEA_TAIL

        enea_block = ENEA_BLOCK.replace('ul. Testowa 1', 'Miasto Testowe, ul. Testowa 1')
        pge = sample_event()
        tauron = {'OutageId': 'one', 'TypeId': 1, 'IsActive': True,
                  'Message': 'Miasto Testowe', 'Center': {'lat': 52.21, 'lng': 21.01}}
        stoen = {'id': 'one', 'comment': 'Miasto Testowe', 'points': [[21.01, 52.21]]}
        bad_index = '<a href="index.php?rejon=1">region bez licznika</a>'
        for source, bad in (
            ('enea', bad_index), ('enea', enea_block + ENEA_TAIL),
            ('pge', dict(pge, revoked='true')), ('pge', dict(pge, addresses=[None])),
            ('tauron', dict(tauron, IsActive='false')), ('stoen', dict(stoen, comment='changed')),
        ):
            with self.subTest(source=source, bad=bad):
                config = self.app[app.CONFIG_KEY]
                config['enabled_sources'] = list(osd_sources.DEFAULT_SOURCES)
                health = self.app[app.RUNTIME_KEY]['sources'][source]
                store = self.app[app.ENGINE_STORE_KEY]
                failed = False

                async def json_response(_session, _method, url, **kwargs):
                    if source == 'pge':
                        return [bad if failed else pge]
                    if source == 'tauron':
                        return [bad if failed else tauron]
                    if 'compressed-report' in url:
                        return [stoen, bad] if failed else [stoen]
                    return {'items': []}

                async def text_response(_session, _method, url, **kwargs):
                    if url.endswith('/awarie/'):
                        return 'Brak awarii'
                    if 'rejon=' in url:
                        return bad if failed else enea_block
                    return bad if failed and bad == bad_index else ENEA_INDEX

                async def scan():
                    health['last_attempt_at'] = None  # tylko zegar testu, bez zmiany konfiguracji
                    with patch('osd_sources._request_json', side_effect=json_response), \
                            patch('osd_sources._request_text', side_effect=text_response), \
                            patch('app.schedule_notifications') as notify:
                        result = await app.scan_source(self.app, source)
                    return result, notify

                good_result, _ = await scan()
                self.assertTrue(good_result['ok'], good_result)
                baseline = deepcopy(self.app[app.STATE_KEY])
                self.assertTrue(any(app.event_source(e) == source for e in baseline['events'].values()))
                history = store.read_history(limit=200)
                failed = True
                bad_result, notify = await scan()
                self.assertFalse(bad_result['ok'], bad_result)
                notify.assert_not_called()
                persisted, _revision = store.load_current()
                for field in ('events', 'source_geometries', 'missing', 'object_fingerprints'):
                    self.assertEqual(persisted.get(field), baseline.get(field), field)
                self.assertEqual(persisted['sources'][source]['last_success_at'], baseline['sources'][source]['last_success_at'])
                self.assertEqual(store.read_history(limit=200), history)
                self.assertTrue(persisted['sources'][source]['error'])
                self.assertFalse(persisted['sources'][source]['running'])
                # oddzielne połączenie SQLite, niezależne od cache aplikacji
                import sqlite3
                with closing(sqlite3.connect(store.database_path)) as connection:
                    saved = json.loads(connection.execute('SELECT state_json FROM engine_state').fetchone()[0])
                self.assertEqual(saved, persisted)
                failed = False
                recovered, _ = await scan()
                self.assertTrue(recovered['ok'], recovered)
                self.assertIsNone(health['error'])
                self.assertEqual(store.read_history(limit=200), history)

    async def test_rejected_pge_payload_between_empty_scans_does_not_advance_absence(self):
        health = self.app[app.RUNTIME_KEY]['sources']['pge']
        async def scan(payload):
            health['last_attempt_at'] = None
            with patch('osd_sources._request_json', AsyncMock(return_value=payload)), \
                    patch('app.schedule_notifications') as notify:
                result = await app.scan_source(self.app, 'pge')
            return result, notify
        self.assertTrue((await scan([sample_event()]))[0]['ok'])
        self.assertTrue((await scan([]))[0]['ok'])
        before = deepcopy(self.app[app.STATE_KEY])
        result, notify = await scan([dict(sample_event(), revoked='true')])
        self.assertFalse(result['ok'])
        notify.assert_not_called()
        self.assertEqual(self.app[app.STATE_KEY]['missing'], before['missing'])
        self.assertEqual(self.app[app.STATE_KEY]['events'], before['events'])
        self.assertTrue((await scan([]))[0]['ok'])
        self.assertEqual(self.app[app.STATE_KEY]['events'], {})
        self.assertEqual(self.app[app.STATE_KEY]['history'][-1]['action'], 'not_published')

    async def test_history_preserves_entire_large_scan_and_confirmed_departures(self):
        self.app[app.CONFIG_KEY]['enabled_sources'] = ['pge']
        store = self.app[app.ENGINE_STORE_KEY]
        sources = [osd_sources.normalize_pge(dict(sample_event(), id=index, uuid=f'event-{index}')) for index in range(250)]

        async def scan(rows):
            self.app[app.RUNTIME_KEY]['sources']['pge']['last_attempt_at'] = None
            with patch('app.fetch_source', new=AsyncMock(return_value=rows)), patch('app.schedule_notifications'):
                result = await app.scan_source(self.app, 'pge')
            self.assertTrue(result['ok'], result)

        await scan(sources)
        self.assertEqual(store.read_history()['total'], 250)
        await scan(sources)
        self.assertEqual(store.read_history()['total'], 250)
        await scan([])
        self.assertEqual(store.read_history()['total'], 250)
        await scan([])
        self.assertEqual(store.read_history()['total'], 500)
        self.assertEqual(store.read_history(departures_only=True)['total'], 250)
        self.assertEqual(len(self.app[app.STATE_KEY]['history']), 200)
        self.assertEqual(self.app[app.STATE_KEY]['events'], {})
        self.assertEqual({row['action'] for row in store.read_history()['items']}, {'not_published'})

    async def test_history_tracks_baseline_updates_return_and_cancellation_without_duplicates(self):
        self.app[app.CONFIG_KEY]['enabled_sources'] = ['pge']
        store = self.app[app.ENGINE_STORE_KEY]
        for description, revoked in [('A',False),('A',False),('B',False),('A',False),('A',True)]:
            self.app[app.RUNTIME_KEY]['sources']['pge']['last_attempt_at'] = None
            source = osd_sources.normalize_pge(sample_event(description=description, revoked=revoked))
            with patch('app.fetch_source',new=AsyncMock(return_value=[source])), patch('app.schedule_notifications'):
                result = await app.scan_source(self.app,'pge')
            self.assertTrue(result['ok'],result)
        history = store.read_history(limit=200)['items']
        self.assertEqual([r['action'] for r in reversed(history)],['snapshot','updated','updated','cancelled'])
        self.assertEqual(history[1]['before']['description'],'B')
        self.assertEqual(history[1]['after']['description'],'A')

    async def test_backoff_is_shared_by_perform_scan_and_scan_source(self):
        self.app[app.CONFIG_KEY]["enabled_sources"] = ["pge"]
        health = self.app[app.RUNTIME_KEY]["sources"]["pge"]
        health.update(
            last_attempt_at=(datetime.now().astimezone() - timedelta(seconds=3700)).isoformat(),
            consecutive_errors=3,
        )
        with patch("app.fetch_source", new_callable=AsyncMock) as fetch:
            result = await app.perform_scan(self.app)
            direct = await app.scan_source(self.app, "pge")
        fetch.assert_not_awaited()
        self.assertEqual(result["updated_sources"], [])
        self.assertTrue(direct["skipped"])

    async def test_second_scan_waits_for_lock_and_rechecks_schedule(self):
        self.app[app.CONFIG_KEY]["enabled_sources"] = ["pge"]
        release = asyncio.Event()
        started = asyncio.Event()

        async def fetch(_session, _source, _config):
            started.set()
            await release.wait()
            return []

        with patch("app.fetch_source", side_effect=fetch) as mocked_fetch:
            first = asyncio.create_task(app.scan_source(self.app, "pge"))
            await started.wait()
            second = asyncio.create_task(app.scan_source(self.app, "pge"))
            release.set()
            first_result, second_result = await asyncio.gather(first, second)
        self.assertTrue(first_result["ok"])
        self.assertTrue(second_result["skipped"])
        self.assertEqual(mocked_fetch.await_count, 1)

    async def test_attempt_is_persisted_before_http(self):
        self.app[app.CONFIG_KEY]["enabled_sources"] = ["pge"]

        async def inspect_state(_session, _source, _config):
            saved = json.loads(
                self.app[app.STATE_PATH_KEY].read_text(encoding="utf-8")
            )
            self.assertTrue(saved["sources"]["pge"]["last_attempt_at"])
            self.assertTrue(saved["sources"]["pge"]["running"])
            return []

        with patch("app.fetch_source", side_effect=inspect_state):
            result = await app.scan_source(self.app, "pge")
        self.assertTrue(result["ok"])

    async def test_failed_attempt_reservation_prevents_http(self):
        self.app[app.CONFIG_KEY]["enabled_sources"] = ["pge"]
        with patch.object(
                self.app[app.ENGINE_STORE_KEY],
                "write_state",
                new=AsyncMock(side_effect=OSError("disk full")),
            ), \
                patch("app.fetch_source", new_callable=AsyncMock) as fetch:
            result = await app.scan_source(self.app, "pge")
        self.assertTrue(result["storage_error"])
        fetch.assert_not_awaited()

    async def test_failed_final_commit_does_not_publish_uncommitted_state(self):
        self.app[app.CONFIG_KEY]["enabled_sources"] = ["pge"]
        original_write = self.app[app.ENGINE_STORE_KEY].write_state
        calls = 0

        async def fail_after_reservation(snapshot):
            nonlocal calls
            calls += 1
            if calls == 1:
                return await original_write(snapshot)
            raise app.EngineStoreError("symulowany błąd zapisu")

        with patch.object(
                self.app[app.ENGINE_STORE_KEY],
                "write_state",
                side_effect=fail_after_reservation,
            ), patch("app.fetch_source", new=AsyncMock(return_value=[])):
            result = await app.scan_source(self.app, "pge")

        self.assertTrue(result["storage_error"])
        committed, _revision = self.app[app.ENGINE_STORE_KEY].load_current()
        self.assertIsNone(committed.get("saved_at"))
        self.assertIsNone(self.app[app.STATE_KEY].get("saved_at"))
        self.assertIsNone(self.app[app.RUNTIME_KEY]["sources"]["pge"].get("last_success_at"))

    async def test_retry_after_is_saved_and_respected(self):
        self.app[app.CONFIG_KEY]["enabled_sources"] = ["pge"]
        retry_at = datetime.now(timezone.utc) + timedelta(hours=8)
        error = osd_sources.SourceHttpError(
            "HTTP 429", status=429, retry_after=retry_at
        )
        with patch("app.fetch_source", side_effect=error):
            result = await app.scan_source(self.app, "pge")
        self.assertFalse(result["ok"])
        saved = json.loads(self.app[app.STATE_PATH_KEY].read_text(encoding="utf-8"))
        blocked = datetime.fromisoformat(saved["sources"]["pge"]["blocked_until"])
        self.assertGreaterEqual(blocked, retry_at.replace(microsecond=0))
        self.assertGreater(
            app.source_wait_seconds(
                "pge",
                self.app[app.RUNTIME_KEY]["sources"]["pge"],
                3600,
                datetime.now().astimezone(),
            ),
            7 * 3600,
        )

    async def test_adapter_has_total_timeout(self):
        self.app[app.CONFIG_KEY]["enabled_sources"] = ["pge"]

        async def never_finishes(_session, _source, _config):
            await asyncio.sleep(1)

        with patch("app.SOURCE_FETCH_TIMEOUT_SECONDS", 0.01), \
                patch("app.fetch_source", side_effect=never_finishes):
            result = await app.scan_source(self.app, "pge")
        self.assertFalse(result["ok"])
        self.assertTrue(result["error"].startswith("TimeoutError"))

    async def test_notifications_do_not_hold_source_lock(self):
        self.app[app.CONFIG_KEY]["enabled_sources"] = ["pge"]
        source = osd_sources.normalize_pge(sample_event())
        fingerprints = app.object_fingerprints(self.app[app.CONFIG_KEY], "pge")
        self.app[app.STATE_KEY]["object_fingerprints"] = {"pge": fingerprints}
        self.app[app.CONFIG_KEY].update(notifications_enabled=True, our_api_url="http://receiver.test/hook")
        release = asyncio.Event()
        started = asyncio.Event()

        async def slow_notification(*_args, **_kwargs):
            started.set()
            await release.wait()
            return {"status": "delivered", "http_status": 200}

        with patch("app.fetch_source", return_value=[source]), \
                patch("app.point_confirmed", return_value=True), \
                patch("notification_queue.deliver", side_effect=slow_notification):
            result = await app.scan_source(self.app, "pge")
            self.assertTrue(result["ok"])
            self.assertFalse(self.app[app.SOURCE_LOCKS_KEY]["pge"].locked())
            await asyncio.wait_for(started.wait(),2)
            self.assertFalse(self.app[app.SOURCE_LOCKS_KEY]['pge'].locked())
            release.set()

    async def test_fast_operator_publishes_before_slow_operator(self):
        self.app[app.CONFIG_KEY]["enabled_sources"] = ["pge", "tauron"]
        release = asyncio.Event()
        published = asyncio.Event()
        original_scan = app.scan_source
        async def scan(application, source):
            result = await original_scan(application, source)
            if source == 'pge':
                published.set()
            return result
        async def fetch(_session, source, _config):
            if source == "tauron":
                await release.wait()
                return []
            return [osd_sources.normalize_pge(sample_event())]
        with patch("app.fetch_source", side_effect=fetch), patch('app.scan_source', side_effect=scan):
            task = asyncio.create_task(app.perform_scan(self.app))
            try:
                await asyncio.wait_for(published.wait(), timeout=30)
                self.assertFalse(task.done())
                self.assertEqual(len(self.app[app.RUNTIME_KEY]["events"]), 1)
                saved = json.loads(self.app[app.STATE_PATH_KEY].read_text(encoding="utf-8"))
                self.assertEqual(len(saved["events"]), 1)
            finally:
                release.set()
                await task
        self.assertEqual(len(self.app[app.RUNTIME_KEY]["events"]), 1)

    async def test_first_migration_is_silent_and_failures_do_not_count_as_absence(self):
        self.app[app.CONFIG_KEY]["enabled_sources"] = ["pge"]
        health = self.app[app.RUNTIME_KEY]["sources"]["pge"]
        with patch("app.fetch_source", return_value=[osd_sources.normalize_pge(sample_event())]), \
                patch("app.send_notification", new_callable=AsyncMock) as notify:
            await app.scan_source(self.app, "pge")
            notify.assert_not_called()
        health["last_attempt_at"] = None
        with patch("app.fetch_source", return_value=[]):
            await app.scan_source(self.app, "pge")
        missing = self.app[app.STATE_KEY]["missing"]["pge"].copy()
        health["last_attempt_at"] = None
        with patch("app.fetch_source", side_effect=RuntimeError("HTTP 500")):
            await app.scan_source(self.app, "pge")
        self.assertEqual(self.app[app.STATE_KEY]["missing"]["pge"], missing)
        health["last_attempt_at"] = None
        with patch("app.fetch_source", return_value=[]):
            await app.scan_source(self.app, "pge")
        self.assertEqual(self.app[app.STATE_KEY]["events"], {})
        self.assertEqual(self.app[app.STATE_KEY]["history"][-1]["action"], "not_published")


class PgeSnapshotTests(unittest.IsolatedAsyncioTestCase):
    async def test_partial_or_invalid_payload_is_rejected(self):
        for bad in ({"error": "bad"}, None, [None], [{"uuid": "x"}]):
            with self.subTest(payload=bad), patch("osd_sources._request_json", side_effect=[bad, []]):
                with self.assertRaises(ValueError):
                    await osd_sources.fetch_pge(None, {})

    async def test_complete_pge_list_uses_one_request(self):
        request = AsyncMock(return_value=[sample_event()])
        with patch("osd_sources._request_json", request):
            events = await osd_sources.fetch_pge(None, {})
        self.assertEqual(len(events), 1)
        request.assert_awaited_once()
        self.assertEqual(request.await_args.kwargs["retries"], 1)
        self.assertNotIn("types[]", request.await_args.kwargs["params"])


class OperatorAdapterTests(unittest.IsolatedAsyncioTestCase):
    async def test_malformed_operator_payloads_are_rejected(self):
        with patch("osd_sources._request_json", return_value={"error": "maintenance"}):
            with self.assertRaises(ValueError):
                await osd_sources.fetch_tauron(None, {})

        with patch("osd_sources._request_json", return_value={"error": "maintenance"}):
            with self.assertRaises(ValueError):
                await osd_sources.fetch_stoen(None, {})

        with patch("osd_sources._request_text", return_value='<div data-shutdowns="/test" data-mass="/mass"></div>'), \
                patch("osd_sources._request_json", return_value={"error": "maintenance"}):
            with self.assertRaises(ValueError):
                await osd_sources.fetch_energa(None, {})

        with patch("osd_sources._request_text", return_value='<div data-mass="/mass"></div>'):
            with self.assertRaisesRegex(RuntimeError, "dynamicznego źródła wyłączeń"):
                await osd_sources.fetch_energa(None, {})

    async def test_energa_accepts_current_shutdowns_only_contract(self):
        page = '<div data-shutdowns="/test"></div>'
        payload = {
            "document": {
                "payload": {
                    "shutdowns": [
                        {
                            "guid": "one",
                            "shutdownType": 3,
                            "message": "Miasto Testowe",
                        }
                    ]
                }
            }
        }
        with patch("osd_sources._request_text", return_value=page), \
                patch("osd_sources._request_json", return_value=payload):
            events = await osd_sources.fetch_energa(None, {})

        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["type"], "mass_outage")

        with patch("osd_sources._request_text", return_value="<html>Maintenance</html>"):
            with self.assertRaises(ValueError):
                await osd_sources.fetch_enea(None, {})

    async def test_recognized_empty_enea_page_is_a_valid_empty_result(self):
        with patch("osd_sources._request_text", return_value="<html>Brak wyłączeń</html>"):
            self.assertEqual(await osd_sources.fetch_enea(None, {}), [])

    async def test_enea_fetches_planned_and_outage_modes_in_sequence(self):
        calls = []
        planned_index = '<a href="index.php?rejon=1" title="1 wyłączenie">region</a>'
        outage_index = '<a href="index.php?rejon=2" title="1 awaria">region</a>'
        event_page = """
        <div class="unpl block info">
          <h4 class="title_">Obszar Testowy</h4>
          <p class="description">Miasto Testowe</p>
          <!-- 2026-09-10 08:00 - 2026-09-10 10:00 -->
          <!-- 6bef872f-08a2-4159-991a-52af025a8651 -->
        </div>
        """

        async def fake_text(_session, _method, url, **_kwargs):
            calls.append(url)
            return [planned_index, event_page, outage_index, event_page][len(calls) - 1]

        with patch("osd_sources._request_text", side_effect=fake_text):
            events = await osd_sources.fetch_enea(None, {})

        self.assertEqual([event["type"] for event in events], ["planned_outage", "outage"])
        self.assertEqual(calls[0], osd_sources.ENEA_BASE_URL)
        self.assertIn("rejon=1", calls[1])
        self.assertTrue(calls[2].endswith("/awarie/"))
        self.assertIn("page=awarie&rejon=2", calls[3])

    async def test_enea_limits_parallel_region_requests_to_four(self):
        active = 0
        maximum = 0
        release = asyncio.Event()
        started_four = asyncio.Event()

        async def fake_text(_session, _method, url, **_kwargs):
            nonlocal active, maximum
            if url == "https://example.test/index":
                return "index"
            active += 1
            maximum = max(maximum, active)
            if active == osd_sources.ENEA_MAX_CONCURRENT_REQUESTS:
                started_four.set()
            try:
                await release.wait()
                return "Brak wyłączeń"
            finally:
                active -= 1

        with patch("osd_sources._request_text", side_effect=fake_text), \
                patch("osd_sources.enea_index_is_recognized", return_value=True), \
                patch("osd_sources.enea_active_regions", return_value=list(range(1, 9))):
            task = asyncio.create_task(
                osd_sources._fetch_enea_mode(
                    None, "https://example.test/index", "planned_outage", []
                )
            )
            await started_four.wait()
            self.assertEqual(maximum, 4)
            release.set()
            self.assertEqual(await task, [])
        self.assertEqual(active, 0)

    async def test_enea_active_region_requires_events_or_explicit_empty_message(self):
        index = '<a href="index.php?rejon=1" title="1 wyłączenie">region</a>'
        with patch(
            "osd_sources._request_text",
            side_effect=[index, '<form id="unpl_search_form"></form>'],
        ):
            with self.assertRaisesRegex(ValueError, "nierozpoznana strona rejonu"):
                await osd_sources.fetch_enea(None, {})

        with patch("osd_sources._request_text", return_value="<script>Brak wyłączeń</script>"):
            with self.assertRaisesRegex(ValueError, "nierozpoznana strona indeksu"):
                await osd_sources.fetch_enea(None, {})

    async def test_enea_incomplete_event_block_is_rejected(self):
        index = '<a href="index.php?rejon=1" title="1 wyłączenie">region</a>'
        page = '<div class="unpl block info"><h4 class="title_">Obszar Testowy</h4></div>'
        with patch("osd_sources._request_text", side_effect=[index, page]):
            with self.assertRaisesRegex(ValueError, "niekompletny blok"):
                await osd_sources.fetch_enea(None, {})

    async def test_stoen_reads_all_planned_pages_and_deduplicates_ids(self):
        calls = []

        async def fake_json(_session, _method, _url, **kwargs):
            offset = kwargs["json"]["page"]["offset"]
            calls.append(offset)
            if offset == 0:
                return [{"id": "1"}, {"id": "2"}]
            if offset == 2:
                return [{"id": "2"}, {"id": "3"}]
            return []

        with patch("osd_sources._request_json", side_effect=fake_json):
            rows = await osd_sources._fetch_stoen_planned(
                None, "https://example.test/stoen", page_size=2
            )

        self.assertEqual([row["id"] for row in rows], ["1", "2", "3"])
        self.assertEqual(calls, [0, 2, 4])

    async def test_pge_and_tauron_request_fourteen_day_window(self):
        calls = []

        async def fake_json(_session, _method, _url, **kwargs):
            calls.append(kwargs.get("params", {}))
            return []

        with patch("osd_sources._request_json", side_effect=fake_json):
            await osd_sources.fetch_pge(None, {})
        self.assertIn("startAtTo", calls[0])
        pge_to = datetime.strptime(calls[0]["startAtTo"], "%Y-%m-%d %H:%M:%S").replace(
            tzinfo=ZoneInfo("Europe/Warsaw")
        )
        self.assertGreaterEqual((pge_to - datetime.now(timezone.utc)).days, 13)
        self.assertEqual(len(calls), 1)

        calls.clear()
        with patch("osd_sources._request_json", side_effect=fake_json):
            await osd_sources.fetch_tauron(None, {})
        self.assertGreaterEqual(calls[0]["toDate"], calls[0]["fromDate"])
        self.assertIn("T", calls[0]["toDate"])
        tauron_to = datetime.fromisoformat(calls[0]["toDate"].replace("Z", "+00:00"))
        self.assertGreaterEqual((tauron_to - datetime.now(timezone.utc)).days, 13)


class SourceScheduleTests(unittest.TestCase):
    def test_pge_restart_waits_for_persisted_hourly_interval(self):
        now = datetime(2026, 9, 16, 20, 30, tzinfo=timezone.utc)
        health = {
            "last_attempt_at": "2026-09-16T20:00:00+00:00",
            "consecutive_errors": 0,
        }
        self.assertEqual(app.source_wait_seconds("pge", health, 3600, now), 1800)

    def test_pge_errors_back_off_to_six_hours(self):
        health = {"consecutive_errors": 1}
        self.assertEqual(app.source_effective_interval("pge", health, 3600), 7200)
        health["consecutive_errors"] = 2
        self.assertEqual(app.source_effective_interval("pge", health, 3600), 14400)
        health["consecutive_errors"] = 3
        self.assertEqual(app.source_effective_interval("pge", health, 3600), 21600)
        health["consecutive_errors"] = 20
        self.assertEqual(app.source_effective_interval("pge", health, 3600), 21600)

    def test_catalog_change_keeps_hourly_interval_and_retry_after(self):
        now = datetime(2026, 10, 6, 7, 0, tzinfo=timezone.utc)
        health = {"last_attempt_at": "2026-10-06T06:30:00+00:00", "consecutive_errors": 2,
                  "error_category": "catalog_changed"}
        self.assertEqual(app.source_wait_seconds("pge", health, 3600, now), 1800)
        health["blocked_until"] = "2026-10-06T09:00:00+00:00"
        self.assertEqual(app.source_wait_seconds("pge", health, 3600, now), 7200)

    def test_legacy_catalog_error_does_not_delay_pge_two_hours(self):
        health = {"consecutive_errors": 1, "error": app.LEGACY_CATALOG_CHANGE_ERROR}
        self.assertEqual(app.source_effective_interval("pge", health, 3600), 3600)

    def test_pge_backoff_never_shortens_long_configuration(self):
        health = {"consecutive_errors": 1}
        self.assertEqual(app.source_effective_interval("pge", health, 86400), 86400)

    def test_retry_after_parses_seconds_and_http_date(self):
        now = datetime(2026, 9, 21, 20, 0, tzinfo=timezone.utc)
        self.assertEqual(
            osd_sources.parse_retry_after("120", now=now),
            now + timedelta(seconds=120),
        )
        self.assertEqual(
            osd_sources.parse_retry_after("Mon, 21 Sep 2026 22:00:00 GMT", now=now),
            datetime(2026, 9, 21, 22, 0, tzinfo=timezone.utc),
        )
        self.assertIsNone(osd_sources.parse_retry_after("niepoprawne", now=now))


class ScanOnceTests(unittest.IsolatedAsyncioTestCase):
    async def test_scan_once_exit_code_reflects_source_errors(self):
        config = {
            "request_timeout_seconds": 1,
            "our_api_url": "",
            "enabled_sources": ["pge"],
            "teryt_locations": [],
        }
        with patch("app.load_config", return_value=dict(config)), \
                patch("app.load_objects", return_value=[]), \
                patch("app.fetch_source", return_value=[]):
            self.assertEqual(await app.scan_once(), 0)

        with patch("app.load_config", return_value=dict(config)), \
                patch("app.load_objects", return_value=[]), \
                patch("app.fetch_source", side_effect=RuntimeError("simulated unavailable")):
            self.assertEqual(await app.scan_once(), 1)
