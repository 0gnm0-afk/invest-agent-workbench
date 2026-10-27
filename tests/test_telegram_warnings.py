from contextlib import closing
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'scripts'))
from alerts import delivery as telegram
from alerts.trailing import POLICY, TrailingStore, warning_id


class TelegramWarnings(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.store = TrailingStore(self.root/'runtime/trailing-warnings.sqlite3')
        self.now = datetime(2026,10,6,1,tzinfo=timezone.utc)
        clock = patch('alerts.trailing.datetime', wraps=datetime)
        self.clock = clock.start()
        self.addCleanup(clock.stop)
        self.clock.now.return_value = self.now
        self.config = dict(token='fake', chat='123', enabled_at='2026-10-06T00:00:00+00:00')
        self.event = dict(id='warning1', key='ACCOUNT_PRIVATE|KR|005930', account='ACCOUNT_PRIVATE', market='KR', symbol='005930',
            name='합성종목', currency='KRW', date='2026-10-05', created_at='2026-10-06T01:00:00+00:00',
            kind='trailing', policy='high95-approach-v2', alert_threshold=266150, tolerance=3000, close=263000, threshold=263150, line=263150, high=277000, atr=6000, ma5=266000,
            average_cost=212345, reason='종가가 최종 트레일링 경고선에 접촉·이탈')
        self.sent = []

    def add(self, **changes):
        e = {**self.event, **changes}
        e['id'] = warning_id(e['key'], 1, e['date'], e['kind'])
        position = {k:e[k] for k in ('key','account','market','symbol','currency')}
        position.update(active=True, status='warning', issue=None, generation=1, policy=POLICY,
                        last_date=self.event['date'], account_as_of=self.now.isoformat(),
                        approaching={kind:True for kind in ('trailing','ma5','ma10')})
        with closing(self.store.connect()) as db, db:
            db.execute('INSERT OR REPLACE INTO warnings VALUES(?,?)',(e['id'],json.dumps(e)))
            db.execute('INSERT OR REPLACE INTO monitors VALUES(?,?)',(position['key'],json.dumps(position)))

    def send(self, text):
        self.sent.append(text)
        return {'message_id':len(self.sent)}

    def dispatch(self, sender=None, **kw):
        return telegram.dispatch(self.root, {'KR':'2026-10-05','US':'2026-10-05'}, config=self.config, sender=sender or self.send, **kw)

    def test_disabled_without_private_config_even_if_env_exists(self):
        self.add()
        with patch.dict('os.environ', {'TELEGRAM_BOT_TOKEN':'123:secret','TELEGRAM_CHAT_ID':'123'}):
            self.assertEqual(telegram.dispatch(self.root)['state'],'disabled')

    def test_separate_warnings_and_no_duplicate_after_restart(self):
        self.add(); self.add(id='warning2',kind='ma5',threshold=266000)
        self.assertEqual(self.dispatch()['counts'],{'sent':2})
        self.dispatch()
        self.assertEqual(len(self.sent),2)
        self.assertIn('263,150원',self.sent[0])
        self.assertIn('5일 이동평균선',self.sent[1])
        self.assertNotIn('ACCOUNT_PRIVATE','\n'.join(self.sent))
        self.assertNotIn('212,345','\n'.join(self.sent))

    def test_pre_activation_and_old_session_not_sent(self):
        self.add(created_at='2026-10-05T01:00:00+00:00')
        self.add(id='old',date='2026-10-02')
        self.assertEqual(self.dispatch()['counts'],{'expired':1})
        self.assertFalse(self.sent)

    def test_known_rejection_retry_capped_at_three(self):
        self.add()
        calls=[]
        def reject(text):
            calls.append(text)
            raise telegram.DeliveryError('failed','telegram_http_429')
        for _ in range(5):self.dispatch(reject)
        self.assertEqual(len(calls),3)
        self.assertEqual(self.dispatch()['counts'],{'failed':1})
        self.assertFalse(self.sent)

    def test_unknown_response_not_resent(self):
        self.add()
        def timeout(text):raise TimeoutError('credential_must_not_leak')
        self.assertEqual(self.dispatch(timeout)['counts'],{'unknown':1})
        self.dispatch()
        self.assertFalse(self.sent)
        self.assertNotIn('credential_must_not_leak',(self.root/'runtime/telegram-delivery/latest.json').read_text())

    def test_crashed_inflight_delivery_not_resent(self):
        self.add();self.dispatch()
        with closing(sqlite3.connect(self.root/'runtime/telegram-delivery/delivery.sqlite3')) as db, db:
            db.execute("UPDATE deliveries SET state='sending'")
        self.assertEqual(self.dispatch()['counts'],{'unknown':1})
        self.assertEqual(len(self.sent),1)

    def test_usd_format_and_no_raw_account_data(self):
        body=telegram.message({**self.event,'currency':'USD','close':120.1,'threshold':125.2})
        self.assertIn('120.10 USD',body)
        self.assertIn('-4.07%',body)

    def test_three_approach_messages_and_persistent_dedup(self):
        for kind,threshold in [('trailing',263150),('ma5',266000),('ma10',267000)]:
            self.add(id='new-'+kind,kind=kind,policy='high95-approach-v2',close=268000,
                     threshold=threshold,alert_threshold=threshold+3000,tolerance=3000,ma10=267000,
                     reason='0.5 ATR 접근')
        self.assertEqual(self.dispatch()['counts'],{'sent':3})
        self.assertTrue(all('0.5 ATR' in body and '알림 기준:' in body for body in self.sent))
        self.assertIn('10일 이동평균선',self.sent[2]);self.assertIn('H × 95%',self.sent[0])
        self.assertNotIn('ACCOUNT_PRIVATE',''.join(self.sent));self.assertNotIn('212,345',''.join(self.sent))
        self.dispatch();self.assertEqual(len(self.sent),3)
    def test_approach_gap_below_message(self):
        body=telegram.message({**self.event,'policy':'high95-approach-v2','kind':'trailing',
                              'alert_threshold':266150,'tolerance':3000})
        self.assertIn('접촉·이탈',body);self.assertNotIn('2.5 ×',body)

    def test_legacy_warning_preserved_but_not_dispatched(self):
        self.add(policy=None)
        self.dispatch()
        self.assertFalse(self.sent)
        self.assertEqual(len(self.store.read()['events']),1)

    def calculated_events(self):
        bundle={'as_of':self.now.isoformat(),'query_state':'received','accounts':[
            {'alias':'ACCOUNT_PRIVATE','holdings_complete':True,'positions':[
                {'market':'US','symbol':'S','currency':'USD','quantity':2,'average_cost':90}]}]}
        bars=[{'date':(datetime(2026,9,19)+timedelta(days=i)).date().isoformat(),
               'high':102,'low':98,'close':100} for i in range(15)]
        def calculate():
            return self.store.update(bundle,{'US:S':{'currency':'USD','bars':bars}},
                {'US':bars[-1]['date']},self.now,sessions={'US':[b['date'] for b in bars]})
        calculate()
        bars.append({'date':'2026-10-04','high':105,'low':103,'close':104});calculate()
        bars.append({'date':'2026-10-05','high':96,'low':94,'close':95})
        events=calculate()['events'];self.assertEqual({e['kind'] for e in events},{'trailing','ma5','ma10'})
        return bundle, calculate

    def test_calculated_three_events_reach_delivery_and_no_repeat(self):
        _, calculate = self.calculated_events()
        self.assertEqual(self.dispatch()['counts'],{'sent':3})
        calculate();self.dispatch();self.assertEqual(len(self.sent),3)
        self.assertTrue(all('0.5 ATR' in body for body in self.sent))
        self.assertNotIn('ACCOUNT_PRIVATE',''.join(self.sent))

    def failed_calculated_events(self):
        bundle, calculate = self.calculated_events()
        def reject(_): raise telegram.DeliveryError('failed','telegram_http_429')
        self.assertEqual(self.dispatch(reject)['counts'], {'failed':1})
        return bundle, calculate

    def test_closed_holding_blocks_failed_and_unsent_events(self):
        bundle, calculate = self.failed_calculated_events()
        bundle['accounts'][0]['positions'] = []
        before = calculate()
        self.assertEqual(before['positions'][0]['status'], 'closed')
        self.assertEqual(self.dispatch()['counts'], {'failed':1})
        self.assertFalse(self.sent)
        self.assertEqual(self.store.read(), before)
        with closing(sqlite3.connect(self.root/'runtime/telegram-delivery/delivery.sqlite3')) as db:
            self.assertEqual(db.execute('SELECT state,attempts FROM deliveries').fetchall(), [('failed',1)])

    def test_incomplete_holding_blocks_retry_until_confirmed_again(self):
        bundle, calculate = self.failed_calculated_events()
        bundle['query_state'] = 'partial'
        bundle['accounts'][0]['holdings_complete'] = False
        before = calculate()
        self.assertEqual(before['positions'][0]['issue'], 'account_stale_or_incomplete')
        self.assertEqual(self.dispatch()['counts'], {'failed':1})
        self.assertFalse(self.sent)
        self.assertEqual(self.store.read(), before)
        bundle['query_state'] = 'received'
        bundle['accounts'][0]['holdings_complete'] = True
        calculate()
        self.assertEqual(self.dispatch()['counts'], {'sent':3})
        with closing(sqlite3.connect(self.root/'runtime/telegram-delivery/delivery.sqlite3')) as db:
            self.assertEqual(sorted(r[0] for r in db.execute('SELECT attempts FROM deliveries')), [1,1,2])

    def test_same_day_reentry_does_not_send_previous_generation(self):
        bundle, calculate = self.calculated_events()
        original = {e['id'] for e in self.store.read()['events']}
        position = bundle['accounts'][0]['positions'].pop()
        calculate()
        bundle['accounts'][0]['positions'].append(position)
        current = calculate()
        self.assertEqual(current['positions'][0]['generation'], 2)
        eligible = self.store.delivery_events({'US':'2026-10-05'}, self.now)
        self.assertTrue(eligible)
        self.assertFalse(original.intersection(e['id'] for e in eligible))
        self.assertEqual(self.dispatch()['counts'], {'sent':len(eligible)})
        self.assertEqual(self.store.read(), current)

    def test_stale_account_blocks_delivery_without_rewriting_events(self):
        self.calculated_events()
        before = self.store.read()
        self.assertEqual(len(self.store.delivery_events({'US':'2026-10-05'}, self.now+timedelta(hours=24))), 3)
        self.clock.now.return_value = self.now+timedelta(hours=24,seconds=1)
        self.assertEqual(self.dispatch()['counts'], {})
        self.assertFalse(self.sent)
        self.assertEqual(self.store.read(), before)

    def test_configuration_dpapi_roundtrip_no_plaintext(self):
        with patch.dict('os.environ',{'TELEGRAM_BOT_TOKEN':'123:secret','TELEGRAM_CHAT_ID':'123'}):
            telegram.configure(self.root)
        self.assertEqual(telegram.settings(self.root)['token'],'123:secret')
        self.assertNotIn('123:secret',(self.root/'secrets/telegram.dpapi.json').read_text())
        with self.assertRaises(ValueError):telegram.configure(self.root)

    def test_lock_prevents_concurrent_delivery(self):
        self.add()
        with patch.object(telegram,'instance_lock',side_effect=telegram.BusyError()):
            self.assertEqual(self.dispatch()['state'],'busy')
        self.assertFalse(self.sent)


if __name__ == '__main__':unittest.main()
