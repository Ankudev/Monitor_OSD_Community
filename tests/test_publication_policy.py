"""Rzeczywista polityka: domyślne ograniczenia, wycieki, rewizje i migracja."""
import asyncio
from contextlib import closing
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import AsyncMock, patch
from aiohttp.test_utils import AioHTTPTestCase

import app
import publication_policy as publication
from auth_helpers import login_admin
from engine_store import history_record
from policy_helpers import source


def approve_object(obj, *, source_id='project'):
    return dict(visibility='public',reason='Jawna decyzja dla danych syntetycznych.',fields={
        key:dict(source_id=source_id,source_url='https://example.org/fixture',value_sha256=publication.digest(value),
                 acquired_at=None,method='Dane syntetyczne w teście.',modified=True)
        for key,value in obj.items() if key in publication.FIELDS})


def allow_source(policy, identifier='project'):
    return policy.write('source',identifier,source('https://example.org/fixture',approved=True,active=True,
                        basis='Wyłącznie dane syntetyczne testu.'),policy.snapshot().revision)


class PolicyStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.policy=publication.PublicationPolicy(self.root/'policy.sqlite3')

    def test_absent_corrupt_unknown_schema_and_stale_recovery_fail_closed(self):
        self.assertFalse(self.policy.snapshot().available)
        self.policy.initialize()
        self.assertTrue(self.policy.snapshot().available)
        minimum=self.root/'minimum';minimum.write_text('3')
        with patch.dict(os.environ,{'PGE_PUBLICATION_MIN_REVISION_FILE':str(minimum)}):
            self.assertFalse(self.policy.snapshot().available)
        with closing(sqlite3.connect(self.policy.path)) as db:
            db.execute('UPDATE policy_meta SET version=99');db.commit()
        self.assertFalse(self.policy.snapshot().available)
        self.policy.path.write_bytes(b'not a database')
        self.assertFalse(self.policy.snapshot().allows('project'))

    def test_revision_conflict_idempotency_and_value_binding(self):
        self.policy.initialize();allow_source(self.policy)
        obj=dict(id='x',name='Publiczny',city_sym='1',latitude=52.0,longitude=21.0)
        decision=approve_object(obj)
        revision=self.policy.write('object','x',decision,1)
        self.assertEqual(self.policy.write('object','x',decision,revision),revision)
        self.assertEqual(self.policy.snapshot().public_object(obj)['latitude'],52.0)
        with self.assertRaises(ValueError):self.policy.write('object','x',dict(decision,visibility='private'),0)
        self.assertIsNone(self.policy.snapshot().public_object(dict(obj,name='Inna nazwa')))
        changed=self.policy.snapshot().public_object(dict(obj,latitude=53.0))
        self.assertIsNone(changed['latitude']);self.assertIsNone(changed['longitude'])
        self.assertEqual(obj['latitude'],52.0)

    def test_location_verification_status_is_preserved_without_private_evidence(self):
        self.policy.initialize();allow_source(self.policy)
        obj=dict(id='x',name='Test',location_audit={'status':'verified','requires_review':False,'review_reason':'PRIVATE-EVIDENCE','binding':'PRIVATE-BINDING'})
        self.policy.write('object','x',approve_object(obj),self.policy.snapshot().revision)
        public=self.policy.snapshot().public_object(obj)
        self.assertEqual(public['location_audit'],{'status':'verified','requires_review':False})
        self.assertNotIn('PRIVATE',json.dumps(public))

    def test_expired_decision_and_source_change_token_and_hide_values(self):
        self.policy.initialize();allow_source(self.policy)
        obj=dict(id='x',name='Test')
        entry=approve_object(obj)
        revision=self.policy.write('object','x',entry,1)
        old=self.policy.snapshot().token
        entry['fields']['name']['valid_until']=(datetime.now(timezone.utc)-timedelta(seconds=1)).isoformat()
        self.policy.write('object','x',entry,revision)
        self.assertNotEqual(self.policy.snapshot().token,old)
        self.assertIsNone(self.policy.snapshot().public_object(obj))

    def test_whole_object_expiration_is_enforced_independently_of_field_expiration(self):
        self.policy.initialize();allow_source(self.policy)
        obj=dict(id='x',name='Test')
        decision=approve_object(obj)
        decision['valid_until']=(datetime.now(timezone.utc)-timedelta(seconds=1)).isoformat()
        self.policy.write('object','x',decision,self.policy.snapshot().revision)
        self.assertIsNone(self.policy.snapshot().public_object(obj))

    def test_invalid_urls_and_unacknowledged_pending_source_rejected(self):
        self.policy.initialize()
        for address in ('file:///etc/passwd','http://example.org','https://user:pass@example.org'):
            with self.subTest(address=address),self.assertRaises(ValueError):
                self.policy.write('source','project',source(address,approved=True,active=True,basis='Dane testowe.'),0)
        pending=source('https://example.org',active=True,basis='Nierozstrzygnięte dane testowe.')
        pending.pop('interim_owner_decision')
        with self.assertRaises(ValueError):self.policy.write('source','project',pending,0)



class PublicationApiTests(AioHTTPTestCase):
    async def get_application(self):
        self.directory=tempfile.TemporaryDirectory();self.root=Path(self.directory.name)
        self.public=dict(id='public',name='Obiekt syntetyczny',city_sym='123',street_sym='',latitude=52.0,longitude=21.0)
        self.secret=dict(id='PRIVATE-ID',name='PRIVATE-NAME',city_sym='999',latitude=51.0,longitude=20.0,
                         power_supply={'notes':'PRIVATE-NOTE'})
        (self.root/'objects.json').write_text(json.dumps([self.public,self.secret]))
        config=self.root/'config.json'
        config.write_text(json.dumps(dict(enabled_sources=['pge','tauron'],geoportal_enabled=False,notifications_enabled=False)))
        self.policy=publication.PublicationPolicy(self.root/'data/publication-policy.sqlite3');self.policy.initialize()
        for identifier in ('project','demo_locations','pge','tauron'):allow_source(self.policy,identifier)
        self.policy.write('object','public',approve_object(self.public,source_id='demo_locations'),self.policy.snapshot().revision)
        return app.create_app(config,self.root/'state.json',start_scanner=False)

    async def asyncTearDown(self):
        await super().asyncTearDown();self.directory.cleanup()

    async def test_all_public_projections_exclude_private_records_and_counts(self):
        for route in ('/api/objects','/api/events','/api/status','/api/v1/objects?view=full','/api/v1/locations',
                      '/api/v1/localities','/api/v1/map','/api/v1/summary','/api/v1/history',
                      '/api/v1/export.json','/api/v1/export.csv?dataset=objects','/api/v1/export.csv?dataset=history'):
            with self.subTest(route=route):
                response=await self.client.get(route)
                self.assertEqual(response.status,200,await response.text())
                body=await response.text()
                for marker in ('PRIVATE-ID','PRIVATE-NAME','PRIVATE-NOTE'):
                    self.assertNotIn(marker,body)
                self.assertEqual(response.headers['Cache-Control'],'no-store')
        body=await (await self.client.get('/api/v1/map')).json()
        self.assertEqual(len(body['locations']),1)
        for object_id in ('PRIVATE-ID','missing'):
            self.assertEqual((await self.client.get('/api/v1/objects/'+object_id)).status,404)
        self.assertEqual((await self.client.get('/api/admin/publication')).status,401)
        self.assertEqual((await self.client.get('/api/v1/diagnostics/quality')).status,401)


    async def test_history_filters_before_count_pagination_and_exports(self):
        store=self.app[app.ENGINE_STORE_KEY]
        state=deepcopy(self.app[app.STATE_KEY])
        events=[]
        for index, obj in enumerate((self.public,self.secret,self.public,self.secret)):
            events.append(dict(event_key=f'pge:{index}',object_id=obj['id'],configured_name=obj['name'],city_sym=obj['city_sym'],
                               source='pge',status='active',type='planned_outage',description='Komunikat testowy'))
        records=[history_record('not_published',event,None,'2026-10-24T12:00:00Z') for event in events]
        await store.write_state(state,history_records=records)
        first=await (await self.client.get('/api/v1/history?limit=1')).json()
        self.assertEqual(first['total'],2);self.assertEqual(first['archive_total'] if 'archive_total' in first else first['count'],1)
        self.assertIsNotNone(first['next'])
        second=await (await self.client.get(first['next'])).json()
        self.assertEqual(second['total'],2);self.assertNotIn('PRIVATE',json.dumps(second))
        self.assertNotIn('PRIVATE',(await (await self.client.get('/api/v1/export.csv?dataset=history')).text()))
        preview=await (await self.client.get('/api/v1/history?latest_per_event=1&limit=3')).json()
        self.assertEqual(preview['total'],2)
        self.assertNotIn('PRIVATE',json.dumps(preview))
        hidden=await (await self.client.get('/api/v1/history?object_id=PRIVATE-ID&latest_per_event=1&limit=3')).json()
        self.assertEqual(hidden['items'],[])
        self.assertEqual(hidden['total'],0)

    async def test_admin_publication_csrf_conflicts_edit_binding_and_revoke(self):
        decision=dict(visibility='private',fields={})
        route='/api/admin/publication/object/public'
        self.assertEqual((await self.client.put(route,json={})).status,401)
        await login_admin(self)
        state=await (await self.client.get('/api/admin/publication')).json()
        self.assertEqual(len(state['objects']),2)
        wrong=await self.client.put(route,json={'revision':state['revision'],'decision':decision},headers={'X-CSRF-Token':'bad'})
        self.assertEqual(wrong.status,403)
        before=await self.client.get('/api/v1/map');etag=before.headers['ETag']
        success=await self.client.put(route,json={'revision':state['revision'],'decision':decision})
        self.assertEqual(success.status,200,await success.text())
        after=await self.client.get('/api/v1/map',headers={'If-None-Match':etag})
        self.assertEqual(after.status,200);self.assertEqual((await after.json())['locations'],[])
        conflict=await self.client.put(route,json={'revision':state['revision'],'decision':approve_object(self.public)})
        self.assertEqual(conflict.status,409)
        self.assertEqual((await self.client.get('/api/admin/publication/setup')).status,200)

    async def test_disabled_source_scan_preserves_state_and_no_network(self):
        state=deepcopy(self.app[app.STATE_KEY])
        self.policy.write('source','pge',source('https://example.org',basis='Tymczasowe ograniczenie danych.'),self.policy.snapshot().revision)
        with patch('app.fetch_source',AsyncMock()) as fetch:
            result=await app.scan_source(self.app,'pge')
            fetch.assert_not_awaited()
        self.assertTrue(result['skipped']);self.assertEqual(state,self.app[app.STATE_KEY])

    async def test_missing_policy_health_and_objects_fail_closed(self):
        self.policy.path.unlink()
        self.assertEqual((await self.client.get('/health/ready')).status,503)
        self.assertEqual((await (await self.client.get('/api/objects')).json())['objects'],[])

    async def test_new_backup_cannot_silently_omit_missing_policy(self):
        await login_admin(self)
        self.policy.path.unlink()
        with patch.dict(os.environ,{'PGE_BACKUP_STAGING':str(self.root/'staging')}):
            response=await self.client.post('/api/admin/backups/snapshot')
        self.assertEqual(response.status,503)
        self.assertFalse((self.root/'staging').exists())



    async def test_owner_location_requires_explicit_field_and_source_approval(self):
        decision=approve_object(self.secret,source_id='owner_locations')
        self.policy.write('object',self.secret['id'],decision,self.policy.snapshot().revision)
        before=await (await self.client.get('/api/v1/map')).json()
        self.assertNotIn(self.secret['id'],{item['object_id'] for item in before['locations']})
        allow_source(self.policy,'owner_locations')
        response=await self.client.get('/api/v1/map')
        result=await response.json()
        self.assertEqual({item['object_id'] for item in result['locations']},{self.public['id'],self.secret['id']})
        text=await response.text()
        self.assertNotIn('PRIVATE-KEYWORD',text)
        self.assertNotIn('PRIVATE-NOTE',text)
        owner=next(item for item in result['locations'] if item['object_id']==self.secret['id'])
        self.assertEqual(owner['provenance']['name']['source_id'],'owner_locations')
        self.policy.write('object',self.secret['id'],dict(visibility='private',fields={}),self.policy.snapshot().revision)
        revoked=await self.client.get('/api/v1/map',headers={'If-None-Match':response.headers['ETag']})
        self.assertEqual(revoked.status,200)
        self.assertNotIn(self.secret['id'],{item['object_id'] for item in (await revoked.json())['locations']})

    async def test_policy_changed_during_response_does_not_return_old_values(self):
        original=app.enriched_objects
        changed=False
        def revoke(*args,**kwargs):
            nonlocal changed
            if not changed:
                changed=True
                self.policy.write('object','public',dict(visibility='private',fields={}),self.policy.snapshot().revision)
            return original(*args,**kwargs)
        with patch('app.enriched_objects',side_effect=revoke):
            response=await self.client.get('/api/v1/map')
        self.assertEqual(response.status,409)
        self.assertNotIn(self.public['name'],await response.text())


class PolicyBackupTests(unittest.TestCase):
    def test_new_policy_snapshot_and_old_snapshot_restore_keep_closed_defaults(self):
        import test_operations
        from service_backup import create_snapshot, verify_snapshot, restore_snapshot, POLICY_FILE
        fixture=test_operations.SnapshotTests('test_snapshot_restore_preserves_values_and_cannot_overwrite')
        fixture.setUp();self.addCleanup(fixture.doCleanups)
        policy=publication.PublicationPolicy(fixture.root/'publication-policy.sqlite3')
        policy.initialize();allow_source(policy)
        files=dict(fixture.files,**{POLICY_FILE:policy.path})
        created=create_snapshot(files,fixture.snapshot)
        self.assertEqual(created['version'],3)
        self.assertEqual(verify_snapshot(fixture.snapshot)['version'],3)
        target=fixture.root/'restore'
        restore_snapshot(fixture.snapshot,target)
        restored=publication.PublicationPolicy(target/POLICY_FILE)
        self.assertEqual(restored.snapshot().revision,policy.snapshot().revision)
        old=fixture.root/'old'
        create_snapshot(fixture.files,old)
        target_old=fixture.root/'restore-old'
        restore_snapshot(old,target_old)
        self.assertFalse(publication.PublicationPolicy(target_old/POLICY_FILE).snapshot().available)
