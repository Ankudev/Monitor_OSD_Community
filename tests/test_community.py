"""Odbiór prawdziwej instancji demo: izolacja, publikacja i odtwarzanie."""
import json
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from aiohttp.test_utils import AioHTTPTestCase, TestServer

import app
import community
from tools.init_demo import initialize, refresh


class CommunityTests(AioHTTPTestCase):
    async def get_application(self):
        import asyncio
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)/'demo'
        await asyncio.to_thread(initialize, self.root)
        return app.create_app(self.root/'config.json', self.root/'state.json')

    async def asyncTearDown(self):
        await super().asyncTearDown()
        self.directory.cleanup()

    async def test_public_demo_has_sixteen_locations_and_explicit_simulation(self):
        response = await self.client.get('/api/v1/locations')
        self.assertEqual(response.status, 200)
        data = await response.json()
        self.assertTrue(data['demo'])
        self.assertEqual(len(data['items']), 16)
        self.assertIn('demonstracyjne', data['notice'])
        self.assertEqual(response.headers['X-OSD-Data-Mode'], 'demo')
        self.assertEqual((await self.client.get('/health/ready')).status, 200)
        self.assertTrue((await (await self.client.get('/api/community')).json())['demo'])

    async def test_demo_scenario_uses_real_event_contract_and_planned_filters(self):
        events = (await (await self.client.get('/api/v1/events?view=full')).json())['items']
        self.assertEqual(len(events), 10)
        self.assertEqual({event['type'] for event in events}, {'outage', 'planned_outage'})
        self.assertEqual(sum(event['time_phase'] == 'closed' for event in events), 4)
        for event in events:
            self.assertEqual(event['match_evidence']['scope'], 'candidate')
            self.assertFalse(event['match_evidence']['point_confirmed'])
            self.assertEqual(event['match_explanation']['basis'], 'locality')
        planned = await (await self.client.get('/api/v1/events?type=planned_outage&time_phase=upcoming')).json()
        self.assertEqual(planned['total'], 2)

    async def test_edits_import_scan_login_and_raw_data_are_blocked(self):
        for method, path in [('POST', '/api/admin/login'), ('POST', '/api/objects'),
                             ('DELETE', '/api/objects/demo-wroclaw'),
                             ('GET', '/api/admin/objects'), ('GET', '/api/admin/check'),
                             ('GET', '/api/location/reverse'), ('GET', '/api/v1/events?include_raw=1')]:
            response = await self.client.request(method, path, json={})
            self.assertEqual(response.status, 403, path)
        self.assertFalse((await (await self.client.get('/api/admin/session')).json())['authenticated'])

    async def test_backend_does_not_start_network_services(self):
        self.assertNotIn(app.SESSION_KEY, self.app)
        self.assertNotIn(app.NOTIFICATION_WORKER_KEY, self.app)
        with patch('aiohttp.ClientSession', side_effect=AssertionError('Network forbidden')):
            for path in ['/api/v1/map', '/api/v1/events', '/api/v1/history', '/api/status']:
                response = await self.client.get(path)
                self.assertEqual(response.status, 200, path)
                self.assertTrue((await response.json())['demo'])

    async def test_initializer_never_overwrites_and_refresh_preserves_history(self):
        before = (self.root/'objects.json').read_bytes()
        with self.assertRaises(ValueError):
            initialize(self.root)
        history_before = self.app[app.ENGINE_STORE_KEY].read_history(limit=1)['total']
        await refresh(self.root, application=self.app)
        self.assertEqual(before, (self.root/'objects.json').read_bytes())
        self.assertEqual(history_before, self.app[app.ENGINE_STORE_KEY].read_history(limit=1)['total'])

    async def test_approvals_bind_values_and_old_integration_routes_are_absent(self):
        policy = self.app[app.publication.KEY].snapshot()
        objects = json.loads((self.root/'objects.json').read_text(encoding='utf-8'))
        self.assertEqual(len(policy.objects(objects)), 16)
        self.assertIsNone(policy.public_object(dict(objects[0], name='Zmieniona wartość')))
        for path in ['/em'+'itel', '/api/v1/em'+'itel/catalog']:
            self.assertEqual((await self.client.get(path)).status, 404)

    async def test_csv_carries_simulation_notice(self):
        response = await self.client.get('/api/v1/export.csv')
        self.assertEqual(response.status, 200)
        self.assertIn('data_mode', await response.text())
        self.assertIn(community.NOTICE, await response.text())

    async def test_openapi_uses_extension_fields_for_demo(self):
        response = await self.client.get('/api/v1/openapi.json')
        document = await response.json()
        self.assertNotIn('demo', document)
        self.assertNotIn('notice', document)
        self.assertTrue(document['x-osd-demo'])
        self.assertEqual(document['x-osd-notice'], community.NOTICE)
        self.assertIn('paths', document)
        original = json.loads((Path(__file__).resolve().parents[1]/'docs/openapi-v1.json').read_text(encoding='utf-8'))
        self.assertEqual({key: value for key, value in document.items() if not key.startswith('x-osd-')}, original)

    async def test_html_labels_demo_without_javascript_or_metadata_request(self):
        for path in ['/', '/readme', '/api-docs/', '/examples/locations.html', '/examples/osd-display.html', '/examples/history.html']:
            response = await self.client.get(path)
            self.assertEqual(response.status, 200, path)
            self.assertIn('id="community-demo-notice"', await response.text(), path)

    async def test_readme_keeps_external_links_and_resolves_repository_links(self):
        response = await self.client.get('/readme')
        self.assertEqual(response.status, 200)
        content = await response.text()
        self.assertIn('href="https://github.com/Ankudev/Monitor_OSD_Community/issues"', content)
        self.assertIn('href="https://github.com/Ankudev/Monitor_OSD_Community/blob/main/CONTRIBUTING.md"', content)

    async def test_demo_metadata_errors_and_preflight_keep_mode_headers(self):
        for method, path, status in [
            ('GET', '/api/community', 200), ('HEAD', '/api/community', 200),
            ('GET', '/api/admin/session', 200), ('POST', '/api/admin/login', 403),
            ('GET', '/api/%61dmin/check', 403), ('GET', '/api/v1/events?include_raw=%31', 403),
            ('GET', '/api/v1/events?limit=0', 400), ('GET', '/api/v1/missing', 404),
            ('OPTIONS', '/api/v1/events', 204), ('DELETE', '/api/v1/events', 403),
        ]:
            with self.subTest(method=method, path=path):
                response = await self.client.request(method, path)
                self.assertEqual(response.status, status)
                self.assertEqual(response.headers.get('X-OSD-Data-Mode'), 'demo')
                self.assertEqual(response.headers.get('Cache-Control'), 'no-store')
                if path.startswith('/api/v1/'):
                    self.assertEqual(response.headers.get('Access-Control-Allow-Origin'), '*')

    async def test_demo_lifecycle_never_constructs_outbound_client(self):
        import asyncio
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)/'demo'
            with patch('aiohttp.ClientSession', side_effect=AssertionError('Outbound network forbidden')):
                await asyncio.to_thread(initialize, root)
                application = app.create_app(root/'config.json', root/'state.json')
                server = TestServer(application)
                await server.start_server()
                try:
                    ids = set(application[app.STATE_KEY]['events'])
                    count = application[app.ENGINE_STORE_KEY].read_history(limit=1)['total']
                    for now in [datetime(2026, 3, 29, 0, 30, tzinfo=timezone.utc),
                                datetime(2026, 10, 25, 1, 30, tzinfo=timezone.utc)]:
                        await refresh(root, now=now, application=application)
                        self.assertEqual(set(application[app.STATE_KEY]['events']), ids)
                        self.assertEqual(application[app.ENGINE_STORE_KEY].read_history(limit=1)['total'], count)
                        self.assertEqual(application[app.STATE_KEY]['saved_at'], now.isoformat())
                    self.assertNotIn(app.SESSION_KEY, application)
                    self.assertNotIn(app.NOTIFICATION_WORKER_KEY, application)
                finally:
                    await server.close()
