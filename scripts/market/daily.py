"""One private market cycle: Toss collection, shared price references and analysis."""

if not __package__:
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import atomic_io as artifact_io
import argparse
import html
import json
import socket
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4
from zoneinfo import ZoneInfo

from invest_agent.trading.store import instance_lock
from market.collect import audit, run as collect
from atomic_io import write_json
import market.nh_fallback as nh_fallback

from workbench_paths import ROOT


def now():
    return datetime.now(timezone.utc).isoformat()


def code_hashes(engine):
    paths = list((ROOT/'scripts').rglob('*.py')) + list((ROOT/'scripts').rglob('*.json'))
    paths += list((engine/'src/invest_agent').rglob('*.py'))
    paths += [ROOT/'daily.ps1', ROOT/'config.json']
    return {str(p.resolve()):artifact_io.sha(p) for p in paths if p.is_file()}


def check_hashes(entries):
    for path, expected in entries.items():
        artifact_io.checked(Path(path), expected, {})


def source_check(root):
    manifest = artifact_io.load(root/'manifest.json')
    for name, expected in manifest.items():
        path = (root/name).resolve()
        if not path.is_relative_to(root.resolve()):
            raise ValueError('source_manifest_path_outside_root')
        artifact_io.checked(path, expected, {})


def saved_fallback_summary(fallback):
    result = {'attempted':fallback['attempted'], 'adopted':fallback['adopted'],
            'unrecovered':fallback.get('unrecovered', fallback['attempted']-fallback['adopted']-fallback.get('excluded',0))}
    if 'excluded' in fallback:
        result['excluded'] = fallback['excluded']
    return result


def render(run, output):
    """Publish collection status and quality without retired selection artifacts."""
    lines = ['# 일일 시장 수집', '', f"실행 상태: **{run['state']}** / 처리: {run['processing_state']}",
             f"모드: {run['mode']} / 시작: {run['started_at']} / 종료: {run.get('finished_at', '진행 중')}", '']
    if run.get('error'):
        lines += [f"오류: {run['error']}", '']
    lines += ['| 단계 | 상태 |', '|---|---|'] + [f'| {k} | {v} |' for k,v in run['stages'].items()]
    if run.get('quality'):
        q = run['quality']
        lines += ['', f"수신 {q['received']}/{q['requested']} · 일봉 실패 {q['failed']} · 품질 이슈 시계열 {q['quality_issue_series']}",
                  '[자료 품질 상세](quality.json)']
    (output/'report.md').write_text('\n'.join(lines)+'\n', encoding='utf-8')
    page = '<!doctype html><html lang="ko"><meta charset="utf-8"><title>일일 시장 수집</title><body><pre>'
    page += html.escape('\n'.join(lines)) + '</pre><a href="quality.json">자료 품질 상세</a></body></html>'
    (output/'report.html').write_text(page, encoding='utf-8')


def run(config, source=None, recover_source=None, nh_evidence=None, *, session_cutoff=None, evaluated_at=None, prior_result=None):
    date = datetime.now(ZoneInfo('Asia/Seoul')).date().isoformat()
    mode = 'replay' if source else 'recover' if recover_source else 'live'
    output = ROOT/'runtime'/date/(datetime.now(timezone.utc).strftime('%H%M%S')+'-'+mode+'-'+uuid4().hex[:8])
    output.mkdir(parents=True)
    run = {'schema':1, 'state':'running', 'processing_state':'running', 'mode':mode, 'started_at':now(),
           'output':str(output), 'stages':{s:'not_started' for s in ('inputs','collection','nh_fallback','adr_metadata','quality','report')}}
    if session_cutoff:
        run.update(session_cutoff=session_cutoff, evaluated_at=evaluated_at)
    started = time.monotonic()
    hashes, inputs = {}, {}
    stage = 'inputs'
    def update(name, value):
        run['stages'][name] = value
        write_json(output/'run.json', run)
        print(json.dumps({'stage':name,'state':value,'output':str(output)}, ensure_ascii=True), flush=True)
    try:
        hashes = code_hashes(Path(config['engine']))
        write_json(output/'config.json', config)
        update('inputs', 'completed')
        stage = 'collection'
        update(stage, 'running')
        if source or recover_source:
            source = Path(source or recover_source).resolve()
            source_check(source)
            update(stage, 'saved_source_verified_no_network' if mode == 'replay' else 'saved_toss_source_verified')
        else:
            collection_root = output/'collection'
            previous = None
            latest = ROOT/'runtime/latest.json'
            if latest.is_file():
                prior_run = Path(artifact_io.load(latest)['output'])/'run.json'
                if artifact_io.available(prior_run):
                    previous = artifact_io.load(prior_run).get('market_source')
            if prior_result:
                from launch.daily import DailyLaunch
                checked_prior = DailyLaunch(ROOT).checked_run(prior_result)
                previous = checked_prior.get('market_source')
                inputs[str(Path(prior_result)/'run.json')] = artifact_io.sha(Path(prior_result)/'run.json')
            kwargs = {'session_cutoff':session_cutoff,'evaluated_at':evaluated_at} if session_cutoff else {}
            collect(collection_root, ROOT/'config.json', prior_source=previous, **kwargs)
            frozen_path = collection_root/'frozen.json'
            seed_revision = artifact_io.load(frozen_path)['config'].get('seed_revision') if frozen_path.is_file() else None
            if seed_revision is not None:
                run['incremental_seed'] = {'provider':'toss','price_revision':seed_revision}
            elif previous:
                previous = Path(previous)
                inputs[str(previous/'snapshot.json')] = artifact_io.sha(previous/'snapshot.json')
                inputs[str(previous/'manifest.json')] = artifact_io.sha(previous/'manifest.json')
                run['incremental_seed'] = str(previous)
            source = Path(artifact_io.load(collection_root/'latest.json')['attempt'])
            update(stage, 'completed')
        if mode != 'replay' and config.get('ohlcv',{}).get('provider') == 'toss' and not nh_evidence:
            run['toss_source'] = str(source)
            run['nh_fallback'] = {'attempted':0, 'adopted':0, 'unrecovered':0,
                                  'reason':'single_provider_toss'}
            update('nh_fallback', 'not_requested_single_provider')
        elif mode != 'replay':
            original_source = source
            run['toss_source'] = str(original_source)
            source_check(original_source)
            inputs.update({str(original_source/name):h for name,h in artifact_io.load(original_source/'manifest.json').items()})
            inputs[str(original_source/'manifest.json')] = artifact_io.sha(original_source/'manifest.json')
            stage = 'nh_fallback'
            update(stage, 'running')
            previous = None
            latest = ROOT/'runtime/latest.json'
            if latest.is_file():
                prior_run = Path(artifact_io.load(latest)['output'])/'run.json'
                if artifact_io.available(prior_run):
                    previous = artifact_io.load(prior_run).get('market_source')
            if prior_result:
                from launch.daily import DailyLaunch
                previous = DailyLaunch(ROOT).checked_run(prior_result).get('market_source')
            source, fallback, fallback_inputs = nh_fallback.recover(
                original_source,output/'nh_fallback',config,
                evidence_root=nh_evidence,prior_source=previous)
            run['nh_fallback'] = {k:fallback[k] for k in ('attempted','adopted','unrecovered')}
            run['nh_fallback']['excluded'] = len(fallback.get('automatic_exclusions',[]))
            if not nh_evidence:
                import market.scope as market_scope
                market_scope.remember(fallback.get('automatic_exclusions',[]),market_scope.load())
            inputs.update(fallback_inputs)
            update(stage, 'completed' if fallback['unrecovered'] == 0 else 'completed_with_exceptions')
        else:
            update('nh_fallback', 'not_requested_offline')
        if mode != 'replay' and not nh_evidence and config.get('adr_metadata',{}).get('enabled'):
            import market.adr as adr_metadata
            stage = 'adr_metadata'
            update(stage, 'running')
            inputs.update({str(source/name):h for name,h in artifact_io.load(source/'manifest.json').items()})
            inputs[str(source/'manifest.json')] = artifact_io.sha(source/'manifest.json')
            reference = adr_metadata.collect(artifact_io.load(source/'snapshot.json'), output/'adr-evidence')
            source = adr_metadata.derive(source, output/'adr-market', reference)
            records = adr_metadata.read(reference['path'])['records']
            run['adr_metadata'] = {'requested':len(records),'available':sum(r['state']=='available' for r in records)}
            update(stage, 'completed' if all(r['state']=='available' for r in records) else 'completed_with_exceptions')
        else:
            update('adr_metadata', 'not_requested_offline' if mode == 'replay' or nh_evidence else 'disabled')
        run['market_source'] = str(source)
        inputs.update({str(source/name):h for name,h in artifact_io.load(source/'manifest.json').items()})
        inputs[str(source/'manifest.json')] = artifact_io.sha(source/'manifest.json')
        stage = 'quality'
        update(stage, 'running')
        snapshot = artifact_io.load(source/'snapshot.json')
        if mode == 'replay' and snapshot.get('nh_fallback'):
            run['nh_fallback'] = saved_fallback_summary(snapshot['nh_fallback'])
            run['nh_fallback']['provenance'] = 'saved_source_no_new_requests'
        quality = audit(snapshot)
        write_json(output/'quality.json', quality)
        from market.exclusions import inventory
        write_json(output/'market-failures.json', inventory(snapshot,quality))
        run['quality'] = {k:quality[k] for k in ('state','processing_state','data_complete','quality_issue_series','unexpected_failures')}
        run['quality'].update({k:snapshot['collection_stats'][k] for k in ('requested','received','failed')})
        run['quality']['markets'] = {
            market: {field: sum(row[field] for exchange, row in quality['exchanges'].items()
                               if (exchange == 'KOSPI') == (market == 'KR'))
                     for field in ('requested', 'received', 'failed')}
            for market in ('KR', 'US')}
        run['data_times'] = {k:snapshot.get(k) for k in ('fetched_at','collected_until')}
        run['data_times']['session_dates'] = {m: dates[-1] for m, dates in snapshot['sessions'].items()}
        update(stage, quality['processing_state'])
        run['approved_exclusions'] = snapshot.get('scope_exclusions', [])
        del snapshot
        incomplete = not quality['data_complete']
        run['state'] = 'failed' if quality['unexpected_failures'] else 'partial' if incomplete else 'succeeded'
        run['processing_state'] = 'failed' if quality['unexpected_failures'] else 'completed_with_exceptions' if incomplete else 'completed'
        stage = 'report'
        update(stage, 'completed')
    except (Exception, SystemExit, KeyboardInterrupt) as exc:
        run.update(state='failed', processing_state='failed', error=type(exc).__name__+': '+str(exc))
        update(stage, 'failed')
    finally:
        run.update(finished_at=now(), elapsed_seconds=round(time.monotonic()-started,3))
        render(run, output)
        write_json(output/'run.json', run)
        write_json(output/'manifest.json', {'schema':1, 'code_hashes':hashes, 'input_hashes':inputs,
                   'output_hashes':{str(p.relative_to(output)):artifact_io.sha(p) for p in output.rglob('*')
                                    if p.is_file() and 'cache' not in p.relative_to(output).parts}})
        # A run is visible as latest only after its immutable records are durable.
        # Existing migrated runs retain their original files; only this new run
        # replaces freshly generated structured outputs with pinned references.
        from records.store import publish_run
        from records.files import publish_thin
        publish_run(output, root=ROOT)
        publish_thin(output, root=ROOT)
        write_json(ROOT/'runtime/latest.json', {'output':str(output), 'state':run['state'], 'mode':mode})
    return run


def verify(output):
    manifest = artifact_io.load(output/'manifest.json')
    check_hashes(manifest['input_hashes'])
    check_hashes(manifest['code_hashes'])
    check_hashes({str(output/p):h for p,h in manifest['output_hashes'].items()})
    run = artifact_io.load(output/'run.json')
    if run['stages'].get('quality') not in ('completed', 'completed_with_exceptions', 'failed') or not run.get('market_source'):
        return {'verified':True, 'state':run['state'], 'analysis_replayed':False}
    source = Path(run['market_source'])
    snapshot = artifact_io.load(source/'snapshot.json')
    if audit(snapshot) != artifact_io.load(output/'quality.json'):
        raise ValueError('quality_replay_mismatch')
    return {'verified':True, 'state':run['state'], 'analysis_replayed':False, 'network_calls':0}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, help='Offline replay of an immutable market attempt')
    parser.add_argument('--verify', type=Path, help='Offline checksum and calculation replay')
    parser.add_argument('--recover', type=Path, help='New run from a saved Toss attempt with NH fallback')
    parser.add_argument('--nh-evidence', type=Path, help='Previously captured NH requests for --recover')
    parser.add_argument('--session-cutoff', help='Dashboard-only latest permitted session date')
    parser.add_argument('--evaluated-at', help='Frozen timezone-aware first launch timestamp')
    parser.add_argument('--prior-run', type=Path, help='Verified prior result for incremental seed after a failed attempt')
    args = parser.parse_args()
    if sum(bool(x) for x in (args.source,args.verify,args.recover)) > 1:
        parser.error('choose source, verify, or recover')
    if args.nh_evidence and not args.recover:
        parser.error('--nh-evidence requires --recover')
    if bool(args.session_cutoff) != bool(args.evaluated_at) or args.session_cutoff and any((args.source,args.verify,args.recover)):
        parser.error('cutoff/evaluated-at must be paired and live-only')
    if args.session_cutoff:
        from launch.daily import plan_for
        if plan_for(datetime.fromisoformat(args.evaluated_at))['cutoff'] != args.session_cutoff:
            parser.error('cutoff must match the first launch KST previous date')
    config = artifact_io.load(ROOT/'config.json')
    from contextlib import nullcontext
    no_network = patch.object(socket.socket, 'connect', side_effect=AssertionError('offline_network_forbidden'))
    with no_network if args.source or args.verify or args.nh_evidence  else nullcontext():
        if args.verify:
            result = verify(args.verify.resolve())
        else:
            with instance_lock(ROOT/'runtime'):
                result = run(config, args.source, args.recover, args.nh_evidence, session_cutoff=args.session_cutoff, evaluated_at=args.evaluated_at, prior_result=args.prior_run)
    print(json.dumps(result, ensure_ascii=True), flush=True)
    return 1 if result.get('state') == 'failed' else 2 if result.get('state') == 'partial' else 0


if __name__ == '__main__':
    raise SystemExit(main())

