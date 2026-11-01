"""Metadata required to identify, verify or increment a price observation."""

PRICE_FIELDS = frozenset({
    'market','symbol','currency','provider','provider_symbol','price_basis','isin_code',
    'name','instrument_type','security_type','listing_exchange_family','raw_exchange_code',
    'benchmark','benchmark_proxy','benchmark_proxy_reason','source_url','source_sha256',
    'collected_at','requested_start','requested_end','first','last','state','reason',
    'provider_end_reached','adjusted','correction_reason',
})
LISTING_FIELDS = frozenset({
    'market','symbol','provider_symbol','name','isinCode','securityType','isCommonShare',
    'listing_exchange','listing_exchange_family',
})
COLLECTION_FIELDS = frozenset({
    'first_requested_session','last_requested_session','adjusted','full_history_collected_at','full_history_requested_from',
    'mode','new_bars','overlap_bars','refresh_reason','boundary',
})
PAGE_FIELDS = frozenset({'before','returned_count','next_before','requested_count','page_size','source_sha256'})
HISTORY_FIELDS = frozenset({
    'state','reason','screening_price_difference','requested_start','requested_end',
    'first','last','daily_count','source_id','can_retry','daily_update_mode','daily_update_reason',
})


def _scalars(value, fields):
    return {k:v for k,v in value.items() if k in fields and (v is None or type(v) in (str,int,float,bool))}


def observation_metadata(value):
    """Run snapshots retain universe/financial fields; this projection is for PriceStore only."""
    result = _scalars(value, PRICE_FIELDS)
    listing = value.get('listing')
    if isinstance(listing,dict):
        result['listing'] = _scalars(listing,LISTING_FIELDS)
    responses = value.get('responses')
    if isinstance(responses,list) and all(isinstance(h,str) for h in responses):
        result['responses'] = responses
    collection = value.get('collection')
    if isinstance(collection,dict):
        data = _scalars(collection,COLLECTION_FIELDS)
        if isinstance(collection.get('pages'),list):
            data['pages'] = [_scalars(p,PAGE_FIELDS) for p in collection['pages'] if isinstance(p,dict)]
        prior = collection.get('prior_source')
        if isinstance(prior,dict):
            data['prior_source'] = _scalars(prior,{'path','sha256','series_id','revision'})
        elif 'prior_source' in collection and prior is None:
            data['prior_source'] = None
        result['collection'] = data
    history = value.get('selected_history')
    if isinstance(history,dict):
        result['selected_history'] = _scalars(history,HISTORY_FIELDS)
    return result
