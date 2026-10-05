"""Durable condition events and delivery state. No Telegram/network implementation.

Caller supplies authoritative active plan references from the shared service.
This stores risk events only; it does not own or adopt any stock plan.
"""
import json
import sqlite3
from contextlib import contextmanager
from risk_values import fingerprint, instant
from risk_conditions import evaluate


class DefinitelyNotSent(Exception):
    """Transport guarantees no delivery occurred; retry can be bounded."""


class DeliveryUnknown(Exception):
    """The request may have been delivered. Never retry automatically."""


class FakeSender:
    def __init__(self, outcomes=()):
        self.outcomes = list(outcomes)
        self.calls = []

    def send(self, payload):
        self.calls.append(payload)
        outcome = self.outcomes.pop(0) if self.outcomes else 'sent'
        if outcome == 'failed':
            raise DefinitelyNotSent()
        if outcome == 'unknown':
            raise DeliveryUnknown()
        return 'fake-message-' + str(len(self.calls))


class EventStore:
    def __init__(self, path):
        self.db = sqlite3.connect(path, timeout=10, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.executescript('''
        CREATE TABLE IF NOT EXISTS risk_condition_state(
          key TEXT PRIMARY KEY, plan_id TEXT, version INTEGER, condition_id TEXT,
          latched INTEGER NOT NULL, occurrence INTEGER NOT NULL, observed_at TEXT, result TEXT);
        CREATE TABLE IF NOT EXISTS risk_observations(
          key TEXT, snapshot_id TEXT, digest TEXT, result TEXT,
          PRIMARY KEY(key,snapshot_id));
        CREATE TABLE IF NOT EXISTS risk_events(
          id TEXT PRIMARY KEY, key TEXT, plan_id TEXT, version INTEGER, condition_id TEXT,
          occurrence INTEGER, kind TEXT, payload TEXT, status TEXT, attempts INTEGER DEFAULT 0,
          retry_at REAL, message_ref TEXT);
        ''')

    def close(self):
        self.db.close()

    @contextmanager
    def transaction(self):
        self.db.execute('BEGIN IMMEDIATE')
        try:
            yield
            self.db.execute('COMMIT')
        except BaseException:
            self.db.execute('ROLLBACK')
            raise

    def observe(self, plan, condition, observation, *, active_refs):
        """A draft/hypothesis is never eligible for a monitoring event."""
        ref = (plan['id'], plan['version'])
        if ref not in set(active_refs):
            return {'state': 'inactive_plan', 'event_id': None, 'latched': None}
        key = fingerprint([*ref, condition.get('id')])
        snapshot = observation.get('snapshot_id')
        digest = fingerprint([condition, observation])
        if not snapshot:
            raise ValueError('observation_snapshot_required')
        with self.transaction():
            previous = self.db.execute('SELECT * FROM risk_condition_state WHERE key=?', (key,)).fetchone()
            seen = self.db.execute('SELECT * FROM risk_observations WHERE key=? AND snapshot_id=?', (key, snapshot)).fetchone()
            if seen:
                if seen['digest'] != digest:
                    raise ValueError('immutable_observation_changed')
                return json.loads(seen['result'])
            if previous and instant(observation['as_of']) < instant(previous['observed_at']):
                return {'state': 'out_of_order', 'event_id': None, 'latched': bool(previous['latched'])}
            result = evaluate(condition, observation)
            latched = bool(previous['latched']) if previous else False
            occurrence = previous['occurrence'] if previous else 0
            event_id, kind = None, None
            if result['matched'] is True and not latched:
                occurrence += 1
                kind = 'triggered' if occurrence == 1 else 'recurred'
                latched = True
            elif result['matched'] is False and latched:
                kind, latched = 'resolved', False
            if kind:
                event_id = fingerprint([key, occurrence, kind])
                payload = {'plan_id': plan['id'], 'version': plan['version'], 'condition_id': condition['id'],
                           'symbol': plan.get('symbol'), 'kind': kind, 'as_of': observation['as_of'],
                           'snapshot_id': snapshot, 'source_ref': plan.get('source_ref')}
                self.db.execute('INSERT INTO risk_events(id,key,plan_id,version,condition_id,occurrence,kind,payload,status) VALUES(?,?,?,?,?,?,?,?,?)',
                                (event_id,key,*ref,condition['id'],occurrence,kind,json.dumps(payload),
                                 'recorded' if kind == 'resolved' else 'pending'))
                if kind == 'resolved':
                    self.db.execute("UPDATE risk_events SET status='cancelled' WHERE key=? AND status IN ('pending','retry')", (key,))
            result.update(event_id=event_id, event_kind=kind, latched=latched, occurrence=occurrence)
            packed = json.dumps(result)
            self.db.execute('INSERT OR REPLACE INTO risk_condition_state VALUES(?,?,?,?,?,?,?,?)',
                            (key,*ref,condition['id'],int(latched),occurrence,observation['as_of'],packed))
            self.db.execute('INSERT INTO risk_observations VALUES(?,?,?,?)', (key,snapshot,digest,packed))
            return result

    def recover_inflight(self):
        """Call once after a stopped process; a crash is not proof of no delivery."""
        with self.transaction():
            return self.db.execute("UPDATE risk_events SET status='unknown' WHERE status='sending'").rowcount

    def deliver(self, sender, *, active_refs, now, max_attempts=3, retry_delay=30):
        if max_attempts < 1 or retry_delay < 0:
            raise ValueError('invalid_retry_policy')
        active = set(active_refs)
        with self.transaction():
            for row in self.db.execute("SELECT * FROM risk_events WHERE status IN ('pending','retry')").fetchall():
                if (row['plan_id'], row['version']) not in active:
                    self.db.execute("UPDATE risk_events SET status='cancelled' WHERE id=?", (row['id'],))
            row = self.db.execute("SELECT * FROM risk_events WHERE status IN ('pending','retry') AND (retry_at IS NULL OR retry_at<=?) ORDER BY rowid LIMIT 1", (now,)).fetchone()
            if not row:
                return None
            self.db.execute("UPDATE risk_events SET status='sending',attempts=attempts+1 WHERE id=?", (row['id'],))
        message = None
        try:
            message = sender.send(json.loads(row['payload']))
            if not isinstance(message, str) or not message:
                raise DeliveryUnknown()
            status = 'sent'
        except DefinitelyNotSent:
            status = 'retry' if row['attempts']+1 < max_attempts else 'failed'
        except Exception:
            status = 'unknown'
        with self.transaction():
            self.db.execute('UPDATE risk_events SET status=?,retry_at=?,message_ref=? WHERE id=?',
                            (status, now+retry_delay if status == 'retry' else None, message, row['id']))
        return {'event_id': row['id'], 'status': status, 'message_ref': message}

    def events(self):
        return [dict(r) for r in self.db.execute('SELECT * FROM risk_events ORDER BY rowid')]
