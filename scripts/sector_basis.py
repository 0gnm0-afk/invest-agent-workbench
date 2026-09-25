"""Official provider semantics applied only to the local sector observation path."""
FAQ_URL = 'https://openapi.tossinvest.com/openapi-docs/faq.md'
CONTRACT = 'toss_daily_faq_observed_20260925'


def load_basis(root, checked, load, inputs, applied, age_issue):
    path = root/'calculation-basis.json'
    if not path.exists():
        return None
    config = load(checked(path, None, inputs))
    if config.get('contract') != CONTRACT:
        raise ValueError('unknown_calculation_basis_contract')
    if config.get('kr_reference') != 'saved_universe_equal_weight':
        raise ValueError('approved_KR_reference_required')
    docs = []
    for entry in config['documents']:
        doc = (root/'sources'/entry['file']).resolve()
        if not doc.is_relative_to((root/'sources').resolve()):
            raise ValueError('basis_document_outside_root')
        meta = load(checked(doc.with_suffix(doc.suffix+'.meta.json'), None, inputs))
        checked(doc, entry['sha256'], inputs)
        checked(doc, meta['sha256'], inputs)
        if meta['url'] != entry['url'] or meta['final_url'] != entry['url'] or meta['status'] != 200:
            raise ValueError('basis_document_identity_mismatch')
        if age_issue(meta['observed_at'], applied):
            raise ValueError('basis_document_not_current')
        docs.append({**meta, 'path':str(doc)})
    if not any(d['url'] == FAQ_URL for d in docs):
        raise ValueError('official_toss_faq_required')
    return {**config, 'evidence':docs}


def series_basis(series):
    """Validate saved request metadata. Returned bars are already adjusted by Toss."""
    if not series:
        return None, 'series_missing'
    market = series.get('market')
    if (market == 'KR' and series.get('provider') == 'Toss OpenAPI candles' and
            series.get('source_url') == 'https://openapi.tossinvest.com/api/v1/market-indicators/KOSPI/candles' and
            series.get('currency') == 'KRW' and series.get('provider_symbol') == 'KOSPI' and
            series.get('instrument_type') == 'INDEX' and series.get('price_basis') == 'toss_index_points_observation_only'):
        return {'contract':CONTRACT, 'market':'KR', 'price_session':'KR_KRX_regular_index_close',
                'price_adjustment':'cash_index_points'}, None
    if (market not in ('KR','US') or series.get('provider') != 'Toss OpenAPI candles' or
            series.get('source_url') != 'https://openapi.tossinvest.com/api/v1/candles'):
        return None, 'unverified_price_provider_or_session'
    if series.get('currency') != ('KRW' if market == 'KR' else 'USD'):
        return None, 'basis_currency_mismatch'
    if (series.get('price_basis') != 'toss_adjusted_ohlc_no_order_authority' or
            series.get('collection',{}).get('adjusted') is not True):
        return None, 'adjusted_price_and_volume_required'
    return {'contract':CONTRACT, 'market':market,
            'price_session':'KR_integrated_provider_daily_close' if market == 'KR' else 'US_regular_official_close',
            'price_adjustment':'provider_price_times_adjustment_ratio_rounded_no_local_readjustment',
            'volume_adjustment':'provider_volume_divided_by_adjustment_ratio_floored',
            'volume_session':'provider_daily_comprehensive_including_off_session_trades',
            'return_semantics':'provider_adjusted_close_return_not_certified_reinvested_total_return',
            'volume_semantics':'relative_provider_activity_not_consolidated_market_flow'}, None


def comparison_issue(stock_basis, benchmark_basis):
    if not stock_basis or not benchmark_basis:
        return 'benchmark_basis_unverified'
    for field in ('market','price_session','price_adjustment'):
        if stock_basis.get(field) != benchmark_basis.get(field):
            return 'benchmark_'+field+'_mismatch'
    return None
