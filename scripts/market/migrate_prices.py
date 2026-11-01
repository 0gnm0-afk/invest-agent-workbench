"""Import audited archives and one saved daily source; never move/delete originals or update latest."""
if not __package__:
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import argparse
from contextlib import closing
import hashlib
import json
from pathlib import Path

from atomic_io import sha, load, write_json
from market.prices import PriceStore, default_path, freeze_snapshot, normalized, digest
from market.history.saved import SavedHistory
from workbench_paths import ROOT


def migrate(root, daily, output, *, path=None):
    root, daily, output = Path(root), Path(daily), Path(output)
    store = PriceStore(path or default_path(root))
    report = {'state':'running', 'database':str(store.path), 'archives':{}, 'daily':{}, 'originals_deleted':0}
    for market in ('KR','US'):
        archive = SavedHistory(root/'runtime'/f'{market.lower()}-history/active.json', market)
        if not archive.enabled or archive.error:
            raise ValueError('archive_not_verified:'+market)
        refs, received, count = {}, 0, 0
        for symbol in sorted(archive.listings):
            # Existing verifier checks pinned manifests, wrapped content, identity, quality and coverage.
            packet = archive.packet({'market':market, 'symbol':symbol})
            if packet['history']['state'] == 'failed':
                raise ValueError('archive_series_not_verified:'+market+':'+symbol)
            name = 'series/'+hashlib.sha256(symbol.encode()).hexdigest()+'.json'
            series = archive.read(name, wrapped=True)
            source = archive.root/name
            ref = store.put(series, source=source, source_sha256=sha(source), kind='archive',
                            allow_corrections=False)
            if normalized(store.read(ref)) != normalized(series['bars']):
                raise ValueError('archive_database_roundtrip_mismatch:'+symbol)
            refs[name] = [ref, {k:v for k,v in series.items() if k != 'bars'}]
            received += 1
            count += len(series['bars'])
            if received % 250 == 0:
                print(json.dumps({'market':market,'verified_series':received,'bars':count}), flush=True)
        store.set_meta('archive_'+market, {'source':str(archive.root), 'config':archive.config,
                       'listings':archive.listings,'coverage':archive.coverage,'references':refs,
                       'manifest_sha256':sha(archive.root/'manifest.json')})
        report['archives'][market] = {'series':received, 'bars':count}
    manifest = json.loads((daily/'manifest.json').read_text(encoding='utf-8-sig'))
    if sha(daily/'snapshot.json') != manifest.get('snapshot.json'):
        raise ValueError('daily_manifest_mismatch')
    snapshot = load(daily/'snapshot.json')
    # Corrected overlapping values get an old version and retain the archive reference.
    thin = freeze_snapshot(snapshot, daily/'snapshot.json', store, source_sha256=sha(daily/'snapshot.json'))
    for row in thin['series']:
        store.read(row['bars_ref'])
    output.mkdir(parents=True, exist_ok=True)
    write_json(output/'snapshot.json', thin)
    write_json(output/'manifest.json', {'snapshot.json':sha(output/'snapshot.json')})
    report['daily'] = {'source':str(daily), 'source_sha256':sha(daily/'snapshot.json'),
                       'series':len(snapshot['series']), 'reference_snapshot':str(output/'snapshot.json')}
    # Charts with selected histories are also operational price caches, not user drawings/analysis.
    legacy = root/'runtime/chart-history.sqlite3'
    chart_count = 0
    if legacy.exists():
        with closing(__import__('sqlite3').connect(legacy.resolve().as_uri()+'?mode=ro', uri=True)) as db:
            for key, body, expected in db.execute('SELECT identity,payload,sha256 FROM history'):
                if hashlib.sha256(body.encode()).hexdigest() != expected:
                    raise ValueError('selected_history_hash_mismatch')
                packet = json.loads(body)
                if 'series' in packet:
                    series = {**packet['series'], 'selected_history':packet['history']}
                    store.put(series, source=str(legacy)+':'+key, source_sha256=expected,
                              kind='selected', allow_corrections=False)
                    chart_count += 1
                else:
                    store.set_meta('legacy-selected:'+key,packet)
    store.set_meta('archive_import_complete', True)
    with closing(store.connect(write=True)) as db:
        counts = {t:db.execute('SELECT count(*) FROM '+t).fetchone()[0]
                  for t in ('prices','price_series','price_corrections','price_observations')}
        integrity = db.execute('PRAGMA quick_check').fetchone()[0]
        db.execute('PRAGMA wal_checkpoint(TRUNCATE)')
    report.update(state='verified' if integrity == 'ok' else 'failed', counts=counts,
                  selected_histories=chart_count, database_bytes=store.path.stat().st_size, integrity=integrity)
    store.set_meta('migration', report)
    write_json(output/'migration.json', report)
    write_json(output/'manifest.json', {name:sha(output/name) for name in ('snapshot.json','migration.json')})
    return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--daily', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--database', type=Path)
    args = parser.parse_args()
    print(json.dumps(migrate(ROOT,args.daily,args.output,path=args.database),ensure_ascii=True))
