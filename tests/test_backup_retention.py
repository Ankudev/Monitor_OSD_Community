"""Retencja tylko syntetycznych migawek z potwierdzoną kopią restic."""
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import unittest
import uuid
from unittest.mock import patch

import test_operations as fixtures
from backup_retention import checked_local_path, receipt_path, retention_plan
from runtime_health import backup_health
from service_backup import create_snapshot, no_symlinks
from tools.backup_service import prune_local, run_backup, upload


class LocalRetentionTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.SnapshotTests('test_wal_commits_are_included')
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.root = self.fixture.root
        self.staging = self.root / 'staging'
        self.status = self.root / 'status.json'
        password = self.root / 'password'
        password.write_text('losowy klucz wyłącznie do testu', encoding='utf8')
        environment = patch.dict(os.environ, RESTIC_REPOSITORY='local-test',
                                 RESTIC_PASSWORD_FILE=str(password), PGE_BACKUP_KEEP_LOCAL='')
        environment.start()
        self.addCleanup(environment.stop)
        self.remote = []
        self.paths = []
        self.upload_id = None
        runner = patch('tools.backup_service.subprocess.run', side_effect=self.restic)
        self.runner = runner.start()
        self.addCleanup(runner.stop)

    def restic(self, args, **kwargs):
        if args[1] == 'backup':
            body = {'message_type':'summary', 'snapshot_id':self.upload_id}
            # Restic zapisuje ścieżkę z rzeczywistego polecenia. Katalog TEMP
            # na Windows może używać skrótu 8.3 rozwijanego przez resolve().
            self.remote.append({'id':self.upload_id, 'paths':[args[-1]], 'tags':['monitor-osd']})
        else:
            self.assertEqual(args, ['restic','snapshots','--json','--tag','monitor-osd'])
            body = self.remote
        return subprocess.CompletedProcess(args, 0, json.dumps(body), '')

    def snapshot(self, *, confirmed=True):
        path = self.staging / str(uuid.uuid4())
        create_snapshot(self.fixture.files, path)
        manifest_path = path / 'manifest.json'
        manifest = json.loads(manifest_path.read_text(encoding='utf8'))
        manifest['started_at'] = (datetime.now(timezone.utc) - timedelta(minutes=30-len(self.paths))).isoformat()
        manifest_path.write_text(json.dumps(manifest), encoding='utf8')
        self.paths.append(path)
        if confirmed:
            self.upload_id = hashlib.sha256(path.name.encode()).hexdigest()
            upload(path, self.status)
        return path

    def test_noncanonical_staging_path_matches_the_uploaded_path(self):
        self.staging = self.root / '..' / self.root.name / 'staging'
        current = self.snapshot()
        receipt = json.loads(receipt_path(current).read_text(encoding='utf8'))
        self.assertEqual(self.remote[-1]['paths'], [receipt['source_path']])
        self.assertEqual(prune_local(self.staging,self.status,2,apply=True)['deleted'], [])
        self.assertTrue(current.is_dir())

    def test_default_only_previews_and_apply_keeps_two_and_latest_success(self):
        paths = [self.snapshot() for _ in range(4)]
        preview = prune_local(self.staging, self.status, 2)
        self.assertFalse(preview['applied'])
        self.assertEqual(set(preview['delete']), {p.name for p in paths[:2]})
        self.assertTrue(all(path.is_dir() for path in paths))
        result = prune_local(self.staging, self.status, 2, apply=True)
        self.assertEqual(set(result['deleted']), {p.name for p in paths[:2]})
        self.assertTrue(all(path.is_dir() for path in paths[2:]))
        self.assertFalse(any(path.exists() for path in paths[:2]))
        self.assertEqual(len(self.remote), 4, 'Retencja lokalna nie usuwa punktów restic')
        self.assertTrue(all(call.args[0][1] in ('backup','snapshots') for call in self.runner.call_args_list))

    def test_unconfirmed_partial_foreign_and_changed_copies_are_preserved(self):
        unknown = self.snapshot(confirmed=False)
        changed = self.snapshot()
        (changed/'objects.json').write_text('[{"zmieniono":true}]')
        foreign = self.snapshot()
        receipt = receipt_path(foreign)
        content = json.loads(receipt.read_text(encoding='utf8'))
        content['repository_key'] = 'inne repozytorium'
        receipt.write_text(json.dumps(content), encoding='utf8')
        valid = [self.snapshot() for _ in range(3)]
        partial = self.staging / (str(uuid.uuid4())+'.partial')
        partial.mkdir()
        (partial/'test.txt').write_text('przerwane kopiowanie')
        unrelated = self.staging / 'wazne-pliki'
        unrelated.mkdir()
        result = prune_local(self.staging, self.status, 2, apply=True)
        self.assertEqual(result['deleted'], [valid[0].name])
        self.assertTrue(all(path.exists() for path in (unknown,changed,foreign,partial,unrelated)))

    def test_missing_current_remote_copy_and_network_error_never_delete(self):
        paths = [self.snapshot() for _ in range(3)]
        self.remote.pop()
        with self.assertRaises(ValueError): prune_local(self.staging,self.status,2,apply=True)
        self.assertTrue(all(path.exists() for path in paths))
        self.runner.side_effect = subprocess.TimeoutExpired('restic', 60)
        with self.assertRaises(ValueError): prune_local(self.staging,self.status,2,apply=True)
        self.assertTrue(all(path.exists() for path in paths))

    def test_changed_repository_and_wrong_remote_path_block_removal(self):
        paths = [self.snapshot() for _ in range(3)]
        with patch.dict(os.environ, RESTIC_REPOSITORY='other'):
            with self.assertRaises(ValueError): prune_local(self.staging,self.status,2,apply=True)
        self.remote[-1]['paths'] = [str(self.root/'outside')]
        with self.assertRaises(ValueError): prune_local(self.staging,self.status,2,apply=True)
        self.assertTrue(all(path.exists() for path in paths))

    def test_latest_success_is_kept_even_if_its_timestamp_is_older(self):
        paths = [self.snapshot() for _ in range(3)]
        status = json.loads(self.status.read_text(encoding='utf8'))
        first = json.loads(receipt_path(paths[0]).read_text(encoding='utf8'))
        status.update(local_snapshot_name=paths[0].name, snapshot_id=first['snapshot_id'])
        self.status.write_text(json.dumps(status), encoding='utf8')
        result = prune_local(self.staging,self.status,2,apply=True)
        self.assertEqual(result['deleted'], [])
        self.assertTrue(all(path.exists() for path in paths))

    def test_one_copy_invalid_keep_and_traversal_do_not_delete(self):
        current = self.snapshot()
        self.assertEqual(prune_local(self.staging,self.status,2,apply=True)['deleted'], [])
        for keep in (0,1,49,True):
            with self.assertRaises(ValueError):
                retention_plan(self.staging,current.name,keep,self.remote)
        for name in ('..','../outside',str(self.root),None):
            with self.assertRaises(ValueError): checked_local_path(self.staging,name)
        self.assertTrue(current.exists())

    def test_symlink_to_outside_is_rejected(self):
        current = self.snapshot()
        outside = self.root / 'outside'
        outside.mkdir()
        linked = self.staging / str(uuid.uuid4())
        try:
            linked.symlink_to(outside, target_is_directory=True)
        except OSError:
            self.skipTest('System nie zezwala na tworzenie dowiązań w tej sesji')
        self.addCleanup(linked.unlink)
        with self.assertRaises(ValueError): no_symlinks(linked)
        result = prune_local(self.staging,self.status,2,apply=True)
        self.assertIn(linked.name,result['preserved'])
        self.assertTrue(outside.is_dir())
        self.assertTrue(current.is_dir())

    def test_retention_warning_does_not_replace_successful_backup_with_failure(self):
        self.snapshot()
        status = json.loads(self.status.read_text(encoding='utf8'))
        status['data_started_at'] = datetime.now(timezone.utc).isoformat()
        status['retention'] = {'state':'failed'}
        self.status.write_text(json.dumps(status),encoding='utf8')
        with patch.dict(os.environ, PGE_BACKUP_STATUS_FILE=str(self.status)):
            result = backup_health()
        self.assertEqual(result['state'],'warning')
        self.assertEqual(result['retention_state'],'failed')
        self.assertEqual(json.loads(self.status.read_text())['snapshot_id'],status['snapshot_id'])

    def test_invalid_retention_configuration_fails_before_http(self):
        with patch('tools.backup_service.build_opener') as opener:
            with self.assertRaises(ValueError):
                run_backup('http://127.0.0.1:12345',self.status,keep_local=1)
        opener.assert_not_called()

    def test_cleanup_failure_is_separate_from_successful_upload(self):
        current = self.snapshot()
        token = self.root/'token'
        token.write_text('x'*48)
        with (patch.dict(os.environ,PGE_BACKUP_STAGING=str(self.staging),PGE_BACKUP_TOKEN_FILE=str(token)),
              patch('tools.backup_service.shutil.which',return_value='restic'),
              patch('tools.backup_service.build_opener') as opener,
              patch('tools.backup_service.prune_local_unlocked',side_effect=OSError('test'))):
            opener.return_value.open.return_value.__enter__.return_value.read.return_value = json.dumps({'snapshot_id':current.name}).encode()
            result = run_backup('http://127.0.0.1:12345',self.status,keep_local=2)
        self.assertEqual(result['retention']['state'],'failed')
        self.assertEqual(json.loads((self.root/'status.json.attempt.json').read_text())['state'],'success')
        self.assertEqual(json.loads(self.status.read_text())['snapshot_id'],self.upload_id)
        self.assertTrue(current.exists())
