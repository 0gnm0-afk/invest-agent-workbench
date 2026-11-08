"""Recompute current screening from verified saved prices; preserve immutable run files."""
import hashlib,json
from pathlib import Path
from derived_cache import publish, read_lock
import screening.momentum as momentum_review, screening.watch as watch_review, screening.downtrend as downtrend_approach, screening.trendlines as recent_trendlines, screening.sperandeo as recent_sperandeo
from invest_agent.trading import momentum_entry,momentum_watch,base_structure,market,sperandeo


def cached(root,screen,snapshot,review,source_hash,options):
    cfg={**review['config'],'trendline_policy':recent_trendlines.VERSION}
    cfg['watchlist']={**cfg['watchlist'],'downtrend_approach':downtrend_approach.settings(options)}
    modules = dict(momentum_review=momentum_review, watch_review=watch_review,
                   downtrend_approach=downtrend_approach, recent_trendlines=recent_trendlines,
                   recent_sperandeo=recent_sperandeo)
    modules.update({m.__name__:m for m in (momentum_entry,momentum_watch,base_structure,market,
                                         sperandeo,recent_trendlines.trendln)})
    signature=dict(source=source_hash,screen=screen,config=cfg,
        code={name:hashlib.sha256(Path(module.__file__).read_bytes()).hexdigest() for name,module in modules.items()})
    key=hashlib.sha256(json.dumps(signature,sort_keys=True).encode()).hexdigest()
    folder=Path(root)/'runtime/recent-screen'/key
    path=folder/'review.json'
    with read_lock(folder) as readable:
        if readable and path.exists():
            record=json.loads(path.read_text(encoding='utf-8'))
            result=record['review']
            if record['sha256']!=hashlib.sha256(json.dumps(result,sort_keys=True).encode()).hexdigest():
                raise ValueError('recent_screen_cache_hash_mismatch')
            return result
    result=momentum_review.build(screen,snapshot,cfg)
    publish(root, 'recent-screen', key, {'review.json':json.dumps(dict(review=result,
        sha256=hashlib.sha256(json.dumps(result,sort_keys=True).encode()).hexdigest()),ensure_ascii=False,allow_nan=False)})
    return result
