"""Ostatnie komunikaty obiektu: deduplikacja przed limitem i polityka publikacji."""
import json
from pathlib import Path
import tempfile
import unittest

import app
from engine_store import EngineStore, history_record
import test_api_v1


def event(number, object_id='one', **fields):
    return dict(source='pge', source_event_id=str(number), object_id=object_id,
                type='planned_outage', description=f'Komunikat {number}', **fields)


class ObjectHistoryStoreTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        root = Path(self.directory.name)
        self.store = EngineStore(root/'engine.sqlite3', root/'state.json')
        self.state = {'events': {}, 'history': [], 'sources': {}, 'saved_at': None}
        self.store.load_or_migrate(self.state)

    async def asyncTearDown(self):
        await self.store.close()
        self.directory.cleanup()

    async def test_unique_events_before_limit_object_filter_and_publication(self):
        records = [history_record('new', event(i), None, '2026-11-01T10:00:00Z') for i in range(5)]
        records += [history_record('updated', event(1), None, '2026-11-01T11:00:00Z') for _ in range(5)]
        records += [history_record('new', event(9, 'other'), None, None)]
        await self.store.write_state(self.state, history_records=records)
        result = self.store.read_history(object_id='one', latest_per_event=True, limit=3)
        self.assertEqual(result['total'], 5)
        self.assertEqual([row['source_event_id'] for row in result['items']], ['1', '4', '3'])
        self.assertEqual(result['items'][0]['action'], 'updated')
        self.assertEqual(self.store.read_history(object_id='one')['total'], 10)
        visible = lambda source, object_id, before, after, legacy: json.loads(after)['source_event_id'] != '1'
        result = self.store.read_history(object_id='one', latest_per_event=True, limit=3, publication_filter=visible)
        self.assertEqual(result['total'], 4)
        self.assertEqual([row['source_event_id'] for row in result['items']], ['4', '3', '2'])
        self.assertEqual(self.store.read_history(object_id='missing', latest_per_event=True)['items'], [])

    async def test_legacy_rows_without_identity_remain_separate(self):
        records = [history_record('new', {'object_id':'one', 'source':'pge'}, None, None) for _ in range(4)]
        await self.store.write_state(self.state, history_records=records)
        result = self.store.read_history(latest_per_event=True, limit=3)
        self.assertEqual(result['total'], 4)
        self.assertEqual(len(result['items']), 3)


class ObjectHistoryApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.case = test_api_v1.PublicApiV1Tests('test_index_summary_and_sources_are_public')
        await self.case.asyncSetUp()

    async def asyncTearDown(self):
        await self.case.asyncTearDown()

    async def test_preview_pagination_and_snapshot_bind_grouping_mode(self):
        store = self.case.app[app.ENGINE_STORE_KEY]
        records = [history_record('new', event(i), None, '2026-11-01T10:00:00Z') for i in range(5)]
        records += [history_record('updated', event(0), None, '2026-11-01T11:00:00Z') for _ in range(4)]
        await store.write_state(self.case.app[app.STATE_KEY], history_records=records)
        url = '/api/v1/history?object_id=one&limit=3&latest_per_event=1'
        response = await self.case.client.get(url)
        self.assertEqual(response.status, 200)
        data = await response.json()
        self.assertTrue(data['latest_per_event'])
        self.assertEqual(data['total'], 5)
        self.assertEqual(len(data['items']), 3)
        self.assertEqual(len({item['source_event_id'] for item in data['items']}), 3)
        second = await (await self.case.client.get(data['next'])).json()
        self.assertEqual(second['count'], 2)
        self.assertIsNone(second['next'])
        self.assertEqual((await self.case.client.get(data['next'].replace('latest_per_event=1', 'latest_per_event=0'))).status, 409)
        self.assertEqual((await self.case.client.get(url.replace('latest_per_event=1', 'latest_per_event=true'))).status, 400)
        absent = await (await self.case.client.get(url.replace('object_id=one','object_id=absent'))).json()
        self.assertEqual(absent['items'], [])
