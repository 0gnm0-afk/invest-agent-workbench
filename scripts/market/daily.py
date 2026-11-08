"""One private market cycle: Toss collection, shared price references and analysis."""

if not __package__:
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import atomic_io as artifact_io
import argparse
import csv
import html
import json
import re
import socket
import sys
import time
from datetime import datetime, timezone
from display_format import stock_label
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4
from zoneinfo import ZoneInfo

from screening.engine import screen_market as screen
from invest_agent.trading.store import instance_lock
from market.collect import audit, run as collect
from atomic_io import write_json
import sectors.observation as sector
import market.nh_fallback as nh_fallback
from screening.context import enrich
import screening.priority as priority
import sectors.signals as sector_signals

from workbench_paths import ROOT


def now():
    return datetime.now(timezone.utc).isoformat()


def code_hashes(engine):
    paths = list((ROOT/'scripts').rglob('*.py')) + list((ROOT/'scripts').rglob('*.json'))
    paths += list((engine/'src/invest_agent').rglob('*.py'))
    paths += [ROOT/'daily.ps1', ROOT/'config.json']
    return {str(p.resolve()):artifact_io.sha(p) for p in paths}


def screen_code_hashes(entries, config):
    """Compare only calculation code, retaining legacy manifest paths and hashes."""
    from runtime_paths import previous_roots
    roots = [Path(config['engine']).resolve()]
    roots += previous_roots(config.get('records', {}), 'previous_engine_root', 'previous_engine_roots')
    package_roots = [p / 'src' / 'invest_agent' for p in roots]
    return {path: expected for path, expected in entries.items()
            if any(Path(path).resolve().is_relative_to(source) for source in package_roots)
            or Path(path).name == 'stock_screen.py'
            or Path(path).resolve() == (ROOT / 'scripts/screening/engine.py').resolve()}


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


def seed_check(config):
    path = Path(config['sector_manifest'])
    artifact_io.checked(path, config['sector_manifest_sha256'], {})
    seed = artifact_io.load(path)
    old_market = Path(seed['roots'][0]).resolve()
    # Reuse immutable classification evidence only; do not copy old market results.
    inputs = {p:h for p,h in seed['input_hashes'].items() if not Path(p).is_relative_to(old_market)}
    check_hashes(inputs)
    if config.get('priority_review',{}).get('strategy') == 'pdf_momentum':
        from screening.momentum import SOURCE
        path=Path(config['screener_source_pdf'])
        artifact_io.checked(path,SOURCE['sha256'],{})
        inputs[str(path.resolve())]=SOURCE['sha256']
        rules=ROOT/'docs/pdf-momentum-rules.md'
        inputs[str(rules.resolve())]=artifact_io.sha(rules)
    return seed, inputs


def saved_fallback_summary(fallback):
    result = {'attempted':fallback['attempted'], 'adopted':fallback['adopted'],
            'unrecovered':fallback.get('unrecovered', fallback['attempted']-fallback['adopted']-fallback.get('excluded',0))}
    if 'excluded' in fallback:
        result['excluded'] = fallback['excluded']
    return result


def percent(value):
    return '결측' if value is None else f'{value:.2%}'


def inline_links(text):
    # Escape all text first; activate only local artifact paths, never URLs/user markup.
    escaped = html.escape(text)
    return re.sub(r'\[([^\]<>]+)\]\(((?:charts/[A-Za-z0-9-]+\.html)|(?:[A-Za-z0-9_-]+\.(?:json|html))|(?:sectors/[A-Za-z0-9_.-]+))\)',
                  r'<a href="\2">\1</a>', escaped)


def render(run, output, screening=None, observation=None):
    if artifact_io.available(output/'priority-review.json') and artifact_io.load(output/'priority-review.json').get('schema')==4:
        import screening.watch as watch_review
        return watch_review.render(run,output,screening,observation)
    review = None
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
                  f"기존 스페란데오 후보 {screening['candidate_count']} · 종목 판단 불가 {screening['unavailable_count']}",
                  '', '## 후보와 섹터 맥락', '', '공통 유니버스 뒤 두 후보 경로는 독립입니다. 아래 표는 기존 스페란데오 전체 후보와 이평 위치를 원순서로 보존합니다.', '',
                  '| 시장 | 종목 | 단계·기간 | 테이버 위치 | 섹터 | RS 3M / 6M | 조사 참고 |', '|---|---|---|---|---|---|---|']
        if run.get('nh_fallback'):
            fb = run['nh_fallback']
            lines.insert(lines.index('## 후보와 섹터 맥락')-1,
                f"NH 표적 보완: 시도 {fb['attempted']} · 채택 {fb['adopted']} · 양쪽 자료 부재 제외 {fb.get('excluded',0)} · 미해결 {fb['unrecovered']}. "
                '원 Toss 실패는 원본에 보존; NH 거래량 정밀도는 종목별 표시하고 동일 공급자 섹터 집계에서 제외합니다.')
        if artifact_io.available(output/'priority-review.json'):
            review = artifact_io.load(output/'priority-review.json')
            if review.get('taver_state') == 'disabled':
                lines = [line.replace('공통 유니버스 뒤 두 후보 경로는 독립입니다. ', '')
                         .replace('테이버 위치', '장기 이평 위치') for line in lines]
            missing_count = sum(g['selected_count'] for g in review['markets'].values())
            review_lines = priority.report_lines(review,
                artifact_io.load(output/'priority-charts.json') if artifact_io.available(output/'priority-charts.json')
                else {'requested':missing_count,'available':0,'failed':missing_count,'rows':[]})
            at = lines.index('## 후보와 섹터 맥락')
            lines[at:at] = review_lines
            if review.get('schema') in (3,4):
                lines=lines[:lines.index('## 후보와 섹터 맥락')]
                lines=[line.replace(f"기존 스페란데오 후보 {screening['candidate_count']}",f"활성 진입 후보 {review['candidate_count']} · 기존 S단계 감사 후보 {screening['candidate_count']}") for line in lines]
        for row in ([] if review and review.get('schema') in (3,4) else screening['rows']):
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
            cells = [row['market'], stock_label(row)+(' [NH]' if nh_price else ''),
                     structure, location, label, rs, note]
            lines.append('| ' + ' | '.join(str(x).replace('|','/').replace('\n',' ') for x in cells) + ' |')
        if not (review and review.get('schema') in (3,4)):
            lines += ['', '## 조사 순서', '', '국가별 섹터 RS63 내림차순, 결측은 마지막, 동률은 원 후보 순서입니다. 합성 점수는 없습니다.']
            display_rows = {(r['market'],r['symbol']):r for r in screening['rows']}
            for market, rows in screening['research_order'].items():
                lines += ['', f"{market}: " + ', '.join(stock_label(display_rows.get((market,r['symbol']),r),market) for r in rows)]
        lines += ['', '[기존 S단계 감사 자료](screen.json) · [자료 품질 상세](quality.json)',
                  '[섹터 상태·변화](sectors/observation.html) · [기초 지표 상세](sectors/metrics.html) · [분류 미확인](sectors/unmatched.json)']
    if observation:
        lines += ['', f"분류 적용 시각: {observation['applied_at']}. 원천별 관측일·효력일은 groups.json의 evidence에 보존합니다.",
                  '현재 분류로 과거 가격을 묶습니다. 과거 시점 백테스트가 아닙니다.',
                  '한국 RS: 같은 Toss 대상의 동일비중 지수, KOSPI는 참고. 미국 RS: Toss SPY 프록시.',
                  '가격 결측은 지표별 제외/null이며 0수익률 대체나 보간하지 않습니다.',
                  '직접 확인한 WICS/Finviz 분류는 7일 경과 시 갱신 필요로 표시하고 분석에 유지합니다. 충돌·미확인은 제외합니다. 공식 계산 근거 문서가 오래되면 섹터 계산은 실패로 남깁니다.',
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
            body.append('<tr>'+''.join('<td>'+inline_links(c.strip())+'</td>' for c in cells)+'</tr>')
        else:
            if table:
                body.append('</table></div>')
                table = False
            if line.startswith('#'):
                level = min(len(line)-len(line.lstrip('#')), 3)
                body.append(f'<h{level}>'+html.escape(line.lstrip('# '))+f'</h{level}>')
            elif line:
                body.append('<p>'+inline_links(line)+'</p>')
    if table:
        body.append('</table></div>')
    sector_panel = ''
    if artifact_io.available(output/'sectors/signals.json'):
        states = artifact_io.load(output/'sectors/signals.json')
        sector_panel = '<style>'+sector_signals.CSS+'</style><h1>섹터 변화 먼저 보기</h1><p>완료 일봉 기준 · 현재 분류로 재생한 관찰 · 추정 거래 비중은 순유입 아님</p>'+sector_signals.cards(states,'sectors/',4)+'<p><a href="sectors/observation.html">전체 섹터 상태·필터·추이 →</a></p>'
    page = '<!doctype html><html lang="ko"><meta charset="utf-8"><title>일일 시장 관찰</title><style>body{font-family:system-ui;margin:24px;line-height:1.5}table{border-collapse:collapse;font-size:14px}td{border:1px solid #ccc;padding:8px;min-width:85px}tr:first-child{font-weight:bold;background:#edf2f7}p{overflow-wrap:anywhere}</style><body><nav><a href="sectors/observation.html">섹터 관찰표</a> · <a href="candidates.csv">후보 CSV</a> · <a href="screen.json">후보/제외/결측 상세</a> · <a href="quality.json">품질 상세</a></nav>' + sector_panel + ''.join(body) + '</body></html>'
    if review and review.get('schema') in (3,4):
        page=page.replace('<a href="screen.json">후보/제외/결측 상세</a>','<a href="entry-scan.html">활성 후보·탈락 근거</a> · <a href="screen.json">기존 S단계 감사 자료</a>')
    (output/'report.html').write_text(page, encoding='utf-8')


def export_sectors(output, report, groups):
    output.mkdir()
    artifact_io.write(output/'observation.json', report)
    artifact_io.write(output/'groups.json', groups)
    artifact_io.write(output/'unmatched.json', [r for r in groups if r['group_state'] != 'matched'])
    md, page = sector.render(report)
    (output/'observation.md').write_text(md, encoding='utf-8')
    (output/'observation.html').write_text(page, encoding='utf-8')
    (output/'metrics.html').write_text(page, encoding='utf-8')


def run(config, source=None, recover_source=None, nh_evidence=None, saved_run=None, *, session_cutoff=None, evaluated_at=None, prior_result=None):
    date = datetime.now(ZoneInfo('Asia/Seoul')).date().isoformat()
    mode = 'review' if saved_run else 'replay' if source else 'recover' if recover_source else 'live'
    output = ROOT/'runtime'/date/(datetime.now(timezone.utc).strftime('%H%M%S')+'-'+mode+'-'+uuid4().hex[:8])
    output.mkdir(parents=True)
    run = {'schema':1, 'state':'running', 'processing_state':'running', 'mode':mode, 'started_at':now(),
           'output':str(output), 'stages':{s:'not_started' for s in ('inputs','collection','nh_fallback','adr_metadata','quality','stock_screen','sector_screen','priority_review','charts','sector_states','report')}}
    if session_cutoff:
        run.update(session_cutoff=session_cutoff, evaluated_at=evaluated_at)
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
        basis_rechecks = None
        if len(seed.get('roots', [])) >= 3:
            evidence = Path(seed['roots'][2])
            if mode == 'live' or mode == 'recover' and nh_evidence is None:
                from sectors.basis_refresh import prepare
                basis_rechecks = prepare(evidence, output/'basis', now())
            elif saved_run:
                saved = Path(saved_run).resolve()
                saved_manifest = artifact_io.load(saved/'manifest.json')
                artifact_io.checked(saved/'run.json', saved_manifest['output_hashes']['run.json'], {})
                basis_rechecks = artifact_io.load(saved/'run.json').get('basis_rechecks')
            elif source and (Path(source)/'basis/rechecks.json').is_file():
                source_check(Path(source))
                basis_rechecks = Path(source)/'basis/rechecks.json'
            if basis_rechecks:
                basis_rechecks = Path(basis_rechecks).resolve()
                run['basis_rechecks'] = str(basis_rechecks)
                sector.load_basis(evidence, artifact_io.checked, artifact_io.load, inputs, now(),
                                  sector.age_issue, rechecks=basis_rechecks)
        update('inputs', 'completed')
        if saved_run:
            saved_run = Path(saved_run).resolve()
            prior_manifest = artifact_io.load(saved_run/'manifest.json')
            check_hashes(prior_manifest['input_hashes'])
            check_hashes({str(saved_run/p):h for p,h in prior_manifest['output_hashes'].items()})
            # A saved screen may only be reused with the identical calculation engine.
            check_hashes(screen_code_hashes(prior_manifest['code_hashes'], config))
            source = Path(artifact_io.load(saved_run/'run.json')['market_source'])
            run['base_screen_source'] = str(saved_run)
            for name in ('manifest.json','run.json','screen-base.json'):
                inputs[str(saved_run/name)] = artifact_io.sha(saved_run/name)
        stage = 'collection'
        update(stage, 'running')
        if source or recover_source:
            source = Path(source or recover_source).resolve()
            source_check(source)
            update(stage, 'saved_source_verified_no_network' if mode in ('replay','review') else 'saved_toss_source_verified')
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
        if mode not in ('replay','review') and config.get('ohlcv',{}).get('provider') == 'toss' and not nh_evidence:
            run['toss_source'] = str(source)
            run['nh_fallback'] = {'attempted':0, 'adopted':0, 'unrecovered':0,
                                  'reason':'single_provider_toss'}
            update('nh_fallback', 'not_requested_single_provider')
        elif mode not in ('replay','review'):
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
        if mode not in ('replay','review') and not nh_evidence and config.get('adr_metadata',{}).get('enabled'):
            import sectors.adr as adr_metadata
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
            update('adr_metadata', 'not_requested_offline' if mode in ('replay','review') or nh_evidence else 'disabled')
        run['market_source'] = str(source)
        inputs.update({str(source/name):h for name,h in artifact_io.load(source/'manifest.json').items()})
        inputs[str(source/'manifest.json')] = artifact_io.sha(source/'manifest.json')
        stage = 'quality'
        update(stage, 'running')
        snapshot = artifact_io.load(source/'snapshot.json')
        if mode == 'review' and snapshot.get('classification_refresh') != config.get('classification_refresh'):
            from sectors.classification_refresh import refresh_classification_source
            source = refresh_classification_source(source,config.get('classification_refresh'),output/'classification-source')
            run['market_source'] = str(source)
            inputs.update({str(source/name):h for name,h in artifact_io.load(source/'manifest.json').items()})
            inputs[str(source/'manifest.json')] = artifact_io.sha(source/'manifest.json')
            snapshot = artifact_io.load(source/'snapshot.json')
        if mode in ('replay','review') and snapshot.get('nh_fallback'):
            run['nh_fallback'] = saved_fallback_summary(snapshot['nh_fallback'])
            run['nh_fallback']['provenance'] = 'saved_source_no_new_requests'
        quality = audit(snapshot)
        write_json(output/'quality.json', quality)
        from market.exclusions import inventory
        write_json(output/'market-failures.json', inventory(snapshot,quality))
        run['quality'] = {k:quality[k] for k in ('state','processing_state','data_complete','quality_issue_series','unexpected_failures')}
        run['quality'].update({k:snapshot['collection_stats'][k] for k in ('requested','received','failed')})
        run['data_times'] = {k:snapshot.get(k) for k in ('fetched_at','collected_until')}
        update(stage, quality['processing_state'])
        stage = 'stock_screen'
        update(stage, 'running')
        screening = artifact_io.load(saved_run/'screen-base.json') if saved_run else screen(snapshot)
        write_json(output/'screen-base.json', screening)
        update(stage, 'completed')
        stage = 'sector_screen'
        update(stage, 'running')
        observation, groups, sector_inputs = sector.build(source, *[Path(p) for p in seed['roots'][1:]], now(), allow_universe_changes=True, basis_rechecks=basis_rechecks, snapshot=snapshot)
        inputs.update(sector_inputs)
        export_sectors(output/'sectors', observation, groups)
        screening = enrich(screening, observation, groups)
        write_json(output/'screen.json', screening)
        csv_name='legacy-candidates.csv' if config.get('priority_review',{}).get('strategy')=='pdf_momentum' else 'candidates.csv'
        with (output/csv_name).open('w',encoding='utf-8-sig',newline='') as stream:
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
        stage = 'priority_review'
        update(stage, 'running')
        review = priority.build(screening, snapshot, config.get('priority_review'))
        write_json(output/'priority-review.json', review)
        if review.get('schema') in (3,4):
            import screening.momentum as momentum_review
            momentum_review.write_outputs(output,review)
            run['legacy_sperandeo_candidate_count']=screening['candidate_count']
            run['candidate_count']=review['candidate_count']
            if review.get('schema')==4:run['entry_candidate_count']=review['entry_candidate_count']
        else:
            priority.taver_review.write_scan(output,review)
        update(stage, 'completed')
        stage = 'charts'
        update(stage, 'running')
        try:
            chart_result = priority.charts(review, screening, snapshot, output)
        except Exception as exc:
            failures = [{'market':m,'symbol':e['symbol'],'as_of':e['as_of'],'state':'failed',
                         'reason':'chart_initialization: '+type(exc).__name__+': '+str(exc)}
                        for m,g in review['markets'].items() for e in g['entries'] if e['selected']]
            chart_result = {'requested':len(failures),'available':0,'failed':len(failures),'rows':failures}
        write_json(output/'priority-charts.json', chart_result)
        run['priority_counts'] = {m:g['selected_count'] for m,g in review['markets'].items()}
        run['chart_counts'] = {k:chart_result[k] for k in ('requested','available','failed')}
        update(stage, 'completed_with_exceptions' if chart_result['failed'] else 'completed')
        stage = 'sector_states'
        update(stage, 'running')
        states = sector_signals.build(snapshot, groups, observation, config.get('sector_states'))
        sector_signals.attach_candidates(states, review, groups, chart_result)
        sector_signals.render(states, output/'sectors')
        run['sector_state_counts'] = dict(sector_signals.Counter(s['current']['state'] for s in states['sectors']))
        update(stage, 'completed_with_limitations')
        from market.scope import analysis_pending
        run['analysis_pending'] = analysis_pending(screening, snapshot)
        run['approved_exclusions'] = snapshot.get('scope_exclusions', [])
        del snapshot
        incomplete = chart_result['failed'] > 0 or not quality['data_complete'] or screening['unavailable_count'] > len(run['analysis_pending']) or any(r['unmatched'] for r in observation['coverage'].values())
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
    if run['stages']['sector_screen'] != 'completed_with_limitations':
        return {'verified':True, 'state':run['state'], 'analysis_replayed':False}
    config = artifact_io.load(output/'config.json')
    seed, _ = seed_check(config)
    source = Path(run['market_source'])
    snapshot = artifact_io.load(source/'snapshot.json')
    if (source/'automatic-derivation.json').exists():
        raise ValueError('retired_scope_requires_historical_code_revision')
    if audit(snapshot) != artifact_io.load(output/'quality.json'):
        raise ValueError('quality_replay_mismatch')
    base = screen(snapshot)
    if 'analysis_pending' in run:
        from market.scope import analysis_pending
        if analysis_pending(base, snapshot) != run['analysis_pending'] or snapshot.get('scope_exclusions',[]) != run.get('approved_exclusions'):
            raise ValueError('scope_or_analysis_pending_replay_mismatch')
    saved = artifact_io.load(output/'screen.json')
    report, groups, _ = sector.build(source, *[Path(p) for p in seed['roots'][1:]],
        artifact_io.load(output/'sectors/observation.json')['applied_at'], allow_universe_changes=True,
        basis_rechecks=run.get('basis_rechecks'), snapshot=snapshot)
    if report != artifact_io.load(output/'sectors/observation.json') or groups != artifact_io.load(output/'sectors/groups.json'):
        raise ValueError('sector_replay_mismatch')
    if base != artifact_io.load(output/'screen-base.json') or enrich(base, report, groups) != saved:
        raise ValueError('candidate_replay_mismatch')
    priority_counts = {}
    active_candidate_count = base['candidate_count']
    if artifact_io.available(output/'priority-review.json'):
        review = priority.build(saved, snapshot, config.get('priority_review'))
        if review != artifact_io.load(output/'priority-review.json'):
            raise ValueError('priority_replay_mismatch')
        chart_result = artifact_io.load(output/'priority-charts.json')
        expected = {(m,e['symbol']) for m,g in review['markets'].items() for e in g['entries'] if e['selected']}
        actual = [(r['market'],r['symbol']) for r in chart_result['rows']]
        if len(actual) != len(expected) or set(actual) != expected:
            raise ValueError('priority_chart_membership_mismatch')
        for row in chart_result['rows']:
            if row['state'] == 'available':
                for key in ('path','image','metadata'):
                    target = (output/row[key]).resolve()
                    if not target.is_relative_to(output.resolve()) or not target.is_file():
                        raise ValueError('priority_chart_missing_or_unsafe')
                if row.get('structure_image'):
                    target = (output/row['structure_image']).resolve()
                    if not target.is_relative_to(output.resolve()) or not target.is_file() or artifact_io.sha(target)!=row['structure_image_sha256']:
                        raise ValueError('taver_chart_image_hash_mismatch')
                if artifact_io.sha(output/row['image']) != row['image_sha256']:
                    raise ValueError('priority_chart_image_hash_mismatch')
                if ('href="'+row['path']+'"') not in (output/'report.html').read_text(encoding='utf-8'):
                    raise ValueError('priority_chart_report_link_missing')
        priority_counts = {m:g['selected_count'] for m,g in review['markets'].items()}
        if review.get('schema') in (3,4):
            active_candidate_count=review['candidate_count']
            with (output/'candidates.csv').open(encoding='utf-8-sig',newline='') as stream:
                csv_rows=list(csv.DictReader(stream))
            wanted={(e['market'],e['symbol']) for g in review['markets'].values() for e in g['entries']}
            if len(csv_rows)!=len(wanted) or {(e['market'],e['symbol']) for e in csv_rows}!=wanted:
                raise ValueError('active_candidate_csv_mismatch')
            if run['candidate_count']!=active_candidate_count:
                raise ValueError('active_candidate_count_mismatch')
            if review.get('schema')==4:
                if run.get('entry_candidate_count')!=review['entry_candidate_count']:
                    raise ValueError('entry_candidate_count_mismatch')
                lookup={(e['market'],e['symbol']):e for g in review['markets'].values() for e in g['entries']}
                for row in csv_rows:
                    if row['phase']!=lookup[(row['market'],row['symbol'])]['phase']:
                        raise ValueError('candidate_phase_csv_mismatch')
                for group in review['markets'].values():
                    if sum(group['phase_counts'].values())!=group['candidate_count']:
                        raise ValueError('candidate_phase_count_mismatch')
    sector_state_counts = {}
    if run['stages'].get('sector_states') == 'completed_with_limitations' and not artifact_io.available(output/'sectors/signals.json'):
        raise ValueError('sector_states_output_missing')
    if artifact_io.available(output/'sectors/signals.json'):
        states = sector_signals.build(snapshot, groups, report, config.get('sector_states'))
        sector_signals.attach_candidates(states, review, groups, chart_result)
        sector_state_counts = sector_signals.verify_saved(output/'sectors',states)
    return {'sector_state_counts':sector_state_counts,'priority_counts':priority_counts,'verified':True,'analysis_replayed':True,'network_calls':0,'candidate_count':active_candidate_count,'legacy_sperandeo_candidate_count':base['candidate_count'],
            'universe_rows':len(groups),'sector_groups':len(report['observations']),'state':run['state']}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, help='Offline replay of an immutable market attempt')
    parser.add_argument('--review', type=Path, help='Offline report/priority refresh from a verified saved screen; no market collection')
    parser.add_argument('--verify', type=Path, help='Offline checksum and calculation replay')
    parser.add_argument('--recover', type=Path, help='New run from a saved Toss attempt with NH fallback')
    parser.add_argument('--nh-evidence', type=Path, help='Previously captured NH requests for --recover')
    parser.add_argument('--session-cutoff', help='Dashboard-only latest permitted session date')
    parser.add_argument('--evaluated-at', help='Frozen timezone-aware first launch timestamp')
    parser.add_argument('--prior-run', type=Path, help='Verified prior result for incremental seed after a failed attempt')
    args = parser.parse_args()
    if sum(bool(x) for x in (args.source,args.verify,args.recover,args.review)) > 1:
        parser.error('choose source, verify, recover, or review')
    if args.nh_evidence and not args.recover:
        parser.error('--nh-evidence requires --recover')
    if bool(args.session_cutoff) != bool(args.evaluated_at) or args.session_cutoff and any((args.source,args.verify,args.recover,args.review)):
        parser.error('cutoff/evaluated-at must be paired and live-only')
    if args.session_cutoff:
        from launch.daily import plan_for
        if plan_for(datetime.fromisoformat(args.evaluated_at))['cutoff'] != args.session_cutoff:
            parser.error('cutoff must match the first launch KST previous date')
    config = artifact_io.load(ROOT/'config.json')
    from contextlib import nullcontext
    no_network = patch.object(socket.socket, 'connect', side_effect=AssertionError('offline_network_forbidden'))
    with no_network if args.source or args.verify or args.nh_evidence or args.review else nullcontext():
        if args.verify:
            result = verify(args.verify.resolve())
        else:
            with instance_lock(ROOT/'runtime'):
                result = run(config, args.source, args.recover, args.nh_evidence, args.review, session_cutoff=args.session_cutoff, evaluated_at=args.evaluated_at, prior_result=args.prior_run)
    print(json.dumps(result, ensure_ascii=True), flush=True)
    return 1 if result.get('state') == 'failed' else 2 if result.get('state') == 'partial' else 0


if __name__ == '__main__':
    raise SystemExit(main())

