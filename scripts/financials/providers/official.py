"""Official financial sources behind one replaceable, internal boundary.

Collector retains SEC/DART identity checks and issuer IR supplements. Saved
normalization never instantiates a collector or reads authentication settings.
"""
from copy import deepcopy
from dataclasses import dataclass
from datetime import date, datetime, timezone
import hashlib
from pathlib import Path
import re

from financials.business import unsupported
from financials.collect import Collector, SourceError
from financials.normalize import dart_normalize, sec_normalize


@dataclass(frozen=True)
class FinancialPeriod:
    """Describe the existing policy, not a new user-selectable date range.

    Collector and normalizers own the six-year lookback. All their retained
    rows reach calculation before its existing five-year/twelve-quarter view.
    """
    start_year: int
    end_year: int
    annual_limit: int = 5
    quarterly_limit: int = 12

    @classmethod
    def current(cls):
        year = date.today().year
        return cls(year - 6, year)


def _request(key, period):
    if not isinstance(key, str) or not re.fullmatch(
            r'(KR:[0-9]{6}(?:\.KS|\.KQ)?|US:[A-Z0-9][A-Z0-9.\-]{0,14})', key):
        raise ValueError('invalid_financial_symbol')
    if period != FinancialPeriod.current():
        raise ValueError('unsupported_financial_period')
    return key.split(':', 1)


def validate_sources(sources, raw_folder):
    """Return deduplicated receipts only after checking their exact raw bytes.

    A repeated request for identical bytes may update its receipt timestamp;
    conflicting bodies under one ID cannot be silently collapsed into a bundle.
    No paths outside this snapshot's raw folder are read.
    """
    folder = Path(raw_folder).resolve()
    if not isinstance(sources, (list, tuple)):
        raise SourceError('INVALID_FINANCIAL_SOURCES')
    by_id = {}
    for receipt in sources:
        if not isinstance(receipt, dict):
            raise SourceError('INVALID_FINANCIAL_SOURCE')
        name = receipt.get('id')
        digest = receipt.get('sha256')
        size = receipt.get('bytes')
        if (not isinstance(name, str) or not name or name in ('.', '..')
                or any(c in name for c in ('/', '\\', ':'))):
            raise SourceError('INVALID_FINANCIAL_SOURCE_PATH')
        if (not isinstance(digest, str) or not re.fullmatch(r'[a-f0-9]{64}', digest)
                or not isinstance(size, int) or isinstance(size, bool) or size < 0
                or not isinstance(receipt.get('url'), str) or not receipt['url']
                or not isinstance(receipt.get('fetched_at'), str) or not receipt['fetched_at']):
            raise SourceError('INVALID_FINANCIAL_SOURCE_RECEIPT')
        previous = by_id.get(name)
        if previous and (previous['sha256'], previous['bytes']) != (digest, size):
            raise SourceError('FINANCIAL_SOURCE_ID_CONFLICT')
        by_id[name] = deepcopy(receipt)
    for name, receipt in by_id.items():
        path = (folder / name).resolve()
        if not path.is_relative_to(folder):
            raise SourceError('INVALID_FINANCIAL_SOURCE_PATH')
        try:
            raw = path.read_bytes()
        except FileNotFoundError:
            raise SourceError('FINANCIAL_SOURCE_BYTES_UNAVAILABLE') from None
        if len(raw) != receipt['bytes'] or hashlib.sha256(raw).hexdigest() != receipt['sha256']:
            raise SourceError('FINANCIAL_SOURCE_HASH_MISMATCH')
    return list(by_id.values())


class OfficialFinancialAdapter:
    """SEC/DART plus their existing issuer supplements; no provider settings UI."""

    def collect(self, key, period, raw_folder, progress):
        market, symbol = _request(key, period)
        collector = Collector(raw_folder, progress)
        # usa already includes official issuer IR PDFs and SEC filing extras;
        # korea already includes its annual DART supplement policy.
        source = (collector.korea(symbol.split('.')[0]) if market == 'KR'
                  else collector.usa(symbol))
        return self.normalize_saved(key, period, source, collector.sources, raw_folder, progress)

    def normalize_saved(self, key, period, source, sources, raw_folder, progress):
        market, symbol = _request(key, period)
        provider = source.get('provider')
        if provider not in ('SEC', 'DART'):
            raise SourceError('UNSUPPORTED_FINANCIAL_PROVIDER')
        if provider != {'KR': 'DART', 'US': 'SEC'}[market]:
            raise SourceError('FINANCIAL_SOURCE_IDENTITY_MISMATCH')
        source = deepcopy(source)
        source['symbol'] = symbol
        sources = validate_sources(sources, raw_folder)
        progress('공시 기간 정규화 · Python 재무 계산')
        try:
            if source.get('eligibility', {}).get('status') == 'unsupported':
                normalized = unsupported(source)
            else:
                normalized = dart_normalize(source) if provider == 'DART' else sec_normalize(source)
        except (ValueError, SourceError) as exc:
            # Store retains official input for known unsupported formats. Keep
            # the original exception class/message for its existing mapping.
            exc.source = source
            exc.sources = sources
            raise
        return dict(source=source, normalized=normalized, sources=sources)

    def supplement_saved(self, key, period, source, sources, raw_folder, progress):
        market, _ = _request(key, period)
        if source.get('provider') != 'DART':
            # SEC saved recalculation, even with supplement=True, stays offline.
            return self.normalize_saved(key, period, source, sources, raw_folder, progress)
        if market != 'KR':
            raise SourceError('FINANCIAL_SOURCE_IDENTITY_MISMATCH')
        # Verify inherited bytes before any explicit request can replace a file.
        sources = validate_sources(sources, raw_folder)
        source = deepcopy(source)
        from financials.providers.dart_supplement import collect_annual_supplements
        collector = Collector(raw_folder, progress)
        source = collect_annual_supplements(collector, source)
        source['supplemented_at'] = datetime.now(timezone.utc).isoformat()
        return self.normalize_saved(key, period, source, [*sources, *collector.sources], raw_folder, progress)
