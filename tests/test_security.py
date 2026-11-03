"""No user credentials; every test uses an isolated temporary directory."""
import hashlib
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from fastapi import HTTPException
import main
from security import Credentials


def request():
    return SimpleNamespace(client=SimpleNamespace(host='password-test'), url=SimpleNamespace(scheme='http'), headers={})


class PasswordTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.directory = Path(self.tmp.name)
        self.store = Credentials(self.directory)
        self.store.bootstrap.write_text('test-initial-password\n')
        self.store.initialize()

    def tearDown(self):
        self.tmp.cleanup()

    def test_legacy_password_migrates_without_changing_login(self):
        self.assertTrue(self.store.verify('test-initial-password'))
        self.assertFalse(self.store.verify('wrong'))
        record = self.store.path.read_text()
        self.assertNotIn('test-initial-password', record)
        self.assertEqual(self.store.path.stat().st_mode & 0o777, 0o600)

    def test_new_install_default_and_custom_password_survives_upgrade(self):
        directory = self.directory / 'fresh'
        store = Credentials(directory)
        store.initialize()
        self.assertTrue(store.verify('password'))
        store.change('password', 'custom-password-123')
        store.initialize()
        self.assertTrue(store.verify('custom-password-123'))
        self.assertFalse(store.verify('password'))

    def test_password_change_removes_bootstrap_and_survives_restart(self):
        self.store.change('test-initial-password', 'test-replacement-password')
        self.assertFalse(self.store.bootstrap.exists())
        restarted = Credentials(self.directory)
        restarted.initialize()
        self.assertTrue(restarted.verify('test-replacement-password'))
        self.assertFalse(restarted.verify('test-initial-password'))
        self.assertFalse(restarted.bootstrap.exists())

    def test_incorrect_current_password_leaves_credentials_unchanged(self):
        before = self.store.path.read_bytes()
        with self.assertRaises(ValueError): self.store.change('wrong', 'new-password-123')
        self.assertEqual(before, self.store.path.read_bytes())

    def test_changed_password_invalidates_previous_sessions(self):
        with patch.object(main, 'DATA', self.directory), patch.object(main, 'sessions', {}), patch.object(main, 'attempts', main.collections.defaultdict(list)):
            response = main.login(main.Login(password='test-initial-password'), request())
            old_keys = set(main.sessions)
            self.assertEqual(len(old_keys), 1)
            with self.assertRaises(HTTPException) as failure:
                main.change_password(main.PasswordChange(current_password='wrong',new_password='new-password-123',confirm_password='new-password-123'), request())
            self.assertEqual(failure.exception.status_code, 403)
            self.assertEqual(set(main.sessions), old_keys)
            response = main.change_password(main.PasswordChange(current_password='test-initial-password',new_password='new-password-123',confirm_password='new-password-123'), request())
            self.assertEqual(response.status_code, 200)
            self.assertEqual(len(main.sessions), 1)
            self.assertFalse(old_keys & set(main.sessions))
            self.assertNotIn(b'new-password-123', response.body)

    def test_confirmation_mismatch_does_not_change_password(self):
        with patch.object(main, 'DATA', self.directory):
            with self.assertRaises(HTTPException) as failure:
                main.change_password(main.PasswordChange(current_password='test-initial-password',new_password='new-password-123',confirm_password='different-password-123'), request())
            self.assertEqual(failure.exception.status_code, 400)
            self.assertTrue(self.store.verify('test-initial-password'))


if __name__ == '__main__':
    unittest.main(verbosity=2)
