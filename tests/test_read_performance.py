"""Zgodność liczników i granicy prywatności po optymalizacji odczytu."""
import json
from unittest.mock import patch
import unittest

from aiohttp import web
from aiohttp.test_utils import AioHTTPTestCase, TestClient, TestServer
import admin_auth
import api_v1
from auth_helpers import login_admin
from public_projection import public_data
import test_app as fixtures


class LocationProjectionTests(unittest.TestCase):
    def test_grouping_preserves_counts_order_pending_and_fresh_reads(self):
        objects = [{'id':'2','name':'Żuraw','city_sym':'1'}, {'id':'1','name':'Anteny','city_sym':'1'}]
        events = [dict(object_id='1',phase='ongoing',match_evidence={'version':2,'scope':'confirmed_point','point_confirmed':True}),
                  dict(object_id=1,phase='upcoming'),dict(object_id='1',phase='unknown'),
                  dict(object_id='2',phase='overdue'),dict(object_id='outside',phase='ongoing')]
        context = {'objects':lambda:objects,'event_time_phase':lambda event:event['phase']}
        result = api_v1._locations(context,events=events,pending={'2':['enea']})
        self.assertEqual([item['object_id'] for item in result],['1','2'])
        one,two = result
        self.assertEqual([one[key] for key in ('confirmed_event_count','candidate_event_count','active_event_count','upcoming_event_count','attention_event_count')], [1,2,1,1,1])
        self.assertEqual(two['attention_event_count'],1)
        self.assertEqual(two['matching_pending_sources'],['enea'])
        events[0]['phase'] = 'overdue'
        objects[0]['name'] = 'Nowa nazwa'
        fresh = api_v1._locations(context,events=events,pending={})
        self.assertEqual(fresh[0]['active_event_count'],0)
        self.assertEqual(next(item for item in fresh if item['object_id']=='2')['name'],'Nowa nazwa')

    def test_event_collection_is_traversed_once_for_many_objects(self):
        class CountedEvents(list):
            visits = 0
            def __iter__(self):
                for item in super().__iter__():
                    self.visits += 1
                    yield item
        events = CountedEvents(dict(object_id=str(index),phase='ongoing') for index in range(200))
        context = {'objects':lambda:[{'id':str(i),'name':str(i)} for i in range(200)],
                   'event_time_phase':lambda event:event['phase']}
        result = api_v1._locations(context,events=events,pending={})
        self.assertEqual(sum(item['active_event_count'] for item in result),200)
        self.assertEqual(events.visits,200)


class PublicResponseTests(AioHTTPTestCase):
    get_application = fixtures.ApiTests.get_application

    async def asyncTearDown(self):
        await super().asyncTearDown()
        self.temp_directory.cleanup()

    async def test_public_reads_skip_unused_raw_and_keep_response_and_engine_unchanged(self):
        import app
        from copy import deepcopy
        class RawNotCopied(dict):
            def __deepcopy__(self,memo):
                raise AssertionError('Publiczny odczyt nie powinien kopiować surowego rekordu')
        events=app.normalize_source_events([fixtures.osd_sources.normalize_pge(fixtures.sample_event())],self.app[app.CONFIG_KEY])
        self.assertTrue(events)
        self.app[app.RUNTIME_KEY]['events']=events
        paths=('/api/v1/map','/api/v1/locations','/api/v1/events?view=full','/api/v1/summary')
        expected={path:await (await self.client.get(path)).json() for path in paths}
        before=deepcopy(events)
        for event in events.values():
            event['pge_event']=RawNotCopied(event.get('pge_event') or {})
            event['source_event']['raw']=RawNotCopied(event['source_event'].get('raw') or {})
        for path in paths:
            response=await self.client.get(path)
            self.assertEqual(response.status,200)
            actual=await response.json()
            self.assertEqual(actual,expected[path])
            self.assertNotIn('pge_event',json.dumps(actual))
        self.assertEqual(events,before)

    async def test_v1_is_projected_once_and_legacy_still_has_boundary_filter(self):
        import app
        obj = self.app[app.CONFIG_KEY]['teryt_locations'][0]
        obj.update(keyword='PRIVATE-KEYWORD',power_supply={'notes':'PRIVATE-NOTE'},
                   location_audit={'private_file':'PRIVATE-PATH'})
        headers = {'X-Osd-Public-Projection':'true'}
        with patch('admin_auth.public_data',side_effect=AssertionError('Powtórna projekcja')):
            response = await self.client.get('/api/v1/map',headers=headers)
            self.assertEqual(response.status,200)
            text = await response.text()
            self.assertNotIn('PRIVATE',text)
        with patch('admin_auth.public_data',wraps=public_data) as boundary:
            response = await self.client.get('/api/objects',headers=headers)
            self.assertNotIn('PRIVATE',await response.text())
            boundary.assert_called_once()

    async def test_raw_requires_session_and_admin_catalog_stays_private(self):
        import app
        obj = self.app[app.CONFIG_KEY]['teryt_locations'][0]
        obj.update(keyword='PRIVATE-KEYWORD',power_supply={'notes':'PRIVATE-NOTE'})
        self.assertEqual((await self.client.get('/api/v1/export.json?include_raw=1')).status,401)
        await login_admin(self)
        response = await self.client.get('/api/admin/objects')
        self.assertIn('PRIVATE-NOTE',await response.text())
        self.assertEqual(response.headers['Cache-Control'],'no-store')
        public = await self.client.get('/api/v1/objects?view=full')
        self.assertNotIn('PRIVATE',await public.text())

    async def test_etag_stays_valid_and_private_data_does_not_reappear_in_304(self):
        response = await self.client.get('/api/v1/objects')
        tag = response.headers['ETag']
        self.assertTrue((await response.json())['items'])
        cached = await self.client.get('/api/v1/objects',headers={'If-None-Match':tag})
        self.assertEqual(cached.status,304)
        self.assertEqual(await cached.read(),b'')

    async def test_changing_marked_body_restores_boundary_filter(self):
        @web.middleware
        async def replace_body(request,handler):
            response = await handler(request)
            response.body = json.dumps({'keyword':'PRIVATE','visible':True}).encode()
            return response
        application = web.Application(middlewares=[admin_auth.security_middleware,replace_body])
        admin_auth.register(application)
        async def handler(request):
            return api_v1._response(request,{'visible':False})
        application.router.add_get('/api/test',handler)
        async with TestClient(TestServer(application)) as client:
            result = await (await client.get('/api/test')).json()
            self.assertEqual(result,{'visible':True})
