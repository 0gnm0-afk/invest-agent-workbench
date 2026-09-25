"""NH market-only OAuth token cache; credentials remain in process environment."""
import ctypes
import hashlib
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from invest_agent.trading.store import instance_lock

BASE = 'https://api.nhplug.com:8443'
ROOT = Path(__file__).resolve().parents[1] / 'runtime/nh-auth'


class NHMarketError(Exception):
    pass


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise NHMarketError('redirect_refused')


def _protect(data, decrypt=False):
    if os.name != 'nt':
        raise NHMarketError('windows_dpapi_required')
    class Blob(ctypes.Structure):
        _fields_ = [('size', ctypes.c_ulong), ('data', ctypes.POINTER(ctypes.c_ubyte))]
    buffer = ctypes.create_string_buffer(data)
    incoming = Blob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    outgoing = Blob()
    crypt = ctypes.windll.crypt32
    method = crypt.CryptUnprotectData if decrypt else crypt.CryptProtectData
    if not method(ctypes.byref(incoming), None, None, None, None,
                  1, ctypes.byref(outgoing)):
        raise NHMarketError('token_cache_crypto_failed')
    try:
        return ctypes.string_at(outgoing.data, outgoing.size)
    finally:
        ctypes.windll.kernel32.LocalFree(outgoing.data)


def token(rejected_token=None):
    key, secret = os.environ.get('NH_APP_KEY'), os.environ.get('NH_APP_SECRET')
    if not key or not secret:
        raise NHMarketError('credentials_missing')
    fingerprint = hashlib.sha256((key+'\0'+secret).encode()).hexdigest()
    ROOT.mkdir(parents=True, exist_ok=True)
    with instance_lock(ROOT):
        cache = ROOT/'token.dpapi'
        if cache.exists():
            try:
                saved = json.loads(_protect(cache.read_bytes(), decrypt=True))
                value = saved['token']
                if (saved['fingerprint'] == fingerprint and isinstance(value, str) and value
                        and saved['expires_at'] > time.time()+60
                        and (rejected_token is None or value != rejected_token)):
                    return value
            except (NHMarketError, KeyError, TypeError, ValueError, OSError):
                pass
        query = urllib.parse.urlencode({'appkey':key,'appsecretkey':secret,
                                        'grant_type':'client_credentials','scope':'oob'})
        req = urllib.request.Request(BASE+'/oauth2/token?'+query, data=b'', method='POST',
                                     headers={'Content-Type':'application/x-www-form-urlencoded'})
        try:
            with urllib.request.build_opener(NoRedirect()).open(req, timeout=30) as response:
                payload = json.load(response)
        except urllib.error.HTTPError as exc:
            raise NHMarketError('authentication_http_'+str(exc.code)) from None
        except (urllib.error.URLError, TimeoutError, OSError, ValueError):
            raise NHMarketError('authentication_unavailable') from None
        value = payload.get('access_token')
        lifetime = payload.get('expires_in')
        if (not isinstance(value, str) or not value or not isinstance(lifetime, int)
                or lifetime <= 60 or str(payload.get('token_type','')).lower() != 'bearer'):
            raise NHMarketError('token_metadata_invalid')
        saved = {'token':value,'fingerprint':fingerprint,'expires_at':time.time()+lifetime}
        temporary = cache.with_suffix('.tmp')
        temporary.write_bytes(_protect(json.dumps(saved).encode()))
        temporary.replace(cache)
        return value
