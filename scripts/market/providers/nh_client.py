"""Read captured NH market evidence; never authenticate or make new requests."""
import hashlib
import json
from pathlib import Path


class NHMarketError(ValueError):
    """Unavailable or invalid saved NH evidence (no authentication dependency)."""


PERIOD = '/gbstock/quote/v1/period'
KR_PERIOD = '/krstock/quote/v1/period'
CURRENT = '/gbstock/quote/v1/current'


def us_period_inputs(symbol, end):
    return {'iem_cd':symbol,'end_dt':end,'count':'200','maxavg':'000',
            'gubun':'3','xtick':'0001','today_cls':'0','market_cls':'1'}


def kr_period_inputs(symbol, end):
    return {'market_cd':'UNT','iem_cd':symbol,'mrkt_div_cls_code':'1',
            'edate':end,'array_cnt':'200','maxavg':'000','gubun':'1','xtick':'000',
            'today_cls_code':'0','fake_tick':'0','sur_flag':'0','sur_gb_day_cnt':'00',
            'sur_bf_end_time':'000000','out1_scale_change':'0','out2_scale_change':'0',
            'view_main_yn':'N'}


def digest(path, inputs):
    return hashlib.sha256(json.dumps([path,inputs],sort_keys=True).encode()).hexdigest()


class MarketClient:
    def __init__(self, root, evidence_root=None):
        self.root = Path(root)
        self.evidence_root = Path(evidence_root) if evidence_root else self.root

    def read(self, path, inputs):
        if path not in (PERIOD, KR_PERIOD, CURRENT):
            raise NHMarketError('market_only_period_required')
        cached = self.evidence_root / 'responses' / (digest(path, inputs) + '.json')
        if not cached.is_file():
            raise NHMarketError('requested_evidence_not_saved')
        manifest_path = self.evidence_root / 'manifest.json'
        if not manifest_path.is_file():
            raise NHMarketError('requested_evidence_hash_unavailable')
        manifest = json.loads(manifest_path.read_bytes())
        hashes = manifest.get('output_hashes', manifest)
        relative = cached.relative_to(self.evidence_root).as_posix()
        # Captures made on Windows used native separators in the manifest.
        expected = hashes.get(relative, hashes.get(relative.replace('/', '\\')))
        if not isinstance(expected, str) or len(expected) != 64:
            raise NHMarketError('requested_evidence_hash_unavailable')
        raw = cached.read_bytes()
        if hashlib.sha256(raw).hexdigest() != expected:
            raise NHMarketError('cached_market_response_hash_mismatch')
        response = json.loads(raw)
        if response.get('path') != path or response.get('inputs') != inputs:
            raise NHMarketError('cached_market_request_mismatch')
        if response.get('http_status') in (400, 404):
            raise NHMarketError('market_http_' + str(response['http_status']))
        return response, cached
