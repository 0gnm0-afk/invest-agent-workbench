from contextlib import closing
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'scripts'))
import telegram_warnings as telegram
from trailing_warnings import TrailingStore


class TelegramWarnings(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.store = TrailingStore(self.root/'runtime/trailing-warnings.sqlite3')
        self.config = dict(token='fake', chat='123', enabled_at='2026-10-06T00:00:00+00:00')
        self.event = dict(id='warning1', key='private', account='ACCOUNT_PRIVATE', market='KR', symbol='005930',
            name='합성종목', currency='KRW', date='2026-10-05', created_at='2026-10-06T01:00:00+00:00',
            kind='trailing', close=263000, threshold=263150, line=263150, high=277000, atr=6000, ma5=266000,
            average_cost=212345, reason='종가가 최종 트레일링 경고선에 접촉·이탈')
        self.sent = []

    def add(self, **changes):
        e = {**self.event, **changes}
        with closing(self.store.connect()) as db, db:
            db.execute('INSERT OR REPLACE INTO warnings VALUES(?,?)',(e['id'],json.dumps(e)))

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
