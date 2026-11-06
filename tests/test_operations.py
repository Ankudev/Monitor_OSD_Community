import asyncio
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
import unittest
from datetime import datetime, timezone, timedelta
from contextlib import closing
from unittest.mock import patch
from aiohttp.test_utils import TestClient, TestServer

import app
import admin_auth
import test_app as fixtures
from runtime_health import JobHealth, Readiness, OperationsToken, RequestMetrics, backup_health
from service_backup import create_snapshot, verify_snapshot, restore_snapshot, DATABASES
from tools.backup_service import upload, run_backup, exclusive_job, NoRedirect
from engine_store import EngineStore
from engine_store import history_record
import notification_queue as queue
from test_api_schema import validator


class SnapshotTests(unittest.TestCase):
    def test_job_lock_and_failed_attempt_do_not_destroy_last_success(self):
        status=self.root/'status.json'; status.write_text('previous')
        with exclusive_job(self.root/'job.lock'):
            with self.assertRaises(OSError):
                with exclusive_job(self.root/'job.lock'): pass
        with patch.dict(os.environ,{'RESTIC_REPOSITORY':''}), patch('tools.backup_service.shutil.which',return_value='restic'):
            with self.assertRaises(ValueError): run_backup('http://127.0.0.1:12345',status)
        self.assertEqual(status.read_text(),'previous')
        self.assertEqual(json.loads((self.root/'status.json.attempt.json').read_text())['state'],'failed')
        with self.assertRaises(ValueError): run_backup('http://remote.invalid',status)
        self.assertIsNone(NoRedirect().redirect_request(None,None,302,'',{},'https://other.invalid'))
    def setUp(self):
        self.directory=tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root=Path(self.directory.name)
        self.files={}
        for name in DATABASES:
            path=self.root/name
            if name == 'engine.sqlite3':
                store=EngineStore(path,self.root/'state.json')
                store.load_or_migrate(app.empty_state)
                asyncio.run(store.close())
            with closing(sqlite3.connect(path)) as db:
                db.execute('CREATE TABLE sample(value)')
                db.execute('INSERT INTO sample VALUES (?)',('wartość',))
                db.commit()
            self.files['data/'+name]=path
        for name,content in [('config.json',{'notifications_enabled':True,'our_api_url':'https://example.invalid/private'}),('objects.json',[{'id':'1'}])]:
            path=self.root/name
            path.write_text(json.dumps(content),encoding='utf8')
            self.files[name]=path
        self.snapshot=self.root/'snapshot'

    def test_snapshot_restore_preserves_values_and_cannot_overwrite(self):
        create_snapshot(self.files,self.snapshot,revision=4)
        self.assertEqual(verify_snapshot(self.snapshot)['revision'],4)
        target=self.root/'restored'
        restore_snapshot(self.snapshot,target)
        with closing(sqlite3.connect(target/'data/engine.sqlite3')) as db:
            self.assertEqual(db.execute('SELECT value FROM sample').fetchone()[0],'wartość')
        config=json.loads((target/'config-restore-test.json').read_text())
        self.assertFalse(config['notifications_enabled'])
        self.assertEqual(config['our_api_url'],'')
        with self.assertRaises(ValueError): restore_snapshot(self.snapshot,target)
        with self.assertRaises(ValueError): create_snapshot(self.files,self.snapshot)

    def test_wal_commits_are_included(self):
        connection=sqlite3.connect(self.files['data/engine.sqlite3'])
        self.addCleanup(connection.close)
        connection.execute('PRAGMA journal_mode=WAL')
        connection.execute('INSERT INTO sample VALUES (?)',('z WAL',))
        connection.commit()
        create_snapshot(self.files,self.snapshot)
        with closing(sqlite3.connect(self.snapshot/'data/engine.sqlite3')) as copied:
            self.assertEqual(copied.execute('SELECT COUNT(*) FROM sample').fetchone()[0],2)
            self.assertEqual(copied.execute('PRAGMA journal_mode').fetchone()[0],'delete')

    def test_extra_files_are_not_silently_uploaded(self):
        create_snapshot(self.files,self.snapshot)
        (self.snapshot/'unexpected-secret.txt').write_text('test')
        with self.assertRaises(ValueError): verify_snapshot(self.snapshot)

    def test_invalid_manifest_shapes_are_rejected(self):
        create_snapshot(self.files,self.snapshot)
        manifest=self.snapshot/'manifest.json'
        for value in ([], {'version':1,'scope':'application','files':[None]},
                      {'version':1,'scope':'application','files':[{'path':[]}]}):
            manifest.write_text(json.dumps(value))
            with self.assertRaises(ValueError): verify_snapshot(self.snapshot)

    def test_corruption_paths_and_missing_files_are_rejected(self):
        create_snapshot(self.files,self.snapshot)
        path=self.snapshot/'manifest.json'
        manifest=json.loads(path.read_text())
        manifest['files'][0]['path']='../config.json'
        path.write_text(json.dumps(manifest))
        with self.assertRaises(ValueError): verify_snapshot(self.snapshot)
        with self.assertRaises(ValueError): create_snapshot({'config.json':self.files['config.json']},self.root/'missing')

    def test_failed_copy_leaves_no_finished_or_partial_snapshot(self):
        with patch('service_backup.copy_database',side_effect=OSError('disk full')):
            with self.assertRaises(OSError): create_snapshot(self.files,self.snapshot)
        self.assertFalse(self.snapshot.exists())
        self.assertFalse(self.snapshot.with_name('snapshot.partial').exists())

    def test_upload_requires_success_summary_and_preserves_previous_status(self):
        create_snapshot(self.files,self.snapshot)
        password=self.root/'key'; password.write_text('test-only')
        status=self.root/'status.json'; status.write_text('previous')
        import subprocess
        with patch.dict(os.environ,RESTIC_REPOSITORY='local-test',RESTIC_PASSWORD_FILE=str(password)), patch('tools.backup_service.subprocess.run') as run:
            run.return_value=subprocess.CompletedProcess([],3,'','secret')
            with self.assertRaises(ValueError): upload(self.snapshot,status)
            self.assertEqual(status.read_text(),'previous')
            for stdout in ('not-json', '[]', '{"message_type":"summary","snapshot_id":"abc"}'):
                run.return_value=subprocess.CompletedProcess([],0,stdout,'')
                with self.assertRaises(ValueError): upload(self.snapshot,status)
                self.assertEqual(status.read_text(),'previous')
            run.return_value=subprocess.CompletedProcess([],0,json.dumps({'message_type':'summary','snapshot_id':'a'*64}),'')
            result=upload(self.snapshot,status)
            self.assertEqual(result['snapshot_id'],'a'*64)
            self.assertFalse(result['offsite_verified'])


class HealthTests(unittest.IsolatedAsyncioTestCase):
    async def test_backup_age_uses_data_time_and_failure_is_visible_immediately(self):
        now=datetime.now(timezone.utc)
        with tempfile.TemporaryDirectory() as directory:
            status=Path(directory)/'status.json'
            with patch.dict(os.environ,PGE_BACKUP_STATUS_FILE=str(status)):
                self.assertEqual(backup_health(now)['state'],'unavailable')
                status.write_text(json.dumps({'data_started_at':(now-timedelta(minutes=10)).isoformat()}))
                self.assertEqual(backup_health(now)['state'],'fresh')
                self.assertFalse(backup_health(now)['offsite_verified'])
                status.write_text(json.dumps({'data_started_at':(now-timedelta(minutes=61)).isoformat()}))
                self.assertEqual(backup_health(now)['state'],'stale')
                (Path(directory)/'status.json.attempt.json').write_text(json.dumps({'state':'failed'}))
                self.assertEqual(backup_health(now)['state'],'failed')
                (Path(directory)/'status.json.attempt.json').write_text('[]')
                self.assertEqual(backup_health(now)['state'],'unavailable')
    async def test_concurrent_readiness_queries_share_the_same_result(self):
        calls=[]
        def reader():
            calls.append(1)
            time.sleep(.02)
        readiness=Readiness(reader)
        results=await asyncio.gather(*(readiness.check() for _ in range(20)))
        self.assertTrue(all(results))
        self.assertEqual(len(calls),1)

    async def test_metrics_are_bounded_and_old_samples_expire(self):
        clock=[0]
        metrics=RequestMetrics(clock=lambda:clock[0])
        for i in range(1000): metrics.record(.05,500 if i == 999 else 200)
        self.assertEqual(metrics.payload()['sample_count'],512)
        self.assertEqual(metrics.payload()['errors_5xx'],1)
        clock[0]=301
        self.assertIsNone(metrics.payload()['p95_seconds'])

    async def test_readiness_timeout_reuses_one_inflight_read(self):
        finish=threading.Event()
        def reader(): finish.wait(5)
        readiness=Readiness(reader)
        try:
            with self.assertLogs('osd-monitor.readiness', level='WARNING') as logs:
                self.assertFalse(await readiness.check())
                original=readiness.task
                waiter=asyncio.create_task(readiness.check())
                await asyncio.sleep(0)
                waiter.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await waiter
                self.assertFalse(await readiness.check())
                self.assertIs(readiness.task,original)
            self.assertEqual(len(logs.output), 1)
            self.assertIn('reason=engine_read_timeout', logs.output[0])
        finally:
            finish.set()
            if readiness.task is not None: await readiness.task
        with self.assertLogs('osd-monitor.readiness', level='INFO') as logs:
            self.assertTrue(await readiness.check())
        self.assertIn('Przywrócono gotowość', logs.output[0])

    async def test_wait_and_stall_are_distinct_and_task_death_is_visible(self):
        now=[0]
        job=JobHealth(clock=lambda:now[0])
        event=asyncio.Event()
        job.task=asyncio.create_task(event.wait())
        job.mark('waiting',3600)
        now[0]=3500
        self.assertEqual(job.payload()['state'],'ok')
        now[0]=4000
        self.assertEqual(job.payload()['state'],'stalled')
        job.task.cancel(); await asyncio.gather(job.task,return_exceptions=True)
        self.assertEqual(job.payload()['state'],'stopped')
        self.assertEqual(job.payload(enabled=False)['state'],'disabled')

    async def test_readiness_distinguishes_policy_and_reader_errors_without_details(self):
        def fail():
            raise TimeoutError('private configuration')

        readiness=Readiness(fail)
        with self.assertLogs('osd-monitor.readiness', level='WARNING') as logs:
            self.assertFalse(await readiness.check())
            self.assertFalse(await readiness.check(policy_available=False))
        self.assertEqual(len(logs.output), 2)
        self.assertIn('reason=engine_read_error', logs.output[0])
        self.assertIn('reason=publication_policy_unavailable', logs.output[1])
        self.assertNotIn('private configuration', '\n'.join(logs.output))


class OperationsApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.fixture=fixtures.ApiTests()
        self.application=await self.fixture.get_application()
        self.application[app.OBJECTS_PATH_KEY].write_text(json.dumps(self.application[app.CONFIG_KEY]['teryt_locations']),encoding='utf8')
        self.client=TestClient(TestServer(self.application))
        await self.client.start_server()
        auth=self.application[admin_auth.KEY]
        auth.origin=str(self.client.make_url('')).rstrip('/')
        stamp=time.monotonic()
        auth.sessions['local-test']={'csrf':'test-csrf','created':stamp,'used':stamp}
        self.headers={'Cookie':auth.cookie+'=local-test','Origin':auth.origin,'X-CSRF-Token':'test-csrf'}

    async def asyncTearDown(self):
        await self.client.close()
        self.fixture.temp_directory.cleanup()

    async def test_health_is_separate_from_upstream_failure_and_admin_is_protected(self):
        self.application[app.RUNTIME_KEY]['sources']['pge']['error']='SourceMaintenanceError: private text'
        for path in ('/health/live','/health/ready'):
            response=await self.client.get(path)
            self.assertEqual(response.status,200)
            payload=await response.json()
            validator('LocalHealth').validate(payload)
            self.assertNotIn('private',json.dumps(payload))
        self.assertEqual((await self.client.get('/api/admin/operations')).status,401)
        response=await self.client.get('/api/admin/operations',headers=self.headers)
        self.assertEqual(response.status,200)
        payload=await response.json()
        validator('OperationsStatus').validate(payload)
        self.assertTrue(payload['local_ready'])
        self.assertEqual((await self.client.post('/api/admin/backups/snapshot')).status,401)
        with patch.dict(os.environ,{'PGE_BACKUP_STAGING':''}):
            self.assertEqual((await self.client.post('/api/admin/backups/snapshot',headers=self.headers)).status,503)

    async def test_operations_token_is_read_only_and_separate_from_admin(self):
        with tempfile.TemporaryDirectory() as directory:
            token=Path(directory)/'token'; token.write_text('x'*48)
            with patch.dict(os.environ,PGE_OPERATIONS_TOKEN_FILE=str(token)):
                self.application[app.OPERATIONS_TOKEN_KEY]=OperationsToken()
            self.assertEqual((await self.client.get('/api/operations')).status,401)
            headers={'Authorization':'Bearer '+'x'*48}
            response=await self.client.get('/api/operations',headers=headers)
            self.assertEqual(response.status,200)
            self.assertNotIn('xxxx',await response.text())
            for path in ('/api/admin/backups/snapshot','/api/admin/objects','/api/operations/snapshot'):
                self.assertEqual((await self.client.post(path,headers=headers)).status,401)

    async def test_snapshot_token_has_no_admin_or_metrics_rights(self):
        with tempfile.TemporaryDirectory() as directory:
            token=Path(directory)/'token';token.write_text('y'*48)
            with patch.dict(os.environ,PGE_BACKUP_TOKEN_FILE=str(token),PGE_BACKUP_STAGING=''):
                self.application[app.BACKUP_TOKEN_KEY]=OperationsToken('PGE_BACKUP_TOKEN_FILE')
                headers={'Authorization':'Bearer '+'y'*48}
                self.assertEqual((await self.client.post('/api/operations/snapshot',headers=headers)).status,503)
                self.assertEqual((await self.client.get('/api/admin/notifications',headers=headers)).status,401)
                self.assertEqual((await self.client.post('/api/admin/objects',headers=headers)).status,401)
                self.assertEqual((await self.client.get('/api/operations',headers=headers)).status,401)

    async def test_delivery_history_is_private_paginated_and_omits_destination(self):
        event=dict(source='pge',object_id='one',event_key='pge:one',source_hash='b'*64,
                   description='Test',status='active')
        record=history_record('new',event,None,'2026-10-23T08:00:00Z')
        job=queue.make_job(record,event,'https://receiver.invalid/SECRET','fp',{'private':'SECRET'},now=1000)
        await self.application[app.ENGINE_STORE_KEY].write_state(self.application[app.STATE_KEY],history_records=[record],notification_jobs=[job])
        self.assertEqual((await self.client.get('/api/admin/notifications')).status,401)
        response=await self.client.get('/api/admin/notifications?limit=1',headers=self.headers)
        self.assertEqual(response.status,200)
        payload=await response.json()
        validator('DeliveryHistoryPage').validate(payload)
        self.assertEqual(payload['total'],1)
        self.assertEqual(payload['items'][0]['status'],'pending')
        self.assertNotIn('SECRET',json.dumps(payload))
        self.assertNotIn('destination_url',payload['items'][0])
        self.assertNotIn('payload_json',payload['items'][0])
        for query in ('limit=101','offset=-1','status=unknown','unknown=1','limit=1&limit=2'):
            self.assertEqual((await self.client.get('/api/admin/notifications?'+query,headers=self.headers)).status,400)

    async def test_stalled_job_is_visible_while_readiness_stays_successful(self):
        job=self.application[app.JOBS_KEY]['pge']
        self.application[app.START_SCANNER_KEY]=True
        job.task=asyncio.create_task(asyncio.Event().wait())
        job.deadline=time.monotonic()-1
        try:
            response=await self.client.get('/api/admin/operations',headers=self.headers)
            self.assertEqual((await response.json())['jobs']['pge']['state'],'stalled')
            self.assertEqual((await self.client.get('/health/ready')).status,200)
        finally:
            job.task.cancel()
            await asyncio.gather(job.task,return_exceptions=True)
            self.application[app.START_SCANNER_KEY]=False

    async def test_snapshot_endpoint_uses_only_configured_paths(self):
        with tempfile.TemporaryDirectory() as staging, patch.dict(os.environ,PGE_BACKUP_STAGING=staging):
            response=await self.client.post('/api/admin/backups/snapshot',headers=self.headers)
            self.assertEqual(response.status,200,await response.text())
            payload=await response.json()
            validator('BackupSnapshot').validate(payload)
            snapshot=Path(staging)/payload['snapshot_id']
            self.assertEqual(len(verify_snapshot(snapshot)['files']),len(DATABASES)+2)
            self.assertEqual((await self.client.post('/api/admin/backups/snapshot?path=anything',headers=self.headers)).status,400)

    async def test_cancelled_snapshot_keeps_catalog_locks_until_thread_finishes(self):
        from types import SimpleNamespace
        entered, finish = threading.Event(), threading.Event()
        def slow_snapshot(*args,**kwargs):
            entered.set()
            if not finish.wait(5): raise TimeoutError()
            return {'scope':'application','started_at':'test','completed_at':'test'}
        with tempfile.TemporaryDirectory() as staging, patch.dict(os.environ,PGE_BACKUP_STAGING=staging), patch('app.create_snapshot',side_effect=slow_snapshot):
            request=SimpleNamespace(app=self.application, query={}, can_read_body=False)
            task=asyncio.create_task(app.backup_snapshot_handler(request))
            await asyncio.to_thread(entered.wait,2)
            task.cancel()
            await asyncio.sleep(0)
            self.assertTrue(self.application[app.OBJECTS_LOCK_KEY].locked())
            self.assertTrue(self.application[app.SCAN_LOCK_KEY].locked())
            finish.set()
            with self.assertRaises(asyncio.CancelledError): await task
            self.assertFalse(self.application[app.OBJECTS_LOCK_KEY].locked())
