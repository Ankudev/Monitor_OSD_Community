from policy_helpers import create_test_app
"""Odtwarzanie prywatnych raportów i ich dowodów, bez sieci i produkcji."""
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from aiohttp.test_utils import TestClient, TestServer
import app
from address_matching import address_binding
from auth_helpers import login_admin
from location_review import ReviewStore
from service_backup import create_snapshot, verify_snapshot, restore_snapshot, private_files, digest
from runtime_health import backup_health
from operations_check import evaluate
from tools.backup_service import upload
import test_operations as fixtures
import test_operations_check as monitor_fixtures


def stored_report(store, obj):
    body = {'type':'FeatureCollection','features':[], 'prywatna_uwaga':'PRIVATE-EVIDENCE'}
    identifier = hashlib.sha256(json.dumps(body,ensure_ascii=False,separators=(',',':')).encode()).hexdigest()
    report = dict(version=1,object_id=str(obj['id']),binding=address_binding(obj),
                  checked_at=datetime.now(timezone.utc).isoformat(),status='inconclusive',
                  evidence=[{'sha256':identifier,'url':'https://example.invalid/PRIVATE-SOURCE'}])
    store.save(report,[{'sha256':identifier,'body':body}])
    return report,identifier


class EvidenceSnapshotTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.SnapshotTests('test_snapshot_restore_preserves_values_and_cannot_overwrite')
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.root = self.fixture.root
        self.store = ReviewStore(self.root/'data')
        self.report,self.identifier = stored_report(self.store,{'id':'1'})

    def files(self):
        managed,missing = private_files(self.root)
        self.assertEqual(missing,[])
        return dict(self.fixture.files,**managed)

    def test_report_and_proof_are_preserved_byte_for_byte_and_restore_to_new_root(self):
        files = self.files()
        created = create_snapshot(files,self.fixture.snapshot)
        self.assertEqual(created['version'],2)
        self.assertEqual(created['evidence'],dict(location_reports=1,location_evidence=1,manual_address_sources=0,external_address_sources=0))
        self.assertEqual(created['recovery_warnings'],[])
        verify_snapshot(self.fixture.snapshot)
        restored = self.root/'restored'
        restore_snapshot(self.fixture.snapshot,restored)
        for name,path in files.items():
            if name.startswith('data/location-'):
                self.assertEqual(path.read_bytes(),(restored/name).read_bytes())
        report = ReviewStore(restored/'data').read({'id':'1'})
        self.assertEqual(report['status'],'inconclusive')
        self.assertTrue(report['current'])

    def test_v1_without_private_files_is_still_readable(self):
        create_snapshot(self.fixture.files,self.fixture.snapshot)
        path = self.fixture.snapshot/'manifest.json'
        manifest = json.loads(path.read_text(encoding='utf8'))
        manifest['version'] = 1
        manifest.pop('evidence')
        manifest.pop('recovery_warnings')
        path.write_text(json.dumps(manifest),encoding='utf8')
        self.assertEqual(verify_snapshot(self.fixture.snapshot)['version'],1)
        restore_snapshot(self.fixture.snapshot,self.root/'restored-v1')

    def test_legacy_manifest_cannot_declare_private_files_or_path_traversal(self):
        create_snapshot(self.files(),self.fixture.snapshot)
        path = self.fixture.snapshot/'manifest.json'
        manifest = json.loads(path.read_text(encoding='utf8'))
        manifest['version'] = 1
        path.write_text(json.dumps(manifest),encoding='utf8')
        with self.assertRaises(ValueError): verify_snapshot(self.fixture.snapshot)
        manifest['version'] = 2
        manifest['files'][-1]['path'] = 'data/location-reviews/../../outside.json'
        path.write_text(json.dumps(manifest),encoding='utf8')
        with self.assertRaises(ValueError): verify_snapshot(self.fixture.snapshot)

    def test_upload_persists_evidence_scope_for_new_and_legacy_copies(self):
        create_snapshot(self.files(),self.fixture.snapshot)
        status_path = self.root/'upload-status.json'
        password = self.root/'test-key'
        password.write_text('synthetic-test-key')
        environment = patch.dict(os.environ,RESTIC_REPOSITORY='synthetic-test',RESTIC_PASSWORD_FILE=str(password))
        environment.start()
        self.addCleanup(environment.stop)
        summary = json.dumps({'message_type':'summary','snapshot_id':'a'*64})
        with patch('tools.backup_service.subprocess.run') as runner:
            runner.return_value.returncode = 0
            runner.return_value.stdout = summary
            self.assertEqual(upload(self.fixture.snapshot,status_path)['evidence_state'],'managed_complete')
        # Starszy zestaw nie zawierał zarządzanych dowodów.
        legacy = self.root/'legacy-snapshot'
        create_snapshot(self.fixture.files,legacy)
        path = legacy/'manifest.json'
        old = json.loads(path.read_text(encoding='utf8'))
        old.update(version=1)
        old.pop('evidence')
        old.pop('recovery_warnings')
        path.write_text(json.dumps(old),encoding='utf8')
        with patch('tools.backup_service.subprocess.run') as runner:
            runner.return_value.returncode = 0
            runner.return_value.stdout = summary
            self.assertEqual(upload(legacy,status_path)['evidence_state'],'unknown')

    def test_missing_or_changed_managed_evidence_does_not_create_finished_copy(self):
        evidence = self.root/'data/location-evidence'/(self.identifier+'.json')
        original = evidence.read_bytes()
        evidence.write_text('{}')
        with self.assertRaises(ValueError): create_snapshot(self.files(),self.fixture.snapshot)
        self.assertFalse(self.fixture.snapshot.exists())
        self.assertFalse(self.fixture.snapshot.with_name('snapshot.partial').exists())
        evidence.write_bytes(original)
        evidence.unlink()
        with self.assertRaises(ValueError): create_snapshot(self.files(),self.fixture.snapshot)
        self.assertFalse(self.fixture.snapshot.exists())

    def test_external_address_evidence_is_reported_but_never_opened(self):
        objects = [{'id':'1','address_audit':{'sources':[{'file':'/etc/DO-NOT-READ','sha256':'f'*64}]}}]
        self.fixture.files['objects.json'].write_text(json.dumps(objects),encoding='utf8')
        manifest = create_snapshot(self.files(),self.fixture.snapshot)
        self.assertEqual(manifest['evidence']['external_address_sources'],1)
        self.assertEqual(manifest['recovery_warnings'],['external_address_evidence_not_in_snapshot'])
        self.assertNotIn('/etc/DO-NOT-READ',json.dumps(manifest))
        verify_snapshot(self.fixture.snapshot)
        path = self.fixture.snapshot/'manifest.json'
        manifest['evidence']['external_address_sources']=0
        manifest['recovery_warnings']=[]
        path.write_text(json.dumps(manifest),encoding='utf8')
        with self.assertRaises(ValueError): verify_snapshot(self.fixture.snapshot)

    def test_unknown_names_and_directories_are_not_silently_omitted(self):
        unknown = self.root/'data/location-evidence/not-a-hash.json'
        unknown.write_text('{}')
        with self.assertRaises(ValueError): private_files(self.root)
        unknown.unlink()
        unknown.mkdir()
        with self.assertRaises(ValueError): private_files(self.root)

    def test_atomic_temporary_files_are_not_published_as_evidence(self):
        temporary = self.root/'data/location-reviews/.review-test.tmp'
        temporary.write_text('unfinished')
        managed,_ = private_files(self.root)
        self.assertEqual(len(managed),2)
        self.assertTrue(temporary.exists())

    def test_large_manifest_is_supported_with_bounded_number_of_proofs(self):
        directory = self.root/'data/location-evidence'
        for index in range(1000):
            body = json.dumps({'index':index},separators=(',',':')).encode()
            (directory/(hashlib.sha256(body).hexdigest()+'.json')).write_bytes(body)
        create_snapshot(self.files(),self.fixture.snapshot)
        self.assertGreater((self.fixture.snapshot/'manifest.json').stat().st_size,256*1024)
        self.assertEqual(verify_snapshot(self.fixture.snapshot)['evidence']['location_evidence'],1001)

    def test_backup_warning_is_distinct_from_source_freshness(self):
        path = self.root/'status.json'
        path.write_text(json.dumps({'data_started_at':datetime.now(timezone.utc).isoformat(),
                                    'evidence_state':'requires_inventory'}),encoding='utf8')
        with patch.dict(os.environ,PGE_BACKUP_STATUS_FILE=str(path)):
            health = backup_health()
        self.assertEqual(health['state'],'warning')
        payload = monitor_fixtures.healthy()
        payload['backups'] = health
        warnings = {row['code'] for row in evaluate(payload)}
        self.assertIn('backup_evidence',warnings)
        self.assertNotIn('source:pge',warnings)


class EvidenceApiTests(unittest.IsolatedAsyncioTestCase):
    async def test_snapshot_restores_private_report_and_does_not_publish_it(self):
        case = fixtures.OperationsApiTests('test_snapshot_endpoint_uses_only_configured_paths')
        await case.asyncSetUp()
        try:
            obj = case.application[app.CONFIG_KEY]['teryt_locations'][0]
            report,identifier = stored_report(case.application[app.REVIEW_STORE_KEY],obj)
            with tempfile.TemporaryDirectory() as staging, patch.dict(os.environ,PGE_BACKUP_STAGING=staging):
                response = await case.client.post('/api/admin/backups/snapshot',headers=case.headers)
                self.assertEqual(response.status,200,await response.text())
                name = (await response.json())['snapshot_id']
                snapshot = Path(staging)/name
                self.assertEqual(verify_snapshot(snapshot)['evidence']['location_reports'],1)
                restored = Path(staging)/'recovered'
                restore_snapshot(snapshot,restored)
                recovered = create_test_app(restored/'config-restore-test.json',restored/'state.json',start_scanner=False)
                async with TestClient(TestServer(recovered)) as client:
                    path = '/api/v1/objects/'+str(obj['id'])+'/location-review'
                    self.assertEqual((await client.get(path)).status,401)
                    await login_admin(type('Fixture',(),{'app':recovered,'client':client})())
                    private = await (await client.get(path)).json()
                    self.assertEqual(private['item']['checked_at'],report['checked_at'])
                    self.assertTrue(private['item']['current'])
                    proof = restored/'data/location-evidence'/(identifier+'.json')
                    self.assertEqual(digest(proof),identifier)
                    for public_path in ('/api/v1/map','/api/v1/objects?view=full'):
                        response = await client.get(public_path)
                        self.assertNotIn('PRIVATE',await response.text())
        finally:
            await case.asyncTearDown()
