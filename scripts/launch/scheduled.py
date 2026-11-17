"""Finite, headless 06:40 KST market job using saved account observations."""
import argparse
from datetime import datetime, timezone
import getpass
import json
from pathlib import Path
import sys
import time
from uuid import uuid4

from launch.daily import DailyLaunch, plan_for
from atomic_io import atomic_write
from launch.journal import RunJournal, error_frames

from workbench_paths import ROOT


def run_scheduled(daily, now=None, sleep=time.sleep):
    now = now or datetime.now(timezone.utc)
    record = daily.once(now, scheduled=True)
    while record.get('following'):
        sleep(5)
        record = daily.once(now, scheduled=True)
    return record


def exit_code(record):
    return 0 if record['state'] in ('succeeded', 'not_due', 'check_passed') else 2 if record['state'] == 'partial' else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check', action='store_true', help='Offline environment/calendar/write check; never collects')
    args = parser.parse_args()
    now = datetime.now(timezone.utc)
    folder = ROOT/'runtime/morning-schedule'
    kind = 'check' if args.check else 'run'
    result = {'kind':kind, 'started_at':now.isoformat(), 'user':getpass.getuser(), 'python':sys.executable}
    try:
        if args.check:
            # Imported worker dependencies and calendar are checked without market access.
            import market.daily as daily_market
            config = json.loads((ROOT/'config.json').read_text(encoding='utf-8'))
            if not Path(config['engine']).is_dir() or not Path(config['mother']).is_dir():
                raise ValueError('configured_root_missing')
            result.update(state='check_passed', plan=plan_for(now), network_calls=0)
        else:
            daily = DailyLaunch(ROOT, accounts=False)
            record = run_scheduled(daily, now)
            result.update(daily.public(record))
            result.update(state=record['state'], run=record.get('run'), scheduled_day=record.get('scheduled_day'))
    except Exception as exc:
        result.update(state='failed', error='scheduled_job_failed', error_type=type(exc).__name__, error_frames=error_frames(exc))
    result['finished_at'] = datetime.now(timezone.utc).isoformat()
    body = json.dumps(result, ensure_ascii=True, allow_nan=False)
    name = kind+'-'+now.strftime('%Y%m%dT%H%M%S')+'-'+uuid4().hex[:8]+'.json'
    atomic_write(folder/name, body)
    atomic_write(folder/('latest-'+kind+'.json'), body)
    print(body, flush=True)
    return exit_code(result)


def run_cli():
    with RunJournal(ROOT/'runtime/morning-schedule', 'check' if '--check' in sys.argv else 'run') as journal:
        code = main()
        journal.complete(code)
    return code
