import asyncio
import copy
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

import aiohttp
from aiohttp import web
from aiohttp.test_utils import TestServer
import app
import notification_queue as q
from engine_store import EngineStore, EngineStoreError, history_record, inspect_database, restore_database
import test_app as fixtures
from test_app import sample_event
from address_matching import address_binding
import osd_sources


class QueueTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.db = self.base/'engine.sqlite3'
        self.store = EngineStore(self.db, self.base/'state.json')
        self.store.load_or_migrate({'events':{}})

    async def asyncTearDown(self):
        await self.store.close()
        self.tmp.cleanup()

    def job(self, object_id='one', url='http://receiver.test/hook', action='updated'):
        event = dict(source='pge',object_id=object_id,event_key='pge:e:'+object_id,
                     source_hash='B'*64,description='Test',status='active')
        record = history_record(action,event,None,'2026-10-07T18:00:00Z')
        job = q.make_job(record,event,url,'fp',app.notification_payload(action,event),now=1000)
        return record,job

    async def insert(self, object_id='one', url='http://receiver.test/hook'):
        record,job = self.job(object_id,url)
        await self.store.write_state({'events':{}},history_records=[record],notification_jobs=[job])
        return job

    def rows(self):
        with closing(sqlite3.connect(self.db)) as c:
            c.row_factory = sqlite3.Row
            return [dict(r) for r in c.execute('SELECT * FROM notification_jobs ORDER BY sequence')]

    async def claim(self, at=1000, url='http://receiver.test/hook'):
        return await self.store.queue_operation('claim',q.destination_id(url),at)

    async def test_atomic_rollback_and_export_failure(self):
        record,job = self.job()
        before = self.store.load_current()
        with self.assertRaises(EngineStoreError):
            await self.store.write_state({'events':{'new':{}}},history_records=[record],notification_jobs=[job],failpoint='before_commit')
        self.assertEqual(self.store.load_current(),before)
        self.assertEqual(self.rows(),[])
        with patch('engine_store._atomic_json_export',side_effect=OSError('disk')):
            result = await self.store.write_state({'events':{}},history_records=[record],notification_jobs=[job])
        self.assertIsNotNone(result.export_error)
        self.assertEqual(len(self.rows()),1)
        bad_record,bad_job = self.job()
        bad_job['change_id']='absent'
        with self.assertRaises(EngineStoreError):
            await self.store.write_state({'events':{'bad':{}}},history_records=[bad_record],notification_jobs=[bad_job])
        self.assertEqual(len(self.rows()),1)
        self.assertEqual(self.store.load_current()[0]['events'],{})

    async def test_transition_identity_payload_and_duplicate_conflict(self):
        jobs = [self.job() for _ in range(2)]
        self.assertNotEqual(jobs[0][1]['job_id'],jobs[1][1]['job_id'])
        record,job = jobs[0]
        await self.store.write_state({'events':{}},history_records=[record],notification_jobs=[job])
        await self.store.write_state({'events':{}},notification_jobs=[job])
        self.assertEqual(len(self.rows()),1)
        conflict = dict(job,payload_json=job['payload_json']+' ')
        with self.assertRaises(EngineStoreError):
            await self.store.write_state({'events':{}},notification_jobs=[conflict])
        body = json.loads(job['payload_json'])
        self.assertEqual(body['change_id'],record['change_id'])
        self.assertEqual(body['notification_id'],job['job_id'])
        self.assertEqual(inspect_database(self.db)['integrity'],'ok')

    async def test_two_claims_lease_expiry_and_stale_token(self):
        await self.insert()
        first,second = await asyncio.gather(self.claim(),self.claim())
        claimed = first or second
        self.assertIsNone(second if first else first)
        self.assertIsNone(await self.claim(1089))
        self.assertIsNone(await self.claim(1090))
        recovered = await self.claim(1150)
        self.assertNotEqual(recovered['lease_token'],claimed['lease_token'])
        self.assertFalse(await self.store.queue_operation('finish',claimed,{'status':'delivered'},1151))
        self.assertTrue(await self.store.queue_operation('finish',recovered,{'status':'delivered'},1152))

    async def test_retry_six_attempts_and_manual_budget(self):
        await self.insert()
        at=1000
        for attempt in range(1,7):
            job=await self.claim(at)
            self.assertEqual(job['attempts'],attempt)
            await self.store.queue_operation('finish',job,{'status':'pending','code':'TimeoutError'},at)
            row=self.rows()[0]
            self.assertEqual(row['status'],'failed' if attempt==6 else 'pending')
            self.assertIsNone(await self.claim(at))
            at=row['next_attempt_at']
        await self.store.queue_operation('decide',job['job_id'],'retry','Odbiorca naprawiony',at)
        retried=await self.claim(at)
        self.assertEqual(retried['attempts'],7)
        self.assertEqual(retried['max_attempts'],12)
        self.assertEqual(retried['payload_json'],job['payload_json'])

    async def test_ordering_failed_blocks_only_same_object(self):
        a=await self.insert();b=await self.insert();other=await self.insert('two')
        first=await self.claim()
        await self.store.queue_operation('finish',first,{'status':'failed','http_status':400},1000)
        next_job=await self.claim()
        self.assertEqual(next_job['job_id'],other['job_id'])
        await self.store.queue_operation('finish',next_job,{'status':'delivered'},1000)
        self.assertIsNone(await self.claim())
        report=await self.store.queue_operation('summary',q.destination_id(a['destination_url']),1000)
        self.assertEqual(report['blocked_by_failure'],1)
        await self.store.queue_operation('decide',a['job_id'],'skip','Nie wysyłać starej zmiany',1000)
        self.assertEqual((await self.claim())['job_id'],b['job_id'])

    async def test_destination_isolation_and_catalog_skip(self):
        old=await self.insert()
        new=await self.insert(url='http://new.test/hook')
        self.assertEqual((await self.claim(url=new['destination_url']))['job_id'],new['job_id'])
        report=await self.store.queue_operation('summary',q.destination_id(new['destination_url']),1000)
        self.assertEqual(report['other_destination_count'],1)
        worker=q.NotificationWorker(self.store,lambda:dict(notifications_enabled=True,our_api_url=old['destination_url']),
                                    lambda job: asyncio.sleep(0,result='Adres zmieniony'))
        with patch('notification_queue.time.time',return_value=1000):
            await worker.start()
            try:
                for _ in range(50):
                    if self.rows()[0]['status']=='skipped': break
                    await asyncio.sleep(.01)
                self.assertEqual(self.rows()[0]['status'],'skipped')
            finally: await worker.close()


    async def test_six_expired_claims_terminal_failure_and_corrupt_payload(self):
        await self.insert()
        at=1000
        for attempt in range(6):
            if attempt:
                self.assertIsNone(await self.claim(at))
                at=self.rows()[0]['next_attempt_at']
            job=await self.claim(at)
            self.assertEqual(job['attempts'],attempt+1)
            at+=91
        self.assertIsNone(await self.claim(at))
        self.assertEqual(self.rows()[0]['status'],'failed')
        with closing(sqlite3.connect(self.db)) as c:
            with c: c.execute("UPDATE notification_jobs SET payload_json='{}'")
        with self.assertRaises(EngineStoreError): inspect_database(self.db)

    async def test_worker_pause_and_storage_failure_never_marks_memory_success(self):
        await self.insert()
        config={'notifications_enabled':False,'our_api_url':'http://receiver.test/hook'}
        worker=q.NotificationWorker(self.store,lambda:config,lambda job:asyncio.sleep(0,result=None))
        with patch('notification_queue.time.time',return_value=1000),patch('notification_queue.deliver',return_value={'status':'delivered'}) as delivery:
            await worker.start()
            try:
                await asyncio.sleep(.03)
                delivery.assert_not_called()
                config['notifications_enabled']=True
                original=self.store.queue_operation
                async def failure(operation,*args):
                    if operation=='finish': raise OSError('disk')
                    return await original(operation,*args)
                with patch.object(self.store,'queue_operation',side_effect=failure):
                    worker.wake.set()
                    for _ in range(100):
                        if worker.error: break
                        await asyncio.sleep(.01)
                    self.assertEqual(worker.state,'storage_error')
                    self.assertEqual(self.rows()[0]['status'],'sending')
            finally: await worker.close()

    async def test_cli_decisions_do_not_overwrite_live_lease_and_payload(self):
        original=await self.insert();job=await self.claim()
        with self.assertRaises(ValueError):
            await self.store.queue_operation('decide',job['job_id'],'skip','reason',1001)
        await self.store.queue_operation('finish',job,{'status':'failed'},1001)
        result=await asyncio.to_thread(subprocess.run,[sys.executable,'-B','notification_queue.py',str(self.db),'retry','--job-id',job['job_id'],'--reason','Naprawiono odbiorcę'],capture_output=True,timeout=10)
        self.assertEqual(result.returncode,0,result.stderr)
        row=self.rows()[0]
        self.assertEqual(row['payload_json'],original['payload_json'])
        self.assertEqual(row['last_decision_reason'],'Naprawiono odbiorcę')

    async def test_actual_process_death_before_after_commit_and_claim(self):
        script=r'''
import sys,json,os
import engine_store as e
original=e._connect
class Crash:
 def __init__(self,c):self.c=c
 def __getattr__(self,n):return getattr(self.c,n)
 def commit(self):
  if sys.argv[3]=='before':os._exit(71)
  self.c.commit();os._exit(72)
 def __enter__(self):return self
 def __exit__(self,t,v,b):
  if t:self.c.rollback()
  else:self.commit()
e._connect=lambda p:Crash(original(p))
s=e.EngineStore(sys.argv[1],sys.argv[2])
data=json.loads(sys.argv[4])
if sys.argv[5]=='claim':s._queue_operation('claim',data['destination_id'],1000)
else:s._write_state({'events':{'new':{}}},history_records=[data['record']],notification_jobs=[data['job']])
'''
        for mode in ('write','claim'):
            for position in ('before','after'):
                with self.subTest(mode=mode,position=position):
                    db=self.base/(mode+position+'.sqlite3')
                    if mode=='claim' and not self.rows(): await self.insert()
                    await self.store.backup(db)
                    record,job=self.job()
                    data={'record':record,'job':job,'destination_id':job['destination_id']}
                    result=await asyncio.to_thread(subprocess.run,[sys.executable,'-B','-c',script,str(db),str(self.base/'crash.json'),position,json.dumps(data),mode],capture_output=True,timeout=20)
                    self.assertEqual(result.returncode,71 if position=='before' else 72,result.stderr)
                    with closing(sqlite3.connect(db)) as c:
                        if mode=='write':
                            self.assertEqual(c.execute('SELECT COUNT(*) FROM notification_jobs').fetchone()[0],0 if position=='before' else 1)
                            self.assertEqual(c.execute('SELECT COUNT(*) FROM event_changes').fetchone()[0],0 if position=='before' else 1)
                        else:
                            self.assertEqual(c.execute('SELECT status FROM notification_jobs').fetchone()[0],'pending' if position=='before' else 'sending')
                    self.assertEqual(inspect_database(db)['integrity'],'ok')

    async def test_process_death_during_http_and_after_receiver_accepts(self):
        """Odbiorca ma trwałą deduplikację; proces nadawcy faktycznie ginie."""
        receiver_db=self.base/'receiver.sqlite3'
        with closing(sqlite3.connect(receiver_db)) as c:
            c.execute('CREATE TABLE accepted(id TEXT PRIMARY KEY, body TEXT)'); c.commit()
        received=asyncio.Event()
        release=asyncio.Event()
        blocking=True
        async def receiver(request):
            body=await request.text()
            with closing(sqlite3.connect(receiver_db)) as c:
                c.execute('INSERT OR IGNORE INTO accepted VALUES(?,?)',(request.headers['Idempotency-Key'],body));c.commit()
            received.set()
            if blocking:
                await release.wait()
            return web.Response(status=204)
        application=web.Application();application.router.add_post('/hook',receiver)
        script=r'''
import asyncio,sys,os
from engine_store import EngineStore
import notification_queue as q
import aiohttp
async def run():
 s=EngineStore(sys.argv[1],sys.argv[2])
 job=await s.queue_operation('claim',q.destination_id(sys.argv[3]),1000)
 async with aiohttp.ClientSession() as session:
  outcome=await q.deliver(session,job)
  assert outcome['status']=='delivered'
  os._exit(73)
asyncio.run(run())
'''
        async with TestServer(application) as server,aiohttp.ClientSession() as session:
            url=str(server.make_url('/hook'))
            for mode in ('during','after'):
                with self.subTest(mode=mode):
                    job=await self.insert(object_id=mode,url=url)
                    received.clear();release.clear();blocking=mode=='during'
                    process=await asyncio.create_subprocess_exec(sys.executable,'-B','-c',script,str(self.db),str(self.base/'child.json'),url,
                                                                stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE)
                    try:
                        await asyncio.wait_for(received.wait(),5)
                        if mode=='during':
                            process.kill();release.set()
                        _out,err=await asyncio.wait_for(process.communicate(),5)
                        if mode=='after': self.assertEqual(process.returncode,73,err)
                    finally:
                        release.set()
                        if process.returncode is None:
                            process.kill();await process.wait()
                    self.assertIsNone(await self.claim(1091,url))
                    recovered=await self.claim(1151,url)
                    self.assertEqual(recovered['job_id'],job['job_id'])
                    blocking=False
                    outcome=await q.deliver(session,recovered)
                    self.assertEqual(outcome['status'],'delivered')
                    await self.store.queue_operation('finish',recovered,outcome,1152)
        with closing(sqlite3.connect(receiver_db)) as c:
            self.assertEqual(c.execute('SELECT COUNT(*) FROM accepted').fetchone()[0],2)


class HttpTests(unittest.IsolatedAsyncioTestCase):
    async def test_http_contract_statuses_bounded_body_no_redirect(self):
        received=[]
        async def receiver(request):
            received.append((request.headers.get('Idempotency-Key'),await request.json()))
            status=int(request.match_info['status'])
            return web.Response(status=status,body=b'x'*10000 if status>=400 else b'',
                                headers={'Retry-After':'120','Location':'/200'})
        application=web.Application();application.router.add_post('/{status}',receiver)
        async with TestServer(application) as server, aiohttp.ClientSession() as session:
            for status in (200,201,202,204,302,400,401,404,408,429,500,503):
                with self.subTest(status=status):
                    job={'job_id':'same','destination_url':str(server.make_url('/'+str(status))),
                         'payload_json':'{"notification_id":"same"}'}
                    outcome=await q.deliver(session,job,now=1000)
                    self.assertEqual(outcome['status'],'delivered' if status<300 else 'pending' if status in (408,429) or status>=500 else 'failed')
                    self.assertEqual(received[-1][0],'same')
            self.assertEqual(len(received),12)
        self.assertEqual(q.retry_after('120',1000),1120)
        self.assertIsNone(q.retry_after('bad',1000))
        self.assertIsNone(q.retry_after('-1',1000))
        self.assertIsNone(q.retry_after('9'*400,1000))
        self.assertEqual(q.retry_after('Wed, 07 Oct 2026 19:00:00 GMT',1000),1791399600)

    async def test_lost_response_reuses_identical_id_and_receiver_deduplicates(self):
        with tempfile.TemporaryDirectory() as tmp:
            receiver_db=Path(tmp)/'receiver.sqlite3'
            with closing(sqlite3.connect(receiver_db)) as c:
                c.execute('CREATE TABLE accepted(id TEXT PRIMARY KEY,body TEXT)');c.commit()
            calls=[]
            async def receiver(request):
                body=await request.text();identifier=request.headers['Idempotency-Key'];calls.append(body)
                with closing(sqlite3.connect(receiver_db)) as c:
                    c.execute('INSERT OR IGNORE INTO accepted VALUES(?,?)',(identifier,body));c.commit()
                if len(calls)==1:
                    request.transport.close()
                    return web.Response()
                return web.Response(status=204)
            application=web.Application();application.router.add_post('/hook',receiver)
            async with TestServer(application) as server,aiohttp.ClientSession() as session:
                job={'job_id':'stable','destination_url':str(server.make_url('/hook')),'payload_json':'{"notification_id":"stable"}'}
                self.assertEqual((await q.deliver(session,job))['status'],'pending')
                self.assertEqual((await q.deliver(session,job))['status'],'delivered')
            self.assertEqual(calls,[job['payload_json']]*2)
            with closing(sqlite3.connect(receiver_db)) as c:
                self.assertEqual(c.execute('SELECT COUNT(*) FROM accepted').fetchone()[0],1)


class ScanQueueTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.case=fixtures.ApiTests('test_status_endpoint')
        await self.case.asyncSetUp()
        self.application=self.case.app
        self.store=self.application[app.ENGINE_STORE_KEY]
        await self.application[app.NOTIFICATION_WORKER_KEY].close()
        self.config=self.application[app.CONFIG_KEY]
        self.config.update(enabled_sources=['pge'],notifications_enabled=True,our_api_url='http://receiver.test/hook')
        location=self.config['teryt_locations'][0]
        location.update(street_sym='11111',house_number='1')
        location['address_audit']=dict(status='verified',object_address_confirmed=True,street_context_confirmed=True,checked_at='2026-10-07T18:00:00Z',sources=[dict(reference='synthetic',sha256='a'*64)],binding=address_binding(location))
        self.application[app.STATE_KEY]['object_fingerprints']={'pge':app.object_fingerprints(self.config,'pge')}

    async def asyncTearDown(self):
        await self.case.asyncTearDown()

    async def scan(self,raw):
        self.application[app.RUNTIME_KEY]['sources']['pge']['last_attempt_at']=None
        with patch('app.fetch_source',return_value=[osd_sources.normalize_pge(raw)]):
            result=await app.scan_source(self.application,'pge')
        self.assertTrue(result['ok'],result)

    def jobs(self):
        with closing(sqlite3.connect(self.store.database_path)) as c:
            return c.execute('SELECT job_id,payload_json FROM notification_jobs ORDER BY sequence').fetchall()

    async def test_real_exact_address_transitions_raw_only_and_full_payload(self):
        for description in ('A','B','A','B'):
            await self.scan(sample_event(description=description))
        jobs=self.jobs()
        self.assertEqual(len(jobs),4)
        self.assertEqual(len({row[0] for row in jobs}),4)
        body=json.loads(jobs[-1][1]);self.assertEqual(body['pge_event']['futurePgeField'],{'kept':True})
        self.assertTrue(body['match_evidence']['point_confirmed'])
        raw=sample_event(description='B');raw['futurePgeField']={'kept':False}
        await self.scan(raw)
        self.assertEqual(len(self.jobs()),4)
        self.assertEqual(self.store.read_history()['total'],4)

    async def test_disabled_candidate_review_and_baseline_do_not_queue(self):
        self.config['notifications_enabled']=False
        await self.scan(sample_event())
        self.assertEqual(self.jobs(),[])
        self.config['notifications_enabled']=True
        self.config['teryt_locations'][0].pop('house_number')
        self.application[app.STATE_KEY]['object_fingerprints']={'pge':app.object_fingerprints(self.config,'pge')}
        await self.scan(sample_event(description='Changed'))
        self.assertEqual(self.jobs(),[])

    async def test_queue_insert_failure_preserves_state_history_and_source_data(self):
        await self.scan(sample_event())
        before=self.store.read_history()['total']
        old=copy.deepcopy(self.application[app.STATE_KEY]['events'])
        self.application[app.RUNTIME_KEY]['sources']['pge']['last_attempt_at']=None
        with patch('app.fetch_source',return_value=[osd_sources.normalize_pge(sample_event(description='New'))]),patch('notification_queue.insert_jobs',side_effect=OSError('full')):
            result=await app.scan_source(self.application,'pge')
        self.assertFalse(result['ok'])
        self.assertEqual(self.application[app.STATE_KEY]['events'],old)
        self.assertEqual(self.store.read_history()['total'],before)
        self.assertEqual(len(self.jobs()),1)

    async def test_error_after_actual_commit_does_not_restore_old_state_or_duplicate_job(self):
        original=self.store.write_state
        async def committed_error(*args,**kwargs):
            await original(*args,**kwargs)
            raise EngineStoreError('Odpowiedź po COMMIT zaginęła')
        with patch.object(self.store,'write_state',side_effect=committed_error):
            await self.scan(sample_event())
        self.assertEqual(len(self.jobs()),1)
        self.assertEqual(self.store.read_history()['total'],1)
        self.assertEqual(self.application[app.STATE_KEY]['events'],self.store.load_current()[0]['events'])
        await self.scan(sample_event())
        self.assertEqual(len(self.jobs()),1)
        self.assertEqual(self.store.read_history()['total'],1)

    async def test_eligibility_address_review_delete_source_and_power_metadata(self):
        await self.scan(sample_event())
        job=await self.store.queue_operation('claim',q.destination_id(self.config['our_api_url']),time.time())
        eligibility=self.application[app.NOTIFICATION_WORKER_KEY].eligibility
        self.assertIsNone(await eligibility(job))
        location=self.config['teryt_locations'][0]
        location['power_supply']={'generator':'szr','secondary_feed':'none','notes':'test'}
        self.assertIsNone(await eligibility(job))
        location['latitude']=52.9
        self.assertIsNotNone(await eligibility(job))
        location['location_audit']={'requires_review':True}
        self.assertEqual(await eligibility(job),'Lokalizacja wymaga przeglądu')
        self.config['teryt_locations']=[]
        self.assertEqual(await eligibility(job),'Obiekt usunięty')
        self.config['enabled_sources']=[]
        self.assertEqual(await eligibility(job),'Źródło wyłączone')



if __name__=='__main__':
    unittest.main()
