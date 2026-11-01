"""Regresje trwałej historii R6; wszystkie dane są odizolowane od produkcji."""
import asyncio
from contextlib import closing
from copy import deepcopy
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

import app
from engine_store import EngineStore, EngineStoreError, history_record, inspect_database, restore_database, export_history
import test_api_v1


def event(index=0, **fields):
    return dict(event_key=f'pge:id-{index}:object:one', object_id='one', source='pge',
                source_event_id=f'id-{index}', status='active', type='planned_outage',
                configured_name='Dawna nazwa', description='Wersja A', **fields)


def initial():
    return {'events': {}, 'history': [], 'sources': {}, 'saved_at': None}


class HistoryStoreTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.db = self.base/'engine.sqlite3'
        self.store = EngineStore(self.db, self.base/'state.json')
        self.store.load_or_migrate(initial())

    async def asyncTearDown(self):
        await self.store.close()
        self.tmp.cleanup()

    def schema_one(self, state):
        with closing(sqlite3.connect(self.db)) as c:
            with c:
                c.execute('DROP TABLE notification_jobs')
                c.execute("DELETE FROM schema_meta WHERE key LIKE 'queue_%'")
                c.execute('DROP TABLE event_changes')
                c.execute("DELETE FROM schema_meta WHERE key NOT IN ('schema_version','migrated_at','legacy_state_sha256','history_complete')")
                c.execute("UPDATE schema_meta SET value='1' WHERE key='schema_version'")
                c.execute('UPDATE engine_state SET state_json=?', (json.dumps(state),))





    async def test_large_batch_pages_preview_and_backup(self):
        records=[history_record('new',event(i),None,'2026-10-01T10:00:00Z') for i in range(1000)]
        state=initial();state['history']=[{'at':'same','action':'not_published','event':event(i)} for i in range(200)]
        await self.store.write_state(state,history_records=records)
        pages=[]
        for offset in range(0,1000,200): pages.extend(self.store.read_history(limit=200,offset=offset)['items'])
        self.assertEqual(len({r['history_id'] for r in pages}),1000)
        self.assertEqual(inspect_database(self.db)['history_preview_count'],200)
        self.assertEqual(inspect_database(self.db)['history_archive_count'],1000)
        backup=await self.store.backup(self.base/'copy.sqlite3')
        restore_database(backup,self.base/'restored.sqlite3')
        restored=EngineStore(self.base/'restored.sqlite3',self.base/'restored.json')
        try:
            self.assertEqual(self.store.read_history(),restored.read_history())
            self.assertEqual(restored.load_current(),(state,2))
        finally: await restored.close()
        output=export_history(backup,self.base/'archive.jsonl')
        exported=[json.loads(line) for line in output.read_text(encoding='utf-8').splitlines()]
        self.assertEqual(len(exported),1000)
        self.assertEqual({r['history_id'] for r in exported},{r['history_id'] for r in pages})

    async def test_insert_failure_is_atomic_and_json_failure_is_secondary(self):
        record=history_record('new',event(),None,'2026-10-01T10:00:00Z')
        with self.assertRaises(EngineStoreError):
            await self.store.write_state({'events':{'one':event()}},history_records=[record,record])
        self.assertEqual(self.store.load_current(),(initial(),1))
        self.assertEqual(self.store.read_history()['total'],0)
        with patch('engine_store._atomic_json_export',side_effect=OSError('disk full')):
            result=await self.store.write_state(initial(),history_records=[record])
        self.assertIn('disk full',result.export_error)
        self.assertEqual(self.store.read_history()['total'],1)

    async def test_concurrent_commits_and_return_to_same_content(self):
        batches=[[history_record('updated',event(),None,'2026-10-01T10:00:00Z')] for _ in range(3)]
        await asyncio.gather(*(self.store.write_state(initial(),history_records=batch) for batch in batches))
        self.assertEqual(self.store.read_history()['total'],3)
        self.assertEqual({r['state_revision'] for r in self.store.read_history()['items']},{2,3,4})

    async def test_corrupt_archive_rejected_before_restore(self):
        await self.store.write_state(initial(),history_records=[history_record('new',event(),None,None)])
        backup=await self.store.backup(self.base/'copy.sqlite3')
        with closing(sqlite3.connect(backup)) as c:
            with c: c.execute("UPDATE event_changes SET after_json='broken'")
        with self.assertRaises(EngineStoreError): restore_database(backup,self.db)
        self.assertEqual(self.store.read_history()['total'],1)

    async def test_old_restore_new_branch_has_distinct_public_uuid(self):
        backup=await self.store.backup(self.base/'empty.sqlite3')
        record=history_record('new',event(),None,'2026-10-01T10:00:00Z')
        await self.store.write_state(initial(),history_records=[record])
        old=self.store.read_history()
        restore_database(backup,self.db)
        await self.store.write_state(initial(),history_records=[history_record('new',event(),None,'2026-10-01T10:00:00Z')])
        new=self.store.read_history()
        self.assertEqual(old['meta']['archive_revision'],new['meta']['archive_revision'])
        self.assertNotEqual(old['items'][0]['history_id'],new['items'][0]['history_id'])


    async def test_cancelled_batch_commits_before_unlock(self):
        started=threading.Event();proceed=threading.Event();lock=asyncio.Lock();published=[]
        write=self.store._write_state
        def delayed(*args):
            started.set();proceed.wait(10);return write(*args)
        async def caller():
            async with lock:
                await self.store.write_state(initial(),history_records=[history_record('new',event(),None,None)])
                published.append(self.store.read_history()['total'])
            await asyncio.sleep(0)
        with patch.object(self.store,'_write_state',side_effect=delayed):
            task=asyncio.create_task(caller());await asyncio.to_thread(started.wait,10)
            task.cancel();await asyncio.sleep(0);self.assertTrue(lock.locked());proceed.set()
            with self.assertRaises(asyncio.CancelledError):await task
        self.assertEqual(published,[1])


class HistoryApiTests(test_api_v1.PublicApiV1Tests):
    async def test_pages_snapshot_changes_and_export_preview_ids(self):
        store=self.app[app.ENGINE_STORE_KEY]
        records=[history_record('new',event(i),None,'2026-10-01T10:00:00Z') for i in range(450)]
        await store.write_state(self.app[app.STATE_KEY],history_records=records)
        response=await (await self.client.get('/api/v1/history?limit=200')).json()
        first=response;items=response['items']
        while response['next']:
            response=await (await self.client.get(response['next'])).json();items.extend(response['items'])
        self.assertEqual(len({r['history_id'] for r in items}),451)
        await store.write_state(self.app[app.STATE_KEY])
        self.assertEqual((await self.client.get(first['next'])).status,200)
        await store.write_state(self.app[app.STATE_KEY],history_records=[history_record('new',event(999),None,None)])
        self.assertEqual((await self.client.get(first['next'])).status,409)
        exported=await (await self.client.get('/api/v1/export.json')).json()
        self.assertEqual(exported['history']['total'],1)
        self.assertFalse(exported['history']['truncated'])
        self.assertEqual(exported['history']['items'][0]['history_id'],items[-1]['history_id'])
        csv=await self.client.get('/api/v1/export.csv?dataset=history')
        self.assertEqual(csv.headers['X-History-Total'],'1')

    async def test_invalid_ranges_and_sql_filters(self):
        for path in ['/api/v1/history?from=2026-01-02T00:00:00Z&to=2026-01-01T00:00:00Z',
                     '/api/v1/history?from=2026-01-01T00:00:00Z', '/api/v1/history?limit=201',
                     '/api/v1/history?source=pge&source=enea']:
            self.assertEqual((await self.client.get(path)).status,400,path)
        response=await (await self.client.get('/api/v1/history?source=enea')).json()
        self.assertEqual(response['total'],0)

    async def test_fractional_time_bounds_and_storage_errors(self):
        store=self.app[app.ENGINE_STORE_KEY]
        await store.write_state(self.app[app.STATE_KEY],history_records=[history_record('new',event(),None,'2026-01-01T10:00:00Z')])
        response=await (await self.client.get('/api/v1/history?from=2026-01-01T10:00:00.500Z&to=2026-01-01T10:00:01Z')).json()
        self.assertEqual(response['total'],0)
        with patch.object(store,'read_history',side_effect=EngineStoreError('Niepoprawna baza')):
            for path in ['/api/v1/history','/api/v1/export.json','/api/v1/export.csv?dataset=history']:
                result=await self.client.get(path)
                self.assertEqual(result.status,503)
                self.assertEqual(result.headers['Access-Control-Allow-Origin'],'*')

    async def test_export_consistent_while_commit_occurs(self):
        store=self.app[app.ENGINE_STORE_KEY];read=store.read_history
        started=threading.Event();proceed=threading.Event()
        def delayed(**kwargs):
            result=read(**kwargs);started.set();proceed.wait(10);return result
        before=store.load_current()
        with patch.object(store,'read_history',side_effect=delayed):
            task=asyncio.create_task(self.client.get('/api/v1/export.json'))
            await asyncio.to_thread(started.wait,10)
            await store.write_state({'events':{},'sources':{}},history_records=[history_record('new',event(),None,None)])
            proceed.set();payload=await (await task).json()
        self.assertEqual(payload['state_revision'],before[1])
        self.assertEqual(len(payload['events']),1)
        self.assertEqual(payload['history']['archive_total'],1)
