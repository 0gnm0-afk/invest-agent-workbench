"""Financial facts with per-value provenance; missing never means zero."""
import math
import re
from datetime import date, timedelta
from financial_revenue import select_dart_revenue, select_sec_revenue, revenue_metadata
from financial_dart_supplement import annual_supplement_candidates, COMMON_STOCK

VERSION = 'financial-dart-periods-4'

def number(value):
    if value is None or value in ('', '-', 'N/A') or isinstance(value, bool):
        return None
    try:
        v = float(str(value).replace(',', '').replace('(', '-').replace(')', ''))
        return v if math.isfinite(v) else None
    except (TypeError, ValueError):
        return None

def fact(value=None, *, unit='', end=None, start=None, filed=None, sources=(), formula='공시 원값', reason=None, **extra):
    value = number(value)
    return dict(value=value, unit=unit, end=end, start=start, filed=filed, sources=list(sources), formula=formula,
                reason=reason or ('공시 태그/기간/단위 미확보' if value is None else None), **extra)

def calc(formula, inputs, fn, unit='', positive=()):
    cells = [x or fact() for x in inputs]
    valid = all(x['value'] is not None for x in cells) and all(cells[i]['value'] > 0 for i in positive)
    value = None
    if valid:
        try:
            value = fn(*[x['value'] for x in cells])
        except (ZeroDivisionError, ValueError, OverflowError):
            pass
    return fact(value, unit=unit, end=max((x.get('end') or '' for x in cells), default='') or None,
                start=min((x['start'] for x in cells if x.get('start')), default=None),
                filed=max((x.get('filed') or '' for x in cells), default='') or None,
                sources=sorted(set(s for x in cells for s in x.get('sources',[]))), formula=formula,
                reason=None if value is not None else '필수 입력 결측 또는 분모가 0/음수',
                inputs=[dict(value=x['value'],unit=x['unit'],end=x.get('end'),formula=x.get('formula'),
                             **({'eps_basis':x['eps_basis']} if x.get('eps_basis') else {}),**revenue_metadata(x)) for x in cells])

def revenue_calc(formula, inputs, fn, unit):
    cells=[cell or fact() for cell in inputs]
    result=calc(formula,cells,fn,unit)
    if len({cell.get('comparison_key') for cell in cells})>1:
        result.update(value=None,reason='매출 집계 기준이 달라 기간 실적을 합산/차감하지 않음')
    result.update(revenue_metadata(cells[0]))
    return result

DART_MAP = {
 'sales':('IS', ['ifrs-full_Revenue','ifrs_Revenue'], ['매출액','수익(매출액)']),
 'income':('IS',['ifrs-full_ProfitLossAttributableToOwnersOfParent'],['지배기업 소유주지분','지배기업의 소유주에게 귀속되는 당기순이익']),
 'net_income':('IS',['ifrs-full_ProfitLoss'],['당기순이익','분기순이익','반기순이익']),
 'gross':('IS',['ifrs-full_GrossProfit'],['매출총이익']),
 'operating':('IS',['dart_OperatingIncomeLoss'],['영업이익','영업이익(손실)']),
 'pretax':('IS',['ifrs-full_ProfitLossBeforeTax'],['법인세비용차감전순이익']),
 'tax':('IS',['ifrs-full_IncomeTaxExpenseContinuingOperations'],['법인세비용(수익)','법인세비용']),
 'eps':('IS',['ifrs-full_DilutedEarningsLossPerShare'],['희석주당이익','희석주당이익(손실)']),
 'basic_eps':('IS',['ifrs-full_BasicEarningsLossPerShare'],['기본주당이익','기본주당이익(손실)']),
 'assets':('BS',['ifrs-full_Assets'],['자산총계']),
 'equity':('BS',['ifrs-full_EquityAttributableToOwnersOfParent'],['지배기업 소유주지분']),
 'total_equity':('BS',['ifrs-full_Equity'],['자본총계']),
 'cash':('BS',['ifrs-full_CashAndCashEquivalents'],['현금및현금성자산']),
 'current_assets':('BS',['ifrs-full_CurrentAssets'],['유동자산']),
 'current_liabilities':('BS',['ifrs-full_CurrentLiabilities'],['유동부채']),
 'inventory':('BS',['ifrs-full_Inventories'],['재고자산']),
 'receivables':('BS',['ifrs-full_CurrentTradeReceivables'],['매출채권']),
 'debt_short':('BS',['ifrs-full_ShorttermBorrowings'],['단기차입금']),
 'debt_current':('BS',['ifrs-full_CurrentPortionOfLongtermBorrowings'],['유동성장기부채','유동성장기차입금']),
 'debt_long':('BS',['ifrs-full_LongtermBorrowings'],['장기차입금']),
 'bonds':('BS',[],['사채']),
 'cfo':('CF',['ifrs-full_CashFlowsFromUsedInOperatingActivities'],['영업활동현금흐름']),
 'capex':('CF',['ifrs-full_PurchaseOfPropertyPlantAndEquipmentClassifiedAsInvestingActivities'],['유형자산의 취득']),
 'da':('CF',['ifrs-full_AdjustmentsForDepreciationDepletionAndAmortisationExpense'],['감가상각비와 무형자산상각비']),
}

def dart_period_name(value):
    match=re.search(r'제\s*(\d+)\s*기',str(value or ''))
    if not match:return None
    quarter=re.search(r'([1-4])\s*분기',str(value))
    return int(match[1]),int(quarter[1]) if quarter else 2 if '반기' in str(value) else 0

def dart_comparative_period(row, report, column, offset, quarter):
    """Comparative values need an explicit matching fiscal-term label, never position alone."""
    codes={1:'11013',2:'11012',3:'11014',4:'11011'}
    if str(row.get('bsns_year'))!=str(report['year']) or row.get('reprt_code')!=codes[report['quarter']]:return False
    current=dart_period_name(row.get('thstrm_nm'));previous=dart_period_name(row.get(column))
    expected=0 if quarter==4 else quarter
    return bool(current and previous and current[0]-previous[0]==offset and previous[1]==expected and
                current[1]==(0 if report['quarter']==4 else report['quarter']))

def latest_dart_fact(target, key, cell):
    """Latest filing wins for the actual period; preserve replaced values as evidence."""
    old=target.get(key)
    rank=lambda value:(value.get('filed') or '',value.get('receipt') or '')
    if old and old.get('conflicting_values') and rank(cell)==rank(old):
        target[key]=dict(old,conflicting_values=sorted(set(old['conflicting_values']+[cell['value']])-{None}));return
    if old is None or (old['value'] is None and cell['value'] is not None):target[key]=cell;return
    if cell['value'] is None:return
    if rank(cell)<rank(old):return
    if rank(cell)==rank(old) and cell['value']!=old['value']:
        target[key]=dict(cell,value=None,status='unavailable',reason='동일 공시·회계기간 계정값 충돌',
                         conflicting_values=[old['value'],cell['value']]);return
    if cell['value']!=old['value']:
        cell=dict(cell,previous_disclosures=old.get('previous_disclosures',[])+[
            {k:old.get(k) for k in ('value','filed','receipt','sources','report_column','report_year')}],
            comparison_note='같은 회계기간의 최신 공시 비교값 채택. 과거 제출값과 다르며 재작성·정정 효과를 포함할 수 있습니다.')
    elif old.get('previous_disclosures'):
        cell=dict(cell,previous_disclosures=old['previous_disclosures'],comparison_note=old.get('comparison_note'))
    target[key]=cell

def dart_share_values(report, corp_code):
    rows=report.get('shares',{}).get('list',[]);end=report['end']
    if any(r.get('corp_code') not in (None,corp_code) for r in rows):raise ValueError('DART_SHARES_IDENTITY_MISMATCH')
    clean=lambda value:re.sub(r'\s+','',str(value or ''))
    aliases={'common':COMMON_STOCK,
             'preferred':{'우선주','우선주식','의결권없는우선주','의결권없는우선주식'}}
    values={}
    for kind,labels in aliases.items():
        selected=[r for r in rows if clean(r.get('se')) in labels and r.get('stlm_dt')==end]
        signatures={(r.get('distb_stock_co'),r.get('istc_totqy'),r.get('tesstk_co'),r.get('rcept_no')) for r in selected}
        row=selected[0] if len(signatures)==1 else {}
        receipt=row.get('rcept_no');filed=f'{receipt[:4]}-{receipt[4:6]}-{receipt[6:8]}' if receipt else None
        fields=[('shares','distb_stock_co'),('issued_shares','istc_totqy'),('treasury_shares','tesstk_co')] if kind=='common' else [('preferred_shares','distb_stock_co')]
        for key,field in fields:
            values[key]=fact(row.get(field),unit='shares',end=end,filed=filed,sources=[report['prefix']+'-shares.json'],
                tag=field,receipt=receipt,account=row.get('se'),formula='공시 보통주 유통주식수' if key=='shares' else '주식종류별 공시 원값',
                reason=None if row else '해당 기말의 명시 보통주/우선주 종류별 수량 미확보',status=None if row else 'unavailable',stock_class_present=bool(selected))
        if kind=='common' and all(values[k]['value'] is not None for k in ('shares','issued_shares','treasury_shares')):
            if values['shares']['value']!=values['issued_shares']['value']-values['treasury_shares']['value']:
                values['shares'].update(value=None,status='unavailable',reason='공시 유통주식수와 발행주식수−자기주식 불일치')
    common,preferred=values['shares'],values['preferred_shares']
    totals=[r for r in rows if clean(r.get('se'))=='합계' and r.get('stlm_dt')==end]
    signatures={(r.get('distb_stock_co'),r.get('rcept_no')) for r in totals}
    total=totals[0] if len(signatures)==1 else {}
    # A dash stays unknown unless the same filing's total proves no non-common shares remain.
    if (preferred.get('stock_class_present') and preferred['value'] is None and
            common['value'] is not None and common['value']>=0 and common.get('receipt') and
            common['receipt']==preferred.get('receipt')==total.get('rcept_no') and
            number(total.get('distb_stock_co'))==common['value']):
        total_fact=fact(total['distb_stock_co'],unit='shares',end=end,filed=common.get('filed'),
            sources=common['sources'],formula='공시 합계 유통주식수')
        derived=calc('동일 공시·동일 기말 합계 유통주식수 − 보통주 유통주식수',
            [total_fact,common],lambda a,b:a-b,'shares')
        values['preferred_shares']=dict(preferred,**derived,status='derived',
            reported_value=next(r.get('distb_stock_co') for r in rows
                if clean(r.get('se')) in aliases['preferred'] and r.get('stlm_dt')==end),
            comparison_note='우선주 원공시 수량은 미기재. 같은 공시의 합계와 보통주 유통주식수가 같아 잔여 수량 0을 확인했습니다.')
    return values

def dart_normalize(source):
    company=source['identity']; annual_cells={};quarter_cells={};cumulative={};share_history=[];warnings=[]
    cutoff=date.today().year-6; explicit_quarters=set();bank=False
    def put(store,period,key,cell):latest_dart_fact(store.setdefault(period,{}),key,cell)
    def select(rows,key):
        sj,ids,names=DART_MAP[key]
        candidates=[r for r in rows if r.get('sj_div') in (('IS','CIS') if sj=='IS' else (sj,)) and r.get('currency')=='KRW']
        selected=[]
        for field,aliases in (('account_id',ids),('account_nm',names)):
            for alias in aliases:
                selected=[r for r in candidates if r.get(field)==alias]
                if selected:break
            if selected:break
        if key=='cash' and not any(number(r.get('thstrm_amount')) is not None for r in selected):
            candidates=[r for r in rows if r.get('sj_div')=='CF' and r.get('currency')=='KRW']
            selected=[r for r in candidates if r.get('account_id') in ('ifrs-full_CashAndCashEquivalentsAtEndOfPeriod','dart_CashAndCashEquivalentsAtEndOfPeriod','dart_CashAndCashEquivalentsAtEndOfPeriodCf')]
            if not selected:
                selected=[r for r in candidates if re.fullmatch(r'(기말|반기말|분기말|연도말|보고기간말)(의)?현금및현금성자산',re.sub(r'\s+','',r.get('account_nm','')))]
        return selected
    for report in sorted(source['reports'],key=lambda r:(r['year'],r['quarter'])):
        y,q,end=report['year'],report['quarter'],report['end'];prefix=report['prefix']
        shares=dart_share_values(report,company['corp_code'])
        share_history.append(dict(date=end,values=shares))
        if report['fs'].get('status')!='000':continue
        explicit_quarters.add((y,q));rows=report['fs']['list']
        if any(r.get('corp_code')!=company['corp_code'] for r in rows):raise ValueError('DART_IDENTITY_MISMATCH')
        revenue=select_dart_revenue(rows)
        bank=bank or revenue.get('basis')=='bank_net_revenue' or any(r.get('account_id') in ('ifrs-full_InterestRevenueExpense','ifrs_InterestRevenueExpense') and r.get('sj_div') in ('IS','CIS') for r in rows)
        for key,(sj,_,_) in DART_MAP.items():
            selected=([revenue['row']] if revenue.get('row') else []) if key=='sales' else select(rows,key)
            row=selected[0] if selected else {};metadata=revenue_metadata(revenue) if key=='sales' else {}
            instant=sj=='BS';unit='KRW/share' if 'eps' in key else 'KRW'
            def cell(column,target_y,target_q,*,cumulative_value=False):
                target_end=f'{target_y}-{target_q*3:02d}-'+('31' if target_q in (1,4) else '30')
                receipt=row.get('rcept_no');filed=f'{receipt[:4]}-{receipt[4:6]}-{receipt[6:8]}' if receipt else None
                vals={str(r.get(column)) for r in selected}
                amount=row.get(column) if len(vals)==1 else None
                start=None if instant else f'{target_y}-01-01' if target_q==4 or sj=='CF' or cumulative_value else f'{target_y}-{(target_q-1)*3+1:02d}-01'
                return fact(amount,unit=unit,end=target_end,start=start,filed=filed,sources=[prefix+'-statements.json'],
                    tag=row.get('account_id'),account=row.get('account_nm'),receipt=receipt,report_year=y,report_column=column,
                    period_name=row.get(column.replace('_amount','_nm').replace('_add_nm','_nm')),
                    comparative=target_y!=y,formula='공시 비교기간 원값' if target_y!=y else '공시 원값',**metadata)
            current=cell('thstrm_amount',y,q)
            put(annual_cells if q==4 else quarter_cells,y if q==4 else (y,q),key,current)
            if not instant:
                put(cumulative,(y,q),key,cell('thstrm_add_amount' if sj=='IS' and q in (2,3) else 'thstrm_amount',y,q,cumulative_value=True))
            if key=='sales' and row.get('account_id')=='derived_BankNetRevenue':
                # This helper sums only current/current-YTD components; inherited comparison fields are not totals.
                continue
            # Annual comparisons are annual amounts, never the current report year's values.
            if q==4:
                for offset,column in ((1,'frmtrm'),(2,'bfefrmtrm')):
                    if y-offset>=cutoff and dart_comparative_period(row,report,column+'_nm',offset,4):
                        put(annual_cells,y-offset,key,cell(column+'_amount',y-offset,4))
                        if not instant:put(cumulative,(y-offset,4),key,cell(column+'_amount',y-offset,4,cumulative_value=True))
            elif y-1>=cutoff:
                if instant and row.get('sj_div')=='BS' and dart_comparative_period(row,report,'frmtrm_nm',1,4):
                    put(annual_cells,y-1,key,cell('frmtrm_amount',y-1,4))
                elif dart_comparative_period(row,report,'frmtrm_q_nm',1,q):
                    put(quarter_cells,(y-1,q),key,cell('frmtrm_q_amount',y-1,q))
                    if not instant:
                        put(cumulative,(y-1,q),key,cell('frmtrm_add_amount' if sj=='IS' and q in (2,3) else 'frmtrm_q_amount',y-1,q,cumulative_value=True))
    for candidate in annual_supplement_candidates(source):
        if candidate['fy']<cutoff:continue
        key=candidate['key'];cell=dict(candidate);cell.pop('key');fy=cell.pop('fy')
        cell=fact(**cell)
        put(annual_cells,fy,key,cell)
    warnings.extend(source.get('annual_supplement_errors',[]))
    shares_by_end={row['date']:row['values'] for row in share_history}
    core={'sales','income','operating','eps','basic_eps','reported_eps'}
    annual_cells={y:v for y,v in annual_cells.items() if any(v.get(k,{}).get('value') is not None for k in core)}
    for (y,q),values in list(quarter_cells.items()):
        for key,(sj,_,_) in DART_MAP.items():
            if sj=='CF' and q>1:
                values[key]=calc('당기 누적 현금흐름 − 직전 분기 누적',[cumulative.get((y,q),{}).get(key),cumulative.get((y,q-1),{}).get(key)],lambda a,b:a-b,'KRW')
                values[key]['start']=f'{y}-{(q-1)*3+1:02d}-01'
    for y,annual_values in annual_cells.items():
        if (y,4) not in explicit_quarters and not any(cumulative.get((y,3),{}).get(k,{}).get('value') is not None and annual_values.get(k,{}).get('value') is not None for k in ('sales','income','operating')):continue
        values={k:dict(v) for k,v in annual_values.items() if k in DART_MAP and DART_MAP[k][0]=='BS'}
        for key,(sj,_,_) in DART_MAP.items():
            if sj=='BS':continue
            if 'eps' in key:
                values[key]=fact(unit='KRW/share',end=f'{y}-12-31',start=f'{y}-10-01',reason='4분기 EPS 별도 공시 미확보: 연간 EPS−9개월 EPS는 가중주식수 차이로 계산하지 않음')
            else:
                values[key]=(revenue_calc if key=='sales' else calc)('연간 실적 − 9개월 누적 실적',[annual_values.get(key),cumulative.get((y,3),{}).get(key)],lambda a,b:a-b,'KRW')
                values[key]['start']=f'{y}-10-01'
        quarter_cells[(y,4)]=values
    def finish(values,end):
        values=dict(values);values.update(shares_by_end.get(end,{}))
        values['debt']=calc('단기차입금 + 유동성장기부채 + 장기차입금 + 사채 (리스 별도)',[values.get(k) for k in ('debt_short','debt_current','debt_long','bonds')],lambda *a:sum(a),'KRW')
        return values
    annual=[dict(date=f'{y}-12-31',label=f'FY {y}',fy=y,fq=0,values=finish(v,f'{y}-12-31')) for y,v in sorted(annual_cells.items())]
    quarterly=[dict(date=(end:=f'{y}-{q*3:02d}-'+('31' if q in (1,4) else '30')),label=f'{y} Q{q}',fy=y,fq=q,values=finish(v,end)) for (y,q),v in sorted(quarter_cells.items())]
    return dict(provider='DART',name=company.get('corp_name'),standard='K-IFRS · 연결',currency='KRW',annual=annual,quarterly=quarterly,
                share_history=share_history,industry_model='bank' if bank else 'general',
                identity=dict(corp_code=company['corp_code'],symbol=company['stock_code']),warnings=warnings,
                limitations=['명시된 회계기간의 최신 공시 당기·비교값을 채택합니다. 과거 제출값과 재작성 차이가 있을 수 있습니다.',
                            '4분기 EPS는 별도 확정값 미확보 시 결측. 연간·누적 EPS 차감 금지.',
                            '보통주 유통주식수는 최신 확인 공시일을 따로 표시하며 EPS의 기간 가중평균주식수와 다릅니다.',
                            '배당 공시의 연결 EPS는 기본·희석 구분이 없어 별도 공시 EPS 계열로 보존합니다.'])
SEC_MAP = {
 'sales':['RevenueFromContractWithCustomerExcludingAssessedTax','Revenues','SalesRevenueNet'],
 'income':['NetIncomeLossAvailableToCommonStockholdersBasic','NetIncomeLoss'],
 'net_income':['NetIncomeLoss'], 'gross':['GrossProfit'], 'operating':['OperatingIncomeLoss'],
 'pretax':['IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest'],
 'tax':['IncomeTaxExpenseBenefit'], 'eps':['EarningsPerShareDiluted'], 'basic_eps':['EarningsPerShareBasic'],
 'assets':['Assets'], 'equity':['StockholdersEquity'], 'total_equity':['StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest','StockholdersEquity'],
 'cash':['CashAndCashEquivalentsAtCarryingValue'], 'current_assets':['AssetsCurrent'],
 'current_liabilities':['LiabilitiesCurrent'], 'inventory':['InventoryNet'], 'receivables':['AccountsReceivableNetCurrent'],
 'debt_short':['ShortTermBorrowings'], 'debt_current':['LongTermDebtCurrent'],
 'debt_long':['LongTermDebtNoncurrent'], 'debt_current_total':['DebtCurrent'],
 'cfo':['NetCashProvidedByUsedInOperatingActivities'], 'capex':['PaymentsToAcquirePropertyPlantAndEquipment','PaymentsToAcquireProductiveAssets'],
 'da':['DepreciationDepletionAndAmortization','DepreciationDepletionAndAmortizationPropertyPlantAndEquipment'],
 'shares':['CommonStockSharesOutstanding'], 'weighted_shares':['WeightedAverageNumberOfDilutedSharesOutstanding'],
 'dividend':['CommonStockDividendsPerShareDeclared','CommonStockDividendsPerShareCashPaid']}
INSTANT = {'assets','equity','total_equity','cash','current_assets','current_liabilities','inventory','receivables','debt_short','debt_current','debt_long','debt_current_total','shares'}

def days(start,end):
    return (date.fromisoformat(end)-date.fromisoformat(start)).days+1

def sec_normalize(source):
    data=source['facts']['facts']; identity=source['identity']
    if 'us-gaap' not in data:
        raise ValueError('SEC_IFRS_TAXONOMY_REQUIRES_MAPPING')
    annual={}; quarter={}; selected={}; cutoff=str(date.today().year-6)+'-01-01'
    for key,tags in SEC_MAP.items():
        unit='USD/shares' if key in ('eps','basic_eps','dividend') else 'shares' if 'shares' in key else 'USD'
        pool={}
        for priority,tag in enumerate(tags):
            for r in data['us-gaap'].get(tag,{}).get('units',{}).get(unit,[]):
                if r.get('form') not in ('10-K','10-Q','10-K/A','10-Q/A') or r.get('end','') < cutoff or r.get('filed','')>date.today().isoformat():
                    continue
                if key in INSTANT and r.get('start'): continue
                if key not in INSTANT and not r.get('start'): continue
                span=(r.get('start'),r['end'])
                rank=(r.get('filed',''),r.get('accn',''),-priority)
                if span not in pool or rank>pool[span][0]:
                    pool[span]=(rank,dict(r,tag=tag))
        selected[key]=[x[1] for x in pool.values()]
    selected['sales']=select_sec_revenue(data['us-gaap'],cutoff=cutoff,as_of=date.today().isoformat())
    for r in source.get('inline_shares', []):
        old = next((x for x in selected['shares'] if x['end'] == r['end']), None)
        if old is None or r['filed'] >= old['filed']:
            selected['shares'] = [x for x in selected['shares'] if x['end'] != r['end']] + [r]
    # Splits verified separately against official IR; no price-derived split guesses.
    splits=source.get('splits',[])
    def to_fact(r,key):
        unit='USD/share' if key in ('eps','basic_eps','dividend') else 'shares' if 'shares' in key else 'USD'
        value=r['val']; applied=[]
        if key in ('eps','basic_eps','dividend','weighted_shares','shares'):
            for event in splits:
                # Adopt latest filed restatements, then apply only splits effective after that filing.
                basis=r['filed']
                if basis<event['date']:
                    value=value*event['ratio'] if 'shares' in key else value/event['ratio']
                    applied.append(event)
        return fact(value,unit=unit,end=r['end'],start=r.get('start'),filed=r['filed'],sources=[r.get('source_id','companyfacts.json')],
                    tag=r['tag'],accession=r.get('accn'),form=r.get('form'),reported_value=r['val'],
                    formula='공시 원값'+(' · 공식 주식분할 조정' if applied else ''),splits=applied,**revenue_metadata(r))
    period_facts={'annual':{},'quarterly':{}}; warnings=[]
    def remember(frame,key,cell,direct):
        period_facts[frame].setdefault(cell['end'],{}).setdefault(key,[]).append((cell,direct))
    for key,rows in selected.items():
        if key in INSTANT: continue
        for r in rows:
            duration=days(r['start'],r['end'])
            frame='annual' if 330<=duration<=380 else 'quarterly' if 70<=duration<=105 else None
            if frame:
                remember(frame,key,to_fact(r,key),True)
        # Additive cash-flow and income fields only. EPS and weighted shares never subtract.
        if key not in ('eps','basic_eps','weighted_shares'):
            for r in rows:
                span=days(r['start'],r['end'])
                if not 150<=span<=380: continue
                prev=[p for p in rows if p['start']==r['start'] and 70<=days(p['end'],r['end'])-1<=105]
                if not prev: continue
                p=max(prev,key=lambda p:p['end'])
                cell=(revenue_calc if key=='sales' else calc)('동일 회계연도 누적 실적 − 직전 누적 실적',[to_fact(r,key),to_fact(p,key)],lambda a,b:a-b,to_fact(r,key)['unit'])
                cell['start']=(date.fromisoformat(p['end'])+timedelta(days=1)).isoformat()
                remember('quarterly',key,cell,False)
    def materialize(frame):
        periods=period_facts[frame]; target={}
        core=('sales','operating','income','net_income','eps','basic_eps')
        revenue_ends=[end for end,fields in periods.items() if fields.get('sales')]
        def rank(candidate):
            cell,direct=candidate
            # A directly reported quarter takes precedence over a cumulative difference.
            return (direct,cell.get('filed') or '',cell.get('accession') or '')
        for end,fields in sorted(periods.items()):
            anchor_key=next((key for key in core if fields.get(key)),None)
            if not anchor_key:
                continue
            if anchor_key!='sales':
                nearby=[other for other in revenue_ends if abs((date.fromisoformat(end)-date.fromisoformat(other)).days)<=20]
                if nearby:
                    warnings.append(f'SEC {frame} {end}: 가까운 매출 공시기간 {min(nearby)}과 겹치는 별도 손익기간 제외')
                    continue
            candidates=fields[anchor_key]; latest_rank=max(map(rank,candidates))
            starts={cell.get('start') for cell,direct in candidates if rank((cell,direct))==latest_rank}
            start=next(iter(starts)) if len(starts)==1 else None
            if start is None:
                warnings.append(f'SEC {frame} {end}: 동일 최신 공시의 시작일 충돌로 기간 실적 결측')
            values={}
            for key,options in fields.items():
                matching=[candidate for candidate in options if start and candidate[0].get('start')==start]
                if matching:
                    values[key]=max(matching,key=rank)[0]
                else:
                    exemplar=max(options,key=rank)[0]
                    values[key]=fact(unit=exemplar['unit'],start=start,end=end,
                        filed=exemplar.get('filed'),sources=exemplar.get('sources',[]),
                        reason='선택된 재무기간과 계정 시작일 불일치' if start else '동일 최신 공시의 재무기간 시작일 충돌',
                        period_start_candidates=sorted({cell.get('start') for cell,_ in options if cell.get('start')}),
                        **revenue_metadata(exemplar))
            target[end]=values
        return target
    annual=materialize('annual');quarter=materialize('quarterly')
    for ir in source.get('ir_eps', []):
        cell = to_fact(dict(ir, form='company IR', accn=None), 'eps')
        end = ir['end']
        starts={value.get('start') for value in quarter.get(end,{}).values() if value.get('start')}
        if len(starts)!=1:
            continue
        cell['start'] = next(iter(starts))
        if end in quarter and ('eps' not in quarter[end] or (quarter[end]['eps'].get('filed') or '') <= ir['filed']):
            quarter[end]['eps'] = cell
    for target in (annual,quarter):
        for end,values in target.items():
            for key in INSTANT:
                rs=[r for r in selected.get(key,[]) if r['end']==end]
                values[key]=to_fact(max(rs,key=lambda r:r['filed']),key) if rs else fact(unit='shares' if key=='shares' else 'USD',end=end)
            values.setdefault('eps',fact(unit='USD/share',end=end,reason='해당 3개월 희석 EPS 공시 태그 미확보; 누적 EPS 차감 금지'))
            if values.get('debt_current_total',{}).get('value') is not None:
                values['debt']=calc('공시 유동부채 중 이자부부채 + 비유동 장기부채',[values.get('debt_current_total'),values.get('debt_long')],lambda a,b:a+b,'USD')
            else:
                values['debt']=calc('단기차입금 + 유동성장기부채 + 비유동 장기부채',[values.get(k) for k in ('debt_short','debt_current','debt_long')],lambda *a:sum(a),'USD')
    def rows(target,kind):
        return [dict(date=e,label=('연간 ' if kind=='annual' else '분기 ')+e,fy=int(e[:4]),fq=None,values=v) for e,v in sorted(target.items())]
    return dict(provider='SEC',name=identity['name'],identity=dict(cik=str(identity['cik']),symbol=source.get('symbol')),currency='USD',standard='US GAAP · 연결',
                annual=rows(annual,'annual'),quarterly=rows(quarter,'quarterly'),warnings=warnings,
                guidance=source.get('guidance',[]),limitations=['회계기간 종료일 기준입니다. 기업의 회계연도는 달력연도와 다를 수 있습니다.',
                             'EPS는 연간/분기별로 기본·희석 중 하나의 직접 공시 계열을 사용. 4분기 직접 태그 미확보 시 결측.',
                             'SEC 동일 기간은 최신 제출 비교값 우선. 과거 시점 백테스트용 데이터가 아닙니다.',
                             '기말 유통주식수와 기간 가중평균 희석주식수는 구분합니다. 백만 주 단위 공시의 원자료 반올림 정밀도를 보존합니다.'],splits=splits)

def trailing(rows,key,index=None):
    end_index=len(rows) if index is None else index
    part=rows[max(0,end_index-4):end_index]
    unit=next((r['values'][key]['unit'] for r in part if key in r['values']), '')
    if len(part)!=4 or any(not 65<=days(a['date'],b['date'])-1<=110 for a,b in zip(part,part[1:])):
        return fact(unit=unit,reason='연속 4개 분기 미확보')
    cells=[r['values'].get(key) or fact() for r in part]
    for field in ('eps_basis','comparison_key'):
        bases={cell.get(field) for cell in cells}
        if len(bases)>1:
            return fact(unit=unit,reason='TTM 구성 분기의 EPS 또는 매출 기준 불일치')
    result=calc('연속 최근 4개 분기 합계 (TTM)',cells,lambda *v:sum(v),unit)
    for field in ('eps_basis','comparison_key','basis','account_label','revenue_note','revenue_components'):
        if cells[-1].get(field) is not None:
            result[field]=cells[-1][field]
    return result

def comparable_period(rows, current, years=1, quarter=False):
    """Match actual period ends, never a shifted array position after a missing filing."""
    expected=91 if quarter else years*365.25
    tolerance=25 if quarter else 20
    matches=[r for r in rows if r['date']<current['date'] and
             abs((date.fromisoformat(current['date'])-date.fromisoformat(r['date'])).days-expected)<=tolerance]
    return min(matches,key=lambda r:abs((date.fromisoformat(current['date'])-date.fromisoformat(r['date'])).days-expected)) if matches else None

def growth(current, previous, *, years=None):
    """Ratios require comparable units/bases and positive bases; losses get state labels."""
    a=current or fact(); b=previous or fact()
    formula='(현재 ÷ 비교기간 − 1)' if years is None else '(최근 연간 ÷ 과거 연간)^(1/연수) − 1'
    result=calc(formula,[a,b],lambda x,y:x/y-1 if years is None else (x/y)**(1/years)-1,'ratio',positive=(0,1) if years else (1,))
    result['kind']='percent'
    av,bv=a['value'],b['value']
    reason=None; label=None
    if av is None or bv is None:
        reason='비교할 공시 실적 미확보'; label='비교불가'
    elif a.get('unit')!=b.get('unit'):
        reason='비교기간 통화/단위 불일치'; label='비교불가'
    elif a.get('eps_basis')!=b.get('eps_basis'):
        reason='기본 EPS와 희석 EPS의 비교 금지'; label='비교불가'
    elif a.get('comparison_key')!=b.get('comparison_key'):
        reason='비교기간 매출 집계 기준 불일치'; label='비교불가'
    elif av<0<=bv:
        reason='양수 또는 0 실적에서 손실로 전환'; label='적자전환' if bv>0 else '비교불가'
    elif bv<0:
        reason='비교기간이 적자이므로 성장률을 계산하지 않음'
        label='흑자전환' if av>0 else '적자축소' if av>bv else '적자확대' if av<bv else '적자지속'
    elif bv==0 or (years and av<=0):
        reason='0 기저 또는 CAGR의 양수 조건 미충족'; label='비교불가'
    if label:
        result.update(value=None,reason=reason,display_text=label)
    if a.get('eps_basis'):
        result['eps_basis']=a['eps_basis']
    result.update(revenue_metadata(a))
    return result

def actual_periods(normal):
    """Choose one EPS basis per time series and attach same-period prior-year facts."""
    prepared=dict(normal); basis={}
    for frame in ('annual','quarterly'):
        rows=[dict(row,values=dict(row['values'])) for row in normal[frame]]
        rows.sort(key=lambda row:row['date'])
        visible=rows[-(3 if frame=='annual' else 8):]
        counts={key:sum(r['values'].get(key,{}).get('value') is not None for r in visible) for key in ('eps','basic_eps')}
        selected='basic_eps' if counts['basic_eps']>counts['eps'] else 'eps'
        basis[frame]='basic' if selected=='basic_eps' else 'diluted'
        for row in rows:
            raw=row['values'].get(selected) or fact(unit=normal['currency']+'/share',end=row['date'])
            row['values']['display_eps']=dict(raw,eps_basis=basis[frame])
        for row in rows:
            previous=comparable_period(rows,row)
            row['growth']={key:growth(row['values'].get('display_eps' if key=='eps' else key),
                previous['values'].get('display_eps' if key=='eps' else key) if previous else None)
                for key in ('sales','operating','income','eps')}
        prepared[frame]=rows
    prepared['eps_basis']=basis
    return prepared

def actual_ttm(rows, annual, key, index=None):
    """Use direct fiscal-year facts at year-end, otherwise require four adjacent quarters."""
    end_index=len(rows) if index is None else index
    if end_index<=0:
        return fact(reason='연속 4개 분기 미확보')
    current=rows[end_index-1]
    year=next((r for r in annual if r['date']==current['date']),None)
    eps_basis=current['values'].get('display_eps',{}).get('eps_basis') if key=='eps' else None
    source_key=('basic_eps' if eps_basis=='basic' else 'eps') if key=='eps' else key
    direct=year['values'].get(source_key) if year else None
    if direct and direct.get('value') is not None:
        result=dict(direct)
    else:
        result=trailing(rows,'display_eps' if key=='eps' else key,end_index)
    if eps_basis:
        result['eps_basis']=eps_basis
    return result

def summarize(normal, stock):
    normal=actual_periods(normal)
    annual=normal['annual']; quarters=normal['quarterly']; currency=normal['currency']
    if not quarters or not annual:
        raise ValueError('OFFICIAL_PERIODS_UNAVAILABLE')
    current=quarters[-1]; v=current['values']; year=annual[-1]; qrows=quarters
    bank=normal.get('industry_model')=='bank' or v.get('sales',{}).get('basis')=='bank_net_revenue'
    bank_inapplicable={'fcf','pfcf','ev','evsales','evebitda','current','quick','debt_equity','long_debt_equity','gross_margin','roic'}
    t={key:actual_ttm(qrows,annual,key) for key in ('sales','income','net_income','gross','operating','cfo','capex','eps','dividend','da')}
    eps_label={frame:('기본' if basis=='basic' else '희석')+' EPS' for frame,basis in normal['eps_basis'].items()}
    items=[]
    def add(id,label,cell,group='실적',kind=None):
        if bank and id in bank_inapplicable:
            cell=dict(cell,value=None,status='not_applicable',display_text='업종 비적용',
                      reason='은행은 예금·대출·금융자산 중심으로 운영되어 일반기업의 유동성·산업부채·FCF·EV·매출원가 기준 지표를 적용하지 않습니다.')
        items.append(dict(cell,id=id,label=label,group=group,kind=kind or cell.get('kind') or ('money' if cell['unit'].startswith(currency) else 'number')))
    def ratio(id,label,a,b,group='가치',percent=False):
        if b and b.get('basis') in ('bank_net_revenue','insurance_revenue'):
            label+=' ('+('순영업수익' if b['basis']=='bank_net_revenue' else '보험수익')+' 기준)'
        cell=calc(label,[a,b],lambda x,y:x/y,'ratio',positive=(1,))
        if b:
            cell.update(revenue_metadata(b))
        if b and b.get('eps_basis'):
            cell['eps_basis']=b['eps_basis']
        if bank and id=='pb':
            label='P/B · 지배주주자본 기준'
            cell.update(formula='보통주 시가총액 / 지배주주자본',equity_basis='parent_equity',
                        comparison_note='지배주주자본에는 신종자본증권 등 다른 자본성 항목이 포함될 수 있으며 보통주 귀속 순자산으로 조정한 P/B가 아닙니다.')
        if id in ('pe','pe_annual') and b and b.get('value') is not None and b['value']<=0:
            cell.update(display_text='적자' if b['value']<0 else '비교불가',reason='EPS가 음수여서 P/E를 계산하지 않음' if b['value']<0 else 'EPS가 0이어서 P/E를 계산하지 않음')
        add(id,label,cell,group,'percent' if percent else 'multiple')
    bars=[b for b in stock.get('bars',[]) if all(number(b.get(k)) is not None for k in ('close','high','low','volume'))]
    bars.sort(key=lambda b:b['date']); last=bars[-1] if bars else {}; price_date=last.get('date')
    def market(value,formula,unit=currency):
        return fact(value,unit=unit,end=price_date,sources=['saved-price.json'],formula=formula)
    price=market(last.get('close'),'저장된 완료 일봉 종가 · 공급자 수정주가')
    share_records=[r for r in normal.get('share_history',[]) if r['values'].get('shares',{}).get('value') is not None and (not price_date or r['date']<=price_date)]
    share_record=max(share_records,key=lambda r:r['date']) if share_records else None
    shares=share_record['values']['shares'] if share_record else v.get('shares',fact(unit='shares'))
    if shares['value'] is not None and shares.get('end')!=current['date']:
        shares=dict(shares,comparison_note='최신 수량이 아니라 가장 최근에 수량을 확인한 공시 기말입니다. 이후 주식수 변동을 추정하지 않습니다.')
    cap=calc('저장 종가 × 최근 공시 보통주 유통주식수',[price,shares],lambda a,b:a*b,currency)
    # Price predating financial balance cannot be presented as current valuation.
    if not price_date or price_date<current['date']:
        cap=fact(unit=currency,reason='가격 기준일이 최신 재무기간보다 이전')
        price_for_valuation=fact(unit=currency,reason='가격/재무 시점 불일치')
    else: price_for_valuation=price
    preferred_shares=share_record['values'].get('preferred_shares') if share_record else v.get('preferred_shares')
    multi_class=normal['provider']=='DART' and (preferred_shares or {}).get('value') is not None and preferred_shares['value']>0
    preferred_unknown=normal['provider']=='DART' and (preferred_shares or {}).get('stock_class_present') and preferred_shares.get('value') is None
    enterprise_cap=fact(unit=currency,reason='우선주 종류가 공시되었으나 수량이 미확보되어 전체 시가총액을 확정하지 않음') if preferred_unknown else fact(unit=currency,reason='우선주 시가총액·보통주 귀속 자본 미확보: 보통주만으로 전체 기업 배수 계산 불가') if multi_class else cap
    preferred_price=stock.get('preferred_price',{})
    pb=preferred_price.get('bar') or {}
    if multi_class and preferred_price.get('currency')==currency and pb.get('date')==price_date:
        pp=fact(pb.get('close'),unit=currency,end=price_date,sources=['saved-price.json'],formula='공식 종목 대응 삼성전자우 저장 종가')
        preferred_cap=calc('우선주 저장 종가 × 우선주 기말 유통주식수',[pp,preferred_shares],lambda a,b:a*b,currency)
        enterprise_cap=calc('보통주 시가총액 + 우선주 시가총액 (종류별 가격×유통주식수)',[cap,preferred_cap],lambda a,b:a+b,currency)
    fcf=calc('TTM 영업현금흐름 − TTM 공시 자본지출 (유형/생산자산 태그 기준, 금융상환 제외)',[t['cfo'],t['capex']],lambda a,b:a-b,currency)
    ev=calc('시가총액 + 이자부 부채 − 현금및현금성자산 (단기투자자산 제외)',[enterprise_cap,v.get('debt'),v.get('cash')],lambda a,b,c:a+b-c,currency)
    sales_label=t['sales'].get('account_label') or v.get('sales',{}).get('account_label') or '매출'
    for id,label,cell in [('sales',sales_label+' · TTM',t['sales']),('operating','영업이익 · TTM',t['operating']),('income','지배주주 순이익 · TTM',t['income']),('eps',eps_label['quarterly']+' · TTM',t['eps']),('annual_eps',eps_label['annual']+' · 최근 연간',year['values']['display_eps']),('latest_quarter_eps',eps_label['quarterly']+' · 최근 분기',v['display_eps']),('cfo','영업현금흐름 · TTM',t['cfo']),('fcf','FCF · 자체 산식 TTM',fcf)]:add(id,label,cell)
    add('market_cap','보통주 시가총액',cap,'가치');add('all_market_cap','전체 주식 시가총액',enterprise_cap,'가치');add('ev','기업가치 · EV',ev,'가치')
    ratio('pe','P/E · 실적 TTM',price_for_valuation,t['eps'])
    ratio('pe_annual','P/E · 최근 연간',price_for_valuation,year['values']['display_eps'])
    ratio('ps','P/S · 보통주시총 기준' if bank else 'P/S · TTM',cap if bank else enterprise_cap,t['sales']);ratio('pb','P/B',cap if bank else enterprise_cap,v.get('equity'))
    ratio('pc','P/Cash · 보통주시총 기준' if bank else 'P/Cash',cap if bank else enterprise_cap,v.get('cash'));ratio('pfcf','P/FCF',enterprise_cap,fcf)
    ratio('evsales','EV/Sales',ev,t['sales'])
    ebitda=calc('TTM 영업이익 + TTM 감가상각·무형자산상각',[t['operating'],t['da']],lambda a,b:a+b,currency)
    ratio('evebitda','EV/EBITDA',ev,ebitda)
    book=calc('지배주주자본 ÷ 보통주 유통주식수',[v.get('equity'),shares],lambda a,b:a/b,currency+'/share',positive=(1,)) if not multi_class else fact(unit=currency+'/share',reason='우선주 귀속 자본 분리 미확보')
    add('book','주당 순자산',book,'가치')
    add('cash_share','주당 현금',calc('현금 ÷ 보통주 유통주식수',[v.get('cash'),shares],lambda a,b:a/b,currency+'/share',positive=(1,)) if not multi_class else fact(unit=currency+'/share',reason='우선주 포함 현금 귀속 분리 미확보'),'가치')
    for id,label,num,den in [('gross_margin','매출총이익률',t['gross'],t['sales']),('operating_margin','영업이익률',t['operating'],t['sales']),('net_margin','순이익률',t['net_income'],t['sales'])]:ratio(id,label,num,den,'수익·재무',True)
    prior=next((r for r in reversed(qrows[:-1]) if 330<=days(r['date'],current['date'])-1<=380),None)
    for id,label,nkey,bkey in [('roa','ROA · 평균자산','net_income','assets'),('roe','ROE · 평균자본','income','equity')]:
        avg=calc('현재·전년동기 기말 평균',[v.get(bkey),prior['values'].get(bkey) if prior else None],lambda a,b:(a+b)/2,currency)
        ratio(id,label,t[nkey],avg,'수익·재무',True)
    ratio('current','유동비율',v.get('current_assets'),v.get('current_liabilities'),'수익·재무')
    quick=calc('현금 + 매출채권 (기타 당좌자산 제외)',[v.get('cash'),v.get('receivables')],lambda a,b:a+b,currency)
    ratio('quick','당좌비율 · 보수적',quick,v.get('current_liabilities'),'수익·재무')
    ratio('debt_equity','이자부부채/자본',v.get('debt'),v.get('equity'),'수익·재무')
    ratio('long_debt_equity','장기부채/자본',v.get('debt_long'),v.get('equity'),'수익·재무')
    add('shares','보통주 유통주식수 · 최근 확인',shares,'주식·배당','shares')
    dividend=t['dividend'];payout_eps=t['eps'];dividend_label='선언 주당배당 · TTM';yield_label='배당수익률 · TTM';payout_label='배당성향 · TTM EPS'
    if dividend['value'] is None:
        dividend_year=next((row for row in reversed(annual) if row['values'].get('dividend',{}).get('value') is not None and row['date']<=current['date']),None)
        if dividend_year:
            dividend=dict(dividend_year['values']['dividend'],comparison_note='확정된 최근 연간 주당배당입니다. TTM·예상 배당으로 환산하지 않습니다.')
            payout_eps=dividend_year['values'].get('display_eps')
            dividend_label='주당배당 · 최근 연간';yield_label='배당수익률 · 최근 연간';payout_label='배당성향 · 연간 EPS'
    add('dividend',dividend_label,dividend,'주식·배당')
    ratio('yield',yield_label,dividend,price_for_valuation,'주식·배당',True)
    ratio('payout',payout_label,dividend,payout_eps,'주식·배당',True)
    for key,label in [('sales',sales_label),('operating','영업이익'),('income','지배주주 순이익'),('eps','EPS')]:
        value_key='display_eps' if key=='eps' else key
        for lag,title in [(1,'직전분기 대비'),(4,'전년동기 대비')]:
            previous=comparable_period(qrows,current,quarter=lag==1)
            add(f'{key}_growth_{lag}',label+' '+title,growth(v.get(value_key),previous['values'].get(value_key) if previous else None),'성장','percent')
        previous=comparable_period(qrows,current)
        previous_ttm=actual_ttm(qrows,annual,key,qrows.index(previous)+1) if previous else None
        add(key+'_ttm_growth',label+' TTM 전년 대비',growth(t[key],previous_ttm),'성장','percent')
        for years in (3,5):
            previous=comparable_period(annual,year,years=years)
            cagr=growth(year['values'].get(value_key),previous['values'].get(value_key) if previous else None,years=years)
            cagr_label=label+f' {years}년 CAGR'
            if key=='eps' and (year['values'].get(value_key,{}).get('value') is None or not previous or previous['values'].get(value_key,{}).get('value') is None):
                latest_year=int(year['date'][:4]);reported={int(row['date'][:4]):row['values'].get('reported_eps') for row in annual}
                if all(reported.get(y) and reported[y]['value'] is not None for y in range(latest_year-years,latest_year+1)):
                    cagr=growth(reported[latest_year],reported[latest_year-years],years=years)
                    cagr_label=f'공시 EPS {years}년 CAGR'
                    cagr.update(eps_basis='reported',status='comparison_warning',
                        comparison_note='전기간 배당관련 공시의 연결 EPS라는 동일 원천 계열을 사용합니다. 기본·희석 구분은 명시되지 않았으며 기간 중 회계정책 변경·재작성 효과가 포함될 수 있습니다. XBRL 기본·희석 EPS와 혼합하지 않습니다.')
            add(f'{key}_cagr{years}',cagr_label,cagr,'성장','percent')
    add('price','저장 종가',price,'시세')
    add('previous_close','전일 종가',market(bars[-2]['close'] if len(bars)>1 else None,'직전 저장 완료봉 종가'),'시세')
    for n in (20,50,200):
        avg=market(sum(b['close'] for b in bars[-n:])/n if len(bars)>=n else None,f'{n}개 완료봉 종가 산술평균')
        add('sma'+str(n),f'SMA{n} 이격',calc('종가 / SMA − 1',[price,avg],lambda a,b:a/b-1,'ratio',positive=(1,)),'시세','percent')
    for n,label in [(1,'당일'),(5,'1주'),(21,'1개월'),(63,'3개월'),(126,'6개월'),(252,'1년'),(756,'3년'),(1260,'5년'),(2520,'10년')]:
        prev=market(bars[-n-1]['close'] if len(bars)>n else None,f'{n}거래봉 전 종가')
        add('perf'+str(n),label+' 수익률',calc('종가 / N거래봉 전 종가 − 1',[price,prev],lambda a,b:a/b-1,'ratio',positive=(1,)),'시세','percent')
    for key,fn in [('high52',max),('low52',min)]:
        add(key,'52주 '+('최고' if key=='high52' else '최저'),market(fn(b['high' if key=='high52' else 'low'] for b in bars[-252:]) if len(bars)>=252 else None,'최근 252 완료봉 범위'),'시세')
    add('volume','거래량',market(last.get('volume'),'최근 완료봉 거래량','shares'),'시세','shares')
    av=market(sum(b['volume'] for b in bars[-21:-1])/20 if len(bars)>=21 else None,'직전 20봉 평균 거래량','shares')
    add('avg_volume','평균거래량 · 20일',av,'시세','shares');ratio('relative_volume','상대거래량',market(last.get('volume'),'최근 완료봉 거래량','shares'),av,'시세')
    trs=[max(b['high']-b['low'],abs(b['high']-a['close']),abs(b['low']-a['close'])) for a,b in zip(bars,bars[1:])]
    atr=sum(trs[:14])/14 if len(trs)>=14 else None
    for tr in trs[14:]:atr=(atr*13+tr)/14
    add('atr','ATR · 14 Wilder',market(atr,'TR의 14기간 Wilder 평활'),'시세')
    changes=[b['close']-a['close'] for a,b in zip(bars,bars[1:])];rsi=None
    if len(changes)>=14:
        gain=sum(max(x,0) for x in changes[:14])/14;loss=sum(max(-x,0) for x in changes[:14])/14
        for x in changes[14:]:gain=(gain*13+max(x,0))/14;loss=(loss*13+max(-x,0))/14
        rsi=100-100/(1+gain/loss) if loss else 100 if gain else 50
    add('rsi','RSI · 14 Wilder',market(rsi,'상승/하락 종가 변화 14기간 Wilder','index'),'시세','number')
    for id,label,reason in [('float','유동주식수','비유동 보유분 자료 미확보'),('insider','내부자 지분/변화','내부자 소유권 공시 별도 집계 미구현'),('institution','기관 지분/변화','기관 보유보고서 별도 집계 미구현'),('short','공매도 잔고/비율','공식 공매도 잔고 원자료 미확보'),('beta','베타','동일 기간 시장 기준수익률 미연결'),('roic','ROIC','영업투하자본·세율 조정 근거 미확보')]:
        add(id,label,fact(reason=reason),'별도 자료','number')
    coverage=dict(annual_years=[int(row['date'][:4]) for row in annual],reported_eps_years=[int(row['date'][:4]) for row in annual if row['values'].get('reported_eps',{}).get('value') is not None],
                  quarterly_periods=[row['date'] for row in quarters],shares_latest_end=shares.get('end') if shares.get('value') is not None else None,
                  notes=['당기·전기·전전기 명시기간 확인 후 최신 공시값 채택','공시 EPS는 기본·희석 EPS와 별도 계열'] if normal['provider']=='DART' else [])
    return dict(schema=1,calculation_version=VERSION,**normal,summary=items,ttm=t,price_as_of=price_date,financial_as_of=current['date'],coverage_metadata=coverage,
                price_provider=stock.get('provider'))
