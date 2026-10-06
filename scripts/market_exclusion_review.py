"""User approval of unresolved NH failures; immutable inputs and durable consent."""
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys
import threading
import time
from uuid import uuid4

from invest_agent.trading.runner import atomic_write
from invest_agent.trading.store import instance_lock, BusyError

ROOT = Path(__file__).resolve().parents[1]
REGISTRY = ROOT/'runtime/market-scope-user/records.json'


class ReviewConflict(ValueError):
    pass


def now():
    return datetime.now(timezone.utc).isoformat()


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def load(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def save(path, value):
    body = json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False)
    for attempt in range(20):
        try:
            atomic_write(Path(path), body)
            return
        except PermissionError:
            # Windows readers can briefly hold the destination during atomic replacement.
            if attempt==19: raise
            time.sleep(.05)


def identity(row):
    return tuple(row.get(k) for k in ('market','symbol','isin_code','exchange'))


def registry(path=None):
    path = Path(path or REGISTRY)
    if not path.exists():
        return {'schema':1,'records':[],'jobs':[]}, b''
    raw = path.read_bytes(); value = json.loads(raw)
    if value.get('schema') != 1 or not isinstance(value.get('records'),list) or not isinstance(value.get('jobs'),list):
        raise ValueError('invalid_user_exclusions')
    seen = set()
    for row in value['records']:
        key = identity(row)
        if (not all(key) or key in seen or key[0] not in ('KR','US') or row.get('identity_bound') is not True
                or row.get('reason') != 'user_excluded_nh_unrecovered'
                or not all(row.get(k) for k in ('approved_on','approval','evidence_run','approval_id'))):
            raise ValueError('invalid_user_exclusions')
        seen.add(key)
    return value, raw


def read_records(path=None):
    value, _ = registry(path)
    rows = value['records']
    return rows, json.dumps(rows,sort_keys=True,ensure_ascii=False).encode() if rows else b''


def explanation(code):
    """Only curated messages reach the browser; raw provider errors stay private."""
    code = str(code or '')
    messages = {
        'unavailable_market_cap':'시가총액을 확보하지 못했습니다.',
        'historical_price_gaps':'가격 이력에 빈 거래일이 있습니다.',
        'stale_or_invalid_last_session':'최근 완료 거래일의 가격이 없거나 유효하지 않습니다.',
        'no_completed_session_bars':'완료된 일봉 자료가 없습니다.',
        'nh_identity_exchange_isin_mismatch':'NH 종목 식별자 또는 거래소가 일치하지 않습니다.',
        'nh_kr_short_code_market_mismatch':'NH 종목 코드와 시장이 일치하지 않습니다.',
        'nh_kr_exchange_unverified':'NH 응답의 국내 거래소를 확인하지 못했습니다.',
        'market_transport_or_payload_unavailable':'Toss 연결 또는 응답을 확인하지 못했습니다.',
        'market_transport_failure':'NH 연결 또는 응답을 확인하지 못했습니다.',
        'authentication_unavailable':'NH 인증 서버에 연결하지 못했습니다.',
        'credentials_missing':'NH 인증 설정을 확인하지 못했습니다.',
        'retry_after_requires_later_run':'NH 요청 제한으로 다음 갱신 때 다시 시도해야 합니다.',
        'nh_volume_relation_unverified':'NH 거래량과 기존 Toss 거래량의 관계를 확인하지 못했습니다.',
        'nh_toss_historical_ohlc_mismatch':'NH 가격이 기존 Toss 가격과 일치하지 않습니다.',
        'recent_toss_overlap_insufficient':'최근 Toss 대조 이력이 부족합니다.',
        'verified_prior_toss_series_required':'검증에 필요한 이전 Toss 이력이 없습니다.',
        'nh_period_business_response_unavailable':'NH가 정상 조회 결과를 반환하지 않았습니다.',
        'nh_volume_lower_bound_zero_recent':'NH 최근 거래량의 유효성을 확인하지 못했습니다.',
        'nh_market_cap_field_unavailable':'NH 응답에 확인 가능한 시가총액 자료가 없습니다.',
        'nh_dated_market_cap_unavailable':'NH 시가총액의 기준일·주식수를 확인하지 못했습니다.',
        'nh_period_empty':'NH 가격 응답이 비어 있습니다.',
        'nh_period_history_incomplete':'NH 가격 이력도 부족합니다.',
        'nh_period_completed_session_missing':'NH에 최근 완료 거래일 자료가 없습니다.',
        'nh_volume_interval_changes_universe_decision':'NH 거래량 단위 차이로 검증을 통과하지 못했습니다.',
        'adr_ratio_or_share_units_unverified':'ADR 환산비 또는 주식수 단위를 확인하지 못했습니다.',
    }
    if code in messages:
        return messages[code]
    status = re.fullmatch(r'(?:nh_|market_|authentication_)http_(\d{3})(?:_[a-z_]+)?',code)
    if status:
        number = status[1]
        label = {'400':'조회 요청을 처리하지 못했습니다.','404':'조회 자료를 찾지 못했습니다.',
                 '401':'인증에 실패했습니다.','403':'접근이 거절됐습니다.','429':'요청 한도를 초과했습니다.'}.get(number,'서버 응답에 오류가 있습니다.')
        return label+' (HTTP '+number+')'
    if 'parity' in code or 'comparison' in code or 'prior_toss' in code:
        return '기존 Toss 가격·거래량과의 대조를 통과하지 못했습니다.'
    if code.startswith('nh_period_'):
        return 'NH 가격 이력의 날짜·수치·연속성 검증을 통과하지 못했습니다.'
    return '보완 자료를 확보하거나 검증하지 못했습니다.'


def inventory(snapshot, quality, reports=None):
    listings = {(r['market'],r['symbol']):r for r in snapshot['universe']}
    series = {(r['market'],r['symbol']):r for r in snapshot.get('series',[])}
    errors = {(r['market'],r['symbol']) for r in snapshot.get('errors',[])}
    checks = {(r['market'],r['symbol']):r for r in quality.get('quality',[])}
    issues = {}
    for report in reports if reports is not None else snapshot.get('nh_fallback',{}).get('results',[]):
        key = report.get('market'),report.get('symbol')
        if (key not in listings or report.get('state') not in ('unavailable','observed_not_adopted')
                or report.get('nh_requested') is not True or report.get('scope_decision')):
            continue
        row = listings[key]; prices = series.get(key); check = checks.get(key,{})
        code = report.get('toss_error_code')
        is_cap = code == 'unavailable_market_cap'
        cap = row.get('market_cap')
        cap_missing = not isinstance(cap,(int,float)) or cap <= 0 or str(row.get('market_cap_quality','')).startswith('unavailable')
        price_missing = (key in errors or not prices or any(check.get(k) for k in ('missing_sessions','invalid_sessions','non_sessions'))
                         or check.get('last') != snapshot['sessions'][key[0]][-1])
        if not (cap_missing if is_cap else price_missing):
            continue
        item = {k:row.get(k) for k in ('market','symbol','name','isin_code','provider_symbol')}
        item.update(exchange=row.get('listing_exchange_family'),field='시가총액' if is_cap else '가격 이력',
                    toss_code=code,nh_code=report.get('reason'),toss_reason=explanation(code),
                    nh_reason=explanation(report.get('reason')),nh_attempted=True)
        item['id'] = digest([identity(item),code,item['nh_code']])
        item['exclude_allowed'] = bool(all(identity(item)))
        item['block_reason'] = '' if item['exclude_allowed'] else '종목 식별자가 없어 제외를 저장할 수 없습니다.'
        issues[key] = item
    return {'schema':1,'universe_count':len(listings),'sessions':{k:v[-1] for k,v in snapshot['sessions'].items()},
            'identities':[{k:r.get(k) for k in ('market','symbol','isin_code','listing_exchange_family')} for r in listings.values()],
            'issues':list(issues.values())}


def checked(path, expected):
    from sector_observation import checked as check
    if not expected:
        raise ValueError('unverified_failure_evidence')
    return load(check(Path(path),expected,{}))


def latest(root):
    path = Path(load(root/'runtime/latest.json')['output']).resolve()
    if not path.is_relative_to((root/'runtime').resolve()):
        raise ValueError('run_outside_runtime')
    manifest = load(path/'manifest.json')
    run = checked(path/'run.json',manifest['output_hashes'].get('run.json'))
    return path,manifest,run


class ExclusionReview:
    def __init__(self, root=ROOT, publish=None, worker=None):
        self.root = Path(root).resolve()
        self.path = self.root/'runtime/market-scope-user/records.json'
        self.publish = publish or (lambda path:None)
        self.worker = worker or self._process
        self.lock = threading.RLock()
        self.thread = None
        self.cached = None
        self.published = None

    def _review(self):
        import market_scope
        path,manifest,run = latest(self.root)
        cache_key = str(path),digest(manifest)
        if not self.cached or self.cached[0] != cache_key:
            artifact = 'market-failures.json'
            if artifact in manifest['output_hashes']:
                value = checked(path/artifact,manifest['output_hashes'][artifact])
            elif run.get('market_source'):
                source = Path(run['market_source'])
                snapshot = checked(source/'snapshot.json',manifest['input_hashes'].get(str(source/'snapshot.json')))
                quality = checked(path/'quality.json',manifest['output_hashes'].get('quality.json'))
                reports = snapshot.get('nh_fallback',{}).get('results')
                old_report = path/'nh_fallback/summary.json'
                expected = manifest['input_hashes'].get(str(old_report))
                if reports is None and expected:
                    reports = checked(old_report,expected).get('results',[])
                value = inventory(snapshot,quality,reports or [])
            else:
                value = {'schema':1,'universe_count':0,'sessions':{},'issues':[]}
            self.cached = cache_key,value
        value = self.cached[1]
        policy = market_scope.load(user_registry=self.path)
        rows = [r for r in value['issues'] if not market_scope.match(r,policy)]
        active_count = sum(not market_scope.match(r,policy) for r in value.get('identities',[]))
        _,raw = read_records(self.path)
        return {'review_id':digest([cache_key,hashlib.sha256(raw).hexdigest()]),'run_id':path.name,
                'source_run':str(path),'sessions':value['sessions'],'universe_count':active_count,'issues':rows}

    def _busy(self):
        return bool(self.thread and self.thread.is_alive())

    def _start(self, job_id):
        self.thread = threading.Thread(target=self._finish,args=(job_id,),daemon=True)
        self.thread.start()

    def _finish(self, job_id):
        try:
            value,_ = registry(self.path); job = value['jobs'][-1]
            if job['id'] != job_id:
                return
            if not job.get('result_run'):
                self.worker(job_id)
            value,_ = registry(self.path); job = value['jobs'][-1]
            if job['id']==job_id and job.get('result_run'):
                result = Path(job['result_run'])
                self.publish(result)
                self.published = job_id
                update_job(self.path,job_id,state='completed',finished_at=now(),error=None)
        except Exception:
            import traceback
            self.path.parent.mkdir(parents=True,exist_ok=True)
            with (self.path.parent/(job_id+'.log')).open('a',encoding='utf-8') as stream:
                traceback.print_exc(file=stream)
            update_job(self.path,job_id,state='failed',error='apply_failed')

    def _process(self, job_id):
        executable = Path(sys.executable)
        if executable.name.lower()=='pythonw.exe':
            executable = executable.with_name('python.exe')
        log = self.path.parent/(job_id+'.log')
        import os
        env = dict(os.environ,PYTHONIOENCODING='utf-8',OPENBLAS_NUM_THREADS='1',OMP_NUM_THREADS='1')
        with log.open('a',encoding='utf-8') as stream:
            result = subprocess.run([str(executable),str(self.root/'scripts/market_exclusion_review.py'),'--apply-job',job_id],
                cwd=self.root,env=env,stdout=stream,stderr=stream,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        if result.returncode:
            raise ValueError('apply_failed')

    def status(self):
        with self.lock:
            review = self._review()
            value,_ = registry(self.path); job = value['jobs'][-1] if value['jobs'] else None
            if job and job['state'] in ('computed','completed') and self.published != job['id'] and not self._busy():
                # Only publish a completed approval result while it is still the latest view.
                if Path(job['result_run']).name==review['run_id']:
                    self._start(job['id'])
                else:
                    self.published = job['id']
            active = self._busy()
            interrupted = False
            if job and job['state'] in ('approved','applying') and not active:
                try:
                    with instance_lock(self.root/'runtime'): pass
                    interrupted = True
                except BusyError: active = True
            public = None
            if job:
                state = 'interrupted' if interrupted else 'applying' if active else job['state']
                public = {k:job.get(k) for k in ('id','count','approved_at','finished_at')}
                public.update(state=state,error=job.get('error'),retry_available=not active and state in ('failed','interrupted'))
            fields = ('id','market','symbol','name','field','toss_reason','nh_reason','exclude_allowed','block_reason')
            return {k:review[k] for k in ('review_id','run_id','sessions')} | {
                'issues':[{k:r.get(k) for k in fields} for r in review['issues']],
                'job':public,'busy':active,'view_ready':not active}

    def approve(self, review_id, issue_ids):
        if (not isinstance(review_id,str) or not isinstance(issue_ids,list) or not issue_ids or len(issue_ids)>500
                or any(not isinstance(i,str) for i in issue_ids) or len(set(issue_ids))!=len(issue_ids)):
            raise ValueError('invalid_exclusion_request')
        with self.lock:
            if self._busy(): raise ReviewConflict('exclusion_apply_running')
            try:
                with instance_lock(self.root/'runtime/dashboard-daily'),instance_lock(self.root/'runtime'),instance_lock(self.path.parent):
                    self.cached = None  # Re-verify pinned evidence on every approval.
                    current = self._review()
                    if current['review_id'] != review_id: raise ReviewConflict('exclusion_review_changed')
                    available = {r['id']:r for r in current['issues'] if r['exclude_allowed']}
                    if any(i not in available for i in issue_ids): raise ValueError('invalid_exclusion_selection')
                    if len(issue_ids)>=current['universe_count']: raise ValueError('empty_market_scope')
                    value,_ = registry(self.path)
                    if value['jobs'] and value['jobs'][-1]['state'] in ('approved','applying','computed'):
                        raise ReviewConflict('exclusion_apply_running')
                    job_id = uuid4().hex; at = now()
                    for identifier in issue_ids:
                        item = available[identifier]
                        record = {k:item[k] for k in ('market','symbol','name','isin_code','provider_symbol','exchange')}
                        record.update(identity_bound=True,reason='user_excluded_nh_unrecovered',approved_on=at[:10],
                            approval='사용자: 화면에서 NH 보완 실패 종목의 현재/향후 운영 제외 승인',approval_id=job_id,
                            evidence_run=current['source_run'],failure_evidence=item)
                        value['records'].append(record)
                    value['jobs'].append(dict(id=job_id,state='approved',approved_at=at,count=len(issue_ids),
                        source_run=current['source_run'],review_id=review_id,issue_ids=issue_ids))
                    save(self.path,value)
            except BusyError:
                raise ReviewConflict('market_update_running') from None
            self._start(job_id)
            return self.status()

    def retry(self, job_id):
        with self.lock:
            status = self.status()
            if not status.get('job') or status['job']['id']!=job_id or not status['job']['retry_available']:
                raise ReviewConflict('exclusion_job_changed')
            self._start(job_id)
            return self.status()


def update_job(path, job_id, **fields):
    for attempt in range(20):
        try:
            with instance_lock(Path(path).parent):
                value,_ = registry(path)
                job = next((j for j in value['jobs'] if j['id']==job_id),None)
                if not job: raise ValueError('unknown_exclusion_job')
                job.update(fields); save(path,value)
            return
        except BusyError:
            if attempt==19: raise
            time.sleep(.05)


def apply_job(job_id):
    """Child process: no network, same daily/runtime locks as normal collection."""
    import socket
    from unittest.mock import patch
    import daily_market
    import repair_market_scope
    try:
        with instance_lock(ROOT/'runtime/dashboard-daily'),instance_lock(ROOT/'runtime'),\
                patch.object(socket.socket,'connect',side_effect=AssertionError('offline_network_forbidden')):
            value,_ = registry(); job = value['jobs'][-1]
            if job['id']!=job_id: raise ReviewConflict('exclusion_job_changed')
            if job['state'] in ('computed','completed'): return
            update_job(REGISTRY,job_id,state='applying',error=None,started_at=now())
            # Use the newest verified result if another update finished after approval.
            source_run,_,current = latest(ROOT)
            if not current.get('market_source'):
                source_run = Path(job['source_run'])
            config = load(ROOT/'config.json')
            derived = repair_market_scope.derive(source_run,config)
            before = load(ROOT/'runtime/latest.json')
            result = daily_market.run(config,source=Path(derived['source']))
            # Remaining unapproved gaps can keep overall data quality failed even
            # when all calculation stages completed and selected exclusions applied.
            if result.get('error') or result.get('stages',{}).get('report')!='completed':
                save(ROOT/'runtime/latest.json',before)
                raise ValueError('exclusion_calculation_failed')
            update_job(REGISTRY,job_id,state='computed',result_run=result['output'],calculated_at=now())
    except BusyError:
        return  # Another process owns the calculation; it owns the durable state too.
    except Exception:
        update_job(REGISTRY,job_id,state='failed',error='apply_failed')
        raise


if __name__=='__main__':
    import argparse
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--apply-job',required=True)
    apply_job(parser.parse_args().apply_job)
