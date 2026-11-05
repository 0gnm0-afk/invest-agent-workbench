"""Lossless screen-record recipes over already pinned, read-only daily prices.

These recipes own only calculation metadata. No OHLCV values are written into
the records database or imported into the price database by this module.
"""
from contextlib import closing
from datetime import date, timedelta
import hashlib
import json
from pathlib import Path
import re
import zlib

from market.price_files import PriceFiles
from market.prices import PriceStore, default_path, FIELDS
from .paths import local_runtime_path

ENCODING = 'screen_prices_v1'
BAR_FIELDS = {'date', *FIELDS}


def _contains_prices(value):
    pending = [value]
    while pending:
        item = pending.pop()
        if isinstance(item, dict):
            if BAR_FIELDS <= set(item):
                return True
            pending.extend(item.values())
        elif isinstance(item, list):
            pending.extend(item)
    return False


def completed_weeks_v1(bars, last_session):
    """Frozen charts.completed_week_bars policy, including its holiday rule."""
    groups = {}
    for bar in bars:
        day = date.fromisoformat(bar['date'])
        monday = day - timedelta(days=day.weekday())
        groups.setdefault(monday, []).append(bar)
    result = []
    for monday, group in sorted(groups.items()):
        if monday + timedelta(days=4) > date.fromisoformat(last_session):
            continue
        result.append(dict(date=group[-1]['date'], open=group[0]['open'],
                           high=max(b['high'] for b in group), low=min(b['low'] for b in group),
                           close=group[-1]['close'], volume=sum(b['volume'] for b in group)))
    return result


def _json(raw):
    try:
        return json.loads(raw)
    except (UnicodeError, ValueError):
        return None


def _source(run_dir, root, needed=None):
    run_dir, root = Path(run_dir).resolve(), Path(root).resolve()
    run = json.loads((run_dir / 'run.json').read_bytes())
    source = (Path(run['market_source']) / 'snapshot.json').resolve()
    physical_source = local_runtime_path(source, root=root)
    approved_roots = [root / 'runtime']
    config_path = root / 'config.json'
    config = json.loads(config_path.read_bytes()) if config_path.is_file() else {}
    if config.get('mother'):
        approved_roots.append(Path(config['mother']).resolve() / 'runtime')
    if not any(physical_source.is_relative_to(parent) for parent in approved_roots):
        raise ValueError('record_price_source_outside_runtime')
    manifest = json.loads((run_dir / 'manifest.json').read_bytes())
    expected = manifest.get('input_hashes', {}).get(str(source))
    if not expected:
        raise ValueError('record_price_source_unpinned')
    price_store = PriceStore(default_path(root))
    files = PriceFiles(price_store)
    packed = files._record(source)
    if packed is None and physical_source != source:
        packed = files._record(physical_source)
    refs = {}
    if packed:
        source_sha, _, recipe_sha, compressed = packed
        body = zlib.decompress(compressed)
        if source_sha != expected or hashlib.sha256(body).hexdigest() != recipe_sha:
            raise ValueError('record_price_source_integrity_failed')
        recipe = json.loads(body)
        for patch in recipe['patches']:
            address = patch['path']
            if len(address) == 2 and address[0] == 'series':
                series = recipe['value']['series'][address[1]]
                key = (series['market'], series['symbol'])
                if key in refs:
                    raise ValueError('record_price_source_identity_ambiguous')
                refs[key] = patch['reference']
    else:
        raw = physical_source.read_bytes()
        if hashlib.sha256(raw).hexdigest() != expected:
            raise ValueError('record_price_source_integrity_failed')
        value = json.loads(raw)
        source_db = (value.get('ohlcv_store') or {}).get('path')
        if source_db and local_runtime_path(source_db, root=root).resolve() != price_store.path:
            raise ValueError('record_price_source_database_mismatch')
        for series in value.get('series', []):
            key = (series['market'], series['symbol'])
            if needed is not None and key not in needed:
                continue
            if key in refs:
                raise ValueError('record_price_source_identity_ambiguous')
            if 'bars_ref' in series:
                refs[key] = series['bars_ref']
            elif series.get('bars'):
                # Legacy protected sources may still be inline. Pin only an
                # exactly matching existing price view, never import prices.
                refs[key] = price_store.pin(series)
            else:
                raise ValueError('record_price_source_reference_missing')
    return price_store, refs, str(source), expected


def _layout(bars):
    """Only nonprice formatting facts; original bar numbers never enter recipes."""
    if not bars:
        return dict(order=[], integers=[], order_overrides=[])
    if any(not isinstance(b, dict) or set(b) != BAR_FIELDS for b in bars):
        raise ValueError('record_price_bar_shape_unsupported')
    if any(type(bar[key]) not in (int, float) and not (key == 'volume' and bar[key] is None)
           for bar in bars for key in FIELDS):
        raise ValueError('record_price_bar_value_unsupported')
    order = list(bars[0])
    return dict(order=order,
                integers=[[i, key] for i, bar in enumerate(bars) for key in FIELDS
                          if type(bar[key]) is int],
                order_overrides=[[i, list(bar)] for i, bar in enumerate(bars) if list(bar) != order])


def _format(bars, layout):
    orders = dict(layout['order_overrides'])
    integers = {tuple(item) for item in layout['integers']}
    for index, bar in enumerate(bars):
        for key in FIELDS:
            if (index, key) not in integers and bar[key] is not None:
                bar[key] = float(bar[key])
    for index, key in layout['integers']:
        value = bars[index][key]
        if value is None or int(value) != value:
            raise ValueError('record_price_integer_mismatch')
        bars[index][key] = int(value)
    return [{key: bar[key] for key in orders.get(i, layout['order'])} for i, bar in enumerate(bars)]


def _selected(store, reference, days, connection):
    wanted = set(days)
    bars = [bar for bar in store.read(reference, connection=connection) if bar['date'] in wanted]
    if [bar['date'] for bar in bars] != days:
        raise ValueError('record_price_dates_mismatch')
    return bars


def encode(role, raw, run_dir, root):
    value = _json(raw)
    if not _contains_prices(value):
        return None
    role = Path(str(role)).name
    if role not in ('screen.json', 'screen-base.json', 'screen', 'screen-base', 'screen_base'):
        raise ValueError('record_unhandled_price_array')
    if not isinstance(value, dict) or not isinstance(value.get('rows'), list):
        raise ValueError('record_price_screen_shape_unsupported')
    text = raw.decode('utf-8-sig')
    indentation = re.match(r'^\{\r?\n( +)"', text)
    encoder = dict(ensure_ascii=text.isascii(), indent=len(indentation[1]) if indentation else None,
                   allow_nan=False)
    if not indentation and re.match(r'^\{"[^"]+":(?! )', text):
        encoder['separators'] = (',', ':')
    suffix = text[len(text.rstrip()):].replace('\r\n', '\n')
    needed = {(row['market'], row['symbol']) for row in value['rows']
              if isinstance(row, dict) and isinstance(row.get('technical_context'), dict)
              and row['technical_context'].get('daily_bars')}
    store, refs, source, source_sha = _source(run_dir, root, needed)
    patches = []
    with closing(store.connect()) as db:
        for index, row in enumerate(value['rows']):
            context = row.get('technical_context') if isinstance(row, dict) else None
            if not isinstance(context, dict) or not context.get('daily_bars'):
                continue
            daily, weekly = context['daily_bars'], context.get('weekly_bars', [])
            reference = refs.get((row['market'], row['symbol']))
            if reference is None:
                raise ValueError('record_price_reference_missing')
            ident = reference['identity']
            if any(context.get(k) is not None and context[k] != ident[k]
                   for k in ('market', 'symbol', 'currency', 'price_basis', 'provider')):
                raise ValueError('record_price_identity_mismatch')
            days = [bar['date'] for bar in daily]
            layout, weekly_layout = _layout(daily), _layout(weekly)
            restored = _format(_selected(store, reference, days, db), layout)
            if restored != daily or completed_weeks_v1(restored, days[-1]) != weekly:
                raise ValueError('record_price_values_mismatch')
            patches.append(dict(row=index, reference=reference, days=days, daily=layout,
                                weekly=weekly_layout, weekly_policy='completed_weeks_v1'))
            context['daily_bars'] = []
            if 'weekly_bars' in context:
                context['weekly_bars'] = []
    if not patches or _contains_prices(value):
        raise ValueError('record_unhandled_price_array')
    recipe = dict(schema=1, value=value, patches=patches, price_database=str(store.path),
                  source=source, source_sha256=source_sha, encoder=encoder, suffix=suffix,
                  bom=raw.startswith(b'\xef\xbb\xbf'), crlf='\r\n' in text,
                  original_sha256=hashlib.sha256(raw).hexdigest(), original_bytes=len(raw))
    payload = json.dumps(recipe, ensure_ascii=False, separators=(',', ':'), allow_nan=False).encode('utf-8')
    if decode(ENCODING, payload, {}) != raw:
        raise ValueError('record_price_byte_roundtrip_mismatch')
    return dict(encoding=ENCODING, payload=payload)


def decode(encoding, payload, reference):
    if encoding != ENCODING:
        raise ValueError('record_price_encoding_unsupported')
    recipe = json.loads(payload)
    if recipe.get('schema') != 1 or _contains_prices(recipe):
        raise ValueError('record_price_recipe_invalid')
    store = PriceStore(local_runtime_path(recipe['price_database']))
    with closing(store.connect()) as db:
        for patch in recipe['patches']:
            if patch.get('weekly_policy') != 'completed_weeks_v1':
                raise ValueError('record_price_weekly_policy_unsupported')
            daily = _format(_selected(store, patch['reference'], patch['days'], db), patch['daily'])
            weekly = _format(completed_weeks_v1(daily, patch['days'][-1]), patch['weekly'])
            context = recipe['value']['rows'][patch['row']]['technical_context']
            context['daily_bars'] = daily
            if 'weekly_bars' in context:
                context['weekly_bars'] = weekly
    raw = PriceFiles._encode(recipe['value'], recipe)
    if len(raw) != recipe['original_bytes'] or hashlib.sha256(raw).hexdigest() != recipe['original_sha256']:
        raise ValueError('record_price_recovery_hash_mismatch')
    if reference:
        expected = reference.get('original_sha256')
        if expected and hashlib.sha256(raw).hexdigest() != expected:
            raise ValueError('record_price_reference_hash_mismatch')
    return raw
