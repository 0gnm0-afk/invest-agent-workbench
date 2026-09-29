"""Render synthetic positive chart evidence; never substitutes for market results."""
from pathlib import Path
import json
import hashlib
from PIL import Image
from test_momentum_entry import vcp,cup,spr,m,review

if __name__=='__main__':
    out=Path(__file__).resolve().parents[1]/'runtime/momentum-synthetic-evidence-20260929'
    out.mkdir(exist_ok=True)
    result=[]
    for kind,make in [('range_breakout',vcp),('vcp',vcp),('cup_handle',cup),('sperandeo_s4',vcp)]:
        bars=make()
        if kind=='vcp':bars[-1].update(open=212.,low=211.95,close=214.,high=214.3)
        analysis=m.analyze(bars,95,{'supportive':True},spr(bars))
        event=next(e for e in analysis['active_events'] if e['kind']==kind)
        entry={'market':'KR','symbol':'SYNTHETIC','name':'합성 검증 · 실제 종목 아님','as_of':bars[-1]['date'],'entry_signal':event}
        path=out/(kind+'.png');review.draw(bars,path,entry)
        with Image.open(path) as im:im.verify()
        assert event['known_at']<=event['breakout_date']
        assert event['entry_zone']['lower_exclusive']<bars[-1]['close']<=event['entry_zone']['upper_inclusive']
        result.append({'kind':kind,'event':event,'image':path.name,'sha256':hashlib.sha256(path.read_bytes()).hexdigest()})
    review.write(out/'evidence.json',result)
    (out/'index.html').write_text('<!doctype html><meta charset="utf-8"><h1>합성 양성 사례 · 실제 종목/성과 아님</h1>'+''.join('<h2>'+r['kind']+'</h2><img style="max-width:100%" src="'+r['image']+'">' for r in result),encoding='utf-8')
    print(json.dumps({'synthetic_charts':len(result),'decoded':len(result),'output':str(out)},ensure_ascii=False))
