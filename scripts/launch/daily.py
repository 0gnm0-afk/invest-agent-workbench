"""One market cycle per KST launch day, independent of dashboard server ports."""
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import time
from zoneinfo import ZoneInfo
from uuid import uuid4
from atomic_io import load_json_bytes, read_bytes

from invest_agent.trading.market_provider import completed_sessions
from atomic_io import atomic_write
from workbench_paths import SCRIPTS
from runtime_paths import local_runtime_path
from launch.journal import error_frames
from market.collection_receipts import read_collection_receipt
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


def attempt_id(record):
    return record.get('attempt_id') or record.get('started_at') or record.get('day', '')


class DailyLaunch:
    def __init__(self, root, runner=None, planner=plan_for, *, accounts=False):
        self.root = Path(root).resolve()
        self.folder = self.root / 'runtime/dashboard-daily'
        self.runner = runner or self.execute
        self.planner = planner
        # Legacy callers may still supply/set accounts; it grants no permission.
        self.accounts = False

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
        path = local_runtime_path(value, root=self.root).resolve()
        if not path.is_relative_to(self.root / 'runtime'):
            raise ValueError('run_outside_runtime')
        manifest = load_json_bytes(path/'manifest.json')
        run_path = path/'run.json'
        if hashlib.sha256(read_bytes(run_path)).hexdigest() != manifest['output_hashes'].get('run.json'):
            raise ValueError('run_hash_mismatch')
        return load_json_bytes(run_path)

    def collection_receipt(self, value, targets=None):
        """Use M03's published C9; never reconstruct an older run as success."""
        if not value:
            raise ValueError('collection_result_missing')
        path = local_runtime_path(value, root=self.root).resolve()
        if not path.is_relative_to(self.root / 'runtime'):
            raise ValueError('run_outside_runtime')
        receipt = read_collection_receipt(path)
        if targets is not None and receipt['target_trade_date'] != targets:
            raise ValueError('target_sessions_not_reached')
        if receipt['provenance']['run_id'] != path.name:
            raise ValueError('collection_run_id_mismatch')
        return receipt

    def usable_run(self, value):
        try:
            if self.collection_receipt(value)['status'] == 'failed':
                return None
            return str(local_runtime_path(value, root=self.root).resolve())
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

    def target_complete(self, record, plan):
        """Reuse only a verified complete collection for both target sessions."""
        if record.get('state') != 'succeeded' or record.get('targets') != plan['targets']:
            return False
        try:
            return self.collection_receipt(record.get('run'), plan['targets'])['status'] == 'complete'
        except (OSError, ValueError, KeyError, TypeError):
            return False

    def once(self, now=None, retry=False, expected_attempt=None, *, scheduled=False):
        now = now or utc_now()
        local = now.astimezone(ZoneInfo('Asia/Seoul'))
        day = local.date().isoformat()
        if scheduled and (local.hour, local.minute) < (6, 40):
            return {'day':day, 'state':'not_due', 'targets':{}, 'reused':True}
        try:
            # The OS releases this lock on crash; the durable receipt still prevents a rerun.
            with instance_lock(self.folder):
                # A detached collection worker may outlive the coordinator.
                # Do not mark it interrupted or launch a second collector.
                try:
                    with instance_lock(self.folder/'worker-lock'):
                        pass
                except BusyError:
                    return {**(self.read(day) or {'day':day,'targets':{}}),
                            'state':'running','reused':True,'following':True}
                record = self.read(day)
                scheduled_plan = None
                if scheduled:
                    scheduled_plan = self.planner(now)
                    if record and record.get('scheduled_day') == day:
                        retry = False
                    elif record and self.target_complete(record, scheduled_plan):
                        record['scheduled_day'] = day
                        self.save(record)
                        retry = False
                    else:
                        # User-authorized daily schedule gets one attempt after 06:40,
                        # even when an earlier dashboard launch failed or used older US bars.
                        retry = True
                if retry and expected_attempt is not None:
                    eligible = bool(record and (record['state'] in ('failed', 'interrupted', 'partial') or
                                    (record['state'] == 'succeeded' and not self.target_complete(record, {'targets': record.get('targets')}))))
                    if not record or attempt_id(record) != expected_attempt or not eligible:
                        return {**(record or {'day':day,'state':'unavailable','targets':{}}), 'reused':True}
                if record and not retry:
                    if record['state'] == 'running':
                        record.update(state='interrupted', error='previous_launch_interrupted', finished_at=utc_now().isoformat())
                        self.save(record)
                    if record['state'] == 'succeeded' and not self.target_complete(record, {'targets': record.get('targets')}):
                        # Preserve the historical attempt; missing current evidence is
                        # not permission to recollect or to report fresh success.
                        return {**record, 'state':'failed', 'error':'collection_result_missing', 'reused':True}
                    return {**record, 'reused':True}
                if record:
                    archive = self.folder/'attempts'/(day+'-'+datetime.now(timezone.utc).strftime('%H%M%S%f')+'.json')
                    atomic_write(archive,json.dumps(record,ensure_ascii=False))
                scheduled_day = day if scheduled else (record or {}).get('scheduled_day')
                record = {'scheduled_day':scheduled_day,'scheduled':bool(scheduled),'day':day,'attempt_id':uuid4().hex,'manual_retry':bool(retry and not scheduled),'state':'running','stage':'calendar','started_at':now.isoformat(),
                          'finished_at':None,'targets':{},'display_run':self.display_run(),'run':None,'error':None}
                self.save(record)
                def progress(update):
                    record.update({k:v for k,v in update.items() if k in ('stage','run')})
                    self.save(record)
                try:
                    plan = scheduled_plan or self.planner(now)
                    record.update(plan)
                    self.save(record)
                    run = self.runner({**plan,'prior_run':record['display_run']}, progress)
                    record.update(run=run.get('output'), state=run['state'], stage='finished', telegram_state=run.get('telegram_state'), trailing_state=run.get('trailing_state'), scenario_state=run.get('scenario_state'), account_attempted=run.get('account_attempted',False), account_state=run.get('account_state'), account_error=run.get('account_error'))
                    for key in ('technical_state', 'trailing_result', 'scenario_result'):
                        if key in run:
                            record[key] = run[key]
                    display = self.usable_run(run.get('output'))
                    if display:
                        record['display_run'] = display
                    try:
                        receipt = self.collection_receipt(run.get('output'), plan['targets'])
                    except (OSError, ValueError, KeyError, TypeError) as exc:
                        record.update(state='failed', error='target_sessions_not_reached'
                                      if str(exc) == 'target_sessions_not_reached' else 'collection_result_missing')
                    else:
                        record['collection_receipt'] = receipt
                        record['state'] = {'complete': 'succeeded', 'partial': 'partial', 'failed': 'failed'}[receipt['status']]
                    if record['state'] == 'failed' and record['error'] != 'target_sessions_not_reached' and run.get('error'):
                        allowed_errors = ('market_authentication_http_403_ip_not_allowed',
                                          'market_http_403_ip_not_allowed',
                                          'market_authentication_http_403',
                                          'market_authentication_http_401',
                                          'market_http_401',
                                          'market_http_403',
                                          'collection_result_missing',
                                          'market_listing_unavailable',
                                          'market_authentication_unavailable',
                                          'basis_document_changed_review_required',
                                          'basis_document_refresh_unavailable',
                                          'basis_document_redirect_refused',
                                          'basis_document_not_current')
                        record['error'] = next((code for code in allowed_errors
                                                if code in run.get('error','')), 'market_pipeline_failed')
                    if record['state'] == 'failed' and not record['error']:
                        record['error'] = 'market_pipeline_failed'
                except Exception as exc:
                    error = 'market_worker_failed' if str(exc) == 'market_worker_failed' else 'daily_update_unavailable'
                    record.update(state='failed',error=error, error_type=type(exc).__name__, error_frames=error_frames(exc))
                record['finished_at'] = utc_now().isoformat()
                self.save(record)
                return {**record, 'reused':False}
        except BusyError:
            return {**(self.read(day) or {'day':day,'state':'running','stage':'waiting','targets':{}}),'reused':True,'following':True}

    def execute(self, plan, progress):
        python = Path(sys.executable)
        if python.name.lower() == 'pythonw.exe':
            python = python.with_name('python.exe')
        command = [str(python), '-u', str(SCRIPTS/'market/worker.py'),
                   '--root', str(self.root), '--',
                   '--session-cutoff',plan['cutoff'],'--evaluated-at',plan['evaluated_at']]
        if plan.get('prior_run'):
            command += ['--prior-run', plan['prior_run']]
        log = self.folder/(plan['day']+'-'+datetime.now(timezone.utc).strftime('%H%M%S%f')+'.log')
        result = None
        # The child writes to a real file, never to a pipe owned by this process.
        # Losing a dashboard/scheduler cannot break the child's stdout.
        with log.open('w',encoding='utf-8') as stream:
            with subprocess.Popen(command,cwd=self.root,stdout=stream,stderr=stream,
                                  stdin=subprocess.DEVNULL,
                                  creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0)) as process:
                with log.open('r',encoding='utf-8',errors='replace') as reader:
                    pending = ''
                    while True:
                        finished = process.poll() is not None
                        chunk = reader.read()
                        pending += chunk
                        while '\n' in pending:
                            line, pending = pending.split('\n', 1)
                            try:
                                event=json.loads(line)
                            except ValueError:
                                continue
                            if not isinstance(event, dict):
                                continue
                            if 'stage' in event and 'output' in event:
                                output=Path(event['output']).resolve()
                                if output.is_relative_to(self.root/'runtime'):
                                    progress({'stage':event['stage'],'run':str(output)})
                            if event.get('state') in ('succeeded','partial','failed') and 'stages' in event:
                                result=event
                        if not chunk and finished:
                            break
                        time.sleep(.1)
                process.wait()
                if result is None or process.returncode not in (0,1,2):
                    raise RuntimeError('market_worker_failed')
        # A worker's overall state can include downstream work. C9 alone owns
        # the collection outcome and admission to saved-input evaluation.
        result['account_attempted'] = False
        result['account_state'] = None
        result['account_error'] = None
        try:
            receipt = self.collection_receipt(result.get('output'), plan['targets'])
        except (OSError, ValueError, KeyError, TypeError) as exc:
            result['state'] = 'failed'
            if str(exc) == 'target_sessions_not_reached':
                result['error'] = 'target_sessions_not_reached'
            elif not result.get('error'):
                result['error'] = 'collection_result_missing'
            return result
        result['state'] = {'complete': 'succeeded', 'partial': 'partial', 'failed': 'failed'}[receipt['status']]
        if result['state'] in ('succeeded', 'partial'):
            try:
                from alerts.trailing import refresh_saved
                result['trailing_result'] = refresh_saved(self.root, result['output'], plan['targets'])
                result['trailing_state'] = 'evaluated'
            except Exception:
                result['trailing_state'] = 'unavailable'
            try:
                from scenarios.monitor import refresh_saved as refresh_scenarios
                scenario = refresh_scenarios(self.root, result['output'], plan['targets'])
                result['scenario_result'] = scenario
                result['scenario_state'] = 'evaluated' if any(m['active'] for m in scenario['monitors']) else 'not_configured'
            except Exception:
                result['scenario_state'] = 'unavailable'
            try:
                from signals.store import evaluate_saved as evaluate_technical
                result['technical_state'] = evaluate_technical(self.root, plan['targets'])['state']
            except Exception:
                result['technical_state'] = 'unavailable'
            try:
                from alerts.delivery import dispatch
                result['telegram_state'] = dispatch(self.root, plan['targets'],
                                                    include_trailing=result.get('trailing_state') == 'evaluated',
                                                    include_scenarios=result.get('scenario_state') == 'evaluated',
                                                    include_technical=result.get('technical_state') == 'evaluated')['state']
            except Exception:
                # Delivery failure never changes the collection outcome.
                result['telegram_state'] = 'unavailable'
        return result

    def public(self, record):
        result={k:record.get(k) for k in ('day','state','stage','targets','started_at','finished_at','error','reused','manual_retry','telegram_state','trailing_state','scenario_state','account_attempted','account_state','account_error')}
        for key in ('technical_state', 'trailing_result', 'scenario_result', 'collection_receipt'):
            if key in record:
                result[key] = record[key]
        result['attempt_id'] = attempt_id(record)
        result['display_updated'] = False
        displayed, current = record.get('display_run'), record.get('run')
        result['display_stale'] = bool(displayed and (not current or
            local_runtime_path(displayed, root=self.root).resolve() != local_runtime_path(current, root=self.root).resolve()))
        if result['display_stale'] and record.get('finished_at'):
            try:
                display = self.checked_run(record['display_run'])
                result['display_updated'] = datetime.fromisoformat(display['finished_at']) > datetime.fromisoformat(record['finished_at'])
            except (OSError, ValueError, KeyError, TypeError):
                pass
        if record.get('run') and record.get('state') == 'running':
            try:
                path=local_runtime_path(record['run'], root=self.root).resolve()
                if not path.is_relative_to(self.root/'runtime'):
                    raise ValueError('outside_runtime')
                progress=json.loads((path/'collection/cache/progress.json').read_text())
                result['progress']={k:progress.get(k) for k in ('requested','completed','received','failed')}
            except (OSError,ValueError,KeyError):
                pass
        return result
