"""Publikacja stanu root z odczytem dla aplikacji, bez udostępniania kluczy."""
import json
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch

from tools.backup_service import atomic_status


class BackupStatusPermissionsTests(unittest.TestCase):
    @unittest.skipUnless(os.name == 'posix','Uprawnienia grupowe dotyczą Linuksa.')
    def test_group_can_read_published_state_but_cannot_write_it(self):
        import grp
        group=grp.getgrgid(os.getegid()).gr_name
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ,PGE_BACKUP_STATUS_GROUP=group):
            path=Path(folder)/'status.json'
            original=os.replace
            def publish(source,target):
                self.assertEqual(stat.S_IMODE(os.stat(source).st_mode),0o640)
                self.assertEqual(os.stat(source).st_gid,os.getegid())
                return original(source,target)
            with patch('tools.backup_service.os.replace',side_effect=publish):
                atomic_status(path,{'state':'success'})
            self.assertEqual(json.loads(path.read_text()),{'state':'success'})
            self.assertEqual(stat.S_IMODE(path.stat().st_mode),0o640)

    @unittest.skipUnless(os.name == 'posix','Uprawnienia grupowe dotyczą Linuksa.')
    def test_unknown_group_preserves_previous_status(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'status.json'
            path.write_text('previous')
            with patch.dict(os.environ,PGE_BACKUP_STATUS_GROUP='monitor-osd-nonexistent-group'):
                with self.assertRaises(ValueError): atomic_status(path,{'state':'success'})
            self.assertEqual(path.read_text(),'previous')
            self.assertEqual(list(Path(folder).iterdir()),[path])

    def test_default_status_remains_private(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ,PGE_BACKUP_STATUS_GROUP=''):
            path=Path(folder)/'status.json'
            atomic_status(path,{'state':'success'})
            self.assertEqual(json.loads(path.read_text()),{'state':'success'})
            if os.name == 'posix': self.assertEqual(stat.S_IMODE(path.stat().st_mode),0o600)
