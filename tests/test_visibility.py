from policy_helpers import create_test_app
import asyncio
import json
import unittest
from copy import deepcopy
from unittest.mock import patch, AsyncMock

import app
import test_api_v1 as base


class VisibilityTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.case = base.PublicApiV1Tests('test_index_summary_and_sources_are_public')
        await self.case.asyncSetUp()
        self.app = self.case.app
        self.client = self.case.client
        self.state = self.app[app.STATE_KEY]
        self.state['object_fingerprints'] = {'pge': app.object_fingerprints(self.app[app.CONFIG_KEY], 'pge')}
        self.original = deepcopy(self.state)

    async def asyncTearDown(self):
        await self.case.asyncTearDown()

    async def assert_hidden(self):
        for path, field in [('/api/events', 'events'), ('/api/v1/events', 'items'),
                            ('/api/v1/map', 'events'), ('/api/v1/export.json', 'events')]:
            payload = await (await self.client.get(path)).json()
            self.assertEqual(payload[field], [], path)
        summary = await (await self.client.get('/api/v1/summary')).json()
        self.assertEqual(summary['event_count'], 0)
        self.assertEqual(len(self.state['events']), 1)
        self.assertEqual(self.state['history'], self.original['history'])
        history = await (await self.client.get('/api/v1/history')).json()
        self.assertTrue(history['includes_inactive_assignments'])
        self.assertEqual(history['total'], 1)

    async def test_disabled_source_hidden_without_changing_history_or_age(self):
        self.app[app.RUNTIME_KEY]['sources']['enea'] = deepcopy(self.app[app.RUNTIME_KEY]['sources']['pge'])
        self.app[app.CONFIG_KEY]['enabled_sources'] = ['enea']
        with patch('app.fetch_source') as fetch, patch('app.schedule_notifications') as notify:
            await self.assert_hidden()
            status = await (await self.client.get('/api/status')).json()
            self.assertEqual(list(status['sources']), ['enea'])
            self.assertEqual(status['matched_event_count'], 0)
            fetch.assert_not_called()
            notify.assert_not_called()
        self.app[app.CONFIG_KEY]['enabled_sources'] = ['pge']
        events = await (await self.client.get('/api/v1/events')).json()
        self.assertEqual(events['total'], 1)
        self.assertEqual(self.state['saved_at'], self.original['saved_at'])

    async def test_delete_hides_all_current_views_but_keeps_history(self):
        response = await self.client.delete('/api/objects/dom-1', headers={'Authorization': 'Bearer test-admin-token-' + 'x' * 32})
        self.assertEqual(response.status, 200)
        await self.assert_hidden()
        self.assertEqual((await self.client.get('/api/v1/display/dom-1')).status, 404)
        locations = await (await self.client.get('/api/v1/locations')).json()
        self.assertEqual([x['object_id'] for x in locations['items']], ['warsztat-2'])

    async def test_edit_pending_survives_restart_and_does_not_report_zero(self):
        response = await self.client.patch('/api/admin/objects/dom-1', json={'keyword': '99'}, headers={'Authorization': 'Bearer test-admin-token-' + 'x' * 32})
        self.assertEqual(response.status, 200)
        await self.assert_hidden()
        display = await (await self.client.get('/api/v1/display/dom-1')).json()
        self.assertEqual(display['matching_pending_sources'], ['pge'])
        self.assertIsNone(display['outage_count'])
        self.assertEqual(display['data_status'], 'partial')
        await self.app[app.ENGINE_STORE_KEY].write_state(self.state)
        restarted = create_test_app(self.case.base / 'config.json', self.case.base / 'state.json', objects_path=self.case.base / 'objects.json', teryt_db_path=self.case.base / 'data/teryt.db', start_scanner=False)
        try:
            self.assertEqual(app.event_visibility(restarted), ({}, {'dom-1': ['pge']}))
        finally:
            await restarted[app.ENGINE_STORE_KEY].close()

    async def test_automatic_success_clears_pending_without_fake_departure(self):
        self.app[app.CONFIG_KEY]['teryt_locations'][0]['keyword'] = '99'
        self.app[app.RUNTIME_KEY]['sources']['pge']['last_attempt_at'] = None
        with patch('app.fetch_source', new=AsyncMock(return_value=[])), patch('app.schedule_notifications') as notify:
            result = await app.scan_source(self.app, 'pge')
        self.assertTrue(result['ok'], result)
        self.assertEqual(app.event_visibility(self.app), ({}, {}))
        self.assertEqual(self.state['history'], self.original['history'])
        notify.assert_not_called()

    async def test_edit_during_fetch_commits_only_rematched_catalog(self):
        self.app[app.RUNTIME_KEY]['sources']['pge']['last_attempt_at'] = None
        async def fetch(*args):
            self.app[app.CONFIG_KEY]['teryt_locations'][0]['keyword'] = '99'
            return []
        with patch('app.fetch_source', side_effect=fetch) as fetch_mock, patch('app.schedule_notifications') as notify:
            result = await app.scan_source(self.app, 'pge')
        self.assertTrue(result['ok'])
        self.assertEqual(fetch_mock.call_count, 1)
        self.assertEqual(self.state['events'], {})
        self.assertEqual(self.state['object_fingerprints']['pge'], app.object_fingerprints(self.app[app.CONFIG_KEY], 'pge'))
        self.assertEqual(self.state['history'], self.original['history'])
        notify.assert_not_called()

    async def test_edit_while_commit_waits_for_lock_is_rechecked(self):
        self.app[app.RUNTIME_KEY]['sources']['pge']['last_attempt_at'] = None
        fetched = asyncio.Event()
        release = asyncio.Event()
        async def fetch(*args):
            fetched.set()
            await release.wait()
            return []
        with patch('app.fetch_source', side_effect=fetch) as fetch_mock, patch('app.schedule_notifications') as notify:
            task = asyncio.create_task(app.scan_source(self.app, 'pge'))
            await asyncio.wait_for(fetched.wait(), 2)
            async with self.app[app.OBJECTS_LOCK_KEY]:
                release.set()
                await asyncio.sleep(.05)
                self.app[app.CONFIG_KEY]['teryt_locations'].pop(0)
            result = await asyncio.wait_for(task, 2)
        self.assertTrue(result['ok'])
        self.assertEqual(fetch_mock.call_count, 1)
        self.assertEqual(self.state['events'], {})
        self.assertEqual(self.state['object_fingerprints']['pge'], app.object_fingerprints(self.app[app.CONFIG_KEY], 'pge'))
        self.assertEqual(self.state['history'], self.original['history'])
        notify.assert_not_called()
        self.assertEqual(app.event_visibility(self.app)[0], {})

    async def test_legacy_snapshot_checks_known_location_fields(self):
        self.state.pop('object_fingerprints')
        self.assertEqual(len(app.event_visibility(self.app)[0]), 1)
        self.app[app.CONFIG_KEY]['teryt_locations'][0]['city_sym'] = '7654321'
        self.assertEqual(app.event_visibility(self.app), ({}, {'dom-1': ['pge']}))

    async def test_disabled_source_stays_hidden_after_restart(self):
        await self.app[app.ENGINE_STORE_KEY].write_state(self.state)
        config_path = self.case.base / 'config.json'
        config = json.loads(config_path.read_text())
        config['enabled_sources'] = ['enea']
        app.save_json(config_path, config)
        restarted = create_test_app(config_path, self.case.base / 'state.json', objects_path=self.case.base / 'objects.json', teryt_db_path=self.case.base / 'data/teryt.db', start_scanner=False)
        try:
            self.assertEqual(app.event_visibility(restarted)[0], {})
            self.assertEqual(restarted[app.STATE_KEY]['events'], self.original['events'])
            self.assertEqual(restarted[app.RUNTIME_KEY]['matched_event_count'], 0)
        finally:
            await restarted[app.ENGINE_STORE_KEY].close()

    async def test_edit_keeps_unchanged_objects_visible(self):
        event = deepcopy(next(iter(self.state['events'].values())))
        event.update(object_id='warsztat-2', event_key='pge:evt-2:object:warsztat-2')
        self.state['events'][event['event_key']] = event
        self.app[app.RUNTIME_KEY]['events'] = deepcopy(self.state['events'])
        self.app[app.CONFIG_KEY]['teryt_locations'][0]['keyword'] = '99'
        visible, pending = app.event_visibility(self.app)
        self.assertEqual(list(visible), [event['event_key']])
        self.assertEqual(pending, {'dom-1': ['pge']})

    async def test_reenabled_source_preserves_stale_status(self):
        health = self.app[app.RUNTIME_KEY]['sources']['pge']
        health['last_success_at'] = '2020-01-01T00:00:00+00:00'
        self.app[app.CONFIG_KEY]['enabled_sources'] = []
        self.assertEqual(app.event_visibility(self.app)[0], {})
        self.app[app.CONFIG_KEY]['enabled_sources'] = ['pge']
        payload = await (await self.client.get('/api/v1/events')).json()
        self.assertEqual(payload['total'], 1)
        self.assertEqual(payload['data_status'], 'stale')
        self.assertEqual(health['last_success_at'], '2020-01-01T00:00:00+00:00')
