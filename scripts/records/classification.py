"""Missing-only archive of the seven pinned classification inputs used by saved runs."""
import hashlib
import json
from pathlib import Path

from atomic_io import read_bytes
from records.store import RecordStore, default_path
from runtime_paths import local_runtime_path
from workbench_paths import ROOT


BASELINE = Path('sector-external-review-20260925/report-final/manifest.json')
BASELINE_INPUTS = {
    Path('sector-classification-complete-20260924/report/manifest.json'): 'classification_table_manifest',
    Path('sector-classification-complete-20260924/report/classification.json'): 'classification_table',
    Path('sector-external-review-20260925/evidence/calculation-basis.json'): 'classification_calculation_basis',
}
REFRESH_INPUTS = tuple(Path('classification-refresh') / f'2026-10-02-{name}' / 'manifest.json'
                       for name in ('partial-repair', 'operating-units', 'share-classes'))
REFRESH_AUTHORITIES = (
    Path('2026-10-02/081825-review-b482f0f9'),
    Path('2026-10-02/111426-replay-71e1ca9f'),
)


def _sha(raw):
    return hashlib.sha256(raw).hexdigest()


def _payload(path, expected):
    raw = read_bytes(path)
    if _sha(raw) != expected:
        raise ValueError('classification_policy_hash_mismatch:' + str(path))
    return raw


def _prepare(root, store):
    config_path = root / 'config.json'
    config_raw = config_path.read_bytes()
    settings = json.loads(config_raw)
    mother = Path(settings['mother'])
    if not mother.is_absolute():
        raise ValueError('classification_mother_root_not_absolute')
    mother = mother.resolve()
    baseline = (mother / 'runtime' / BASELINE).resolve()

    def local(path):
        return local_runtime_path(path, root=root).resolve()

    if local(settings['sector_manifest']) != baseline:
        raise ValueError('classification_baseline_outside_known_scope')
    expected = settings['sector_manifest_sha256']
    baseline_raw = _payload(baseline, expected)
    prepared = [(mother, baseline, 'classification_baseline_manifest', baseline_raw, config_path, _sha(config_raw))]

    def pin_inputs(inputs, targets, pins, authority, authority_sha):
        names = {(path.parent.name.casefold(), path.name.casefold()) for path in targets}
        for path, digest in inputs.items():
            path = Path(path)
            if (path.parent.name.casefold(), path.name.casefold()) not in names:
                continue
            path = local(path)
            if path not in targets:
                continue
            if path in pins and digest != pins[path][0]:
                raise ValueError('classification_policy_pin_conflict:' + str(path))
            pins.setdefault(path, (digest, authority, authority_sha))

    baseline_targets = {(mother / 'runtime' / path).resolve(): kind for path, kind in BASELINE_INPUTS.items()}
    baseline_pins = {}
    pin_inputs(json.loads(baseline_raw)['input_hashes'], baseline_targets, baseline_pins, baseline, expected)
    refresh_targets = {(root / 'runtime' / path).resolve(): 'classification_refresh_manifest' for path in REFRESH_INPUTS}
    refresh_pins = {}
    active = settings['classification_refresh']
    if local(active['path']) not in refresh_targets:
        raise ValueError('classification_refresh_outside_known_scope')
    pin_inputs({active['path']: active['sha256']}, refresh_targets, refresh_pins, config_path, _sha(config_raw))
    for relative in REFRESH_AUTHORITIES:
        run = root / 'runtime' / relative
        reference = next((ref for ref in store.artifacts(run) if ref['role'] == 'manifest.json'), None)
        if reference is None:
            raise ValueError('classification_policy_authority_missing:' + str(run))
        manifest = json.loads(store.read(reference))
        pin_inputs(manifest.get('input_hashes', {}), refresh_targets, refresh_pins,
                   reference['source_path'], reference['sha256'])
    for owner, targets, pins in ((mother, baseline_targets, baseline_pins), (root, refresh_targets, refresh_pins)):
        for path, kind in targets.items():
            if path not in pins:
                raise ValueError('classification_policy_pin_missing:' + str(path))
            digest, authority, authority_sha = pins[path]
            prepared.append((owner, path, kind, _payload(path, digest), authority, authority_sha))
    return prepared


def migrate(root=ROOT, *, apply=False, store=None):
    """Validate the pinned input set before writing and add only missing versions."""
    root = Path(root).resolve()
    store = store if store is not None else RecordStore(default_path(root))
    result = []
    for owner, path, kind, raw, authority, authority_sha in _prepare(root, store):
        digest = _sha(raw)
        try:
            reference = store.lookup(path, digest)
        except ValueError as exc:
            if str(exc) != 'record_reference_hash_mismatch':
                raise
            reference = None
        state = 'existing' if reference else 'missing'
        if reference is None and apply:
            reference = store.put(path, raw, kind=kind, root=owner, metadata={
                'authority_path': str(authority), 'authority_sha256': authority_sha,
            })
            state = 'published'
        if reference is not None and store.read(reference) != raw:
            raise ValueError('classification_policy_roundtrip_mismatch:' + str(path))
        result.append({'path': str(path), 'kind': kind, 'sha256': digest, 'state': state})
    return result
