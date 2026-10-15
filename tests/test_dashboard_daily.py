"""Retry receipt, publication, and loopback HTTP boundaries (temporary data only)."""
import hashlib
import http.client
import json
import multiprocessing
import sys
import tempfile
import threading
import unittest
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from daily_launch import DailyLaunch, attempt_id
from dashboard_daily import DashboardDaily, RetryConflict
from dashboard import handler_for, ThreadingHTTPServer

NOW = datetime(2026, 10, 4, 5, tzinfo=timezone.utc)
DAY = '2026-10-04'
TARGETS = {'KR': '2026-10-02', 'US': '2026-10-02'}


def plan(now):
    return {'day': DAY, 'cutoff': '2026-10-03', 'evaluated_at': now.isoformat(), 'targets': TARGETS}


def saved_run(root, name, state='succeeded'):
    root = Path(root)
    folder = root / 'runtime' / name
    folder.mkdir(parents=True, exist_ok=True)
    result = {'output': str(folder), 'state': state, 'market_source': str(folder / 'source'),
              'stages': {'report': 'completed'}}
    hashes = {}
    for filename, value in {'run.json': result, 'priority-review.json': {'session_dates': TARGETS}}.items():
        body = json.dumps(value).encode('utf-8')
        (folder / filename).write_bytes(body)
        hashes[filename] = hashlib.sha256(body).hexdigest()
    (folder / 'manifest.json').write_text(json.dumps({'output_hashes': hashes}), encoding='utf-8')
    (root / 'runtime/latest.json').write_text(json.dumps({'output': str(folder)}), encoding='utf-8')
    return result


def process_retry(root, expected, entered, release, result_queue):
    """Spawn-safe real lock owner; its runner can only write synthetic test data."""
    def runner(_plan, _progress):
        (Path(root) / 'executed.txt').write_text('once', encoding='utf-8')
        entered.set()
        if not release.wait(10):
            raise RuntimeError('test_release_timeout')
        return saved_run(root, 'process-result')
    gate = DailyLaunch(root, runner=runner, planner=plan)
    result_queue.put(gate.once(NOW, retry=True, expected_attempt=expected))


class RetryFixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.calls, self.builds = [], []
        self.old = saved_run(self.root, 'old')['output']
        self.gate = DailyLaunch(self.root, self.runner, plan)
        self.seed = {'day': DAY, 'state': 'failed', 'stage': 'finished', 'targets': TARGETS,
                     'attempt_id': 'failed-attempt', 'started_at': NOW.isoformat(),
                     'finished_at': NOW.isoformat(), 'display_run': self.old, 'run': None,
                     'error': 'daily_update_unavailable'}
        self.gate.save(self.seed)
        self.views = {'view': ({'generation': 'old', 'market': {'state': 'succeeded'}}, {'US:OLD': {'bars': [1]}})}
        self.controllers = []
        self.release_events = []

    def tearDown(self):
        for event in self.release_events:
            event.set()
        for controller in self.controllers:
            if controller.thread:
                controller.thread.join(5)
        self.temp.cleanup()

    def runner(self, args, progress):
        self.calls.append(args)
        progress({'stage': 'stock_screen'})
        return saved_run(self.root, 'result-' + str(len(self.calls)))

    def builder(self, **kwargs):
        self.builds.append(kwargs)
        return self.root / 'view', {'generation': kwargs['market_run'], 'market': {'state': 'succeeded'},
                                   'daily_update': kwargs['daily_update']}, {'US:NEW': {'bars': [2]}}

    def controller(self, **kwargs):
        controller = DashboardDaily(self.gate, self.views, kwargs.pop('builder', self.builder), self.old,
                                    now=lambda: NOW, **kwargs)
        self.controllers.append(controller)
        controller.start()
        self.wait(controller)
        return controller

    def wait(self, controller):
        controller.thread.join(5)
        self.assertFalse(controller.thread.is_alive(), 'test worker did not finish')


class DashboardRetry(RetryFixture):
    def test_reopen_upgrades_saved_server_and_preserves_explicit_retry(self):
        controller = self.controller(observe_only=True)
        controller.launch(daily_refresh=True, retry_market=False, saved_accounts=True)
        self.wait(controller)
        self.assertEqual(self.calls, [])  # Reopening does not retry a failed day.
        self.assertFalse(controller.observe_only)
        self.assertFalse(self.gate.accounts)
        controller.launch(daily_refresh=True, retry_market=True, saved_accounts=True)
        self.wait(controller)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(controller.views['view'][1], {'US:NEW': {'bars': [2]}})


    def test_saved_market_reopen_can_refresh_accounts_without_market_collection(self):
        controller = self.controller(observe_only=True)
        controller.launch(daily_refresh=False, retry_market=False, saved_accounts=False)
        self.wait(controller)
        self.assertEqual(self.calls, [])
        self.assertEqual(len(self.builds), 1)
        self.assertTrue(self.builds[0]['refresh'])

    def test_loaded_account_receipt_does_not_rebuild_the_same_market_at_startup(self):
        self.seed.update(account_attempted=True,account_state='received')
        self.gate.save(self.seed)
        controller = self.controller(observe_only=True,baseline_record=self.seed)
        self.assertEqual(self.builds, [])
        self.gate.save({**self.seed,'attempt_id':'new-account-receipt'})
        controller.status()
        self.wait(controller)
        self.assertEqual(len(self.builds), 1)
        self.assertFalse(self.builds[0]['refresh'])
        self.assertEqual(self.calls, [])

    def test_account_failure_alone_offers_explicit_retry(self):
        self.seed.update(state='succeeded',account_attempted=True,account_state='failed')
        self.gate.save(self.seed)
        controller=self.controller()
        self.assertTrue(controller.status()['retry_available'])
        controller.retry('failed-attempt');self.wait(controller)
        self.assertEqual(len(self.calls),1)


    def test_completed_cycle_reuses_account_refresh_without_second_query(self):
        self.seed.update(account_attempted=True,account_state='received')
        self.gate.save(self.seed)
        self.views['view'][0]['accounts']={'stale':True}
        self.controller(saved_accounts=False)
        self.assertEqual(self.builds,[])


    def test_initial_open_refreshes_stale_accounts_even_when_scheduled_market_reused(self):
        self.views['view'][0]['accounts'] = {'stale':True}
        self.controller(saved_accounts=False)
        self.assertEqual(len(self.builds),1)
        self.assertTrue(self.builds[0]['refresh'])
        self.assertEqual(self.calls,[])

    def test_saved_accounts_remain_read_only_when_stale(self):
        self.views['view'][0]['accounts'] = {'stale':True}
        self.controller(saved_accounts=True)
        self.assertEqual(self.builds,[])


    def test_failed_receipt_is_reused_until_explicit_retry_then_new_view_published(self):
        controller = self.controller(saved_accounts=False)
        first = controller.status()
        self.assertEqual(self.calls, [])
        self.assertEqual(self.builds, [])
        self.assertTrue(first['retry_available'])
        controller.retry(first['attempt_id'])
        self.wait(controller)
        status = controller.status()
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(len(self.builds), 1)
        self.assertFalse(self.builds[0]['refresh'], 'manual retry must never query accounts')
        self.assertEqual(status['state'], 'succeeded')
        self.assertTrue(status['view_ready'])
        self.assertFalse(status['retry_available'])
        self.assertNotEqual(status['attempt_id'], first['attempt_id'])
        self.assertEqual(self.views['view'][0]['generation'], self.gate.read(DAY)['display_run'])
        self.assertIn('US:NEW', self.views['view'][1])
        self.assertEqual(len(list((self.gate.folder / 'attempts').glob('*.json'))), 1)
        self.assertNotIn(str(self.root), json.dumps(status))

    def test_double_click_does_not_start_second_worker(self):
        controller = self.controller()
        entered, release = threading.Event(), threading.Event()
        self.release_events.append(release)
        def slow(args, progress):
            entered.set()
            if not release.wait(5):
                raise RuntimeError('test_release_timeout')
            return self.runner(args, progress)
        self.gate.runner = slow
        controller.retry('failed-attempt')
        self.assertTrue(entered.wait(5))
        worker = controller.thread
        duplicate = controller.retry('failed-attempt')
        self.assertIs(controller.thread, worker)
        self.assertFalse(duplicate['retry_available'])
        self.assertFalse(duplicate['view_ready'])
        release.set()
        self.wait(controller)
        self.assertEqual(len(self.calls), 1)
        with self.assertRaises(RetryConflict):
            controller.retry('failed-attempt')

    def test_stale_attempt_does_not_run_market(self):
        controller = self.controller()
        with self.assertRaises(RetryConflict):
            controller.retry('yesterday-or-other-attempt')
        self.assertEqual(self.calls, [])
        self.assertEqual(self.gate.read(DAY), self.seed)

    def test_midnight_disables_previous_day_retry_without_automatic_collection(self):
        controller = self.controller()
        previous_thread = controller.thread
        self.assertTrue(controller.status()['retry_available'])
        controller.clock = lambda: datetime(2026, 10, 4, 15, tzinfo=timezone.utc)
        for _ in range(3):
            status = controller.status()
            self.assertEqual(status['day'], DAY)
            self.assertTrue(status['view_ready'])
            self.assertFalse(status['retry_available'])
        with self.assertRaises(RetryConflict):
            controller.retry('failed-attempt')
        self.assertIs(controller.thread, previous_thread)
        self.assertIsNone(self.gate.read('2026-10-05'))
        self.assertEqual(self.calls, [])
        self.assertEqual(self.builds, [])

    def test_legacy_failed_receipt_can_retry_using_started_at(self):
        del self.seed['attempt_id']
        self.gate.save(self.seed)
        controller = self.controller()
        self.assertEqual(controller.status()['attempt_id'], NOW.isoformat())
        controller.retry(NOW.isoformat())
        self.wait(controller)
        self.assertEqual(len(self.calls), 1)
        self.assertNotEqual(controller.status()['attempt_id'], NOW.isoformat())

    def test_partial_receipt_allows_explicit_retry(self):
        self.seed.update(state='partial', error=None)
        self.gate.save(self.seed)
        controller = self.controller()
        self.assertTrue(controller.status()['retry_available'])
        controller.retry('failed-attempt')
        self.wait(controller)
        self.assertEqual(len(self.calls), 1)

    def test_external_process_receipt_refreshes_view_without_another_collection(self):
        controller = self.controller()
        other = DailyLaunch(self.root, self.runner, plan)
        result = other.once(NOW, retry=True, expected_attempt='failed-attempt')
        self.assertEqual(result['state'], 'succeeded')
        controller.status()
        self.wait(controller)
        self.assertEqual(controller.status()['state'], 'succeeded')
        self.assertEqual(self.views['view'][0]['generation'], result['display_run'])
        self.assertEqual(len(self.calls), 1)
        self.assertFalse(self.builds[0]['refresh'])

    def test_new_saved_result_after_midnight_without_receipt_does_not_collect(self):
        controller=self.controller()
        controller.clock=lambda: datetime(2026,10,4,15,tzinfo=timezone.utc)
        result=saved_run(self.root,'new-direct-daily')
        controller.status();self.wait(controller)
        self.assertEqual(self.views['view'][0]['generation'],result['output'])
        self.assertEqual(self.calls,[])
        self.assertFalse(self.builds[0]['refresh'])
        self.assertIsNone(self.gate.read('2026-10-05'))

    def test_saved_market_observer_publishes_without_collection_or_account_query(self):
        controller=DashboardDaily(self.gate,self.views,self.builder,self.old,now=lambda:NOW,observe_only=True)
        self.controllers.append(controller);controller.start();self.wait(controller)
        result=saved_run(self.root,'observer-result')
        controller.status();self.wait(controller)
        self.assertEqual(self.views['view'][0]['generation'],result['output'])
        self.assertFalse(controller.status()['retry_available'])
        self.assertTrue(controller.status()['observe_only'])
        self.assertEqual(self.calls,[])
        self.assertFalse(self.builds[0]['refresh'])

    def test_failed_collection_preserves_saved_view_and_sanitized_error(self):
        controller = self.controller()
        previous = self.views['view']
        def fail(_plan, _progress):
            raise RuntimeError('SECRET_TOKEN_AND_PRIVATE_PATH')
        self.gate.runner = fail
        controller.retry('failed-attempt')
        self.wait(controller)
        status = controller.status()
        self.assertEqual(status['state'], 'failed')
        self.assertTrue(status['retry_available'])
        self.assertEqual(self.views['view'][0]['generation'], previous[0]['generation'])
        self.assertIs(self.views['view'][1], previous[1])
        self.assertEqual(self.builds, [])
        self.assertNotIn('SECRET', json.dumps(status))

    def test_failed_view_build_retries_publication_without_recollecting_or_accounts(self):
        should_fail = [True]
        def builder(**kwargs):
            if should_fail[0]:
                raise OSError('SECRET_PRIVATE_PATH')
            return self.builder(**kwargs)
        controller = self.controller(builder=builder, saved_accounts=False)
        previous_revision = controller.status()['revision']
        before = self.views['view']
        controller.retry('failed-attempt')
        self.wait(controller)
        status = controller.status()
        self.assertEqual(status['error'], 'dashboard_preparation_failed')
        self.assertEqual(status['revision'], previous_revision)
        self.assertEqual(status['state'], 'failed')
        self.assertTrue(status['retry_available'])
        self.assertIs(self.views['view'], before)
        self.assertEqual(self.gate.read(DAY)['state'], 'succeeded')
        self.assertNotIn('SECRET', json.dumps(status))
        should_fail[0] = False
        controller.retry(status['attempt_id'])
        self.wait(controller)
        refreshed = controller.status()
        self.assertEqual(refreshed['state'], 'succeeded')
        self.assertEqual(refreshed['attempt_id'], status['attempt_id'])
        self.assertEqual(refreshed['revision'], previous_revision + 1)
        self.assertEqual(len(self.calls), 1)
        self.assertFalse(self.builds[0]['refresh'])
        self.assertIn('US:NEW', self.views['view'][1])

    def test_unavailable_built_market_is_not_published_as_success(self):
        controller = self.controller(builder=lambda **kwargs: (self.root, {'market': {'state': 'unavailable'}}, {}))
        before = self.views['view']
        controller.retry('failed-attempt')
        self.wait(controller)
        self.assertEqual(controller.status()['error'], 'dashboard_preparation_failed')
        self.assertIs(self.views['view'], before)

    def test_previous_failed_attempt_cannot_retry_a_later_failure_with_same_timestamp(self):
        def failed(args, _progress):
            self.calls.append(args)
            raise OSError('temporary_failure')
        self.gate.runner = failed
        first = self.gate.once(NOW, retry=True, expected_attempt='failed-attempt')
        self.assertEqual(first['state'], 'failed')
        self.assertEqual(len(self.calls), 1)
        delayed = self.gate.once(NOW, retry=True, expected_attempt='failed-attempt')
        self.assertTrue(delayed['reused'])
        self.assertEqual(attempt_id(delayed), attempt_id(first))
        self.assertEqual(len(self.calls), 1)
        second = self.gate.once(NOW, retry=True, expected_attempt=attempt_id(first))
        self.assertEqual(second['state'], 'failed')
        self.assertEqual(len(self.calls), 2)
        self.assertNotEqual(attempt_id(first), attempt_id(second))
        self.assertEqual(first['started_at'], second['started_at'])

    def test_guarded_retry_cannot_start_for_missing_or_successful_receipt(self):
        for state in ('succeeded', 'unavailable'):
            with self.subTest(state=state):
                self.seed['state'] = state
                self.gate.save(self.seed)
                result = self.gate.once(NOW, retry=True, expected_attempt='failed-attempt')
                self.assertTrue(result['reused'])
                self.assertEqual(self.gate.read(DAY), self.seed)
        (self.gate.folder / (DAY + '.json')).unlink()
        result = self.gate.once(NOW, retry=True, expected_attempt='failed-attempt')
        self.assertTrue(result['reused'])
        self.assertEqual(result['state'], 'unavailable')
        self.assertIsNone(self.gate.read(DAY))
        self.assertEqual(self.calls, [])

    def test_cross_process_owner_and_delayed_stale_request_cannot_repeat_collection(self):
        controller = self.controller()
        ctx = multiprocessing.get_context('spawn')
        entered, release, result_queue = ctx.Event(), ctx.Event(), ctx.Queue()
        child = ctx.Process(target=process_retry, args=(str(self.root), 'failed-attempt', entered, release, result_queue))
        child.start()
        try:
            self.assertTrue(entered.wait(10))
            pending = controller.status()
            self.assertFalse(pending['view_ready'])
            self.assertFalse(pending['retry_available'])
            following = self.gate.once(NOW, retry=True, expected_attempt='failed-attempt')
            self.assertTrue(following['following'])
            self.assertEqual(self.calls, [])
            release.set()
            child.join(10)
            self.assertEqual(child.exitcode, 0)
            completed = result_queue.get(timeout=5)
            self.wait(controller)
            self.assertEqual(controller.status()['state'], 'succeeded')
            self.assertEqual(self.views['view'][0]['generation'], completed['display_run'])
            stale = self.gate.once(NOW, retry=True, expected_attempt='failed-attempt')
            self.assertTrue(stale['reused'])
            self.assertEqual(attempt_id(stale), attempt_id(completed))
            self.assertEqual(stale['state'], 'succeeded')
            self.assertEqual(self.calls, [])
            self.assertEqual(len(list((self.gate.folder / 'attempts').glob('*.json'))), 1)
        finally:
            release.set()
            child.join(5)
            if child.is_alive():
                child.terminate()
                child.join(5)
            result_queue.close()
            result_queue.join_thread()


class RetryHTTP(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.calls = []
        self.conflict = False
        def retry(value):
            self.calls.append(value)
            if self.conflict:
                raise RetryConflict('daily_attempt_changed')
            return {'enabled': True, 'state': 'running', 'view_ready': False, 'attempt_id': value}
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), handler_for(
            {}, {}, self.root, 'test-token', self.root / 'workspace.sqlite3',
            daily_status=lambda: {'enabled': True, 'state': 'failed'}, daily_retry=retry,
            launch=lambda **flags: self.calls.append(flags) or {'enabled':True}))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.headers = {'Origin': 'http://127.0.0.1:' + str(self.server.server_port),
                        'X-Workbench-Token': 'test-token', 'Content-Type': 'application/json'}

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(5)
        self.temp.cleanup()

    def post(self, value=None, *, raw=None, path='/api/daily-update', headers=None):
        conn = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=5)
        try:
            body = raw if raw is not None else json.dumps(value or {'retry': True, 'attempt_id': 'failed-attempt'})
            conn.request('POST', path, body=body, headers=self.headers if headers is None else headers)
            response = conn.getresponse()
            return response.status, json.loads(response.read())
        finally:
            conn.close()

    def test_reopen_route_requires_origin_token_and_exact_boolean_flags(self):
        flags = dict(daily_refresh=True, retry_market=False, saved_accounts=True)
        self.assertEqual(self.post(flags, path='/api/launch', headers={**self.headers,'X-Workbench-Token':'wrong'})[0], 403)
        self.assertEqual(self.post(flags, path='/api/launch', headers={**self.headers,'Origin':'http://other'})[0], 403)
        self.assertEqual(self.post({**flags,'command':'anything'}, path='/api/launch')[0], 400)
        self.assertEqual(self.post({**flags,'saved_accounts':1}, path='/api/launch')[0], 400)
        self.assertEqual(self.calls, [])
        self.assertEqual(self.post(flags, path='/api/launch')[0], 202)
        self.assertEqual(self.calls, [flags])

    def test_exact_retry_request_is_accepted_and_stale_request_conflicts(self):
        status, body = self.post()
        self.assertEqual(status, 202)
        self.assertEqual(body['state'], 'running')
        self.assertEqual(self.calls, ['failed-attempt'])
        self.conflict = True
        self.assertEqual(self.post()[0], 409)

    def test_origin_host_and_token_each_required(self):
        variants = [{**self.headers, 'Origin': 'https://attacker.example'},
                    {key: value for key, value in self.headers.items() if key != 'Origin'},
                    {**self.headers, 'X-Workbench-Token': 'wrong'},
                    {key: value for key, value in self.headers.items() if key != 'X-Workbench-Token'},
                    {**self.headers, 'Host': 'attacker.example'}]
        for headers in variants:
            with self.subTest(headers=headers):
                self.assertEqual(self.post(headers=headers)[0], 403)
        self.assertEqual(self.calls, [])

    def test_json_body_is_narrow_and_cannot_pass_runner_or_account_options(self):
        bodies = ['[]', 'null', '{}', '{broken',
                  json.dumps({'retry': False, 'attempt_id': 'a'}),
                  json.dumps({'retry': 1, 'attempt_id': 'a'}),
                  json.dumps({'retry': True}),
                  json.dumps({'retry': True, 'attempt_id': ''}),
                  json.dumps({'retry': True, 'attempt_id': 1}),
                  json.dumps({'retry': True, 'attempt_id': 'x' * 129}),
                  json.dumps({'retry': True, 'attempt_id': 'a', 'refresh_accounts': True}),
                  json.dumps({'retry': True, 'attempt_id': 'a', 'command': 'anything'})]
        for body in bodies:
            with self.subTest(body=body):
                status, payload = self.post(raw=body)
                self.assertEqual(status, 400)
                self.assertEqual(payload['error'], 'invalid_daily_request')
        for query in ['?command=anything', '?timeframe=1d', '?retry=true']:
            with self.subTest(query=query):
                self.assertEqual(self.post(path='/api/daily-update' + query)[0], 400)
        self.assertEqual(self.calls, [])

    def test_content_type_and_body_limits(self):
        self.assertEqual(self.post(headers={**self.headers, 'Content-Type': 'text/plain'})[0], 415)
        self.assertEqual(self.post(raw='')[0], 413)
        self.assertEqual(self.post(raw='x', headers={**self.headers, 'Content-Length': '131073'})[0], 413)
        self.assertEqual(self.calls, [])

    def test_get_status_does_not_invoke_retry(self):
        conn = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=5)
        try:
            conn.request('GET', '/api/daily-update')
            response = conn.getresponse()
            self.assertEqual(response.status, 200)
            self.assertEqual(json.loads(response.read())['state'], 'failed')
        finally:
            conn.close()
        self.assertEqual(self.calls, [])


if __name__ == '__main__':
    unittest.main()
