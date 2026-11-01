import os
import unittest
from unittest.mock import patch

import test_app
from aiohttp import web
from aiohttp.test_utils import AioHTTPTestCase
from yarl import URL

import admin_auth
import public_origin

OLD = 'old.example.org'
NEW = 'https://new.example.org'


class RedirectTests(AioHTTPTestCase):
    async def get_application(self):
        with patch.dict(os.environ, PGE_REDIRECT_HOSTS=OLD, PGE_PUBLIC_ORIGIN=NEW):
            app = web.Application(middlewares=[public_origin.redirect_middleware()])

        async def endpoint(request):
            return web.Response(text='backend')

        app.router.add_route('*', '/{tail:.*}', endpoint)
        return app

    async def test_old_links_preserve_raw_path_query_and_repeated_parameters(self):
        for path in ['/', '/snut/', '/?view=objects',
                     '/api/v1/history?limit=50&x=%2F&x=a+b&next=https%3A%2F%2Fexample.org',
                     '/%C5%82%C3%B3d%C5%BA?q=%26%3D', '//other.invalid/path']:
            # Pełny URL zachowuje ścieżkę zaczynającą się od dwóch ukośników.
            url = URL(str(self.server.make_url('/')).rstrip('/') + path, encoded=True)
            response = await self.client.session.get(url, headers={'Host': OLD}, allow_redirects=False)
            self.assertEqual(response.status, 301)
            self.assertEqual(response.headers['Location'], NEW + path)
            self.assertIn('max-age=300', response.headers['Cache-Control'])

    async def test_head_and_host_normalization(self):
        response = await self.client.head('/snut/', headers={'Host': OLD.upper()+':443'}, allow_redirects=False)
        self.assertEqual(response.status, 301)
        self.assertEqual(response.headers['Location'], NEW+'/snut/')
        self.assertEqual(await response.read(), b'')

    async def test_writes_preserve_method_and_are_not_executed_on_old_host(self):
        for method in ['POST', 'PATCH', 'DELETE', 'OPTIONS']:
            response = await self.client.request(method, '/api/admin/objects',
                                                 headers={'Host': OLD}, json={}, allow_redirects=False)
            self.assertEqual(response.status, 308)
            self.assertEqual(response.headers['Location'], NEW+'/api/admin/objects')
            self.assertNotEqual(await response.text(), 'backend')

    async def test_new_domain_local_backup_and_unlisted_hosts_do_not_redirect(self):
        for host in ['new.example.org', '127.0.0.1:30130', '[::1]:30130', 'other.invalid']:
            response = await self.client.post('/api/operations/snapshot', headers={
                'Host': host, 'X-Forwarded-Host': OLD, 'Forwarded': 'host='+OLD,
            }, allow_redirects=False)
            self.assertEqual(response.status, 200)
            self.assertNotIn('Location', response.headers)

    async def test_existing_push_worker_stays_on_old_origin(self):
        response = await self.client.get('/osd-push/OneSignalSDKWorker.js',
                                         headers={'Host': OLD}, allow_redirects=False)
        self.assertEqual(response.status, 200)


class OriginConfigurationTests(unittest.TestCase):
    def test_reject_invalid_origin_and_redirect_loop(self):
        for origin in ['', 'http://example.org', 'https://user:pass@example.org',
                       'https://example.org/path', 'https://example.org?x=1',
                       'https://example.org#fragment', 'https://example.org:123', 'https://'+OLD]:
            with (self.subTest(origin=origin),
                  patch.dict(os.environ, PGE_REDIRECT_HOSTS=OLD, PGE_PUBLIC_ORIGIN=origin),
                  self.assertRaises(ValueError)):
                public_origin.redirect_middleware()

    def test_redirect_is_optional_and_hosts_must_be_explicit(self):
        with patch.dict(os.environ, PGE_REDIRECT_HOSTS='', PGE_PUBLIC_ORIGIN=''):
            self.assertTrue(callable(public_origin.redirect_middleware()))
        for host in ['*.wykr.es', 'https://'+OLD, OLD+':443']:
            with (patch.dict(os.environ, PGE_REDIRECT_HOSTS=host, PGE_PUBLIC_ORIGIN=NEW),
                  self.assertRaises(ValueError)):
                public_origin.redirect_middleware()

    def test_ipv4_and_ipv6_listeners_and_empty_setting(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(public_origin.bind_hosts('127.0.0.1'), ['127.0.0.1'])
        with patch.dict(os.environ, PGE_WEB_HOSTS='0.0.0.0, ::'):
            self.assertEqual(public_origin.bind_hosts('127.0.0.1'), ['0.0.0.0', '::'])
        with patch.dict(os.environ, PGE_WEB_HOSTS=''), self.assertRaises(ValueError):
            public_origin.bind_hosts('127.0.0.1')


class DomainAdminTests(AioHTTPTestCase):
    async def get_application(self):
        with patch.dict(os.environ, PGE_REDIRECT_HOSTS=OLD, PGE_PUBLIC_ORIGIN=NEW):
            return await test_app.ApiTests.get_application(self)

    async def asyncTearDown(self):
        await super().asyncTearDown()
        self.temp_directory.cleanup()

    async def test_redirect_precedes_auth_and_new_origin_supports_secure_login_and_csrf(self):
        auth = self.app[admin_auth.KEY]
        auth.dev = False
        password = 'Hasło wyłącznie do migracji testowej'
        auth.password_hash = admin_auth.PH.hash(password)
        response = await self.client.get('/api/admin/objects', headers={'Host': OLD}, allow_redirects=False)
        self.assertEqual(response.status, 301)
        self.assertEqual(response.headers['Location'], NEW+'/api/admin/objects')
        response = await self.client.post('/api/admin/login', headers={'Host': 'new.example.org', 'Origin': NEW},
                                          json={'password': password})
        self.assertEqual(response.status, 200)
        cookie = response.cookies[auth.cookie]
        self.assertTrue(cookie['secure'])
        self.assertTrue(cookie['httponly'])
        csrf = (await response.json())['csrf_token']
        headers = {'Host': 'new.example.org', 'Origin': 'https://'+OLD,
                   'Cookie': auth.cookie+'='+cookie.value, 'X-CSRF-Token': csrf}
        response = await self.client.post('/api/admin/logout', headers=headers)
        self.assertEqual(response.status, 403)
        headers['Origin'] = NEW
        response = await self.client.post('/api/admin/logout', headers=headers)
        self.assertEqual(response.status, 200)
