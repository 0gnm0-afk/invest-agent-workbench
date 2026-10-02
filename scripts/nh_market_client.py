"""Bounded NH market-only period requests, never account/order endpoints."""
import hashlib
import json
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from nh_market_auth import BASE, NHMarketError, NoRedirect, token

PERIOD = '/gbstock/quote/v1/period'
KR_PERIOD = '/krstock/quote/v1/period'
CURRENT = '/gbstock/quote/v1/current'


def us_period_inputs(symbol, end):
    return {'iem_cd':symbol,'end_dt':end,'count':'200','maxavg':'000',
            'gubun':'3','xtick':'0001','today_cls':'0','market_cls':'1'}


def kr_period_inputs(symbol, end):
    return {'market_cd':'UNT','iem_cd':symbol,'mrkt_div_cls_code':'1',
            'edate':end,'array_cnt':'200','maxavg':'000','gubun':'1','xtick':'000',
            'today_cls_code':'0','fake_tick':'0','sur_flag':'0','sur_gb_day_cnt':'00',
            'sur_bf_end_time':'000000','out1_scale_change':'0','out2_scale_change':'0',
            'view_main_yn':'N'}


def digest(path, inputs):
    return hashlib.sha256(json.dumps([path,inputs],sort_keys=True).encode()).hexdigest()


class MarketClient:
    def __init__(self, root, evidence_root=None):
        self.root = Path(root)
        self.evidence_root = Path(evidence_root) if evidence_root else None
        self.last = 0.0
        self.root.mkdir(parents=True,exist_ok=True)

    def read(self, path, inputs):
        if path not in (PERIOD,KR_PERIOD,CURRENT):
            raise NHMarketError('market_only_period_required')
        key = digest(path, inputs)
        cached = (self.evidence_root or self.root)/'responses'/(key+'.json')
        if cached.is_file():
            response = json.loads(cached.read_text(encoding='utf-8'))
            if response.get('path') != path or response.get('inputs') != inputs:
                raise NHMarketError('cached_market_request_mismatch')
            if response.get('http_status') == 400:
                raise NHMarketError('market_http_400')
            return response, cached
        if self.evidence_root:
            raise NHMarketError('requested_evidence_not_saved')
        current_token = token()
        for attempt in range(1,4):
            time.sleep(max(0,.35-(time.monotonic()-self.last)))
            self.last = time.monotonic()
            started = time.monotonic()
            event = {'path':path,'inputs':inputs,'attempt':attempt,
                     'at':datetime.now(timezone.utc).isoformat()}
            req = urllib.request.Request(BASE+path,
                data=json.dumps({'Input_0':inputs}).encode(),method='POST',
                headers={'Authorization':'Bearer '+current_token,
                         'Content-Type':'application/json; charset=utf-8'})
            try:
                with urllib.request.build_opener(NoRedirect()).open(req,timeout=30) as response:
                    payload = json.load(response)
                    headers = {k:v for k,v in response.headers.items()
                               if k.lower() in ('cts','cts_flag','retry-after') or 'limit' in k.lower()}
                event['status'] = 200
                result = {'path':path,'inputs':inputs,'payload':payload,
                          'headers':headers,'fetched_at':event['at']}
                cached.parent.mkdir(parents=True,exist_ok=True)
                cached.write_text(json.dumps(result,ensure_ascii=False,sort_keys=True),encoding='utf-8')
                return result, cached
            except urllib.error.HTTPError as exc:
                event['status'] = exc.code
                if exc.code == 400:
                    # Persist request/status evidence, never credentials or arbitrary error bodies.
                    cached.parent.mkdir(parents=True,exist_ok=True)
                    cached.write_text(json.dumps({'path':path,'inputs':inputs,'http_status':400,
                        'fetched_at':event['at']},sort_keys=True),encoding='utf-8')
                if exc.code == 401 and attempt == 1:
                    current_token = token(rejected_token=current_token)
                elif exc.code not in (429,500,502,503,504) or attempt == 3:
                    raise NHMarketError('market_http_'+str(exc.code)) from None
                else:
                    try:
                        delay = max(2*attempt,float(exc.headers.get('Retry-After',0)))
                    except ValueError:
                        raise NHMarketError('retry_after_requires_later_run') from None
                    if delay > 30:
                        raise NHMarketError('retry_after_requires_later_run')
                    time.sleep(delay)
            except (urllib.error.URLError, TimeoutError, OSError, ValueError):
                event['status'] = 'transport_or_payload_error'
                if attempt == 3:
                    raise NHMarketError('market_transport_failure') from None
                time.sleep(attempt*2)
            finally:
                event['elapsed_seconds'] = round(time.monotonic()-started,3)
                with (self.root/'http.jsonl').open('a',encoding='utf-8') as stream:
                    stream.write(json.dumps(event,ensure_ascii=False)+'\n')
