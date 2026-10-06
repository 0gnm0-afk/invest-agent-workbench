import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import adr_metadata as adr
import user_classification
import market_scope

ROW={'market':'US','symbol':'ABC','isin_code':'USABC123','listing_exchange_family':'NYSE','security_type':'DEPOSITARY_RECEIPT'}
STATS=b'''<html>NYSE: ABC &middot; Real-Time Price &middot; USD
At close: Oct 5, 2026, 4:00 PM EDT
<table><tr><td>Market Cap</td><td title="2,010,123,456">2.01B</td></tr></table>
Last updated: Oct 6, 2026</html>'''
PROFILE=b'''<html>NYSE: ABC &middot; Real-Time Price &middot; USD<table>
<tr><td>Ticker Symbol</td><td>ABC</td></tr><tr><td>ISIN Number</td><td>USABC123</td></tr>
<tr><td>Sector</td><td>Financials</td></tr><tr><td>Industry</td><td>Banks - Regional</td></tr></table></html>'''


class AdrMetadata(unittest.TestCase):
    def parse(self, stats=STATS, profile=PROFILE, row=ROW):
        return adr.parse_pages(stats,profile,row,'2026-10-05')

    def test_direct_cap_exact_units_and_sector_no_share_arithmetic(self):
        value=self.parse(row={**ROW,'shares_outstanding':99999999999})
        self.assertEqual(value['market_cap'],2010123456)
        self.assertEqual(value['market_cap_as_of'],'2026-10-05')
        self.assertEqual(value['market_cap_statistics_updated_on'],'2026-10-06')
        self.assertEqual(value['sector'],'Financial')

    def test_identity_and_currency_must_match_both_pages(self):
        for before,after in [(b'NYSE',b'NASDAQ'),(b'ABC',b'OTHER'),(b'USD',b'EUR')]:
            for target in ['stats','profile']:
                with self.subTest(target=target,before=before),self.assertRaises(ValueError):
                    self.parse(**{target:({'stats':STATS,'profile':PROFILE}[target]).replace(before,after)})
        with self.assertRaisesRegex(ValueError,'isin'):
            self.parse(profile=PROFILE.replace(b'USABC123',b'REUSED'))

    def test_no_rounded_missing_zero_or_future_caps(self):
        for new in [b'',b'n/a',b'2.01B',b'0',b'-1',b'NaN']:
            with self.subTest(new=new),self.assertRaises(ValueError):
                self.parse(stats=STATS.replace(b'2,010,123,456',new))
        with self.assertRaisesRegex(ValueError,'session'):
            self.parse(stats=STATS.replace(b'At close: Oct 5',b'At close: Oct 6'))
        with self.assertRaisesRegex(ValueError,'date'):
            self.parse(stats=STATS.replace(b'Last updated: Oct 6',b'Last updated: Sep 6'))

    def bundle(self, root):
        documents=[]
        for name,raw in [('statistics',STATS),('company',PROFILE)]:
            path=root/(name+'.html');path.write_bytes(raw)
            url='https://stockanalysis.com/stocks/abc/'+name+'/'
            documents.append(dict(path=str(path),url=url,final_url=url,status=200,sha256=adr.sha(path),observed_at='2026-10-06T00:00:00+00:00'))
        path=root/'bundle.json'
        adr.write(path,dict(schema=1,session='2026-10-05',records=[dict(ROW,state='available',values=self.parse(),documents=documents)]))
        return dict(path=str(path),sha256=adr.sha(path))

    def test_bundle_replay_tampering_and_exact_snapshot_identity(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);ref=self.bundle(root);inputs={}
            records=adr.verified(ref,inputs);self.assertEqual(len(records),1);self.assertEqual(len(inputs),3)
            group=dict(market='US',symbol='ABC',isin_code='USABC123',exchange='NYSE',group_state='unmatched')
            matched=adr.apply_groups([group],{'adr_metadata':ref},{})
            self.assertEqual(matched[0]['group_name'],'Financial')
            self.assertEqual(matched[0]['classification_freshness']['state'],'recent')
            changed={**group,'isin_code':'REUSED'}
            self.assertEqual(adr.apply_groups([changed],{'adr_metadata':ref},{}),[changed])
            (root/'statistics.html').write_bytes(STATS+b'tampered')
            with self.assertRaisesRegex(ValueError,'document'):
                adr.verified(ref)

    def test_cap_patch_preserves_prices_and_unavailable_when_identity_differs(self):
        with tempfile.TemporaryDirectory() as tmp:
            ref=self.bundle(Path(tmp));row={**ROW,'market_cap':None,'market_cap_quality':'unavailable_adr_share_basis','bars':[{'date':'2026-10-05','close':12.34}]}
            snapshot=dict(universe=[copy.deepcopy(row)],series=[copy.deepcopy(row)],sessions={'US':['2026-10-05']})
            with patch.object(market_scope,'refresh_coverage'):
                adr.apply_market(snapshot,ref)
            self.assertEqual(snapshot['series'][0]['bars'],row['bars'])
            self.assertEqual(snapshot['universe'][0]['market_cap'],2010123456)
            snapshot['sessions']['US']=['2026-10-06']
            with self.assertRaisesRegex(ValueError,'session'):
                adr.apply_market(snapshot,ref)


class RequestedOtherAndExclusions(unittest.TestCase):
    def test_new_or_changed_common_only_other_keeps_original_reason(self):
        policy={'schema':1,'approved_on':'2026-10-04','approval':'old user','records':[],
            'unmatched_common_stock_other':{'enabled':True,'approved_on':'2026-10-06','approval':'current user',
            'reasons':['new_listing_classification_not_acquired','classification_not_acquired_for_current_identity']}}
        for reason in policy['unmatched_common_stock_other']['reasons']:
            row=dict(market='US',symbol='ABC',isin_code='USABC123',exchange='NYSE',group_state='unmatched',reason=reason)
            for typ in ['STOCK','DEPOSITARY_RECEIPT']:
                value=user_classification.apply([row],{'user_classification':policy,'universe':[{**ROW,'security_type':typ}]})[0]
                self.assertEqual(value['group_state'],'matched' if typ=='STOCK' else 'unmatched')
                if typ=='STOCK':
                    self.assertEqual(value['group_name'],'기타');self.assertEqual(value['user_classification_previous'],row)
                    self.assertEqual(value['evidence'][0]['approved_on'],'2026-10-06')

    def test_six_failures_are_excluded_only_for_current_isin(self):
        policy=market_scope.load()
        rows=[r for r in policy['records'] if r.get('reason')=='user_excluded_nh_unrecovered']
        self.assertEqual({r['symbol'] for r in rows},{'IPHX','TBCV','VIVK','CHWM','LAWR','RML'})
        for row in rows:
            self.assertIsNotNone(market_scope.match(row,policy))
            self.assertIsNone(market_scope.match({**row,'isin_code':'NEW_ID'},policy))


class OfflineAdrBoundary(unittest.TestCase):
    def test_captured_nh_recovery_never_requests_adr(self):
        import daily_market as daily
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);source=root/'source'
            with patch.object(daily,'ROOT',root), patch.object(daily,'code_hashes',return_value={}), \
                    patch.object(daily,'seed_check',return_value=({},{})), patch.object(daily,'source_check'), \
                    patch.object(daily.sector,'load',return_value={}), patch.object(daily.sector,'sha',return_value='fixture'), \
                    patch.object(daily.nh_fallback,'recover',return_value=(source,dict(attempted=0,adopted=0,unrecovered=0),{})), \
                    patch.object(daily,'audit',side_effect=ValueError('stop_after_adr_stage')), \
                    patch.object(adr,'collect') as collector:
                result=daily.run({'engine':tmp,'adr_metadata':{'enabled':True}},recover_source=source,nh_evidence=root/'captured')
            collector.assert_not_called()
            self.assertEqual(result['stages']['adr_metadata'],'not_requested_offline')
            self.assertEqual(result['stages']['quality'],'failed')


if __name__=='__main__':unittest.main()
