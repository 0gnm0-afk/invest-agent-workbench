"""API credentials and authorization session only; never persist news or login codes."""
import base64
import hashlib
import json
from pathlib import Path
import re
import sys
import winreg
from atomic_io import atomic_write
from local_secrets import protect
from workbench_paths import ROOT


def _save(path, payload):
    encrypted = protect(json.dumps(payload).encode('utf-8'))
    atomic_write(path, json.dumps({'version': 1, 'protected': base64.b64encode(encrypted).decode('ascii')}))


def _load(path):
    envelope = json.loads(path.read_text(encoding='utf-8'))
    if envelope['version'] != 1:
        raise ValueError()
    return json.loads(protect(base64.b64decode(envelope['protected'], validate=True), True))


def validate(api_id, api_hash):
    if type(api_id) is not int or not 1 <= api_id <= 2147483647 or not isinstance(api_hash,str) or not re.fullmatch(r'[a-fA-F0-9]{32}',api_hash):
        raise ValueError('invalid_api_credentials')
    return {'api_id':api_id,'api_hash':api_hash}


class ApiCredentials:
    def __init__(self, root):
        self.path=Path(root)/'secrets/telegram-news-api.dpapi.json'

    def exists(self):
        return self.path.is_file()

    def save(self, api_id, api_hash):
        payload=validate(api_id,api_hash)
        try:
            _save(self.path,payload)
        except Exception:
            raise ValueError('api_credentials_save_failed') from None

    def load(self):
        if not self.exists():
            raise ValueError('api_credentials_missing')
        try:
            data=_load(self.path)
            return validate(data['api_id'],data['api_hash'])
        except Exception:
            raise ValueError('api_credentials_unreadable') from None

    def clear(self):
        try:
            self.path.unlink(missing_ok=True)
        except OSError:
            raise ValueError('api_credentials_delete_failed') from None


def string_session():
    sys.path.insert(0,str(ROOT/'runtime/telegram-news-deps'))
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
            _save(self.path,payload)
        except Exception:
            raise ValueError('session_save_failed') from None

    def load(self,api):
        try:
            payload=_load(self.path)
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


def import_credentials(root=ROOT):
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,'Environment') as key:
            api_id=winreg.QueryValueEx(key,'TELEGRAM_API_ID')[0]
            api_hash=winreg.QueryValueEx(key,'TELEGRAM_API_HASH')[0]
    except FileNotFoundError:
        return {'state':'waiting_for_user_environment','required_names':['TELEGRAM_API_ID','TELEGRAM_API_HASH']}
    try:
        if not isinstance(api_id,str) or not api_id.strip().isdigit() or not isinstance(api_hash,str):
            raise ValueError('invalid_api_credentials')
        store=ApiCredentials(root)
        store.save(int(api_id.strip()),api_hash.strip())
        return {'state':'api_credentials_saved','protection':'windows_current_user_dpapi',
                'user_environment_retained':True,'login_required':True}
    except Exception:
        return {'state':'import_failed','error':'check_user_api_values_or_dpapi_storage'}
