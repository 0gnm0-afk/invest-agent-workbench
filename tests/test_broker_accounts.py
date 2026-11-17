"""Synthetic balance contracts; no authentication, broker, operational DB or network."""
from copy import deepcopy
from datetime import datetime, timezone
import json
import io
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'scripts'))
from accounts.brokers import (body, nh_pages, nh_requests, normalize_nh, normalize_meritz,
                              meritz_next, query_state)
from accounts.saved import merge_saved
from accounts.store import AccountStore
from accounts import account_view


STAMP = '2026-11-01T01:00:00+00:00'


def meritz_page(data, **extra):
    return {'data': data, 'rsp_cd': '5766', 'rsp_msg': '조회가 완료되었습니다.', 'tr_cont': '0', **extra}


def kr_lot(**extra):
    return {'iscd':'A005930', 'isnm':'합성 국내', 'pros_trgt_cls_code':'1', 'mksc_cls_name':'주식일반',
            'rmnd_qty':2, 'psnt_prc':'10000', 'byng_unpr':'8000', **extra}


def us_lot(**extra):
    return {'iscd':'SYN.OQ','ovrs_stck_symb_iscd':'SYN','isnm':'합성 미국',
            'ovrs_exch_vndr_idno':'0537','crcd':'USD','rmnd_clcd':'1',
            'rmnd_dqty':'0.SYNTH_ACCOUNT_c775e7b757ed','psnt_prc':'100.25','avrg_unpr':'80.125', **extra}


def meritz_observation():
    return {'as_of':STAMP, 'accounts_complete':True, 'accounts':[{'alias':'meritz-synthetic', 'responses':{
        'holdings':[meritz_page([kr_lot(), {'pros_trgt_cls_code':'J','iscd':'SYN.OQ'}])],
        'ovs_holdings':[meritz_page([us_lot()])],
        'deposit':[meritz_page({'tfam':1000,'d1_aft_tfam':2000,'d2_aft_tfam':3000,
                               'oder_able_cash_amt':500,'drng_able_amt':400})],
        'valuation':[meritz_page({'aset_vltn_amt':999999,'frcr_tfam':15000,'loan_amt':100})],
        'ovs_currencies':[meritz_page([{'crcd':'USD','frcr_amt':'10','estm_frcr_amt':'11',
                                      'vltn_exrt':'1500','excl_wncr_vltn_amt':16500}])]
    }}]}


def nh_page(rows, **extra):
    return {'Output_0':{'dca':1000},'Output_1':rows,'rsp_msg':'조회가 완료되었습니다.',
            '_pagination':{'cts_flag':'N','cts':None,'request_cts':None}, **extra}


def nh_observation():
    return {'as_of':STAMP,'accounts_complete':True,'accounts':[{'alias':'nh-synthetic','markets':{
        'KR':{'state':'received','pages':[nh_page([{'iem_cd':'005930','iem_nm':'합성 국내',
              'itg_bnc_qty':2,'now_pr':10000,'phs_pr':8000}])]},
        'US':{'state':'received','pages':[nh_page([{'iem_cd':'SYN','iem_nm':'합성 미국','cur_cd':'USD',
              'fc_sec_trd_nat_cd':'200','cns_bse_bnc_qty':3,'fc_sec_end_pr':'100',
              'fc_phs_uit_pr':'70','fc_avg_phs_pr':'80','tdt_sby_bse_xcg_rt':'1400'}])]}
    }}]}


class BrokerContracts(unittest.TestCase):
    def test_mcp_unwrap_and_raw_precision_and_errors(self):
        self.assertEqual(body({'content':[{'type':'text','text':'{"data":{"x":"0.SYNTH_ACCOUNT_c775e7b757ed"}}'}]})['data']['x'],'0.SYNTH_ACCOUNT_c775e7b757ed')
        for wrapper in ({'isError':True}, {'content':[{'type':'text','text':'not JSON'}]}):
            with self.assertRaises(ValueError): body(wrapper)
        with self.assertRaisesRegex(ValueError,'precision'):
            body({'content':[{'type':'text','text':'{"data":{"x":999999999999999999}}'}]})
        exact=body({'raw_json':'{"data":{"x":999999999999999999,"q":0.1234567890123456789}}'})
        self.assertEqual(exact['data']['x'],'999999999999999999')
        self.assertEqual(exact['data']['q'],'0.1234567890123456789')
        wrapped={'ok':True,'http_status':200,'body':meritz_page([kr_lot()])}
        self.assertEqual(body({'content':[],'structuredContent':wrapped})['data'][0]['rmnd_qty'],2)
        with self.assertRaises(ValueError): body({**wrapped,'ok':False})
        with self.assertRaisesRegex(ValueError,'precision'):
            body({'content':[],'structuredContent':{'body':{'data':{'amount':999999999999999999}}}})

    def test_nh_fixed_basis_and_header_completion(self):
        inputs=nh_requests('SYNTH_ACCOUNT_254aa248acb4')['KR'][1]
        self.assertEqual((inputs['aly_qut_cd'],inputs['bnc_bse_cd'],inputs['aet_bse']),('1','1','1'))
        calls=[]
        def read(path, inputs, continuation):
            calls.append(continuation)
            return nh_page([]), ({'cts_flag':'Y','cts':'cursor  '} if not continuation else {'cts_flag':'N'})
        result=nh_pages('/krstock/inquiry/v1/balance',inputs,read)
        self.assertEqual(len(result),2)
        self.assertEqual(calls[1],{'cts':'cursor  ','cts_flag':'Y'})
        for headers in ({},{'cts_flag':'Y'},{'cts_flag':'bad'}):
            with self.assertRaises(ValueError):
                nh_pages('path',inputs,lambda *args:(nh_page([]),headers))
        with self.assertRaisesRegex(ValueError,'cursor'):
            nh_pages('path',inputs,lambda *args:(nh_page([]),{'cts_flag':'Y','cts':'repeat'}))

    def test_nh_both_message_formats_and_business_failure(self):
        source=nh_observation()
        page=source['accounts'][0]['markets']['KR']['pages'][0]
        page.pop('rsp_msg');page['message']={'usr_msg':'조회가 완료되었습니다.','msg_code':'example'}
        result=normalize_nh(source)
        self.assertEqual(query_state(result),'received')
        self.assertEqual(result['accounts'][0]['positions'][1]['average_cost'],'80')
        self.assertEqual(result['accounts'][0]['cash'],{})
        for malformed in (None, {'as_of':0,'accounts':[]},{'as_of':STAMP,'accounts':[None]}):
            with self.assertRaises(ValueError): normalize_nh(malformed)
        page['message']['usr_msg']='권한 오류'
        self.assertEqual(query_state(normalize_nh(source)),'partial')

    def test_patched_official_mcp_headers_and_original_numeric_json(self):
        source=nh_observation()
        for section in source['accounts'][0]['markets'].values():
            original=section['pages'][0];original.pop('_pagination')
            text=json.dumps(original,ensure_ascii=False)
            response={**original,'_nhplug_response':{'raw_json':text,'headers':{'cts_flag':'N'},'request_cts':None}}
            section['pages']=[{'content':[{'type':'text','text':json.dumps(response,ensure_ascii=False)}]}]
        result=normalize_nh(source)
        self.assertTrue(result['accounts'][0]['holdings_complete'])
        raw='{"Output_0":{"dca":999999999999999999},"Output_1":[],"rsp_msg":"정상 완료"}'
        response={'Output_0':{'dca':1000000000000000000},'_nhplug_response':{
            'raw_json':raw,'headers':{'cts_flag':'N'},'request_cts':None}}
        unwrapped=body({'content':[{'type':'text','text':json.dumps(response)}]})
        self.assertEqual(unwrapped['Output_0']['dca'],'999999999999999999')

    def test_nh_missing_headers_average_cost_and_lots_are_not_complete(self):
        source=nh_observation()
        section=source['accounts'][0]['markets']['US']
        section['pages'][0].pop('_pagination')
        self.assertFalse(normalize_nh(source)['accounts'][0]['holdings_complete'])
        source=nh_observation();row=source['accounts'][0]['markets']['US']['pages'][0]['Output_1'][0]
        del row['fc_avg_phs_pr']
        result=normalize_nh(source)['accounts'][0]
        self.assertFalse(result['holdings_complete'])
        self.assertNotIn('average_cost',result['positions'][1])
        source=nh_observation();rows=source['accounts'][0]['markets']['KR']['pages'][0]['Output_1']
        rows.append({**rows[0],'lon_byn_dt':'20261020','itg_bnc_qty':3,'phs_pr':9000})
        result=normalize_nh(source)['accounts'][0]
        self.assertEqual(result['positions'][0]['quantity'],'5')
        self.assertEqual(result['positions'][0]['average_cost'],'8600')
        rows.append(deepcopy(rows[0]))
        self.assertFalse(normalize_nh(source)['accounts'][0]['holdings_complete'])

    def test_meritz_mixed_response_fractional_lots_and_cash_semantics(self):
        source=meritz_observation();before=deepcopy(source)
        result=normalize_meritz(source);a=result['accounts'][0]
        self.assertEqual(query_state(result),'received')
        self.assertEqual(len(a['positions']),2)
        self.assertEqual(a['positions'][1]['quantity'],'0.SYNTH_ACCOUNT_c775e7b757ed')
        self.assertEqual(a['observed_deposits'],{'KR':{'dca':'1000','nxt_dd_dca':'2000','nxt2_dd_dca':'3000'},'US':{'fc_dca':'10'}})
        self.assertEqual(a['cash'],{});self.assertFalse(a['cash_complete'])
        self.assertNotIn('available_cash',a);self.assertNotIn('net_liquidation_value',a)
        self.assertEqual(a['fx_to_base']['USD'],'1500')
        self.assertEqual(source,before)
        source['accounts'][0]['responses']['holdings'][0]['data'].append(kr_lot(mksc_cls_name='신용',rmnd_qty=3,byng_unpr='9000',loan_date='20261020'))
        a=normalize_meritz(source)['accounts'][0]
        self.assertEqual(a['positions'][0]['average_cost'],'8600')
        self.assertEqual(len(a['positions'][0]['lots']),2)

    def test_meritz_page_keys_and_failure_cannot_confirm_absence(self):
        page=meritz_page([kr_lot()],rsp_cd='5762',tr_cont='1',tr_cont_key='key   ')
        self.assertEqual(meritz_next('holdings',[page]),{'tr_cont':'1','tr_cont_key':'key   '})
        with self.assertRaisesRegex(ValueError,'cursor'):
            meritz_next('holdings',[page,page])
        source=meritz_observation();source['accounts'][0]['responses']['holdings']=[page]
        self.assertEqual(query_state(normalize_meritz(source)),'partial')
        partial=normalize_meritz(source)['accounts'][0]
        self.assertEqual(partial['positions'][0]['quantity'],'2')
        self.assertEqual(partial['query_markets']['KR']['reason'],'pagination_incomplete')
        page['rsp_cd']='error';page['tr_cont']='0'
        self.assertEqual(query_state(normalize_meritz(source)),'partial')
        source=meritz_observation();source['accounts'][0]['responses']['ovs_holdings']=[{'rsp_cd':'5820','rsp_msg':'조회할 내역 없음'}]
        self.assertTrue(normalize_meritz(source)['accounts'][0]['holdings_complete'])
        source['accounts'][0]['responses']['ovs_holdings']=[{'rsp_cd':'5820','data':[us_lot()]}]
        self.assertFalse(normalize_meritz(source)['accounts'][0]['holdings_complete'])

    def test_meritz_unsupported_instrument_and_ambiguous_duplicates(self):
        source=meritz_observation()
        source['accounts'][0]['responses']['holdings'][0]['data'].append(kr_lot(mksc_cls_name='미확인상품'))
        self.assertFalse(normalize_meritz(source)['accounts'][0]['holdings_complete'])
        source=meritz_observation();source['accounts'][0]['responses']['ovs_holdings'][0]['data'].append(us_lot())
        a=normalize_meritz(source)['accounts'][0]
        self.assertFalse(a['holdings_complete']);self.assertIn('duplicate',a['positions'][1]['data_unavailable_reason'])


class SavedProviderIntegration(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.store=AccountStore(self.root/'runtime/accounts.sqlite3')

    def complete(self,provider,snapshot,request='first'):
        claim=self.store.begin(provider,request,'mcp')
        return self.store.complete(provider,request,claim['lease'],metadata={'query_state':query_state(snapshot)},snapshot=snapshot)

    def test_provider_merge_and_failure_preserve_each_observation(self):
        nh=normalize_nh(nh_observation());meritz=normalize_meritz(meritz_observation())
        self.complete('meritz',meritz)
        nh_view={'accounts':nh['accounts'],'as_of':STAMP,'query_state':'received','accounts_complete':True,
                 'stale':False,'valuation':{'base_currency':'KRW','fx_to_base':{'USD':'1400'}}}
        merged=merge_saved(self.store,nh_view)
        self.assertEqual(len(merged['accounts']),2)
        self.assertNotIn('USD',merged['valuation']['fx_to_base'])
        self.assertEqual(merged['accounts'][1]['fx_to_base']['USD'],'1500')
        self.assertEqual(len(self.store.read(merged['snapshot_ref'])['accounts']),2)
        claim=self.store.begin('meritz','failed','mcp')
        self.store.complete('meritz','failed',claim['lease'],metadata={'query_state':'failed','reason':'synthetic_transport'})
        failed=merge_saved(self.store,nh_view)
        self.assertEqual(failed['query_state'],'partial')
        self.assertEqual(failed['accounts'][1]['as_of'],STAMP)
        self.assertFalse(failed['accounts'][1]['holdings_complete'])
        self.assertEqual(failed['providers']['meritz']['query_state'],'failed')
        self.assertTrue(self.store.read(self.store.latest('meritz')['snapshot_ref'])['accounts'][0]['holdings_complete'])

    def test_saved_account_view_includes_both_without_network_or_exports(self):
        self.complete('nh',normalize_nh(nh_observation()))
        self.complete('meritz',normalize_meritz(meritz_observation()))
        out=self.root/'runtime/dashboard/synthetic'
        from types import SimpleNamespace
        modules={'nh_api':SimpleNamespace(read=lambda *args:self.fail('Unexpected broker read'),NHError=RuntimeError)}
        with patch.dict(sys.modules,modules), \
             patch.object(AccountStore,'begin',side_effect=AssertionError('Unexpected begin')), \
             patch.object(AccountStore,'save',side_effect=AssertionError('Unexpected save')):
            view=account_view({'mother':str(self.root/'mother')},out,refresh=False)
        self.assertEqual(len(view['accounts']),2)
        self.assertFalse(out.parent.exists())
        self.assertEqual(view['providers']['meritz']['as_of'],STAMP)

    def test_raw_response_storage_rejects_auth_headers_and_embedded_json(self):
        for value in ({'headers':{'x-client-secret':'synthetic'}},
                      {'raw_json':'{"access_token":"synthetic"}'}, {'NH_APP_KEY':'synthetic'}):
            with self.assertRaisesRegex(ValueError,'credentials_forbidden'):
                self.store.save(value)

    def test_failed_nh_attempt_still_keeps_saved_meritz_and_source_times(self):
        nh=normalize_nh(nh_observation());self.complete('nh',nh)
        self.complete('meritz',normalize_meritz(meritz_observation()))
        claim=self.store.begin('nh','failed-session','mcp')
        failure=self.store.complete('nh','failed-session',claim['lease'],
            metadata={'query_state':'failed','reason':'synthetic_transport'})
        out=self.root/'runtime/dashboard/failed'
        from types import SimpleNamespace
        def fail(*args): self.fail('Unexpected broker read')
        with patch.dict(sys.modules,{'nh_api':SimpleNamespace(read=fail,NHError=RuntimeError)}), \
             patch.object(AccountStore,'begin',side_effect=AssertionError('Unexpected begin')), \
             patch.object(AccountStore,'save',side_effect=AssertionError('Unexpected save')):
            result=account_view({'mother':str(self.root/'mother')},out,refresh=True,request_id='fail')
        self.assertEqual(result['query_state'],'partial')
        self.assertEqual(result['providers']['nh']['query_state'],'failed')
        self.assertEqual(result['providers']['nh']['attempted_at'],failure['attempted_at'])
        self.assertEqual(len(result['accounts']),2)
        self.assertFalse(result['accounts'][0]['holdings_complete'])
        self.assertEqual(result['accounts'][0]['as_of'],STAMP)
        self.assertEqual(self.store.read(self.store.latest('nh')['snapshot_ref']),nh)
        self.assertFalse(out.parent.exists())

    def test_cli_observation_complete_stores_raw_and_failure_closes_lease(self):
        from accounts import record
        def command(request, value):
            output=io.StringIO()
            with patch.object(record,'AccountStore',return_value=self.store), \
                 patch.object(sys,'argv',['record.py','complete','--provider','meritz','--request-id',request]), \
                 patch.object(sys,'stdin',io.StringIO(json.dumps(value))),patch.object(sys,'stdout',output):
                record.main()
            return json.loads(output.getvalue())
        claim=self.store.begin('meritz','cli-good','mcp')
        observation=meritz_observation()
        receipt=command('cli-good',{'lease':claim['lease'],'observation':observation})
        self.assertEqual(receipt['query_state'],'received')
        self.assertEqual(self.store.read(receipt['raw_ref']),observation)
        bad=self.store.begin('meritz','cli-bad','mcp')
        receipt=command('cli-bad',{'lease':bad['lease'],'observation':{'as_of':STAMP}})
        self.assertEqual(receipt['query_state'],'failed')
        self.assertIsNone(receipt['snapshot_ref'])
        self.assertEqual(self.store.latest('meritz')['request_id'],'cli-good')
        self.assertFalse(self.store.begin('meritz','cli-bad','mcp')['execute'])


if __name__=='__main__': unittest.main()
