// Unit test the generated filter handler against a minimal DOM contract.
// This does not launch/automate a browser or claim visual validation.
const fs=require('fs'),vm=require('vm'),assert=require('assert');
const data=JSON.parse(fs.readFileSync(process.argv[2],'utf8'));
const buttons=data.filters.map(filter=>({dataset:{filter},attributes:{},setAttribute(k,v){this.attributes[k]=v},addEventListener(k,cb){this.click=cb}}));
const sections=data.sections.map(rows=>({cards:rows.map(dataset=>({dataset,hidden:false})),empty:{hidden:true},querySelector(q){return q==='.empty'?this.empty:this.cards.find(c=>!c.hidden)}}));
const document={querySelectorAll(q){return q==='[data-filter]'?buttons:q==='[data-state]'?sections.flatMap(s=>s.cards):sections}};
vm.runInNewContext(data.script,{document});
const results={};
for(const b of buttons){
  b.click();
  const filter=b.dataset.filter;
  for(const s of sections){
    for(const card of s.cards){
      const d=card.dataset;
      const expected=filter==='all'||(filter==='changed'?d.changed==='true':filter==='concentrated'?d.concentrated==='true':d.state===filter);
      assert.strictEqual(!card.hidden,expected);
    }
    assert.strictEqual(s.empty.hidden,s.cards.some(c=>!c.hidden));
  }
  assert.strictEqual(buttons.filter(x=>x.attributes['aria-pressed']==='true').length,1);
  results[filter]=sections.flatMap(s=>s.cards).filter(c=>!c.hidden).length;
}
console.log(JSON.stringify({filter_unit_checks:buttons.length,visible_counts:results,browser_visual_validation:false},null,2));
