"""C3 saved drawing interpretation. No capture, migration, refresh or monitor IO."""
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import sqlite3

from analysis import digest
from drawings.geometry import geometry, VERSION
from drawings.l1_trendlines import completed_daily_bars, l1_trendlines
from market.history.chart import aggregate
from market.prices import saved_bars, saved_price_context, _stock_key


def _verify_record(record, root):
    # Reuse only the pure serializer, never Collaboration/AnalysisService init.
    from analysis.collaboration import Collaboration
    path = (Path(root) / record['markdown_path']).resolve()
    if not path.is_relative_to(Path(root).resolve()):
        raise ValueError('record_path_outside_root')
    body = path.read_bytes()
    canonical = Collaboration.markdown(None, {k: v for k, v in record.items()
                                             if k not in ('markdown_path', 'markdown_sha256')})
    if (hashlib.sha256(body).hexdigest() != record['markdown_sha256']
            or hashlib.sha256(canonical.encode('utf-8')).hexdigest() != record['markdown_sha256']):
        raise ValueError('record_integrity_failed')


def saved_adopted_references(stock_key, *, root, connection):
    """Temporary M05 adapter: existing records, same C3 read transaction.

    Returns refs containing drawing_ref, identity, exact plan/snapshot IDs and
    immutable snapshot_drawing. It verifies stored snapshot/workspace hashes;
    it deliberately does not hydrate historical account observations. M05 can
    replace this callable with its verified reader using this small contract.
    """
    _stock_key(stock_key)
    result = dict(status='empty', source='saved_analysis_records_adapter', references=[],
                  limitations=['historical_hydrated_snapshot_hash_not_recomputed'])
    if connection is None:
        return dict(result, status='missing_database')
    try:
        tables = {r[0] for r in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not {'analysis_cases', 'analysis_items', 'analysis_snapshots'} <= tables:
            return dict(result, status='missing_table')
        cases = connection.execute('SELECT id,payload FROM analysis_cases WHERE stock_key=? ORDER BY rowid',
                                   (stock_key,)).fetchall()
        for cid, raw in cases:
            case = json.loads(raw)
            if case['id'] != cid or case['stock_key'] != stock_key:
                raise ValueError('analysis_identity_mismatch')
            rows = []
            for rid, kind, body in connection.execute(
                    "SELECT id,kind,payload FROM analysis_items WHERE case_id=? "
                    "AND kind IN ('plan_version','plan_decision') ORDER BY rowid", (cid,)):
                record = json.loads(body)
                if (record['id'] != rid or record['kind'] != kind or record['case_id'] != cid
                        or record['stock_key'] != stock_key):
                    raise ValueError('plan_identity_mismatch')
                rows.append(record)
            active, lookup = {}, {r['id']: r for r in rows}
            for r in rows:
                if r['kind'] != 'plan_decision':
                    continue
                _verify_record(r, root)
                if r['action'] == 'adopt':
                    active[r['plan_id']] = r
                elif r['action'] == 'revoke':
                    active.pop(r['plan_id'], None)
                else:
                    raise ValueError('invalid_plan_decision')
            for adoption in active.values():
                plan = lookup.get(adoption['plan_record_id'])
                if plan is None or plan['kind'] != 'plan_version':
                    raise ValueError('adopted_plan_missing')
                _verify_record(plan, root)
                if (plan['plan_id'] != adoption['plan_id']
                        or plan['plan_version'] != adoption['plan_version']
                        or digest(plan['plan']) != plan['plan_sha256']
                        or plan['plan_sha256'] != adoption['plan_sha256']):
                    raise ValueError('adopted_plan_integrity_failed')
                row = connection.execute('SELECT payload,sha256 FROM analysis_snapshots WHERE id=? AND case_id=?',
                                         (plan['snapshot_id'], cid)).fetchone()
                if row is None:
                    raise ValueError('adopted_snapshot_missing')
                snap = json.loads(row[0])
                if digest(snap) != row[1]:
                    raise ValueError('snapshot_integrity_failed')
                if (snap['id'] != plan['snapshot_id'] or snap['case_id'] != cid
                        or snap['stock_key'] != stock_key
                        or digest(snap['workspace']) != snap['component_hashes']['workspace']):
                    raise ValueError('snapshot_workspace_integrity_failed')
                seen = set()
                for condition in plan['plan']['conditions']:
                    ref = condition.get('drawing_ref')
                    if ref is None:
                        continue
                    signature = json.dumps(ref, sort_keys=True)
                    if signature in seen:
                        continue
                    seen.add(signature)
                    drawing = next((d for d in snap['workspace']['state']['drawings']
                                    if d['id'] == ref['drawing_id']), None)
                    if (drawing is None or ref['workspace_revision'] != snap['workspace']['revision']
                            or ref['timeframe'] != snap['chart']['timeframe']
                            or ref['price_basis'] != snap['chart'].get('price_basis')
                            or ref['geometry_version'] != VERSION):
                        raise ValueError('drawing_basis_mismatch')
                    result['references'].append(dict(
                        drawing_ref=deepcopy(ref), snapshot_drawing=deepcopy(drawing),
                        identity=deepcopy(plan['identity']), analysis_key=cid,
                        plan_record_id=plan['id'], plan_id=plan['plan_id'], plan_version=plan['plan_version'],
                        plan_sha256=plan['plan_sha256'], adoption_id=adoption['id'],
                        snapshot_id=plan['snapshot_id'], snapshot_sha256=plan['snapshot_sha256'],
                        stored_snapshot_sha256=row[1]))
        result['status'] = 'ok' if result['references'] else 'empty'
    except (KeyError, TypeError, ValueError, OSError, sqlite3.DatabaseError) as exc:
        result.update(status='data_error', references=[], reason=str(exc))
    return result


def _price(stock_key, root, now, price_reader, context_reader, sessions_reader):
    from invest_agent.trading.market_provider import completed_sessions
    result = dict(status='price_missing', basis='saved_latest_completed_daily_close',
                  date=None, close=None, currency=None, price_basis=None, bars_ref=None)
    try:
        market, symbol = _stock_key(stock_key)
        cutoff = (sessions_reader or completed_sessions)(market, now)[-1]
        contexts = context_reader(stock_key, root=root)
        packet = price_reader(stock_key, root=root, cutoff=cutoff)
        identity = packet['identity']
        if identity['market'] != market or identity['symbol'] != symbol:
            raise ValueError('price_identity_mismatch')
        if not identity.get('currency') or not identity.get('price_basis'):
            raise ValueError('price_basis_missing')
        reference = packet.get('bars_ref')
        if reference is None or reference.get('identity') != identity:
            raise ValueError('price_reference_identity_mismatch')
        bars = completed_daily_bars(packet['bars'], cutoff)
        if not bars:
            return result, packet, []
        latest = bars[-1]
        for context in contexts:
            metadata = context['metadata']
            if any(metadata.get(k) is not None and metadata[k] != identity[k]
                   for k in ('market', 'symbol', 'currency', 'price_basis')):
                raise ValueError('saved_price_context_identity_conflict')
            prior = context.get('last_completed_saved_bar')
            if prior and prior['day'] == latest['date'] and prior['close'] != latest['close']:
                raise ValueError('saved_price_context_changed_retry')
        result.update(status='ok', date=latest['date'], close=latest['close'],
                      currency=identity['currency'], price_basis=identity['price_basis'],
                      identity=identity, bars_ref=reference, completed_through=cutoff)
        return result, packet, bars
    except FileNotFoundError:
        return result, None, []
    except (IndexError, KeyError, TypeError, ValueError, sqlite3.DatabaseError) as exc:
        return dict(result, status='data_error', reason=str(exc)), None, []


def distance(close, line_value):
    """Return unavailable, never a fabricated zero, for missing/zero line values."""
    if any(type(v) not in (int, float) or not math.isfinite(v) for v in (close, line_value)):
        return dict(status='unavailable', reason='price_or_line_missing', value=None, percent=None)
    if line_value == 0:
        return dict(status='unavailable', reason='zero_line_value', value=None, percent=None)
    return dict(status='ok', value=close-line_value, percent=(close-line_value)/line_value*100)


def interpret_drawing(drawing, *, workspace_revision, workspace_saved_at, price, bars,
                      timeframe='1d', context=None, reference=None):
    """Pure six-kind geometry packet, retaining original coordinates and precision."""
    result = dict(drawing_id=drawing['id'], kind=drawing['kind'], coordinates=deepcopy(drawing['points']),
                  drawing=deepcopy(drawing), workspace_revision=workspace_revision,
                  workspace_saved_at=workspace_saved_at, timeframe=timeframe,
                  evaluate_at=price.get('date'), levels=[], limitations=[])
    if reference is not None:
        result['reference'] = deepcopy(reference)
    if timeframe not in ('1d', '1w', '1mo'):
        return dict(result, status='unavailable', reason='invalid_timeframe', summary='시간틀을 확인할 수 없습니다.')
    chart = dict(bars=aggregate(bars, timeframe), timeframe=timeframe, price_basis=price.get('price_basis'))
    item = geometry({'state': {'drawings': [drawing]}}, chart,
                    {**(context or {}), 'evaluate_at': price.get('date')})['drawings'][0]
    result.update(calculations=item['calculations'], limitations=item['limitations'], geometry_version=VERSION)
    reason = None
    if price['status'] != 'ok':
        reason = price.get('reason', 'saved_completed_price_missing')
    if reference:
        ident = reference.get('identity', {})
        if any(ident.get(k) is not None and ident[k] != price.get('identity', {}).get(k)
               for k in ('market', 'symbol', 'currency', 'price_basis')):
            reason = 'adopted_price_identity_mismatch'
        stable = ident.get('stable_id')
        if stable and price.get('identity', {}).get('isin') and stable != price['identity']['isin']:
            reason = 'adopted_price_identity_mismatch'
        if reference['drawing_ref'].get('price_basis') != price.get('price_basis'):
            reason = 'adopted_price_basis_mismatch'
    blocking = {'unsupported_price_transform', 'anchor_outside_supported_timeline', 'degenerate_projection'}
    if blocking.intersection(item['limitations']):
        reason = next(v for v in item['limitations'] if v in blocking)
    c, kind = item['calculations'], drawing['kind']
    names = {'trend': {'line': 'line_price'}, 'channel': {'line': 'line_price', 'other_line': 'other_line_price'},
             'horizontal': {'line': 'level'}, 'box': {'low': 'low', 'high': 'high'},
             'measure': {'low': 'low', 'high': 'high'},
             'riskreward': {'entry': 'entry', 'stop': 'stop', 'target': 'target'}}[kind]
    for name, key in names.items():
        value = c.get(key)
        delta = distance(price.get('close'), value) if reason is None else dict(
            status='unavailable', reason=reason, value=None, percent=None)
        result['levels'].append(dict(name=name, line_value=value, distance=delta['value'],
                                     distance_pct=delta['percent'], status=delta['status'], reason=delta.get('reason')))
    result['status'] = 'ok' if result['levels'] and all(v['status'] == 'ok' for v in result['levels']) else 'unavailable'
    if reason:
        result['reason'] = reason
    prefix = f"{drawing['id']} · {timeframe} · revision {workspace_revision} · 저장 시각 {workspace_saved_at or '없음'}"
    values = []
    for level in result['levels']:
        if level['status'] == 'ok':
            values.append(f"{level['name']} 선값 {level['line_value']:.10g}, 거리 {level['distance']:+.10g} ({level['distance_pct']:+.10g}%)")
        else:
            values.append(f"{level['name']} 계산 불가 ({level['reason']})")
    basis = f" · 저장 완료 일봉 종가 {price.get('close')} ({price.get('date')})"
    result['summary'] = prefix + basis + ' · ' + '; '.join(values)
    return result


def investment_drawings(stock_key, *, root, workspace_store, start=None, end=None,
                        timeframe='1d', now=None, price_reader=None, price_context_reader=None,
                        adopted_reader=None, sessions_reader=None, include_l1=True):
    """C3 service for M09/M10; M07 passes include_l1=False.

    Required root/store make every storage path explicit. Reader injection:
    price_reader = M03 saved_bars; price_context_reader = saved_price_context;
    adopted_reader(stock_key, *, root, connection) returns the adapter contract.
    No provider, writer, workflow/service constructor or monitoring call occurs.
    """
    _stock_key(stock_key)
    if timeframe not in ('1d', '1w', '1mo'):
        raise ValueError('invalid_timeframe')
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        raise ValueError('timezone_required')
    price, packet, bars = _price(stock_key, root, now, price_reader or saved_bars,
                                 price_context_reader or saved_price_context, sessions_reader)
    with workspace_store.read_transaction() as db:
        latest = workspace_store.saved_workspace(stock_key, connection=db)
        adopted = (adopted_reader or saved_adopted_references)(stock_key, root=root, connection=db)
        latest = {**latest, 'drawings': []}
        if latest['status'] in ('ok', 'empty'):
            for drawing in latest['payload']['drawings']:
                latest['drawings'].append(interpret_drawing(
                    drawing, workspace_revision=latest['workspace_revision'],
                    workspace_saved_at=latest['workspace_saved_at'], price=price, bars=bars,
                    timeframe=drawing.get('timeframe', latest['payload'].get('timeframe', '1d'))))
        adopted = {**adopted, 'drawings': []}
        for ref in adopted.get('references', []):
            basis = ref['drawing_ref']
            workspace = workspace_store.saved_workspace(stock_key, basis['workspace_revision'], connection=db)
            drawing = next((d for d in (workspace.get('payload') or {}).get('drawings', [])
                            if d['id'] == basis['drawing_id']), None)
            if workspace['status'] not in ('ok', 'empty') or drawing is None:
                reason = workspace['status'] if workspace['status'] not in ('ok', 'empty') else 'drawing_missing_at_revision'
                adopted['drawings'].append(dict(status='missing_reference', reason=reason, reference=ref,
                                                drawing_id=basis['drawing_id'], workspace_revision=basis['workspace_revision']))
            elif drawing != ref['snapshot_drawing']:
                adopted['drawings'].append(dict(status='data_error', reason='snapshot_drawing_mismatch', reference=ref))
            else:
                adopted['drawings'].append(interpret_drawing(
                    drawing, workspace_revision=workspace['workspace_revision'],
                    workspace_saved_at=workspace['workspace_saved_at'], price=price, bars=bars,
                    timeframe=basis['timeframe'], reference=ref))
    summaries = []
    for name, group in (('최신 저장 작도', latest), ('채택 계획 참조 작도', adopted)):
        if not group['drawings']:
            summaries.append(f"{name}: {group['status']}")
        for item in group['drawings']:
            summaries.append(name + ': ' + item.get('summary', item.get('reason', item['status'])))
    result = dict(stock_key=stock_key, price=price, latest=latest, adopted=adopted,
                  read_only=True, summary='\n'.join(summaries))
    if include_l1:
        if packet is None or price['status'] != 'ok':
            result['automatic_l1'] = dict(status='unavailable', reason=price.get('reason', price['status']),
                                         kind='automatic_l1_reference', stock_key=stock_key,
                                         window=dict(start=start, end=end, timeframe=timeframe), lines=[], stored=False, signal=False)
        else:
            result['automatic_l1'] = l1_trendlines(
                stock_key, root=root, start=start, end=end, timeframe=timeframe, now=now,
                bars_reader=lambda *args, **kwargs: packet, sessions_reader=sessions_reader)
    return result
