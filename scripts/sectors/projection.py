"""Recalculate sector views from verified saved inputs, without altering source runs."""
from workbench_paths import SCRIPTS
import atomic_io as artifact_io
import hashlib
import json
from pathlib import Path
import sectors.observation as so
import sectors.signals as sector_signals
from derived_cache import publish, read_lock


def cached(root, run, config, source, original_observation, review, *, snapshot=None):
    """Reuse the caller's verified snapshot; still verify all pinned files."""
    seed_path = Path(config['sector_manifest'])
    artifact_io.checked(seed_path, config['sector_manifest_sha256'], {})
    seed = artifact_io.load(seed_path)
    old_market = Path(seed['roots'][0]).resolve()
    inputs = {p:h for p,h in seed['input_hashes'].items() if not Path(p).resolve().is_relative_to(old_market)}
    market_manifest = artifact_io.load(Path(source)/'manifest.json')
    inputs.update({str(Path(source)/p):h for p,h in market_manifest.items()})
    inputs[str(Path(source)/'manifest.json')] = artifact_io.sha(Path(source)/'manifest.json')
    for path, expected in inputs.items():
        artifact_io.checked(path, expected, {})
    applied = original_observation['applied_at']
    run = Path(run)
    run_manifest = artifact_io.load(run/'manifest.json')
    artifact_io.checked(run/'run.json', run_manifest['output_hashes']['run.json'], inputs)
    basis_rechecks = artifact_io.load(run/'run.json').get('basis_rechecks')
    if basis_rechecks:
        basis_inputs = {}
        so.load_basis(Path(seed['roots'][2]), artifact_io.checked, artifact_io.load, basis_inputs, applied,
                      so.age_issue, rechecks=basis_rechecks)
        for path, expected in basis_inputs.items():
            if run_manifest['input_hashes'].get(path) != expected:
                raise ValueError('projection_unpinned_basis_input')
        inputs.update(basis_inputs)
    if snapshot is None:
        snapshot = artifact_io.load(Path(source)/'snapshot.json')
    reference = snapshot.get('classification_refresh')
    if reference:
        from sectors.classification_refresh import apply
        refresh_inputs = {}
        apply([], {'classification_refresh':reference}, artifact_io.checked, refresh_inputs, applied,
              so.classification_age_issue, so.SECTORS)
        for path, expected in refresh_inputs.items():
            if run_manifest['input_hashes'].get(path) != expected:
                raise ValueError('projection_unpinned_classification_refresh')
        inputs.update(refresh_inputs)
    from sectors.adr import verified as verify_adr
    adr_inputs = {}
    verify_adr(snapshot.get('adr_metadata'), adr_inputs)
    if any(run_manifest['input_hashes'].get(p) != h for p,h in adr_inputs.items()):
        raise ValueError('projection_unpinned_adr_metadata')
    inputs.update(adr_inputs)
    names = {'adr_metadata.py': 'sectors/adr.py', 'user_classification.py': 'sectors/user_classification.py', 'sector_projection.py': 'sectors/projection.py', 'sector_observation.py': 'sectors/observation.py', 'sector_signals.py': 'sectors/signals.py', 'sector_basis.py': 'sectors/basis.py', 'wics_classification.py': 'sectors/wics.py', 'finviz_public_classification.py': 'sectors/finviz.py', 'sector_external_review.py': 'sectors/external_review.py', 'classification_refresh.py': 'sectors/classification_refresh.py'}
    key_data = dict(inputs=inputs, seed=config['sector_manifest_sha256'], applied=applied,
                    settings=config.get('sector_states'), review=review,
                    code={n:artifact_io.sha(SCRIPTS/path) for n,path in names.items()})
    key = hashlib.sha256(json.dumps(key_data,sort_keys=True,ensure_ascii=False).encode()).hexdigest()
    folder = Path(root)/'runtime/sector-projection'/key
    output = folder/'view.json'
    with read_lock(folder) as readable:
        if readable and output.exists():
            meta = artifact_io.load(folder/'manifest.json')
            artifact_io.checked(output, meta['sha256'], {})
            return artifact_io.load(output)
    observation, groups, verified = so.build(Path(source), *[Path(p) for p in seed['roots'][1:]],
                                            applied, allow_universe_changes=True, basis_rechecks=basis_rechecks, snapshot=snapshot)
    # Every dependency read during replay must agree with pinned evidence.
    for path, expected in verified.items():
        if path not in inputs or inputs[path] != expected:
            raise ValueError('projection_unpinned_input:'+path)
    signals = sector_signals.build(snapshot, groups, observation, config.get('sector_states'))
    sector_signals.attach_candidates(signals, review, groups, {'rows':[]})
    result = dict(observation=observation, groups=groups, signals=signals,
                  policy='last_verified_classification_refresh_due_after_7_days', source_run=str(run))
    raw = json.dumps(result,ensure_ascii=False,allow_nan=False)
    publish(root, 'sector-projection', key, {'view.json':raw,
        'manifest.json':json.dumps(dict(sha256=hashlib.sha256(raw.encode()).hexdigest(),
            key=key, input_hashes=inputs, network_calls=0))})
    return result
