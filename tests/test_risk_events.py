import sys
import tempfile
import unittest
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from risk_events import EventStore, FakeSender
from test_risk_portfolio import fixture


class Events(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.temp.name)/'events.sqlite3')
        self.store = EventStore(self.path)
        f = fixture()['scenario']['response']
        self.plan = {'id': 'p', 'version': 1, 'symbol': 'ALPHA', 'source_ref': 'fixture'}
        self.c = f['steps'][0]['condition']
        self.o = f['path'][0]['observations']['demo/US']
        self.o['evaluated_at'] = self.o['as_of']
        self.active = [('p', 1)]

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def observe(self):
        return self.store.observe(self.plan, self.c, self.o, active_refs=self.active)

    def next(self, suffix, price):
        self.o.update(snapshot_id=suffix, price=price)

    def test_repeat_resolution_recurrence_and_missing_latch(self):
        first = self.observe()
        self.assertEqual(first, self.observe())
        self.next('repeat', '89')
        self.assertIsNone(self.observe()['event_id'])
        self.next('missing', None)
        self.assertTrue(self.observe()['latched'])
        self.next('resolve', '91')
        self.assertEqual(self.observe()['event_kind'], 'resolved')
        self.next('recur', '88')
        self.assertEqual(self.observe()['event_kind'], 'recurred')
        self.assertEqual(len(self.store.events()), 3)

    def test_restart_and_two_connections_no_double_send(self):
        self.observe()
        self.store.close()
        self.store = EventStore(self.path)
        self.observe()
        sender = FakeSender()
        self.store.deliver(sender, active_refs=self.active, now=0)
        other = EventStore(self.path)
        try:
            self.assertIsNone(other.deliver(sender, active_refs=self.active, now=0))
        finally:
            other.close()
        self.assertEqual(len(sender.calls), 1)

    def test_retry_is_bounded_and_unknown_not_retried(self):
        self.observe()
        sender = FakeSender(['failed', 'unknown'])
        self.assertEqual(self.store.deliver(sender, active_refs=self.active, now=0)['status'], 'retry')
        self.assertIsNone(self.store.deliver(sender, active_refs=self.active, now=1))
        self.assertEqual(self.store.deliver(sender, active_refs=self.active, now=30)['status'], 'unknown')
        self.assertIsNone(self.store.deliver(sender, active_refs=self.active, now=100))

    def test_retry_exhausted(self):
        self.observe()
        sender = FakeSender(['failed']*3)
        for at in [0, 30, 60]:
            result = self.store.deliver(sender, active_refs=self.active, now=at)
        self.assertEqual(result['status'], 'failed')
        self.assertIsNone(self.store.deliver(sender, active_refs=self.active, now=100))

    def test_simultaneous_observation_and_dispatch(self):
        barrier = threading.Barrier(2)
        def worker():
            store = EventStore(self.path)
            try:
                barrier.wait(timeout=5)
                store.observe(self.plan, self.c, self.o, active_refs=self.active)
                return store.deliver(FakeSender(), active_refs=self.active, now=0)
            finally:
                store.close()
        with ThreadPoolExecutor(max_workers=2) as pool:
            a, b = pool.submit(worker), pool.submit(worker)
            results = [a.result(), b.result()]
        self.assertEqual(sum(r is not None for r in results), 1)
        self.assertEqual(len(self.store.events()), 1)
        self.assertEqual(self.store.events()[0]['attempts'], 1)

    def test_plan_replaced_and_draft(self):
        self.active = []
        self.assertEqual(self.observe()['state'], 'inactive_plan')
        self.assertEqual(self.store.events(), [])
        self.active = [('p', 1)]
        self.observe()
        self.active = [('p', 2)]
        sender = FakeSender()
        self.assertIsNone(self.store.deliver(sender, active_refs=self.active, now=0))
        self.assertEqual(self.store.events()[0]['status'], 'cancelled')
        self.assertEqual(sender.calls, [])

    def test_inflight_restart_becomes_unknown(self):
        self.observe()
        self.store.db.execute("UPDATE risk_events SET status='sending'")
        self.assertEqual(self.store.recover_inflight(), 1)
        self.assertEqual(self.store.events()[0]['status'], 'unknown')

    def test_snapshot_is_immutable_and_stale_cannot_resolve(self):
        self.observe()
        self.o['price'] = '100'
        with self.assertRaises(ValueError):
            self.observe()
        self.o.update(snapshot_id='old', as_of='2026-10-01T06:00:00+09:00')
        self.assertTrue(self.observe()['latched'])
        self.assertEqual(len(self.store.events()), 1)


if __name__ == '__main__':
    unittest.main()
