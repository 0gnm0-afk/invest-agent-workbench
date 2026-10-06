"""Private, finite Telegram delivery for committed completed-session warnings."""
import argparse
import base64
from contextlib import closing
import ctypes
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import sqlite3
import urllib.error
import urllib.request

from invest_agent.trading.report_format import money
from invest_agent.trading.runner import atomic_write
from invest_agent.trading.store import instance_lock, BusyError

ROOT = Path(__file__).resolve().parents[1]


def utc():
    return datetime.now(timezone.utc).isoformat()


def protect(data, decrypt=False):
    """Windows current-user DPAPI; no plaintext secret on disk or command line."""
    class Blob(ctypes.Structure):
        _fields_ = [('size', ctypes.c_ulong), ('data', ctypes.POINTER(ctypes.c_ubyte))]
    buffer = ctypes.create_string_buffer(data)
    source = Blob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    target = Blob()
    crypt = ctypes.WinDLL('crypt32', use_last_error=True)
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    fn = crypt.CryptUnprotectData if decrypt else crypt.CryptProtectData
    if not fn(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(target)):
        raise ValueError('secret_protection_failed')
    try:
        return ctypes.string_at(target.data, target.size)
    finally:
        kernel.LocalFree(target.data)


def configure(root):
    path = Path(root)/'secrets/telegram.dpapi.json'
    if path.exists():
        raise ValueError('telegram_already_configured')
    token, chat = (os.environ.get(k, '') for k in ('TELEGRAM_BOT_TOKEN', 'TELEGRAM_CHAT_ID'))
    if not re.fullmatch(r'\d+:[A-Za-z0-9_-]+', token) or not re.fullmatch(r'-?\d+', chat):
        raise ValueError('telegram_environment_missing_or_invalid')
    # Fix the activation boundary before any subsequent warning can be generated.
    config = dict(token=token, chat=chat, enabled_at=utc())
    atomic_write(path, json.dumps({'protected':base64.b64encode(protect(json.dumps(config).encode())).decode()}))
    return {'state':'configured'}


def settings(root):
    path = Path(root)/'secrets/telegram.dpapi.json'
    if not path.exists():
        return None
    return json.loads(protect(base64.b64decode(json.loads(path.read_text())['protected']), True))


class DeliveryError(Exception):
    def __init__(self, state, code):
        self.state, self.code = state, code
        super().__init__(code)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def api(config, method, payload):
    if method not in ('getMe', 'getChat', 'sendMessage'):
        raise ValueError('unsupported_telegram_method')
    request = urllib.request.Request('https://api.telegram.org/bot'+config['token']+'/'+method,
        data=json.dumps(payload).encode(), headers={'Content-Type':'application/json'}, method='POST')
    try:
        with urllib.request.build_opener(NoRedirect).open(request, timeout=15) as response:
            result = json.load(response)
    except urllib.error.HTTPError as exc:
        # No raw exception/URL/body is retained: URLs include the credential.
        code = exc.code
        raise DeliveryError('failed' if code in (400,401,403,429) else 'unknown', 'telegram_http_'+str(code)) from None
    except Exception:
        raise DeliveryError('unknown', 'telegram_response_unconfirmed') from None
    if not result.get('ok'):
        raise DeliveryError('failed', 'telegram_rejected')
    return result['result']


def message(e):
    currency = e['currency']
    price = lambda v: money(v, currency)+('원' if currency == 'KRW' else ' USD')
    trailing = e['kind'] == 'trailing'
    title = '트레일링 경고선 도달' if trailing else '5일 이동평균선 접촉·이탈'
    difference = (e['close']/e['threshold']-1)*100
    lines = [f'🔔 투자에이전트 | {title}', '',
        f"종목: {e.get('name') or e['symbol']} ({e['symbol']} · {e['market']})",
        f"기준: {e['date']} 해당 시장 정규장 완료 일봉",
        f"종가: {price(e['close'])}",
        f"{'최종 트레일링' if trailing else '당일 5일선'}: {price(e['threshold'])}",
        f'기준선 대비: {difference:+.2f}%', '', f"발생 이유: {e['reason']}"]
    if trailing:
        lines += ['종가가 유지 중인 최종 경고선 이하로 확인됐습니다. 보유 판단과 대응 계획을 점검할 시점입니다.',
                  '경고선 후보 = max(H − 2.5 × ATR14, H × 95%). 이전 경고선과 평단보다 낮추지 않습니다.']
    else:
        lines += ['전일에는 5일선 위였으나, 이번 종가가 당일 종가를 포함한 5일선 이하로 내려왔습니다.',
                  '트레일링 경고와 별개인 단기 흐름 변화 알림입니다.']
    for key, label in [('high','감시 시작 이후 최고가 H'), ('atr','ATR14'), ('line','최종 트레일링'), ('ma5','당일 5일선')]:
        if e.get(key) is not None and key != ('line' if trailing else 'ma5'):
            lines.append(f'{label}: {price(e[key])}')
    lines += ['', '완료 일봉 기준 알림이며 장중 실시간 감시가 아닙니다.',
              '같은 경고는 반복 전송하지 않으며, 조건 회복 후 다시 발생하면 새로 알립니다.',
              '확인: PC 투자 대시보드 → 가격 위험 경고. 자동 주문은 실행하지 않습니다.']
    return '\n'.join(lines)


def dispatch(root=ROOT, targets=None, *, config=None, sender=None):
    root = Path(root)
    folder = root/'runtime/telegram-delivery'
    folder.mkdir(parents=True, exist_ok=True)
    try:
        with instance_lock(folder):
            config = config or settings(root)
            if not config:
                return {'state':'disabled'}
            if targets is None:
                from daily_launch import plan_for
                targets = plan_for(datetime.now(timezone.utc))['targets']
            from trailing_warnings import TrailingStore
            events = TrailingStore(root/'runtime/trailing-warnings.sqlite3').read()['events']
            sender = sender or (lambda text: api(config, 'sendMessage', {'chat_id':config['chat'], 'text':text}))
            with closing(sqlite3.connect(folder/'delivery.sqlite3', timeout=30)) as db, db:
                db.execute('CREATE TABLE IF NOT EXISTS deliveries(id TEXT PRIMARY KEY,state TEXT,attempts INTEGER,message_id INTEGER,error TEXT,updated_at TEXT)')
                # Interrupted requests may have reached Telegram. Never blindly resend them.
                db.execute("UPDATE deliveries SET state='unknown',error='interrupted_delivery' WHERE state='sending'")
                db.commit()
                for event in reversed(events):
                    if event['created_at'] < config['enabled_at']:
                        continue
                    row = db.execute('SELECT state,attempts FROM deliveries WHERE id=?',(event['id'],)).fetchone()
                    if row and (row[0] != 'failed' or row[1] >= 3):
                        continue
                    attempts = row[1] if row else 0
                    if event['date'] != targets.get(event['market']):
                        db.execute('INSERT OR REPLACE INTO deliveries VALUES(?,?,?,?,?,?)', (event['id'],'expired',attempts,None,'completed_session_changed',utc()))
                        continue
                    body = message(event)
                    db.execute('INSERT OR REPLACE INTO deliveries VALUES(?,?,?,?,?,?)',(event['id'],'sending',attempts+1,None,None,utc()))
                    db.commit()
                    try:
                        result = sender(body)
                        if not isinstance(result.get('message_id'), int):
                            raise DeliveryError('unknown', 'telegram_response_unconfirmed')
                        state, mid, error = 'sent', result['message_id'], None
                    except DeliveryError as exc:
                        state, mid, error = exc.state, None, exc.code
                    except Exception:
                        state, mid, error = 'unknown', None, 'telegram_response_unconfirmed'
                    db.execute('UPDATE deliveries SET state=?,message_id=?,error=?,updated_at=? WHERE id=?',(state,mid,error,utc(),event['id']))
                    db.commit()
                    if state != 'sent':
                        break  # Avoid flooding a failing/rate-limited endpoint.
                counts = dict(db.execute('SELECT state,count(*) FROM deliveries GROUP BY state'))
            result = dict(state='attention' if counts.get('failed') or counts.get('unknown') else 'ready', counts=counts, checked_at=utc())
            atomic_write(folder/'latest.json', json.dumps(result))
            return result
    except BusyError:
        return {'state':'busy'}
    except Exception:
        result = {'state':'unavailable','error':'telegram_delivery_unavailable','checked_at':utc()}
        atomic_write(folder/'latest.json',json.dumps(result))
        return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--configure', action='store_true')
    parser.add_argument('--check', action='store_true')
    args = parser.parse_args()
    try:
        if args.configure:
            result = configure(ROOT)
        elif args.check:
            result = {'state':'configured' if settings(ROOT) else 'disabled'}
        else:
            result = dispatch()
    except Exception:
        result = {'state':'unavailable','error':'telegram_configuration_unavailable'}
    print(json.dumps(result))
    return 0 if result['state'] in ('ready','configured','disabled') else 1


if __name__ == '__main__':
    raise SystemExit(main())
