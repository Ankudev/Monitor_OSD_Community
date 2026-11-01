import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from tools import set_admin_password
from admin_auth import PH

class PasswordToolTests(unittest.TestCase):
    def test_reset_preserves_access_and_verifies_new_hash(self):
        with tempfile.TemporaryDirectory() as directory:
            target=Path(directory)/'password.hash'
            target.write_text('old')
            target.chmod(0o640)
            old=target.stat()
            with patch('sys.argv',['set_admin_password',str(target)]), patch.object(set_admin_password,'getpass',side_effect=['Nowe hasło testowe 2026']*2),patch('builtins.print'):
                set_admin_password.main()
            self.assertTrue(PH.verify(target.read_text().strip(),'Nowe hasło testowe 2026'))
            if os.name=='posix':
                self.assertEqual(target.stat().st_mode & 0o777,0o640)
                self.assertEqual(target.stat().st_gid,old.st_gid)
