"""Regresje mapy: jeden snapshot, świeże dane i ograniczony odczyt ulic."""
import copy
from contextlib import closing
from datetime import datetime, timezone
import sqlite3
import unittest
from unittest.mock import patch

import app
import api_v1
import event_history
import test_api_v1


class MapOptimizationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.case = test_api_v1.PublicApiV1Tests('test_index_summary_and_sources_are_public')
        await self.case.asyncSetUp()
        self.application = self.case.app
        self.client = self.case.client

    async def asyncTearDown(self):
        await self.case.asyncTearDown()

    async def read_map(self):
        response = await self.client.get('/api/v1/map')
        self.assertEqual(response.status, 200)
        return await response.json()

    async def test_map_calculates_visibility_once_and_does_not_mutate_engine(self):
        config = copy.deepcopy(self.application[app.CONFIG_KEY])
        state = copy.deepcopy(self.application[app.STATE_KEY])
        runtime = copy.deepcopy(self.application[app.RUNTIME_KEY])
        with patch('app.event_visibility', wraps=app.event_visibility) as visibility, \
             patch('app.object_fingerprints', wraps=app.object_fingerprints) as fingerprints:
            result = await self.read_map()
        self.assertEqual(visibility.call_count, 1)
        self.assertEqual(fingerprints.call_count, len(config['enabled_sources']))
        self.assertEqual(result['counts']['locations'], 2)
        self.assertEqual(result['counts']['events'], 1)
        result['events'][0]['time_windows'].clear()
        result['locations'][0]['name'] = 'Client changed this copy'
        self.assertEqual(self.application[app.CONFIG_KEY], config)
        self.assertEqual(self.application[app.STATE_KEY], state)
        self.assertEqual(self.application[app.RUNTIME_KEY], runtime)
        again = await self.read_map()
        self.assertTrue(again['events'][0]['time_windows'])
        self.assertNotEqual(again['locations'][0]['name'], result['locations'][0]['name'])

    async def test_next_request_sees_edit_delete_and_new_scan_without_old_matches(self):
        original = await self.read_map()
        old_id = original['events'][0]['event_id']
        response = await self.client.patch('/api/admin/objects/dom-1', json={'keyword': 'changed'})
        self.assertEqual(response.status, 200)
        changed = await self.read_map()
        self.assertEqual(changed['events'], [])
        obj = next(item for item in changed['locations'] if item['object_id'] == 'dom-1')
        self.assertEqual(obj['matching_pending_sources'], ['pge'])
        self.assertEqual(changed['data_status'], 'partial')
        self.application[app.STATE_KEY]['object_fingerprints']['pge'] = app.object_fingerprints(self.application[app.CONFIG_KEY], 'pge')
        current = next(iter(self.application[app.RUNTIME_KEY]['events'].values()))
        current['keyword'] = 'changed'
        scanned = await self.read_map()
        self.assertEqual(scanned['events'][0]['event_id'], old_id)
        response = await self.client.delete('/api/admin/objects/dom-1')
        self.assertEqual(response.status, 200)
        deleted = await self.read_map()
        self.assertEqual(deleted['counts']['locations'], 1)
        self.assertEqual(deleted['events'], [])
        self.assertEqual(self.application[app.ENGINE_STORE_KEY].read_history()['total'], 1)

    async def test_next_request_sees_teryt_names_and_source_switch(self):
        original = await self.read_map()
        with closing(sqlite3.connect(self.case.base / 'data/teryt.db')) as connection:
            connection.execute("UPDATE ulic SET nazwa_1='Nowa nazwa' WHERE sym='0123456' AND sym_ul='11111'")
            connection.execute("UPDATE simc SET nazwa='Nowe miasto' WHERE sym='0123456'")
            connection.commit()
        changed = await self.read_map()
        self.assertEqual({item['street'] for item in changed['locations']}, {'ul. Nowa nazwa'})
        self.assertEqual({item['city'] for item in changed['locations']}, {'Nowe miasto'})
        self.assertNotEqual(original['snapshot_id'], changed['snapshot_id'])
        self.application[app.CONFIG_KEY]['enabled_sources'] = []
        self.assertEqual((await self.read_map())['events'], [])
        self.application[app.CONFIG_KEY]['enabled_sources'] = ['pge']
        self.assertEqual((await self.read_map())['counts']['events'], 1)

    async def test_time_phase_is_recomputed_when_the_clock_crosses_a_boundary(self):
        event = next(iter(self.application[app.RUNTIME_KEY]['events'].values()))
        event.update(start_at='2026-10-13 08:00:00', stop_at='2026-10-13 09:00:00', periods=[])
        for hour, expected in [(5, 'upcoming'), (6, 'ongoing'), (8, 'overdue')]:
            now = datetime(2026, 10, 13, hour, tzinfo=timezone.utc)
            with patch.dict(self.application[api_v1.API_CONTEXT_KEY],
                            {'event_time_phase': lambda item: event_history.event_time_phase(item, now)}):
                result = await self.read_map()
            self.assertEqual(result['events'][0]['time_phase'], expected)

    async def test_next_scan_deadline_uses_the_same_instant_as_wait_calculation(self):
        health = self.application[app.RUNTIME_KEY]['sources']['pge']
        health.update(last_attempt_at='2026-10-13T05:00:00+00:00', interval_seconds=3600)
        first = datetime(2026, 10, 13, 5, 1, 0, 999999, tzinfo=timezone.utc)
        later = datetime(2026, 10, 13, 5, 1, 1, tzinfo=timezone.utc)
        with patch('api_v1.datetime', wraps=datetime) as clock:
            clock.now.side_effect = [first, later]
            result = await self.read_map()
        source = next(item for item in result['sources'] if item['source'] == 'pge')
        self.assertEqual(source['next_scan_at'], '2026-10-13T06:00:00Z')

    def test_street_lookup_selects_only_used_pairs_and_batches_parameters(self):
        database = self.case.base / 'data/teryt.db'
        with closing(sqlite3.connect(database)) as connection:
            connection.executemany('INSERT INTO ulic VALUES (?, ?, ?, ?, ?)',
                                   [(str(20000 + index), '0123456', 'ul.', 'Ulica ' + str(index), '') for index in range(405)])
            connection.commit()
        objects = [dict(id=str(index), city_sym='0123456', street_sym=str(20000 + index), name='Test') for index in range(405)]
        objects += [dict(id='no-street', city_sym='0123456', street_sym='', name='Test')]
        queries = []
        original = app.open_teryt_database
        def traced(path):
            connection = original(path)
            connection.set_trace_callback(queries.append)
            return connection
        with patch('app.open_teryt_database', side_effect=traced):
            result = app.enriched_objects(objects, database)
        self.assertEqual(result[0]['street'], 'ul. Ulica 0')
        self.assertEqual(result[404]['street'], 'ul. Ulica 404')
        self.assertEqual(result[405]['street'], '')
        self.assertEqual(sum('WITH requested' in query for query in queries), 2)
