import asyncio
import copy
import json
import socket
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import app
import osd_sources
import aiohttp
from aiohttp import web
from aiohttp.test_utils import TestServer
from scan_diagnostics import Diagnostics, SourceContractError, error_details, request_attempt, measured, CURRENT, copy_database, source_trace
from catalog_quality import report
import test_api_v1
import test_notification_queue
from test_notification_queue import sample_event


class DiagnosticsStoreTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Diagnostics(Path(self.temp.name)/'diagnostics.sqlite3', max_runs=2)
        await self.store.start()

    async def success(self, count, scope='one'):
        async with self.store.capture('pge', scope) as run:
            request_attempt(1, 'https://example.test/?secret=hidden')
            with measured('parse'): pass
            run.data.update(status='success', completeness='complete', source_event_count=count, state_revision=5)

    async def test_retention_pagination_warning_and_no_secret(self):
        await self.success(100)
        await self.success(0)
        first = await self.store.read(limit=1)
        self.assertEqual(first['items'][0]['warnings'], ['record_count_drop'])
        self.assertEqual(first['items'][0]['request_count'], 1)
        self.assertNotIn('secret', json.dumps(first))
        second = await self.store.read(before=first['next_before'])
        self.assertEqual(second['items'][0]['source_event_count'], 100)
        await self.success(0, 'different_filters')
        result = await self.store.read()
        self.assertEqual(result['count'], 2)
        self.assertEqual(result['items'][0]['warnings'], [])

    async def test_restart_marks_unfinished_and_preserves_success(self):
        run = self.store.capture('pge', 'one')
        await self.store.safe(self.store._save, run.data)
        await self.success(12)
        await self.store.start()
        rows = (await self.store.read())['items']
        self.assertEqual([x['status'] for x in rows], ['success','interrupted'])

    async def test_cancel_and_isolated_contexts(self):
        with self.assertRaises(asyncio.CancelledError):
            async with self.store.capture('pge','one'):
                raise asyncio.CancelledError()
        self.assertIsNone(CURRENT.get())
        async def task(source):
            async with self.store.capture(source,'one') as run:
                request_attempt(1, source)
                await asyncio.sleep(.01)
                run.data.update(status='success', source_event_count=1)
        await asyncio.gather(task('pge'),task('enea'))
        rows=(await self.store.read())['items']
        self.assertEqual({r['source'] for r in rows}, {'pge','enea'})
        self.assertTrue(all(r['request_count']==1 for r in rows))

    async def test_diagnostics_failure_does_not_raise(self):
        with patch.object(self.store, '_save', side_effect=OSError('secret')):
            await self.success(1)
        self.assertEqual(self.store.error, 'OSError')

    async def test_read_does_not_clear_write_failure_and_write_recovers(self):
        await self.success(1)
        with patch.object(self.store, '_save', side_effect=OSError('full')):
            await self.success(1)
            self.assertEqual((await self.store.read())['count'], 1)
            self.assertEqual(self.store.error, 'OSError')
        await self.success(1)
        self.assertIsNone(self.store.error)

    async def test_redirects_are_counted_as_http_requests(self):
        observed = []
        async def redirect(request):
            observed.append(request.path)
            raise web.HTTPFound('/target')
        async def target(request):
            observed.append(request.path)
            return web.Response(text='ok')
        server_app = web.Application()
        server_app.router.add_get('/redirect',redirect)
        server_app.router.add_get('/target',target)
        async with TestServer(server_app) as server, aiohttp.ClientSession(trace_configs=[source_trace()]) as session:
            async with self.store.capture('pge','synthetic-local-http') as run:
                await osd_sources._request_text(session,'GET',str(server.make_url('/redirect')),retries=1)
                run.data.update(status='success',source_event_count=0)
        row=(await self.store.read())['items'][0]
        self.assertEqual(row['request_count'],len(observed))
        self.assertEqual(row['completed_units']['http_responses'],2)
        self.assertTrue(row['request_count_complete'])

    async def test_internal_connection_retry_is_counted(self):
        observed=[]
        async def handler(request):
            observed.append(request.path)
            if len(observed)==1:
                request.transport.close()
            return web.Response(text='ok')
        server_app=web.Application();server_app.router.add_get('/',handler)
        async with TestServer(server_app) as server, aiohttp.ClientSession(trace_configs=[source_trace()]) as session:
            async with self.store.capture('pge','synthetic-local-http') as run:
                await osd_sources._request_text(session,'GET',str(server.make_url('/')),retries=1)
                run.data.update(status='success',source_event_count=0)
        row=(await self.store.read())['items'][0]
        self.assertEqual(len(observed),2)
        self.assertEqual((row['request_count'],row['retry_count']),(2,1))
        self.assertTrue(row['request_count_complete'])

    async def test_pge_blocks_internal_retry_before_second_request(self):
        observed=[]
        async def handler(request):
            observed.append(request.path)
            request.transport.close()
            return web.Response(text='[]')
        server_app=web.Application();server_app.router.add_get('/',handler)
        async with TestServer(server_app) as server, aiohttp.ClientSession() as session:
            with patch('osd_sources.PGE_URL',str(server.make_url('/'))):
                async with self.store.capture('pge','synthetic-local-http') as run:
                    with self.assertRaises(RuntimeError) as error:
                        await osd_sources.fetch_pge(session,{})
                    run.failed(error.exception)
        row=(await self.store.read())['items'][0]
        self.assertEqual(observed,['/'])
        self.assertEqual((row['request_count'],row['retry_count']),(1,0))
        self.assertEqual(row['error_code'],'client_retry_blocked')

    async def test_pge_does_not_follow_redirect(self):
        observed=[]
        async def redirect(request):
            observed.append(request.path);raise web.HTTPFound('/target')
        async def target(request):
            observed.append(request.path);return web.Response(text='[]')
        server_app=web.Application();server_app.router.add_get('/',redirect);server_app.router.add_get('/target',target)
        async with TestServer(server_app) as server, aiohttp.ClientSession() as session:
            with patch('osd_sources.PGE_URL',str(server.make_url('/'))):
                async with self.store.capture('pge','synthetic-local-http') as run:
                    with self.assertRaises(RuntimeError) as error: await osd_sources.fetch_pge(session,{})
                    run.failed(error.exception)
        row=(await self.store.read())['items'][0]
        self.assertEqual(observed,['/'])
        self.assertEqual((row['request_count'],row['http_status']),(1,302))

    async def test_backup_restore_matches_and_refuses_overwrite(self):
        await self.success(12)
        backup=Path(self.temp.name)/'backup.sqlite3'
        restored=Path(self.temp.name)/'restored.sqlite3'
        copy_database(self.store.path,backup)
        copy_database(backup,restored)
        self.assertEqual(Diagnostics(restored)._read()['items'],self.store._read()['items'])
        with self.assertRaises(ValueError): copy_database(backup,restored)

    async def test_request_counter_counts_attempts_not_only_responses(self):
        class Session:
            def request(self,*args,**kwargs): raise TimeoutError()
        async with self.store.capture('pge','one') as run:
            with self.assertRaises(RuntimeError) as error:
                with patch('osd_sources.asyncio.sleep', return_value=None):
                    await osd_sources._request_text(Session(),'GET','https://example.test/?token=hidden',retries=2)
            run.failed(error.exception)
        row=(await self.store.read())['items'][0]
        self.assertEqual((row['request_count'],row['retry_count'],row['error_code']),(2,1,'timeout'))
        self.assertFalse(row['request_count_complete'])

    async def test_geometry_validation_error_is_a_parser_failure(self):
        async with self.store.capture('energa','one') as run:
            with measured('parse'):
                run.failed(ValueError('Niepoprawna geometria'))
        row=(await self.store.read())['items'][0]
        self.assertEqual((row['error_code'],row['stage'],row['completeness']),('contract','parse','incomplete'))

    def test_typed_causes_and_safe_messages(self):
        for error,code in [(socket.gaierror('secret'),'dns'), (TimeoutError(),'timeout'),
                           (SourceContractError('secret','incomplete'),'incomplete'),
                           (SourceContractError('secret','conflicting_id'),'conflicting_id'),
                           (osd_sources.SourceHttpError('secret',status=429),'http')]:
            outer=RuntimeError('token=hidden');outer.__cause__=error
            result=error_details(outer)
            self.assertEqual(result['error_code'],code)
            self.assertNotIn('secret',json.dumps(result))
            self.assertNotIn('hidden',json.dumps(result))


class DiagnosticsApiTests(test_api_v1.PublicApiV1Tests):
    async def test_diagnostic_api_quality_cache_and_validation(self):
        store = self.app[app.DIAGNOSTICS_KEY]
        async with store.capture('pge','one') as run:
            run.data.update(status='success', source_event_count=0)
        response=await self.client.get('/api/v1/diagnostics/scans?source=pge&limit=1')
        self.assertEqual(response.status,200)
        self.assertEqual((await response.json())['items'][0]['status'],'success')
        self.assertNotIn('Access-Control-Allow-Origin', response.headers)
        response=await self.client.get('/api/v1/diagnostics/quality?limit=1')
        self.assertEqual(response.status,200)
        payload=await response.json()
        self.assertEqual(response.headers['Cache-Control'], 'no-store')
        self.assertEqual(payload['external_requests'],0)
        self.assertEqual((await self.client.get('/api/v1/diagnostics/quality?limit=1')).status,200)
        for query in ['limit=0','limit=201','source=bad','before=-1','status=bad','unknown=1']:
            self.assertEqual((await self.client.get('/api/v1/diagnostics/scans?'+query)).status,400)
        self.assertEqual((await self.client.get('/api/v1/diagnostics/quality?snapshot_id=old')).status,409)

    async def test_report_does_not_edit_catalog_or_confirm_missing_evidence(self):
        objects=copy.deepcopy(self.app[app.CONFIG_KEY]['teryt_locations'])
        before=copy.deepcopy(objects)
        objects.append(copy.deepcopy(objects[0]))
        objects[0]['latitude']=True
        result=report(objects,self.app[app.TERYT_DB_PATH_KEY])
        self.assertIn('duplicate_id',result['summary'])
        self.assertIn('invalid_gps',result['summary'])
        self.assertIn('location_unverified',result['summary'])
        self.assertEqual(self.app[app.CONFIG_KEY]['teryt_locations'],before)

    async def test_api_exposes_write_failure_despite_successful_read(self):
        store=self.app[app.DIAGNOSTICS_KEY]
        with patch.object(store,'_save',side_effect=OSError('full')):
            async with store.capture('pge','test') as run:
                run.data.update(status='success',source_event_count=0)
            response=await self.client.get('/api/v1/diagnostics/scans')
            body=await response.json()
            self.assertTrue(body['diagnostics_degraded'])
            self.assertEqual(body['diagnostics_error_code'],'OSError')
            status=await self.client.get('/api/status')
            self.assertTrue((await status.json())['diagnostics']['degraded'])


class ScanDiagnosticsIntegrationTests(test_notification_queue.ScanQueueTests):
    async def test_success_committed_revision_and_skip_does_not_record(self):
        await self.scan(sample_event())
        diagnostics=self.application[app.DIAGNOSTICS_KEY]
        rows=(await diagnostics.read())['items']
        self.assertEqual(len(rows),1)
        self.assertEqual(rows[0]['status'],'success')
        self.assertEqual(rows[0]['state_revision'],self.application[app.STORAGE_STATUS_KEY]['revision'])
        with patch('app.fetch_source', side_effect=AssertionError('Unexpected HTTP')):
            result=await app.scan_source(self.application,'pge')
        self.assertTrue(result['skipped'])
        self.assertEqual((await diagnostics.read())['count'],1)

    async def test_failed_diagnostics_does_not_lose_state_history_or_job(self):
        diagnostics=self.application[app.DIAGNOSTICS_KEY]
        with patch.object(diagnostics,'_save',side_effect=OSError('full')):
            await self.scan(sample_event())
        self.assertEqual(len(self.jobs()),1)
        self.assertEqual(self.store.read_history()['total'],1)
        self.assertEqual(diagnostics.error,'OSError')

    async def test_incomplete_read_is_recorded_without_deleting_previous(self):
        await self.scan(sample_event())
        state=copy.deepcopy(self.application[app.STATE_KEY]['events'])
        self.application[app.RUNTIME_KEY]['sources']['pge']['last_attempt_at']=None
        with patch('app.fetch_source',side_effect=SourceContractError('incomplete','incomplete')):
            result=await app.scan_source(self.application,'pge')
        self.assertFalse(result['ok'])
        self.assertEqual(self.application[app.STATE_KEY]['events'],state)
        row=(await self.application[app.DIAGNOSTICS_KEY].read())['items'][0]
        self.assertEqual(row['error_code'],'incomplete')
        self.assertEqual(row['completeness'],'incomplete')
