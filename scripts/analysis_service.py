"""Analysis cases over existing dashboard stores. No broker/market/order writes."""
from contextlib import closing
from copy import deepcopy
from datetime import datetime, timezone
from decimal import Decimal, ROUND_FLOOR
import hashlib
import json
from pathlib import Path
import re
import uuid
from urllib.parse import urlencode, quote

from chart_workspace import Conflict, validate
from analysis_geometry import geometry


def packed(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)


def digest(value): return hashlib.sha256(packed(value).encode()).hexdigest()
def now(): return datetime.now(timezone.utc).isoformat()
def ident(prefix): return prefix + uuid.uuid4().hex


def key(value, prefix):
    if not isinstance(value, str) or not re.fullmatch(prefix + '[a-f0-9]{32}', value): raise ValueError('invalid_identifier')
    return value


def short(value, limit=4000):
    if not isinstance(value, str) or len(value) > limit: raise ValueError('invalid_text')
    return value


def context(value):
    if not isinstance(value, dict) or set(value)-{'timeframe','range','visible_range','selected_drawing','scenario','price_scale','evaluate_at'}: raise ValueError('invalid_context')
    result = dict(timeframe='1d', range='6', price_scale='linear', scenario='', selected_drawing=None, visible_range=None, **{})
    result.update(value)
    if result['timeframe'] not in ('1d','1w','1mo') or result['range'] not in ('3','6','12','60','all') or result['price_scale'] != 'linear': raise ValueError('unsupported_chart_context')
    short(result['scenario'],200)
    if result['selected_drawing'] is not None: short(result['selected_drawing'],64)
    if result.get('evaluate_at'): datetime.strptime(result['evaluate_at'],'%Y-%m-%d')
    v=result['visible_range']
    if v is not None and (not isinstance(v,dict) or set(v)!={'from','to'} or any(type(x) not in (int,float) or not -100000<x<100000 for x in v.values()) or v['from']>=v['to']): raise ValueError('invalid_visible_range')
    return result


class AnalysisService:
    def __init__(self, store, histories, financials, view, root):
        self.store=store; self.histories=histories; self.financials=financials; self.view=view; self.root=Path(root)
        with closing(store.connect()) as db, db:
            db.executescript('''
            CREATE TABLE IF NOT EXISTS analysis_cases (id TEXT PRIMARY KEY, stock_key TEXT NOT NULL, payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS analysis_snapshots (id TEXT PRIMARY KEY, case_id TEXT NOT NULL, payload TEXT NOT NULL, sha256 TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS analysis_items (id TEXT PRIMARY KEY, case_id TEXT NOT NULL, kind TEXT NOT NULL, payload TEXT NOT NULL);
            ''')

    def rows(self, table, case_id=None):
        if table not in ('analysis_cases','analysis_snapshots','analysis_items'): raise ValueError('table')
        with closing(self.store.connect()) as db:
            sql='SELECT payload FROM '+table; args=()
            if case_id is not None: sql+=' WHERE case_id=?'; args=(case_id,)
            return [json.loads(r[0]) for r in db.execute(sql+' ORDER BY rowid',args)]

    def case(self, case_id):
        key(case_id,'an_')
        with closing(self.store.connect()) as db:
            row=db.execute('SELECT payload FROM analysis_cases WHERE id=?',(case_id,)).fetchone()
        if not row: raise ValueError('analysis_not_found')
        return json.loads(row[0])

    def snapshot(self, case_id, snapshot_id):
        key(snapshot_id,'sn_'); self.case(case_id)
        with closing(self.store.connect()) as db:
            row=db.execute('SELECT payload,sha256 FROM analysis_snapshots WHERE id=? AND case_id=?',(snapshot_id,case_id)).fetchone()
        if not row: raise ValueError('snapshot_not_found_for_analysis')
        value=json.loads(row[0])
        if digest(value)!=row[1]: raise ValueError('snapshot_integrity_failed')
        return value

    def links(self, case, origin):
        cid=case['id']; route='#stock/'+quote(case['stock_key'].split(':',1)[0])+'/'+quote(case['stock_key'].split(':',1)[1])+'?analysis='+cid
        dashboard=origin+'/'+route
        prompt=f"투자 워크벤치 분석건 {cid} ({case['stock_key']})을 연결해줘. invest_analysis MCP의 analysis_read로 현재 저장 자료와 검토 이력을 읽고 대시보드 복귀 링크를 알려줘. 작도가 없어도 정상이다. 정성분석 스킬은 아직 미연결이므로 자동 분석 완료로 표시하거나 방법론을 대신 만들지 마. 이후 이 분석건의 현재 차트를 다시 봐 달라고 하면 같은 키로 재조회해줘. 대시보드: {dashboard}"
        return {'analysis_key':cid,'dashboard_url':dashboard,'reopen_command':f'.\\analysis.ps1 -AnalysisKey {cid}',
                'copy_text':f'분석건 {cid}\n이 분석건의 현재 차트를 다시 봐줘.\n{dashboard}\n포트 변경/서버 종료 시 워크벤치에서 .\\analysis.ps1 -AnalysisKey {cid}',
                'codex_url':'codex://new?'+urlencode({'path':str(self.root),'prompt':prompt}), 'prompt':prompt}

    def capture(self, case):
        data,stocks=self.view(); stocks={**self.histories.saved.stocks,**stocks}; k=case['stock_key']; ctx=case['context']
        if k not in stocks: raise ValueError('stock_not_available')
        chart=self.histories.view(stocks[k],ctx['timeframe'])
        workspace=self.store.shared(k,ctx['timeframe']); financial=self.financials.read(k)
        value={'id':ident('sn_'),'case_id':case['id'],'captured_at':now(),'stock_key':k,'context':ctx,
               'workspace':workspace,'chart':chart,'financials':financial,'accounts':data['accounts'],
               'market':{k:v for k,v in data['market'].items() if k in ('state','session_dates','source','errors')},
               'geometry':geometry(workspace,chart,ctx),'authority':'saved_input_not_trade_confirmation',
               'component_hashes':{'chart':digest(chart),'workspace':digest(workspace),'financials':digest(financial),'accounts':digest(data['accounts'])}}
        with closing(self.store.connect()) as db,db:
            db.execute('INSERT INTO analysis_snapshots VALUES (?,?,?,?)',(value['id'],case['id'],packed(value),digest(value)))
        return value

    def detail(self, case_id, origin):
        case=self.case(case_id); items=self.rows('analysis_items',case_id)
        with closing(self.store.connect()) as db:
            snapshots=[dict(id=r[0],captured_at=r[1],component_hashes=json.loads(r[2])) for r in db.execute("SELECT id,json_extract(payload,'$.captured_at'),json_extract(payload,'$.component_hashes') FROM analysis_snapshots WHERE case_id=? ORDER BY rowid",(case_id,))]
        return {'case':case,'links':self.links(case,origin),'items':items,
                'snapshots':[{k:s[k] for k in ('id','captured_at','component_hashes')} for s in snapshots],
                'qualitative_state':'records_available' if any(i['kind']=='report' for i in items) else 'skill_not_connected'}

    def item(self, case_id, item_id):
        key(item_id,'it_')
        return next((i for i in self.rows('analysis_items',case_id) if i['id']==item_id),None)

    def append(self, case_id, kind, payload, db=None):
        value={**payload,'id':ident('it_'),'case_id':case_id,'kind':kind,'created_at':now()}
        if db is not None: db.execute('INSERT INTO analysis_items VALUES (?,?,?,?)',(value['id'],case_id,kind,packed(value)))
        else:
            with closing(self.store.connect()) as conn,conn: conn.execute('INSERT INTO analysis_items VALUES (?,?,?,?)',(value['id'],case_id,kind,packed(value)))
        return value

    def proposal(self, case, snapshot_id, drawings, rationale):
        snap=self.snapshot(case['id'],snapshot_id)
        validate({'schema':1,'drawings':drawings,'averages':[]},drawing_limit=30)
        if not drawings: raise ValueError('empty_proposal')
        short(rationale)
        return self.append(case['id'],'proposal',{'snapshot_id':snapshot_id,'base_revision':snap['workspace']['revision'],
                    'drawings':drawings,'rationale':rationale,'author':'llm','status':'proposed', 'test_record':case['test_record']})

    def decide(self, case, proposal_id, action, reason, drawings=None):
        if action not in ('adopt','discard'): raise ValueError('invalid_decision')
        short(reason)
        proposal=self.item(case['id'],proposal_id)
        if not proposal or proposal['kind']!='proposal': raise ValueError('proposal_not_found')
        if drawings is not None: validate({'schema':1,'drawings':drawings,'averages':[]},drawing_limit=30)
        with closing(self.store.connect()) as db,db:
            db.execute('BEGIN IMMEDIATE')
            items=[json.loads(r[0]) for r in db.execute('SELECT payload FROM analysis_items WHERE case_id=? AND kind=?',(case['id'],'decision'))]
            if any(i['proposal_id']==proposal_id for i in items): raise Conflict('proposal_already_decided')
            row=db.execute('SELECT revision,payload,updated_at FROM shared_chart_workspaces WHERE identity=?',(case['stock_key'],)).fetchone()
            if not row: raise Conflict('workspace_missing')
            revision,payload,updated=row[0],json.loads(row[1]),row[2]
            added=[]
            if action=='adopt':
                if revision!=proposal['base_revision']: raise Conflict('stale_proposal_read_current_and_resubmit')
                for d in drawings if drawings is not None else proposal['drawings']:
                    added.append(dict(d,id=ident('ai_')))
                if not added: raise ValueError('empty_proposal')
                before=deepcopy(payload); payload['drawings']+=added
                validate({'schema':1,'drawings':payload['drawings'],'averages':payload['averages'][case['context']['timeframe']]},drawing_limit=300)
                db.execute('UPDATE shared_chart_workspaces SET revision=?,payload=?,updated_at=? WHERE identity=?',(revision+1,packed(payload),now(),case['stock_key']))
            else: before=None
            return self.append(case['id'],'decision',{'proposal_id':proposal_id,'action':action,'reason':reason,'author':'user',
                'snapshot_id':proposal['snapshot_id'],'base_revision':revision,'result_revision':revision+(action=='adopt'),
                'adopted_drawings':added,'original_workspace':before,'edited_proposal':drawings,'authority':'drawing_only_not_trade_or_risk_adoption'},db)

    def record(self, case, a):
        snap=self.snapshot(case['id'],a['snapshot_id']); kind=a['kind']
        if kind not in ('report','review'): raise ValueError('invalid_record_kind')
        title=short(a['title'],200); body=short(a['body'],50000)
        if a['author'] not in ('llm','user'): raise ValueError('invalid_record_author')
        opinion=short(a.get('llm_opinion',''),12000)
        user=a.get('user_response',{})
        if not isinstance(user,dict) or set(user)-{'agreement','counterexample','change_reason','adoption'}: raise ValueError('invalid_user_response')
        for v in user.values(): short(v,8000)
        sources=a.get('sources',[])
        if not isinstance(sources,list) or len(sources)>50: raise ValueError('invalid_sources')
        for s in sources:
            if not isinstance(s,dict) or set(s)-{'label','url','as_of'}: raise ValueError('invalid_source')
            for v in s.values(): short(v,2000)
        iid=ident('it_'); path=self.root/'judgments'/'analyses'/case['id']/(iid+'.md')
        text=f"---\nanalysis_key: {case['id']}\nrecord_id: {iid}\nsnapshot_id: {snap['id']}\ntest_record: {str(case['test_record']).lower()}\n---\n\n# {title}\n\n{'[연결 검증용 — 실제 투자 분석 아님]' if case['test_record'] else '[명시 저장 기록]'}\n\n{body}\n\n## LLM 의견\n{opinion}\n\n## 사용자 응답\n"+'\n'.join(f'- {k}: {v}' for k,v in user.items())+'\n\n## 출처\n'+ '\n'.join(f"- {s.get('label','')}: {s.get('url','')} (기준 {s.get('as_of','미확인')})" for s in sources)+f"\n\n입력 SHA-256: {digest(snap)}\n작도 revision: {snap['workspace']['revision']}\n"
        value={'id':iid,'case_id':case['id'],'kind':kind,'snapshot_id':snap['id'],'created_at':now(),'title':title,'body':body,'author':a['author'],
               'llm_opinion':opinion,'user_response':user,'sources':sources,'test_record':case['test_record'],
               'markdown_path':str(path.relative_to(self.root)).replace('\\','/'),'markdown_sha256':hashlib.sha256(text.encode()).hexdigest()}
        path.parent.mkdir(parents=True,exist_ok=True)
        with path.open('x',encoding='utf-8',newline='\n') as f: f.write(text)
        with closing(self.store.connect()) as db,db: db.execute('INSERT INTO analysis_items VALUES (?,?,?,?)',(iid,case['id'],kind,packed(value)))
        return value

    def holding(self, case, a):
        snap=self.snapshot(case['id'],a['snapshot_id'])
        if a.get('confirmed_by_user') is not True: raise ValueError('explicit_user_confirmation_required')
        account=next((x for x in snap['accounts']['accounts'] if x['alias']==a['account_alias']),None)
        market,symbol=case['stock_key'].split(':',1)
        pos=next((x for x in (account or {}).get('positions',[]) if x.get('market')==market and (x.get('quote_symbol') or x.get('symbol'))==symbol),None)
        if not pos or Decimal(str(pos.get('quantity',0)))<=0 or pos.get('data_unavailable_reason'): raise ValueError('observed_holding_required')
        scenario=self.snapshot(case['id'],a['adopted_snapshot_id'])
        return self.append(case['id'],'holding_link',{'snapshot_id':snap['id'],'adopted_snapshot_id':scenario['id'],
               'account_alias':a['account_alias'],'observed_position':pos,'as_of':snap['accounts']['as_of'],
               'thesis':short(a['thesis'],12000),'invalidation':short(a['invalidation'],12000),
               'confirmation':short(a['confirmation'],4000),'author':'user','test_record':case['test_record'],'authority':'user_attested_link_to_observed_holding_not_execution_evidence'})

    def calculate(self, case, a):
        from invest_agent.trading.portfolio import number
        snap=self.snapshot(case['id'],a['snapshot_id']); p=a['inputs']
        allowed={'entry','stop','budget','max_loss','currency','quantity_step','cost_per_share','slippage_per_share','account_alias','fx_to_base','fx_as_of','equity_base','existing_value_base','orderable_cash','basis_note'}
        if not isinstance(p,dict) or set(p)-allowed: raise ValueError('invalid_calculation_inputs')
        if p.get('account_alias') and not any(x['alias']==p['account_alias'] for x in snap['accounts']['accounts']):
            raise ValueError('unknown_account_alias')
        entry=number(p['entry'],positive=True); stop=number(p['stop'],positive=True)
        if stop>=entry: raise ValueError('long_scenario_requires_stop_below_entry')
        currency=snap['chart'].get('currency') or ('KRW' if case['stock_key'].startswith('KR:') else 'USD')
        if p['currency']!=currency: raise ValueError('budget_and_price_currency_must_match')
        budget=number(p['budget']); loss=number(p['max_loss']); step=number(p['quantity_step'],positive=True)
        limitations=['hypothetical_quantity_not_fill_or_order','gap_risk_not_bounded_by_stop','existing_risk_limits_unchanged','open_orders_not_queried_or_reconciled']
        cost=number(p['cost_per_share']) if p.get('cost_per_share') is not None else Decimal(0)
        slip=number(p['slippage_per_share']) if p.get('slippage_per_share') is not None else Decimal(0)
        if p.get('cost_per_share') is None or p.get('slippage_per_share') is None: limitations.append('costs_or_slippage_unknown_gross_estimate_only')
        if p.get('orderable_cash') is None: limitations.append('actual_orderable_cash_unavailable_budget_is_not_buying_power')
        else: budget=min(budget,number(p['orderable_cash'])); limitations.append('orderable_cash_user_supplied_not_verified')
        # Same Decimal lot-floor formula as portfolio.size_preview; this isolated tranche estimate cannot claim its fresh-cash/order-reconciliation prerequisites.
        unit_loss=entry-stop+cost+slip; quantity=(min(budget/(entry+cost+slip),loss/unit_loss)/step).to_integral_value(rounding=ROUND_FLOOR)*step
        fx=Decimal(1) if currency=='KRW' else (number(p['fx_to_base'],positive=True) if p.get('fx_to_base') is not None and p.get('fx_as_of') else None)
        equity=p.get('equity_base'); existing=p.get('existing_value_base'); weight=None
        if fx is not None and equity is not None and existing is not None:
            eq=number(equity,positive=True)-quantity*(cost+slip)*fx
            if eq>0: weight=str((number(existing)+quantity*entry*fx)/eq)
            limitations.append('weight_uses_explicit_user_supplied_equity_existing_value_and_fx')
        else: limitations.append('post_weight_unavailable_missing_equity_existing_value_or_fx')
        return {'snapshot_id':snap['id'],'inputs':p,'currency':currency,'quantity':str(quantity),'scenario_loss':str(quantity*unit_loss),
                'purchase_value':str(quantity*entry),'post_weight':weight,'account_as_of':snap['accounts'].get('as_of'),
                'existing_risk':snap['accounts'].get('risk'),'limitations':limitations,
                'formula':'q=floor(min(budget/(entry+cost+slippage),max_loss/(entry-stop+cost+slippage))/quantity_step)*quantity_step'}

    def dispatch(self, op, a, origin):
        if not isinstance(a,dict): raise ValueError('object_required')
        if op=='list':
            cases=self.rows('analysis_cases')
            if a.get('stock_key'): cases=[c for c in cases if c['stock_key']==a['stock_key']]
            return {'cases':cases}
        if op=='create':
            k=a['stock_key']; _,stocks=self.view()
            if k not in stocks and k not in self.histories.saved.stocks: raise ValueError('unknown_stock')
            if type(a.get('test_record',False)) is not bool: raise ValueError('invalid_test_flag')
            case={'id':ident('an_'),'stock_key':k,'title':short(a.get('title',k),200),'context':context(a.get('context',{})),
                  'created_at':now(),'test_record':a.get('test_record',False),'qualitative_skill':None}
            with closing(self.store.connect()) as db,db: db.execute('INSERT INTO analysis_cases VALUES (?,?,?)',(case['id'],k,packed(case)))
            self.capture(case)
            return self.detail(case['id'],origin)
        if op=='holding_links':
            items=self.rows('analysis_items'); cases={c['id']:c for c in self.rows('analysis_cases')}
            return {'links':[{**i,'stock_key':cases[i['case_id']]['stock_key'],'links':self.links(cases[i['case_id']],origin)} for i in items if i['kind']=='holding_link' and (not a.get('account_alias') or i['account_alias']==a['account_alias'])]}
        case=self.case(a['analysis_key']); cid=case['id']
        if op=='detail': return self.detail(cid,origin)
        if op=='context':
            case['context']=context(a['context'])
            with closing(self.store.connect()) as db,db: db.execute('UPDATE analysis_cases SET payload=? WHERE id=?',(packed(case),cid))
            return self.detail(cid,origin)
        if op=='read':
            snap=self.snapshot(cid,a['snapshot_id']) if a.get('snapshot_id') else self.capture(case)
            section=a.get('section','summary'); offset=a.get('offset',0); limit=a.get('limit',200)
            if type(offset) is not int or offset<0 or type(limit) is not int or not 1<=limit<=1000: raise ValueError('invalid_pagination')
            base={'analysis_key':cid,'snapshot_id':snap['id'],'captured_at':snap['captured_at'],'context':snap['context'],'component_hashes':snap['component_hashes'],'links':self.links(case,origin),'mode':'historical' if a.get('snapshot_id') else 'current_saved'}
            if section=='summary':
                base.update(workspace=snap['workspace'],geometry=snap['geometry'],financials={k:v for k,v in snap['financials'].items() if k!='result'},financial_snapshot_id=(snap['financials'].get('result') or {}).get('snapshot_id'),chart={k:v for k,v in snap['chart'].items() if k!='bars'},bar_count=len(snap['chart']['bars']),latest_bars=snap['chart']['bars'][-5:],accounts_as_of=snap['accounts'].get('as_of'))
            elif section=='chart': base.update(chart={**snap['chart'],'bars':snap['chart']['bars'][offset:offset+limit]},total=len(snap['chart']['bars']))
            elif section == 'financials' or section in ('financial_annual','financial_quarterly','financial_sources'):
                packet = snap['financials']; result = packet.get('result') or {}
                field = {'financials':'summary','financial_annual':'annual','financial_quarterly':'quarterly','financial_sources':'sources'}[section]
                items = result.get(field, []); page_size = min(limit, 20 if field == 'summary' else 5)
                base['financials'] = {k:v for k,v in packet.items() if k!='result'}
                base['financials']['result'] = {k:v for k,v in result.items() if k not in ('summary','annual','quarterly','sources','ttm')}
                base['financials']['result'][field] = items[offset:offset+page_size]
                base.update(field=field,total=len(items),offset=offset,next_offset=offset+page_size if offset+page_size<len(items) else None)
            elif section in ('accounts','workspace','geometry'): base[section]=snap[section]
            else: raise ValueError('invalid_section')
            return base
        if op=='refresh':
            _, stocks = self.view(); stocks = {**self.histories.saved.stocks, **stocks}
            stock = stocks[case['stock_key']]; mode = a['mode']
            if mode == 'financial_sources':
                source = {**stock, **self.histories.view(stock, '1d')}
                if case['stock_key'] == 'KR:005930.KS':
                    from financial_summary import selected_stock
                    _, source = selected_stock('KR', '005930.KS', self.histories)
                return self.financials.start(case['stock_key'], source, refresh=True)
            if mode == 'price_history':
                value = self.histories.ensure(stock, retry=True)
                return {'history':value['history'], 'scope':'selected_stock_saved_cutoff_only_not_daily_market_refresh'}
            if mode == 'financial_price_recalculate':
                saved = self.financials.read(case['stock_key'])
                sid = (saved.get('result') or {}).get('snapshot_id')
                if not sid or not re.fullmatch('[a-f0-9]{32}', sid): raise ValueError('saved_financial_required')
                seed = self.financials.folder(case['stock_key']) / 'snapshots' / sid
                source = json.loads((seed / 'saved-price.json').read_bytes())
                source.update(self.histories.view(stock, '1d'))
                value = self.financials.import_collected(case['stock_key'], source, seed)
                return {'state':value['state'],'attempt':value['attempt'],'snapshot_id':(value.get('result') or {}).get('snapshot_id'),'scope':'saved_disclosures_and_saved_prices_no_network'}
            raise ValueError('invalid_refresh_mode')
        if op=='propose': return self.proposal(case,a['snapshot_id'],a['drawings'],a['rationale'])
        if op=='decide': return self.decide(case,a['proposal_id'],a['action'],a.get('reason',''),a.get('drawings'))
        if op=='record': return self.record(case,a)
        if op=='holding': return self.holding(case,a)
        if op=='calculate': return self.calculate(case,a)
        raise ValueError('unknown_analysis_operation')
