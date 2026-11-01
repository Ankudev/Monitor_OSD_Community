import unittest
from aiohttp.test_utils import AioHTTPTestCase
import app
from admin_auth import KEY
from auth_helpers import login_admin
import test_app
from public_projection import public_data


class AdminSecurityTests(AioHTTPTestCase):
    get_application = test_app.ApiTests.get_application

    async def asyncTearDown(self):
        await super().asyncTearDown()
        self.temp_directory.cleanup()

    async def test_all_writes_denied_without_session(self):
        before = self.app[app.OBJECTS_PATH_KEY].read_bytes() if self.app[app.OBJECTS_PATH_KEY].exists() else None
        for method, path in [('POST','/api/objects'),('POST','/api/admin/objects'),('PATCH','/api/admin/objects/missing'),('DELETE','/api/objects/missing'),('DELETE','/api/admin/objects/missing'),('POST','/api/location/check')]:
            response = await self.client.request(method,path,json={})
            self.assertEqual(response.status,401,path)
        after = self.app[app.OBJECTS_PATH_KEY].read_bytes() if self.app[app.OBJECTS_PATH_KEY].exists() else None
        self.assertEqual(before,after)

    async def test_login_csrf_logout_and_public_projection(self):
        await login_admin(self)
        obj = self.app[app.CONFIG_KEY]['teryt_locations'][0]
        obj['power_supply'] = {'generator':'none','secondary_feed':'manual','notes':'PRIVATE-MARKER'}
        obj['keyword'] = 'PRIVATE-KEYWORD'
        response = await self.client.get('/api/admin/objects')
        self.assertIn('PRIVATE-MARKER', await response.text())
        self.assertEqual(response.headers['Cache-Control'],'no-store')
        for path in ['/api/objects','/api/v1/objects?view=full','/api/v1/map','/api/v1/locations','/api/v1/export.json','/api/v1/export.csv?dataset=objects']:
            response = await self.client.get(path)
            text = await response.text()
            self.assertEqual(response.status,200,path)
            self.assertNotIn('PRIVATE-MARKER',text,path)
            self.assertNotIn('PRIVATE-KEYWORD',text,path)
        response = await self.client.post('/api/admin/objects', json={},headers={'X-CSRF-Token':'bad'})
        self.assertEqual(response.status,403)
        response = await self.client.post('/api/admin/objects',json={},headers={'Origin':'https://evil.invalid'})
        self.assertEqual(response.status,403)
        response = await self.client.post('/api/admin/logout')
        self.assertEqual(response.status,200)
        self.assertEqual((await self.client.get('/api/admin/objects')).status,401)

    async def test_missing_configuration_and_rate_limit(self):
        auth = self.app[KEY]
        auth.dev = True
        auth.origin = str(self.client.make_url('/')).rstrip('/')
        response = await self.client.post('/api/admin/login',json={'password':'x'*20},headers={'Origin':auth.origin})
        self.assertEqual(response.status,503)
        await login_admin(self)
        for _ in range(6):
            response = await self.client.post('/api/admin/login',json={'password':'incorrect-password-value'})
        self.assertEqual(response.status,429)

    async def test_raw_denied_and_sessions_expire(self):
        self.assertEqual((await self.client.get('/api/v1/export.json?include_raw=1')).status,401)
        await login_admin(self)
        for session in self.app[KEY].sessions.values():
            session['used'] -= 1801
        self.assertEqual((await self.client.get('/api/admin/check')).status,401)

    async def test_stale_catalog_revision_does_not_overwrite(self):
        await login_admin(self)
        obj = self.app[app.CONFIG_KEY]['teryt_locations'][0]
        response = await self.client.patch('/api/admin/objects/'+obj['id'],json={'name':'Nie zapisywać'},headers={'X-Catalog-Revision':'old-revision'})
        self.assertEqual(response.status,409)
        self.assertNotEqual(obj.get('name'),'Nie zapisywać')


class ProjectionTests(unittest.TestCase):
    def test_recursive_projection_and_future_object_field(self):
        value = {'history':[{'raw':{'token':'secret'},'object':{'id':'1','city_sym':'1','name':'Test','future_secret':'secret','power_supply':{'notes':'private'}}}]}
        self.assertEqual(public_data(value),{'history':[{'object':{'id':'1','city_sym':'1','name':'Test'}}]})
