"""Odbiór prawdziwej instancji demo: izolacja, publikacja i odtwarzanie."""
import json
import tempfile
from pathlib import Path
from unittest.mock import patch

from aiohttp.test_utils import AioHTTPTestCase

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
