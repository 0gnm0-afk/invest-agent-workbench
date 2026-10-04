"""Coordinate explicit market retries and publish complete dashboard views."""
import threading
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from daily_launch import attempt_id


class RetryConflict(ValueError):
    pass


class DashboardDaily:
    def __init__(self, daily, views, builder, base_run, *, saved_accounts=True, now=None):
        self.daily, self.views, self.builder = daily, views, builder
        self.display_run = base_run
        self.saved_accounts = saved_accounts
        self.clock = now or (lambda: datetime.now(timezone.utc))
        self.lock = threading.RLock()
        self.thread = None
        self.active = False
        self.ready = False
        self.record = {'day': self.day(), 'state': 'running', 'stage': 'starting', 'targets': {}}
        self.seen = None
        self.revision = 0
        self.view_error = None

    def day(self):
        return self.clock().astimezone(ZoneInfo('Asia/Seoul')).date().isoformat()

    @staticmethod
    def signature(record):
        return tuple(record.get(k) for k in ('attempt_id', 'started_at', 'state', 'finished_at', 'display_run'))

    def _start(self, *, initial=False, retry=False, expected=None):
        self.active, self.ready, self.view_error = True, False, None
        self.thread = threading.Thread(target=self._prepare, kwargs=dict(initial=initial, retry=retry, expected=expected), daemon=True)
        self.thread.start()

    def start(self, retry=False):
        with self.lock:
            if not self.active:
                self._start(initial=True, retry=retry)

    def _prepare(self, *, initial=False, retry=False, expected=None):
        try:
            now = self.clock()
            record = self.daily.once(now, retry=retry, expected_attempt=expected)
            while record.get('following'):
                with self.lock:
                    self.record = record
                time.sleep(.5)
                record = self.daily.once(now)
            with self.lock:
                self.record = record
            # A later saved recalculation is valid display evidence, not a successful retry.
            display = self.daily.display_run() or record.get('display_run')
            update = self.daily.public({**record, 'display_run': display})
            refresh = initial and not retry and not self.saved_accounts and not record.get('reused')
            if display == self.display_run and not refresh:
                data, stocks = self.views['view']
                next_view = ({**data, 'daily_update': update}, stocks)
            else:
                _, data, stocks = self.builder(refresh=refresh, market_run=display, daily_update=update)
                if data.get('market', {}).get('state') == 'unavailable':
                    raise ValueError('dashboard_preparation_failed')
                next_view = (data, stocks)
            with self.lock:
                self.views['view'] = next_view
                self.revision += 1
                self.display_run = display
                self.seen = self.signature(record)
        except Exception:
            with self.lock:
                self.view_error = 'dashboard_preparation_failed'
                self.seen = self.signature(self.record)
        finally:
            with self.lock:
                self.active, self.ready = False, True

    def status(self):
        with self.lock:
            try:
                persisted = self.daily.read(self.day())
            except (OSError, ValueError):
                persisted = None
            if not self.active and persisted and self.signature(persisted) != self.seen:
                # Follow another dashboard's receipt; this path never requests a retry.
                self._start()
            record = persisted if self.active and persisted else self.record
            value = self.daily.public({**record, 'display_run': self.display_run})
            if self.view_error:
                value.update(state='failed', error=self.view_error)
            value.update(enabled=True, view_ready=self.ready, revision=self.revision, reused=self.record.get('reused', False),
                         retry_available=self.ready and value['day'] == self.day() and value['state'] in ('failed', 'interrupted', 'partial'))
            return value

    def retry(self, expected_attempt):
        with self.lock:
            current = self.status()
            if self.active:
                return current
            if not current['retry_available'] or expected_attempt != current['attempt_id']:
                raise RetryConflict('daily_attempt_changed')
            if self.view_error:
                # Market data already finished; retry only failed view publication.
                self._start()
            else:
                self._start(retry=True, expected=expected_attempt)
            return self.status()
