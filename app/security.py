"""Local password storage. Passwords and session tokens are never logged."""
import hashlib
import hmac
import json
import os
from pathlib import Path
import secrets


class Credentials:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.path = self.directory / 'credentials.json'
        self.bootstrap = self.directory / 'access-password.txt'

    @staticmethod
    def digest(password, salt):
        return hashlib.scrypt(password.encode('utf-8'), salt=bytes.fromhex(salt), n=16384, r=8, p=1, dklen=32).hex()

    def write(self, password):
        salt = secrets.token_hex(16)
        record = {'algorithm': 'scrypt-v1', 'salt': salt, 'hash': self.digest(password, salt)}
        temporary = self.directory / ('.credentials-' + secrets.token_hex(8))
        try:
            with temporary.open('x', encoding='utf-8') as f:
                temporary.chmod(0o600)
                json.dump(record, f)
                f.flush()
                os.fsync(f.fileno())
            os.replace(temporary, self.path)
        finally:
            temporary.unlink(missing_ok=True)

    def initialize(self):
        self.directory.mkdir(parents=True, exist_ok=True)
        if self.path.exists():
            return
        if not self.bootstrap.exists():
            with self.bootstrap.open('x', encoding='utf-8') as f:
                self.bootstrap.chmod(0o600)
                f.write('password\n')
        self.write(self.bootstrap.read_text().strip())

    def verify(self, password):
        record = json.loads(self.path.read_text())
        if record.get('algorithm') != 'scrypt-v1':
            raise ValueError('Unsupported credential format')
        return hmac.compare_digest(self.digest(password, record['salt']), record['hash'])

    def change(self, current, new):
        if not self.verify(current):
            raise ValueError('当前密码不正确')
        if not 8 <= len(new) <= 128:
            raise ValueError('新密码需为 8～128 个字符')
        if new == current:
            raise ValueError('新密码不能与当前密码相同')
        self.write(new)
        self.bootstrap.unlink(missing_ok=True)
