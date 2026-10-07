"""NH market authentication uses the same credential cache/lock as account reads.

This module makes no account requests. MarketClient retains its endpoint allowlist.
"""
import json
import sys
from pathlib import Path

_CONFIG = json.loads((Path(__file__).resolve().parents[1] / 'config.json').read_text(encoding='utf-8'))
_MOTHER_SCRIPTS = str(Path(_CONFIG['mother']) / 'scripts')
if _MOTHER_SCRIPTS not in sys.path:
    sys.path.append(_MOTHER_SCRIPTS)
from nh_auth import (BASE, ROOT, NHError as NHMarketError, NoRedirect,
                     protect as _protect, token, http_error, provider_code)
