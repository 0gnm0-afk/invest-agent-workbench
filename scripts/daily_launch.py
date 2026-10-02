"""One market cycle per KST launch day, independent of dashboard server ports."""
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import time
from zoneinfo import ZoneInfo

from invest_agent.trading.market_provider import completed_sessions
from invest_agent.trading.runner import atomic_write
from invest_agent.trading.store import instance_lock, BusyError


def utc_now():
    return datetime.now(timezone.utc)


def capped_sessions(sessions, cutoff=None):
    if cutoff is None:
        return sessions
    datetime.strptime(cutoff, '%Y-%m-%d')
    selected = [d for d in sessions if d <= cutoff]
    if not selected:
        raise ValueError('completed_sessions_unavailable')
    return selected


def plan_for(now):
    if now.tzinfo is None:
        raise ValueError('timezone_required')
    day = now.astimezone(ZoneInfo('Asia/Seoul')).date()
    cutoff = (day - timedelta(days=1)).isoformat()
    return {'day':day.isoformat(), 'cutoff':cutoff, 'evaluated_at':now.isoformat(),
            'targets':{m:capped_sessions(completed_sessions(m, now), cutoff)[-1] for m in ('KR','US')}}


class DailyLaunch:
    def __init__(self, root, runner=None, planner=plan_for):
        self.root = Path(root).resolve()
        self.folder = self.root / 'runtime/dashboard-daily'
        self.runner = runner or self.execute
        self.planner = planner

    def read(self, day):
        path = self.folder / (day + '.json')
        return json.loads(path.read_text(encoding='utf-8')) if path.exists() else None

    def save(self, record):
        for attempt in range(6):
            try:
                atomic_write(self.folder / (record['day'] + '.json'), json.dumps(record, ensure_ascii=False, allow_nan=False))
                return
            except PermissionError:
                if attempt == 5:
                    raise
                time.sleep(.05 * (attempt + 1))

    def checked_run(self, value):
        if not value:
            return None
        path = Path(value).resolve()
        if not path.is_relative_to(self.root / 'runtime'):
            raise ValueError('run_outside_runtime')
        manifest = json.loads((path/'manifest.json').read_text(encoding='utf-8'))
        run_path = path/'run.json'
        if hashlib.sha256(run_path.read_bytes()).hexdigest() != manifest['output_hashes'].get('run.json'):
            raise ValueError('run_hash_mismatch')
        return json.loads(run_path.read_text(encoding='utf-8'))

    def usable_run(self, value):
        try:
            run = self.checked_run(value)
            if not run or not run.get('market_source'):
                return None
            path = Path(value)
            manifest = json.loads((path/'manifest.json').read_text(encoding='utf-8'))
            review = path/'priority-review.json'
            if not review.is_file() or hashlib.sha256(review.read_bytes()).hexdigest() != manifest['output_hashes'].get('priority-review.json'):
                return None
            return str(path.resolve())
        except (OSError, ValueError, KeyError, TypeError):
            return None

    def display_run(self):
        latest = self.root/'runtime/latest.json'
        try:
            current = self.usable_run(json.loads(latest.read_text(encoding='utf-8'))['output'])
            if current:
                return current
        except (OSError, ValueError, KeyError, TypeError):
            pass
        for path in sorted(self.folder.glob('????-??-??.json'), reverse=True):
            try:
                record = json.loads(path.read_text(encoding='utf-8'))
                current = self.usable_run(record.get('display_run'))
                if current:
                    return current
            except (OSError, ValueError, KeyError, TypeError):
                continue
        return None

    def once(self, now=None, retry=False):
        now = now or utc_now()
        day = now.astimezone(ZoneInfo('Asia/Seoul')).date().isoformat()
        try:
            # The OS releases this lock on crash; the durable receipt still prevents a rerun.
            with instance_lock(self.folder):
                record = self.read(day)
                if record and not retry:
                    if record['state'] == 'running':
                        record.update(state='interrupted', error='previous_launch_interrupted', finished_at=utc_now().isoformat())
                        self.save(record)
                    return {**record, 'reused':True}
                if record:
                    archive = self.folder/'attempts'/(day+'-'+datetime.now(timezone.utc).strftime('%H%M%S%f')+'.json')
                    atomic_write(archive,json.dumps(record,ensure_ascii=False))
                record = {'day':day,'state':'running','stage':'calendar','started_at':now.isoformat(),
                          'finished_at':None,'targets':{},'display_run':self.display_run(),'run':None,'error':None}
                self.save(record)
                def progress(update):
                    record.update({k:v for k,v in update.items() if k in ('stage','run')})
                    self.save(record)
                try:
                    plan = self.planner(now)
                    record.update(plan)
                    self.save(record)
                    run = self.runner({**plan,'prior_run':record['display_run']}, progress)
                    record.update(run=run.get('output'), state=run['state'], stage='finished')
                    display = self.usable_run(run.get('output'))
                    if display:
                        record['display_run'] = display
                        review = json.loads((Path(display)/'priority-review.json').read_text(encoding='utf-8'))
                        if review.get('session_dates') != plan['targets']:
                            record.update(state='failed',error='target_sessions_not_reached')
                    if run['state'] == 'failed' and not record['error']:
                        allowed_errors = ('market_authentication_http_403_ip_not_allowed',
                                          'market_http_403_ip_not_allowed',
                                          'market_authentication_http_403',
                                          'market_authentication_http_401',
                                          'market_http_401',
                                          'market_authentication_unavailable',
                                          'basis_document_changed_review_required',
                                          'basis_document_refresh_unavailable',
                                          'basis_document_redirect_refused',
                                          'basis_document_not_current')
                        record['error'] = next((code for code in allowed_errors
                                                if code in run.get('error','')), 'market_pipeline_failed')
                    if run['state'] in ('succeeded','partial') and not display:
                        record.update(state='failed',error='screening_result_missing')
                except Exception:
                    record.update(state='failed',error='daily_update_unavailable')
                record['finished_at'] = utc_now().isoformat()
                self.save(record)
                return {**record, 'reused':False}
        except BusyError:
            return {**(self.read(day) or {'day':day,'state':'running','stage':'waiting','targets':{}}),'reused':True,'following':True}

    def execute(self, plan, progress):
        python = Path(sys.executable)
        if python.name.lower() == 'pythonw.exe':
            python = python.with_name('python.exe')
        command = [str(python), str(self.root/'scripts/daily_market.py'),
                   '--session-cutoff',plan['cutoff'],'--evaluated-at',plan['evaluated_at']]
        if plan.get('prior_run'):
            command += ['--prior-run', plan['prior_run']]
        log = self.folder/(plan['day']+'-'+datetime.now(timezone.utc).strftime('%H%M%S%f')+'.log')
        result = None
        with log.open('w',encoding='utf-8') as stream:
            with subprocess.Popen(command,cwd=self.root,stdout=subprocess.PIPE,stderr=stream,
                                  text=True,encoding='utf-8',errors='replace',
                                  creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0)) as process:
                for line in process.stdout:
                    stream.write(line);stream.flush()
                    try:
                        event=json.loads(line)
                    except ValueError:
                        continue
                    if 'stage' in event and 'output' in event:
                        output=Path(event['output']).resolve()
                        if output.is_relative_to(self.root/'runtime'):
                            progress({'stage':event['stage'],'run':str(output)})
                    if event.get('state') in ('succeeded','partial','failed') and 'stages' in event:
                        result=event
                process.wait()
                if result is None or process.returncode not in (0,1,2):
                    raise RuntimeError('market_worker_failed')
        return result

    def public(self, record):
        result={k:record.get(k) for k in ('day','state','stage','targets','started_at','finished_at','error','reused')}
        result['display_stale'] = bool(record.get('display_run') and record.get('display_run') != record.get('run'))
        if record.get('run') and record.get('state') == 'running':
            try:
                path=Path(record['run']).resolve()
                if not path.is_relative_to(self.root/'runtime'):
                    raise ValueError('outside_runtime')
                progress=json.loads((path/'collection/cache/progress.json').read_text())
                result['progress']={k:progress.get(k) for k in ('requested','completed','received','failed')}
            except (OSError,ValueError,KeyError):
                pass
        return result
