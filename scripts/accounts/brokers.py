"""Balance-only broker contracts. Pure normalization never authenticates or queries."""
from collections import defaultdict
from copy import deepcopy
from datetime import datetime
from decimal import Decimal, InvalidOperation, localcontext
import json


NH_KR_BASIS = {'bnc_bse_cd': '1', 'ltg_aot_dit_cd': '9', 'aet_bse': '1',
               'qut_dit_cd': 'UNT', 'aly_qut_cd': '1'}
NH_US_BASIS = {'qut_iqr_dit_cd': '1', 'fc_sec_trd_nat_cd': '200', 'cur_cd': 'USD', 'xns_dit_cd': '0'}
MERITZ_READS = {'holdings', 'ovs_holdings', 'deposit', 'ovs_currencies', 'valuation'}


def decimal(value, *, nonnegative=False, positive=False):
    if value is None or isinstance(value, bool) or str(value).strip() == '':
        raise ValueError('broker_number_missing')
    try:
        result = Decimal(str(value).strip())
    except InvalidOperation:
        raise ValueError('broker_number_invalid') from None
    if not result.is_finite() or (nonnegative and result < 0) or (positive and result <= 0):
        raise ValueError('broker_number_invalid')
    return result


def text_number(value, **kwargs):
    return format(decimal(value, **kwargs), 'f')


def body(response):
    """Unwrap official MCP content; prefer original JSON to avoid JS number loss.

    A transport may provide {body, headers, raw_json} without exposing auth headers.
    Existing official NH tools supply body only; that does not prove pagination.
    """
    if not isinstance(response, dict) or response.get('isError') or response.get('ok') is False:
        raise ValueError('broker_mcp_error')
    if isinstance(response.get('_nhplug_response'), dict):
        transport = response['_nhplug_response']
        result = body({'raw_json':transport['raw_json']})
        headers = {k.lower():v for k,v in transport.get('headers', {}).items()}
        result['_pagination'] = {'cts_flag':headers.get('cts_flag'), 'cts':headers.get('cts'),
                                 'request_cts':transport.get('request_cts')}
        return result
    if isinstance(response.get('raw_json'), str):
        result = json.loads(response['raw_json'], parse_float=str, parse_int=str)
    elif 'content' in response:
        if isinstance(response.get('structuredContent'), dict):
            def check_numbers(value):
                if isinstance(value, dict):
                    for child in value.values(): check_numbers(child)
                elif isinstance(value, list):
                    for child in value: check_numbers(child)
                elif isinstance(value,(int,float)) and not isinstance(value,bool):
                    if abs(value)>9007199254740991 or len(Decimal(str(value)).as_tuple().digits)>15:
                        raise ValueError('broker_numeric_precision_unconfirmed')
            if '_nhplug_response' not in response['structuredContent']:
                check_numbers(response['structuredContent'])
            return body(response['structuredContent'])
        blocks = [v.get('text') for v in response['content'] if v.get('type') == 'text']
        if len(blocks) != 1 or not isinstance(blocks[0], str):
            raise ValueError('broker_mcp_body_unconfirmed')
        parsed = json.loads(blocks[0], parse_float=str, parse_int=str)
        if isinstance(parsed, dict) and isinstance(parsed.get('_nhplug_response'), dict):
            return body(parsed)
        def exact_integer(value):
            if abs(int(value)) > 9007199254740991:
                raise ValueError('broker_numeric_precision_unconfirmed')
            return value
        def exact_fraction(value):
            if len(Decimal(value).as_tuple().digits) > 15:
                raise ValueError('broker_numeric_precision_unconfirmed')
            return value
        return body(json.loads(blocks[0], parse_float=exact_fraction, parse_int=exact_integer))
    elif isinstance(response.get('body'), dict):
        return body(response['body'])
    else:
        result = deepcopy(response)
    if not isinstance(result, dict):
        raise ValueError('broker_body_invalid')
    return result


def nh_success(response):
    nested = response.get('message')
    message = str(response.get('rsp_msg') or (nested.get('usr_msg') if isinstance(nested, dict) else '') or '').strip()
    if any(word in message for word in ('실패', '오류', '불가', '권한', '않', '실행할 수 없')):
        return False
    return message.lower() in ('ok', 'success') or any(
        word in message for word in ('정상', '완료', '조회되었습니다', '내역이 없습니다', '데이터가 존재하지 않습니다'))


def nh_requests(account):
    return {'KR': ('/krstock/inquiry/v1/balance', {'act_no': account, **NH_KR_BASIS}),
            'US': ('/gbstock/inquiry/v1/balance', {'act_no': account, **NH_US_BASIS})}


def nh_pages(path, inputs, reader, error_type=ValueError):
    """Require response-header evidence, including terminal N; never infer one page."""
    pages, seen, continuation = [], set(), None
    def fail(reason):
        error = error_type(reason)
        error.pages = deepcopy(pages)
        raise error
    for _ in range(100):
        try:
            response, headers = reader(path, inputs, continuation)
            page = body(response)
        except (error_type, ValueError):
            fail('balance_transport_or_body_unconfirmed')
        headers = {str(k).lower(): v for k, v in (headers if isinstance(headers,dict) else {}).items()}
        if not nh_success(page):
            fail('balance_business_response_unconfirmed')
        flag, cursor = headers.get('cts_flag'), headers.get('cts')
        page['_pagination'] = {'cts_flag': flag, 'cts': cursor,
                               'request_cts': (continuation or {}).get('cts')}
        pages.append(page)
        if flag == 'N':
            return pages
        if flag != 'Y':
            fail('pagination_completion_unconfirmed')
        if not isinstance(cursor, str) or not cursor or cursor in seen:
            fail('pagination_cursor_invalid')
        seen.add(cursor)
        continuation = {'cts': cursor, 'cts_flag': 'Y'}
    fail('pagination_limit')


def meritz_next(api_type, responses):
    """Return the exact next read parameters, or None only for a proved last page."""
    if api_type not in MERITZ_READS or not responses:
        raise ValueError('meritz_balance_scope_invalid')
    seen = set()
    for index, response in enumerate(responses):
        page = body(response)
        code, flag = str(page.get('rsp_cd', '')), page.get('tr_cont')
        if code not in ('5762', '5766', '5820', '5822', '0000'):
            raise ValueError('meritz_business_response_unconfirmed')
        if code in ('5820', '5822'):
            if page.get('data') not in (None, []):
                raise ValueError('meritz_empty_response_conflict')
            if flag not in (None, '0'):
                raise ValueError('meritz_pagination_conflict')
            next_params = None
        elif flag == '1' or code == '5762':
            if api_type not in ('holdings', 'ovs_holdings') or flag != '1':
                raise ValueError('meritz_pagination_conflict')
            cursor = page.get('tr_cont_key')
            if not isinstance(cursor, str) or not cursor or cursor in seen:
                raise ValueError('meritz_pagination_cursor_invalid')
            seen.add(cursor)
            next_params = {'tr_cont': '1', 'tr_cont_key': cursor}
        elif flag == '0' or code == '5766':
            next_params = None
        else:
            raise ValueError('meritz_pagination_completion_unconfirmed')
        if index < len(responses) - 1 and next_params is None:
            raise ValueError('meritz_pages_after_completion')
    return next_params


def _meritz_rows(api, responses, *, singleton=False, require_complete=True):
    if meritz_next(api, responses) is not None and require_complete:
        raise ValueError('meritz_pagination_incomplete')
    rows = []
    for response in responses:
        page = body(response)
        data = page.get('data')
        if data is None and str(page['rsp_cd']) in ('5820', '5822') and not singleton:
            data = []
        if singleton:
            if len(responses) != 1 or not isinstance(data, dict):
                raise ValueError('meritz_aggregate_shape_invalid')
            return data
        if not isinstance(data, list) or any(not isinstance(row, dict) for row in data):
            raise ValueError('meritz_holdings_shape_invalid')
        rows.extend(data)
    return rows


def _bundle(provider, observation):
    if (not isinstance(observation,dict) or not isinstance(observation.get('as_of'),str)
            or not isinstance(observation.get('accounts'),list)
            or any(not isinstance(a,dict) for a in observation['accounts'])):
        raise ValueError('broker_observation_shape_invalid')
    if datetime.fromisoformat(observation['as_of'].replace('Z','+00:00')).tzinfo is None:
        raise ValueError('broker_observation_shape_invalid')
    aliases = [a.get('alias') for a in observation['accounts']]
    if len(aliases) != len(set(aliases)):
        raise ValueError('broker_duplicate_account_alias')
    return {'schema_version': 1, 'source': 'broker_export', 'provider': provider,
            'as_of': observation['as_of'], 'max_age_hours': '24', 'base_currency': 'KRW',
            'fx_to_base': {'KRW': '1'}, 'accounts': [],
            'accounts_complete': observation.get('accounts_complete') is True,
            'authority': 'read_only_no_orders', 'limitations': [
                'KR_US_scope_not_all_assets', 'cash_and_liabilities_not_reconciled',
                'working_orders_not_reconciled', 'stops_not_adopted']}


def _account(alias, provider, as_of):
    if not isinstance(alias, str) or not alias.startswith(provider + '-') or len(alias) < len(provider) + 2:
        raise ValueError('broker_account_alias_invalid')
    return {'alias': alias, 'provider': provider, 'as_of': as_of, 'cash': {}, 'cash_complete': False,
            'positions': [], 'holdings_complete': True, 'query_markets': {},
            'source_ref': provider + ' balance', 'observed_deposits': {}, 'observed_balances': {}}


def _lots(positions):
    groups = defaultdict(list)
    for p in positions:
        groups[(p['market'], p['symbol'], p['currency'])].append(p)
    result = []
    for group in groups.values():
        first = deepcopy(group[0])
        lots = [p['broker_lot'] for p in group]
        first['lots'] = lots
        first.pop('broker_lot', None)
        if len(group) > 1:
            def identity(lot):
                if 'pros_trgt_cls_code' in lot:
                    fields = ('mksc_cls_name','loan_dtl_type_code','loan_date','rmnd_srno')
                elif 'ovrs_stck_symb_iscd' in lot:
                    fields = ('rmnd_clcd','loan_dtl_type_code','loan_date','loan_srno')
                else:
                    fields = ('tp_cd_nm','lon_byn_dt','xrn_dt','cfd_lon_cd_nm','lon_dt')
                return tuple(str(lot.get(k, '')) for k in fields)
            signatures = [identity(lot) for lot in lots]
            if len(signatures) != len(set(signatures)):
                first['data_unavailable_reason'] = 'duplicate_lot_or_page_unconfirmed'
            elif any(p.get('data_unavailable_reason') for p in group):
                first['data_unavailable_reason'] = 'lot_value_unconfirmed'
            else:
                with localcontext() as ctx:
                    ctx.prec = 50
                    quantity = sum(decimal(p['quantity']) for p in group)
                    first['quantity'] = format(quantity, 'f')
                    first['average_cost'] = (format(sum(decimal(p['quantity']) * decimal(p['average_cost'])
                                                       for p in group) / quantity, 'f') if quantity else '0')
                if len({p['price'] for p in group}) != 1:
                    first['data_unavailable_reason'] = 'lot_valuation_price_conflict'
        result.append(first)
    return result


def _position(row, market, symbol, currency, quantity, price, cost):
    p = {'market': market, 'symbol': symbol or 'unidentified', 'currency': currency,
         'price_basis': 'broker_last_price_observation', 'asset_type': 'stock',
         'broker_lot': deepcopy(row)}
    if market == 'US':
        p['quote_symbol'] = symbol.replace('.', '-')
        p['benchmark'] = '^GSPC'
    try:
        if not symbol or currency not in ('KRW', 'USD'):
            raise ValueError('instrument_unconfirmed')
        p.update(quantity=text_number(quantity, nonnegative=True),
                 price=text_number(price, positive=True), average_cost=text_number(cost, nonnegative=True))
    except (ValueError, TypeError):
        p['data_unavailable_reason'] = 'broker_holding_value_or_instrument_unconfirmed'
    return p


def normalize_nh(observation, listings=None):
    bundle = _bundle('NH PLUG', observation)
    rates = set()
    for account in observation['accounts']:
        if not isinstance(account.get('markets',{}),dict):
            raise ValueError('broker_markets_shape_invalid')
        a = _account(account['alias'], 'nh', observation['as_of'])
        bundle['accounts'].append(a)
        positions = []
        for market in ('KR', 'US'):
            section = account.get('markets', {}).get(market, {})
            pages = section.get('pages', [])
            valid = section.get('state') == 'received' and bool(pages)
            seen = set()
            for index, response in enumerate(pages):
                page = body(response)
                pagination = page.get('_pagination', {})
                flag, cursor = pagination.get('cts_flag'), pagination.get('cts')
                if index < len(pages)-1:
                    valid = valid and flag == 'Y' and bool(cursor) and cursor not in seen
                    valid = valid and body(pages[index+1]).get('_pagination', {}).get('request_cts') == cursor
                    seen.add(cursor)
                else:
                    valid = valid and flag == 'N'
                aggregate, rows = page.get('Output_0'), page.get('Output_1')
                if (not nh_success(page) or not isinstance(aggregate, dict) or not isinstance(rows, list)
                        or any(not isinstance(row,dict) for row in rows)):
                    valid = False
                    continue
                a['observed_balances'][market] = deepcopy(aggregate)
                a['observed_deposits'][market] = {k: aggregate[k] for k in (
                    'dca', 'nxt_dd_dca', 'nxt2_dd_dca', 'fc_dca', 'krw_dca') if k in aggregate}
                for row in rows:
                    symbol = str(row.get('iem_cd', '')).strip()
                    ccy = 'KRW' if market == 'KR' else str(row.get('cur_cd', '')).strip()
                    cost = row.get('phs_pr') if market == 'KR' else row.get('fc_avg_phs_pr')
                    p = _position(row, market, symbol, ccy,
                                  row.get('itg_bnc_qty' if market == 'KR' else 'cns_bse_bnc_qty'),
                                  row.get('now_pr' if market == 'KR' else 'fc_sec_end_pr'), cost)
                    p['name'] = str(row.get('iem_nm', '')).strip()
                    if market == 'KR':
                        p.update((listings or {}).get(symbol, {}))
                        if 'CMA' in p['name'] and '발행어음' in p['name']:
                            p.update(instrument_class_hint='CMA_note_from_broker_label', asset_type='cash_equivalent',
                                     data_unavailable_reason='NH_CMA_note_separate_valuation_required')
                            if row.get('eal_amt') is not None:
                                p['observed_evaluation_krw'] = text_number(row['eal_amt'])
                    else:
                        p['cost_basis'] = 'NH_fc_avg_phs_pr'
                        if row.get('fc_avg_phs_pr') is None:
                            p['data_unavailable_reason'] = 'NH_average_cost_basis_unconfirmed'
                            p['observed_purchase_unit_price'] = row.get('fc_phs_uit_pr')
                        if str(row.get('fc_sec_trd_nat_cd', '')).strip() != '200' or ccy != 'USD':
                            p['data_unavailable_reason'] = 'NH_overseas_scope_unconfirmed'
                        try:
                            rate = text_number(row.get('tdt_sby_bse_xcg_rt'), positive=True)
                            p['fx_to_base'] = rate
                            rates.add(rate)
                        except ValueError:
                            pass
                    positions.append(p)
            a['query_markets'][market] = {'state': 'received' if valid else 'unavailable', 'pages': len(pages),
                'reason': None if valid else section.get('reason', 'broker_response_or_pagination_unconfirmed')}
            a['holdings_complete'] = a['holdings_complete'] and valid
        a['positions'] = _lots(positions)
        if any(p.get('data_unavailable_reason') and p.get('asset_type') == 'stock' for p in a['positions']):
            a['holdings_complete'] = False
    if len(rates) == 1:
        bundle['fx_to_base']['USD'] = rates.pop()
        bundle['fx_observation'] = {'basis': 'NH_tdt_sby_bse_xcg_rt_broker_reference', 'as_of': observation['as_of']}
    return bundle


def normalize_meritz(observation, listings=None):
    bundle = _bundle('Meritz Open API', observation)
    for account in observation['accounts']:
        if not isinstance(account.get('responses',{}),dict):
            raise ValueError('broker_responses_shape_invalid')
        a = _account(account['alias'], 'meritz', observation['as_of'])
        bundle['accounts'].append(a)
        responses = account.get('responses', {})
        positions = []
        for api, market in (('holdings', 'KR'), ('ovs_holdings', 'US')):
            try:
                complete = meritz_next(api, responses.get(api, [])) is None
                rows = _meritz_rows(api, responses.get(api, []), require_complete=False)
                a['holdings_complete'] = a['holdings_complete'] and complete
                for row in rows:
                    if market == 'KR':
                        if row.get('pros_trgt_cls_code') == 'J':
                            continue  # Overseas uses its dedicated response exactly once.
                        if row.get('pros_trgt_cls_code') != '1' or row.get('mksc_cls_name') not in ('주식일반', '신용'):
                            a['holdings_complete'] = False
                            a.setdefault('unsupported_rows', []).append(deepcopy(row))
                            continue
                        code = str(row.get('iscd', ''))
                        symbol = code[1:] if len(code) == 7 and code.startswith('A') and code[1:].isdigit() else ''
                        p = _position(row, 'KR', symbol, 'KRW', row.get('rmnd_qty'), row.get('psnt_prc'), row.get('byng_unpr'))
                        p.update((listings or {}).get(symbol, {}))
                    else:
                        if row.get('ovrs_exch_vndr_idno') not in ('0537', '0321', '0066') or row.get('crcd') != 'USD':
                            a.setdefault('out_of_scope_rows', []).append(deepcopy(row))
                            continue
                        symbol = str(row.get('ovrs_stck_symb_iscd', '')).strip()
                        p = _position(row, 'US', symbol, row.get('crcd'), row.get('rmnd_dqty'), row.get('psnt_prc'), row.get('avrg_unpr'))
                        # Only known common-share punctuation maps to the saved quote key.
                        p['broker_symbol'] = row.get('iscd')
                        p['broker_exchange'] = row.get('ovrs_exch_vndr_idno')
                        p['observed_evaluation'] = row.get('frcr_vltn_amt')
                        p['observed_fx_rate'] = row.get('exrt')
                    p['name'] = str(row.get('isnm', '')).strip()
                    positions.append(p)
                a['query_markets'][market] = {'state': 'received' if complete else 'partial',
                    'pages': len(responses[api]), 'reason': None if complete else 'pagination_incomplete'}
            except (ValueError, KeyError, TypeError, json.JSONDecodeError):
                a['holdings_complete'] = False
                a['query_markets'][market] = {'state': 'unavailable', 'reason': 'broker_response_or_pagination_unconfirmed'}
        a['positions'] = _lots(positions)
        if any(p.get('data_unavailable_reason') for p in a['positions']):
            a['holdings_complete'] = False
        for api in ('deposit', 'valuation', 'ovs_currencies'):
            try:
                aggregate = _meritz_rows(api, responses.get(api, []), singleton=api != 'ovs_currencies')
                a['observed_balances'][api] = aggregate
                if api == 'deposit':
                    a['observed_deposits']['KR'] = {target: text_number(aggregate[source]) for target, source in (
                        ('dca', 'tfam'), ('nxt_dd_dca', 'd1_aft_tfam'), ('nxt2_dd_dca', 'd2_aft_tfam')) if source in aggregate}
                if api == 'ovs_currencies':
                    deposits = {}
                    for row in aggregate:
                        ccy = row.get('crcd')
                        if not isinstance(ccy,str) or len(ccy)!=3 or ccy in deposits:
                            raise ValueError('meritz_currency_scope_or_duplicate')
                        if ccy not in ('KRW','USD'):
                            a.setdefault('out_of_scope_currency_rows', []).append(deepcopy(row))
                            continue
                        deposits[ccy] = text_number(row.get('frcr_amt'))
                    a['observed_foreign_deposits'] = deposits
                    if 'USD' in deposits:
                        a['observed_deposits']['US'] = {'fc_dca': deposits['USD']}
                    for row in aggregate:
                        if row.get('crcd') == 'USD':
                            rate = decimal(row.get('vltn_exrt'), positive=True)
                            estimated = decimal(row.get('estm_frcr_amt'))
                            won = decimal(row.get('excl_wncr_vltn_amt'))
                            if abs(estimated * rate - won) > 1:
                                raise ValueError('meritz_fx_conversion_unconfirmed')
                            a['fx_to_base'] = {'KRW':'1','USD':format(rate, 'f')}
            except (ValueError, KeyError, TypeError, json.JSONDecodeError):
                a.setdefault('balance_limitations', []).append(api + '_unconfirmed')
                if api == 'ovs_currencies':
                    a.pop('fx_to_base', None)
    return bundle


def query_state(snapshot):
    return ('received' if snapshot.get('accounts_complete') and
            all(a.get('holdings_complete') for a in snapshot['accounts']) else 'partial')
