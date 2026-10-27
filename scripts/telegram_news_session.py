"""Persist only Telegram authorization/DC data, never entities or messages."""
import base64
import hashlib
import json
from pathlib import Path
import sys
from atomic_io import atomic_write
from alerts.delivery import protect


def string_session():
    sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'runtime/telegram-news-deps'))
    from telethon.sessions import StringSession
    return StringSession


def fingerprint(api):
    return hashlib.sha256(f"{api['api_id']}:{api['api_hash']}".encode()).hexdigest()


class EncryptedSession:
    def __init__(self,root):
        self.path=Path(root)/'secrets/telegram-news-session.dpapi.json'

    def exists(self):
        return self.path.is_file()

    def save(self,session,api):
        try:
            value=string_session().save(session)
            if not value or not string_session()(value).auth_key:
                raise ValueError()
            payload={'session':value,'api_fingerprint':fingerprint(api)}
            encrypted=protect(json.dumps(payload).encode('utf-8'))
            atomic_write(self.path,json.dumps({'version':1,'protected':base64.b64encode(encrypted).decode('ascii')}))
        except Exception:
            raise ValueError('session_save_failed') from None

    def load(self,api):
        try:
            envelope=json.loads(self.path.read_text(encoding='utf-8'))
            if envelope['version']!=1:raise ValueError()
            payload=json.loads(protect(base64.b64decode(envelope['protected'],validate=True),True))
            if payload['api_fingerprint']!=fingerprint(api):raise ValueError()
            value=payload['session']
            if not isinstance(value,str) or not value or not string_session()(value).auth_key:raise ValueError()
            return value
        except Exception:
            raise ValueError('session_unreadable') from None

    def clear(self):
        try:
            self.path.unlink(missing_ok=True)
        except OSError:
            raise ValueError('session_delete_failed') from None
