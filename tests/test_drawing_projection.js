const assert=require('node:assert/strict');
global.window={};require('../dashboard/drawing-tools.js');const D=window.DrawingTools;
const daily=[{date:'2026-09-21'},{date:'2026-09-22'},{date:'2026-09-23'},{date:'2026-09-28'}];
const weekly=[{date:'2026-09-21',first_session:'2026-09-21',last_session:'2026-09-23'},{date:'2026-09-28',first_session:'2026-09-28',last_session:'2026-09-30'}];
const monthly=[{date:'2026-09-01',first_session:'2026-09-01',last_session:'2026-09-30'}];
assert.equal(D.index('2026-09-22',daily),1);
for(const [bars,frame] of [[weekly,'1w'],[monthly,'1mo']]){
  const a=D.index('2026-09-21',bars,frame),b=D.index('2026-09-22',bars,frame);
  assert.ok(Number.isFinite(a)&&Number.isFinite(b)&&a<b,'same-period anchors remain distinct');
  assert.equal(D.timeAt(b,bars,frame),'2026-09-22');
  assert.equal(D.index('2026-10-01',bars,frame),null);
}
const original=JSON.stringify(monthly);D.index('2026-09-22',monthly,'1mo');assert.equal(JSON.stringify(monthly),original);
assert.equal(D.index('2026-09-20',daily),null);
assert.ok(Number.isFinite(D.index('2026-09-26',daily)),'weekly/monthly calendar anchors project between daily candles');

const scale={logicalToCoordinate:i=>Number.isInteger(i)?i*10:0};
assert.equal(D.coordinate(scale,1.5),15);assert.equal(D.coordinate(scale,-.4),-4);
assert.notEqual(D.coordinate(scale,D.index('2026-09-21',monthly,'1mo')),D.coordinate(scale,D.index('2026-09-22',monthly,'1mo')));

console.log('drawing projection: all assertions passed');
