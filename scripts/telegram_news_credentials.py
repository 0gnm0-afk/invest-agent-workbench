"""User-approved persistence of API ID/hash only, encrypted with Windows DPAPI."""
import base64
import json
from pathlib import Path
import re
from atomic_io import atomic_write
from telegram_warnings import protect


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
            encrypted=protect(json.dumps(payload).encode('utf-8'))
            atomic_write(self.path,json.dumps({'version':1,'protected':base64.b64encode(encrypted).decode('ascii')}))
        except Exception:
            raise ValueError('api_credentials_save_failed') from None

    def load(self):
        if not self.exists():
            raise ValueError('api_credentials_missing')
        try:
            envelope=json.loads(self.path.read_text(encoding='utf-8'))
            if envelope['version']!=1: raise ValueError()
            data=json.loads(protect(base64.b64decode(envelope['protected'],validate=True),True))
            return validate(data['api_id'],data['api_hash'])
        except Exception:
            raise ValueError('api_credentials_unreadable') from None

    def clear(self):
        try:
            self.path.unlink(missing_ok=True)
        except OSError:
            raise ValueError('api_credentials_delete_failed') from None
