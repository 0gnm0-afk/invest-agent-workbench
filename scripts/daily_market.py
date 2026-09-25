"""One private market cycle: Toss collection, qualified NH failure fallback, analysis."""
import argparse
import csv
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

from stock_screen import screen_market as screen
from invest_agent.trading.store import instance_lock
from market_collect import audit, run as collect, write_json
import sector_observation as sector
import nh_fallback
from candidate_context import enrich

ROOT = Path(__file__).resolve().parents[1]


def now():
    return datetime.now(timezone.utc).isoformat()


def code_hashes(engine):
    paths = list((ROOT/'scripts').glob('*.py')) + list((ROOT/'scripts').glob('*.json'))
    paths += list((engine/'src/invest_agent').rglob('*.py'))
    paths += [ROOT/'daily.ps1', ROOT/'config.json']
    return {str(p.resolve()):sector.sha(p) for p in paths}


def check_hashes(entries):
    for path, expected in entries.items():
        sector.checked(Path(path), expected, {})


def source_check(root):
    manifest = sector.load(root/'manifest.json')
    for name, expected in manifest.items():
        path = (root/name).resolve()
        if not path.is_relative_to(root.resolve()):
            raise ValueError('source_manifest_path_outside_root')
        sector.checked(path, expected, {})


def seed_check(config):
    path = Path(config['sector_manifest'])
    sector.checked(path, config['sector_manifest_sha256'], {})
    seed = sector.load(path)
    old_market = Path(seed['roots'][0]).resolve()
    # Reuse immutable classification evidence only; do not copy old market results.
    inputs = {p:h for p,h in seed['input_hashes'].items() if not Path(p).is_relative_to(old_market)}
    check_hashes(inputs)
    return seed, inputs


def percent(value):
    return '결측' if value is None else f'{value:.2%}'


def render(run, output, screening=None, observation=None):
    lines = ['# 일일 시장 관찰', '', f"실행 상태: **{run['state']}** / 처리: {run['processing_state']}",
             f"모드: {run['mode']} / 시작: {run['started_at']} / 종료: {run.get('finished_at', '진행 중')}",
             '', 'Toss 제공 대상 전수 기준. 거래소 전체와의 동일성을 보증하지 않습니다.',
             '투자 판단 보조 자료이며 후보·조사 순서는 매수/매도 신호가 아닙니다.', '']
    if run.get('error'):
        lines += [f"오류: {run['error']}", '']
    lines += ['| 단계 | 상태 |', '|---|---|'] + [f'| {k} | {v} |' for k,v in run['stages'].items()]
    if screening:
        q = run['quality']
        lines += ['', f"가격 완료 세션: KR {screening['session_dates']['KR']} / US {screening['session_dates']['US']}",
                  f"수집 시작/종료: {run['data_times']['fetched_at']} / {run['data_times']['collected_until']}",
                  f"수신 {q['received']}/{q['requested']} · 일봉 실패 {q['failed']} · 품질 이슈 시계열 {q['quality_issue_series']}",
                  f"원자료 상태 {q['state']} · 처리 {q['processing_state']} · 완전성 {q['data_complete']}",
                  f"후보 {screening['candidate_count']} · 종목 판단 불가 {screening['unavailable_count']}",
                  '', '## 후보와 섹터 맥락', '', '유니버스 → 스페란데오 S1–S4 → 테이버 위치. 아래는 기존 엔진 순서입니다.', '',
                  '| 시장 | 종목 | 단계·기간 | 테이버 위치 | 섹터 | RS 3M / 6M | 조사 참고 |', '|---|---|---|---|---|---|---|']
        if run.get('nh_fallback'):
            fb = run['nh_fallback']
            lines.insert(lines.index('## 후보와 섹터 맥락')-1,
                f"NH 표적 보완: 시도 {fb['attempted']} · 채택 {fb['adopted']} · 미복구 {fb['unrecovered']}. "
                '원 Toss 실패는 원본에 보존; NH 거래량 정밀도는 종목별 표시하고 동일 공급자 섹터 집계에서 제외합니다.')
        for row in screening['rows']:
            if row['state'] != 'candidate':
                continue
            ctx = row['sector_context']
            horizons = ctx.get('horizons', {})
            structure = ', '.join(str(x) for x in row['candidate_reasons'])
            # Preserve the full structured location, including each independent MA, in JSON.
            taver = row.get('taver', {})
            location = '; '.join('SMA'+p+': '+v['location_state']
                                 for p,v in taver.get('lines', {}).items())
            label = ctx.get('group_name') or ctx.get('reason') or '결측'
            rs = ' / '.join(percent(horizons.get(h, {}).get('relative_strength')) for h in ('63','126'))
            note = ctx.get('reason') or '; '.join(ctx.get('warnings', [])) or '분모·추세·참여·활동은 상세 JSON/섹터표 참조'
            nh_price = str(row.get('price_basis','')).startswith('nh_')
            if nh_price:
                volume_note = ('거래량 하한 추정' if row['market']=='US' else '거래량 이전 Toss 직접 대조')
                note = 'NH 가격 보완·'+volume_note+'·섹터 분모 제외; '+note
            cells = [row['market'], f"{row['symbol']}{' [NH]' if nh_price else ''} {row.get('name','')}",
                     structure, location, label, rs, note]
            lines.append('| ' + ' | '.join(str(x).replace('|','/').replace('\n',' ') for x in cells) + ' |')
        lines += ['', '## 조사 순서', '', '국가별 섹터 RS63 내림차순, 결측은 마지막, 동률은 원 후보 순서입니다. 합성 점수는 없습니다.']
        for market, rows in screening['research_order'].items():
            lines += ['', f"{market}: " + ', '.join(r['symbol'] for r in rows)]
        lines += ['', '[후보 전체·탈락/결측 사유](screen.json) · [자료 품질 상세](quality.json)',
                  '[섹터 관찰표](sectors/observation.html) · [분류 미확인](sectors/unmatched.json)']
    if observation:
        lines += ['', f"분류 적용 시각: {observation['applied_at']}. 원천별 관측일·효력일은 groups.json의 evidence에 보존합니다.",
                  '현재 분류로 과거 가격을 묶습니다. 과거 시점 백테스트가 아닙니다.',
                  '한국 RS: 같은 Toss 대상의 동일비중 지수, KOSPI는 참고. 미국 RS: Toss SPY 프록시.',
                  '가격 결측은 지표별 제외/null이며 0수익률 대체나 보간하지 않습니다.',
                  '분류 관측 후 7일 초과는 미확인으로 남깁니다. 공식 계산 근거 문서가 오래되면 섹터 계산은 실패로 남깁니다.',
                  'NH는 Toss 전송 실패분의 검증된 가격 보완에만 사용합니다. 계좌 조회·기업 Evidence·LLM·예약 실행은 포함하지 않습니다.']
    text = '\n'.join(lines) + '\n'
    (output/'report.md').write_text(text, encoding='utf-8')
    body, table = [], False
    for line in lines:
        if line.startswith('|'):
            if not table:
                body.append('<div style="overflow:auto"><table>')
                table = True
            if line.startswith('|---'):
                continue
            cells = line.strip('|').split('|')
            body.append('<tr>'+''.join('<td>'+html.escape(c.strip())+'</td>' for c in cells)+'</tr>')
        else:
            if table:
                body.append('</table></div>')
                table = False
            if line.startswith('#'):
                level = min(len(line)-len(line.lstrip('#')), 3)
                body.append(f'<h{level}>'+html.escape(line.lstrip('# '))+f'</h{level}>')
            elif line:
                body.append('<p>'+html.escape(line)+'</p>')
    if table:
        body.append('</table></div>')
    page = '<!doctype html><html lang="ko"><meta charset="utf-8"><title>일일 시장 관찰</title><style>body{font-family:system-ui;margin:24px;line-height:1.5}table{border-collapse:collapse;font-size:14px}td{border:1px solid #ccc;padding:8px;min-width:85px}tr:first-child{font-weight:bold;background:#edf2f7}p{overflow-wrap:anywhere}</style><body><nav><a href="sectors/observation.html">섹터 관찰표</a> · <a href="candidates.csv">후보 CSV</a> · <a href="screen.json">후보/제외/결측 상세</a> · <a href="quality.json">품질 상세</a></nav>' + ''.join(body) + '</body></html>'
    (output/'report.html').write_text(page, encoding='utf-8')


def export_sectors(output, report, groups):
    output.mkdir()
    sector.write(output/'observation.json', report)
    sector.write(output/'groups.json', groups)
    sector.write(output/'unmatched.json', [r for r in groups if r['group_state'] != 'matched'])
    md, page = sector.render(report)
    (output/'observation.md').write_text(md, encoding='utf-8')
    (output/'observation.html').write_text(page, encoding='utf-8')


def run(config, source=None, recover_source=None, nh_evidence=None):
    date = datetime.now(ZoneInfo('Asia/Seoul')).date().isoformat()
    mode = 'replay' if source else 'recover' if recover_source else 'live'
    output = ROOT/'runtime'/date/(datetime.now(timezone.utc).strftime('%H%M%S')+'-'+mode+'-'+uuid4().hex[:8])
    output.mkdir(parents=True)
    run = {'schema':1, 'state':'running', 'processing_state':'running', 'mode':mode, 'started_at':now(),
           'output':str(output), 'stages':{s:'not_started' for s in ('inputs','collection','nh_fallback','quality','stock_screen','sector_screen','report')}}
    started = time.monotonic()
    hashes, inputs = {}, {}
    screening = observation = None
    stage = 'inputs'
    def update(name, value):
        run['stages'][name] = value
        write_json(output/'run.json', run)
        print(json.dumps({'stage':name,'state':value,'output':str(output)}, ensure_ascii=True), flush=True)
    try:
        hashes = code_hashes(Path(config['engine']))
        write_json(output/'config.json', config)
        seed, inputs = seed_check(config)
        update('inputs', 'completed')
        stage = 'collection'
        update(stage, 'running')
        if source or recover_source:
            source = Path(source or recover_source).resolve()
            source_check(source)
            update(stage, 'saved_source_verified_no_network' if mode == 'replay' else 'saved_toss_source_verified')
        else:
            collection_root = output/'collection'
            collect(collection_root, ROOT/'config.json')
            source = Path(sector.load(collection_root/'latest.json')['attempt'])
            update(stage, 'completed')
        if mode != 'replay':
            original_source = source
            run['toss_source'] = str(original_source)
            source_check(original_source)
            inputs.update({str(original_source/name):h for name,h in sector.load(original_source/'manifest.json').items()})
            inputs[str(original_source/'manifest.json')] = sector.sha(original_source/'manifest.json')
            stage = 'nh_fallback'
            update(stage, 'running')
            previous = None
            latest = ROOT/'runtime/latest.json'
            if latest.is_file():
                prior_run = Path(sector.load(latest)['output'])/'run.json'
                if prior_run.is_file():
                    previous = sector.load(prior_run).get('market_source')
            source, fallback, fallback_inputs = nh_fallback.recover(
                original_source,output/'nh_fallback',config,
                evidence_root=nh_evidence,prior_source=previous)
            run['nh_fallback'] = {k:fallback[k] for k in ('attempted','adopted','unrecovered')}
            inputs.update(fallback_inputs)
            update(stage, 'completed' if fallback['unrecovered'] == 0 else 'completed_with_exceptions')
        else:
            update('nh_fallback', 'not_requested_offline')
        run['market_source'] = str(source)
        inputs.update({str(source/name):h for name,h in sector.load(source/'manifest.json').items()})
        inputs[str(source/'manifest.json')] = sector.sha(source/'manifest.json')
        stage = 'quality'
        update(stage, 'running')
        snapshot = sector.load(source/'snapshot.json')
        quality = audit(snapshot)
        write_json(output/'quality.json', quality)
        run['quality'] = {k:quality[k] for k in ('state','processing_state','data_complete','quality_issue_series','unexpected_failures')}
        run['quality'].update({k:snapshot['collection_stats'][k] for k in ('requested','received','failed')})
        run['data_times'] = {k:snapshot.get(k) for k in ('fetched_at','collected_until')}
        update(stage, quality['processing_state'])
        stage = 'stock_screen'
        update(stage, 'running')
        screening = screen(snapshot)
        del snapshot
        write_json(output/'screen-base.json', screening)
        update(stage, 'completed')
        stage = 'sector_screen'
        update(stage, 'running')
        observation, groups, sector_inputs = sector.build(source, *[Path(p) for p in seed['roots'][1:]], now(), allow_universe_changes=True)
        inputs.update(sector_inputs)
        export_sectors(output/'sectors', observation, groups)
        screening = enrich(screening, observation, groups)
        write_json(output/'screen.json', screening)
        with (output/'candidates.csv').open('w',encoding='utf-8-sig',newline='') as stream:
            writer = csv.writer(stream)
            writer.writerow(['market','symbol','name','candidate_reasons','taver','sector','sector_rs63','sector_rs126','classification_issue','price_basis'])
            for r in screening['rows']:
                if r['state'] == 'candidate':
                    c = r['sector_context']
                    writer.writerow([r['market'],r['symbol'],r.get('name'),json.dumps(r['candidate_reasons']),
                                     json.dumps(r['taver']),c.get('group_name'),
                                     *[c.get('horizons',{}).get(h,{}).get('relative_strength') for h in ('63','126')],
                                     c.get('reason'),r.get('price_basis')])
        run['candidate_count'] = screening['candidate_count']
        run['sector_coverage'] = observation['coverage']
        update(stage, 'completed_with_limitations')
        incomplete = not quality['data_complete'] or screening['unavailable_count'] > 0 or any(r['unmatched'] for r in observation['coverage'].values())
        run['state'] = 'failed' if quality['unexpected_failures'] else 'partial' if incomplete else 'succeeded'
        run['processing_state'] = 'failed' if quality['unexpected_failures'] else 'completed_with_exceptions' if incomplete else 'completed'
        stage = 'report'
        update(stage, 'completed')
    except (Exception, SystemExit, KeyboardInterrupt) as exc:
        run.update(state='failed', processing_state='failed', error=type(exc).__name__+': '+str(exc))
        update(stage, 'failed')
    finally:
        run.update(finished_at=now(), elapsed_seconds=round(time.monotonic()-started,3))
        # A sector failure still leaves the successful base screen available.
        if screening and 'research_order' not in screening:
            write_json(output/'screen.json', screening)
            screening = None
        render(run, output, screening, observation)
        write_json(output/'run.json', run)
        write_json(output/'manifest.json', {'schema':1, 'code_hashes':hashes, 'input_hashes':inputs,
                   'output_hashes':{str(p.relative_to(output)):sector.sha(p) for p in output.rglob('*')
                                    if p.is_file() and 'cache' not in p.relative_to(output).parts}})
        write_json(ROOT/'runtime/latest.json', {'output':str(output), 'state':run['state'], 'mode':mode})
    return run


def verify(output):
    manifest = sector.load(output/'manifest.json')
    check_hashes(manifest['input_hashes'])
    check_hashes(manifest['code_hashes'])
    check_hashes({str(output/p):h for p,h in manifest['output_hashes'].items()})
    run = sector.load(output/'run.json')
    if run['stages']['sector_screen'] != 'completed_with_limitations':
        return {'verified':True, 'state':run['state'], 'analysis_replayed':False}
    config = sector.load(output/'config.json')
    seed, _ = seed_check(config)
    source = Path(run['market_source'])
    snapshot = sector.load(source/'snapshot.json')
    if audit(snapshot) != sector.load(output/'quality.json'):
        raise ValueError('quality_replay_mismatch')
    base = screen(snapshot)
    del snapshot
    saved = sector.load(output/'screen.json')
    report, groups, _ = sector.build(source, *[Path(p) for p in seed['roots'][1:]],
        sector.load(output/'sectors/observation.json')['applied_at'], allow_universe_changes=True)
    if report != sector.load(output/'sectors/observation.json') or groups != sector.load(output/'sectors/groups.json'):
        raise ValueError('sector_replay_mismatch')
    if base != sector.load(output/'screen-base.json') or enrich(base, report, groups) != saved:
        raise ValueError('candidate_replay_mismatch')
    return {'verified':True,'analysis_replayed':True,'network_calls':0,'candidate_count':base['candidate_count'],
            'universe_rows':len(groups),'sector_groups':len(report['observations']),'state':run['state']}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, help='Offline replay of an immutable market attempt')
    parser.add_argument('--verify', type=Path, help='Offline checksum and calculation replay')
    parser.add_argument('--recover', type=Path, help='New run from a saved Toss attempt with NH fallback')
    parser.add_argument('--nh-evidence', type=Path, help='Previously captured NH requests for --recover')
    args = parser.parse_args()
    if sum(bool(x) for x in (args.source,args.verify,args.recover)) > 1:
        parser.error('choose source, verify, or recover')
    if args.nh_evidence and not args.recover:
        parser.error('--nh-evidence requires --recover')
    config = sector.load(ROOT/'config.json')
    from contextlib import nullcontext
    no_network = patch.object(socket.socket, 'connect', side_effect=AssertionError('offline_network_forbidden'))
    with no_network if args.source or args.verify or args.nh_evidence else nullcontext():
        if args.verify:
            result = verify(args.verify.resolve())
        else:
            with instance_lock(ROOT/'runtime'):
                result = run(config, args.source, args.recover, args.nh_evidence)
    print(json.dumps(result, ensure_ascii=True), flush=True)
    return 1 if result.get('state') == 'failed' else 2 if result.get('state') == 'partial' else 0


if __name__ == '__main__':
    raise SystemExit(main())

