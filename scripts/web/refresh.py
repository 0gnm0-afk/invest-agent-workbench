"""Coordinate explicit market retries and publish complete dashboard views."""
import threading
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from web.data import market_update


class RetryConflict(ValueError):
    pass


class DashboardDaily:
    def __init__(self, daily, views, builder, base_run, *, saved_accounts=True, now=None, observe_only=False, baseline_record=None):
        self.daily, self.views, self.builder = daily, views, builder
        self.display_run = base_run
        # Kept as a caller compatibility argument only. The web never requests
        # account collection, including when reopening a running server.
        self.daily.accounts = False
        self.observe_only = observe_only
        self.clock = now or (lambda: datetime.now(timezone.utc))
        self.lock = threading.RLock()
        self.thread = None
        self.active = False
        self.ready = False
        self.record = {'day': self.day(), 'state': 'saved' if observe_only else 'running',
                       'stage': 'finished' if observe_only else 'starting', 'targets': {}}
        self.seen = self.signature(baseline_record) if baseline_record is not None else None
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
                self._start(initial=not self.observe_only, retry=retry)

    def launch(self, *, daily_refresh, retry_market, saved_accounts=True):
        """Reopening an existing server uses the same once-per-day gate."""
        with self.lock:
            if daily_refresh and not self.active:
                self.observe_only = False
                self.daily.accounts = False
                self._start(initial=True, retry=retry_market)
            return self.status()

    def _prepare(self, *, initial=False, retry=False, expected=None):
        try:
            now = self.clock()
            record = (self.daily.once(now, retry=retry, expected_attempt=expected) if initial or retry
                      else self.daily.read(self.day()) or self.record)
            while record.get('following') or record.get('state') == 'running':
                with self.lock:
                    self.record = record
                time.sleep(.5)
                record = (self.daily.once(now) if initial or retry else
                          self.daily.read(record['day']) or {**record, 'state': 'unavailable', 'following': False})
            with self.lock:
                self.record = record
            # A later saved recalculation is valid display evidence, not a successful retry.
            display = self.daily.display_run() or record.get('display_run')
            update = market_update(self.daily.public({**record, 'display_run': display}))
            # Re-read saved observations on explicit opening/publication, even
            # if the market run did not change. This does not collect accounts.
            _, data, stocks = self.builder(refresh=False, market_run=display, daily_update=update)
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
            display = self.daily.display_run()
            if not self.active and ((persisted and self.signature(persisted) != self.seen)
                                    or (display and display != self.display_run and not self.view_error)):
                # Follow another dashboard's receipt; this path never requests a retry.
                self._start()
            record = persisted if self.active and persisted else self.record
            if record.get('state') == 'saved':
                record = {**record, 'run': self.display_run}
            value = market_update(self.daily.public({**record, 'display_run': self.display_run}))
            if self.view_error:
                value.update(state='failed', error=self.view_error)
            value.update(enabled=True, observe_only=self.observe_only, view_ready=self.ready, revision=self.revision, reused=self.record.get('reused', False),
                         retry_available=not self.observe_only and self.ready and value['day'] == self.day() and value['state'] in ('failed', 'interrupted', 'partial'))
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
