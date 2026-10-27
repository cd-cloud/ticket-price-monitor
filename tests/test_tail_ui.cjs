const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const source = fs.readFileSync(path.join(__dirname, '../static/app.js'), 'utf8');
function ui() {
  const elements = {
    tailLatestOnlyInput: {checked:true}, tailSortSelect:{value:'price'}, tailTrustInput:{value:'all'},
    tailDateFilter:{value:''}, tailMaxPriceInput:{value:''}, tailResults:{innerHTML:''},
  };
  const ctx = vm.createContext({URL, Date, document:{getElementById:id=>elements[id]},
    escapeHtml: x=>String(x??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])),
    airportCodeDisplayText:x=>x||'', providerLabel:x=>x, cabinLabel:x=>x||'', formatQueryDate:x=>x,
    tailResultConfidence:()=>({level:'medium',label:'需复核'}), tailResultConfidenceScore:()=>50,
    cleanVisibleText:()=>{},updateWorkspaceTabs:()=>{},renderTailSummary:()=>{},
  });
  for (const name of ['compareTailResults','tailQuoteFresh','tailQuoteVerified','prepareTailResults',
    'groupTailResultsByRoute','safeTailReviewUrl','tailQuoteTermsLabel','renderTailBaselines','renderTailFlightGroups',
    'renderTailResultCard','renderTailRouteGroup','renderTailResults']) {
    const start = source.indexOf(`function ${name}(`);
    assert.ok(start>=0,name);
    const rest=source.slice(start+1);
    const end=rest.search(/\n(?:async )?function /);
    vm.runInContext(source.slice(start, end<0?undefined:start+1+end),ctx);
  }
  return {ctx,elements};
}
function quote(extra={}) {
  return {id:1,itinerary_key:'one',first_leg_key:'CA1001',origin:'BJS',transfer:'SHA',destination:'CAN',
    provider:'feizhu',currency:'CNY',cabin:'economy',passengers:1,price:700,departure_date:'2027-06-20',
    observed_at:'2026-10-27T00:00:00Z',expires_at:new Date(Date.now()+60000).toISOString(),
    validation:{status:'valid'},flight_details:[{origin:'BJS',destination:'SHA',flight_no:'CA1001',departure_time:'09:00',arrival_time:'11:00'}],
    ...extra};
}
test('latest identity is chosen before price filtering; stale cheap quote cannot reappear',()=>{
  const {ctx,elements}=ui(); elements.tailMaxPriceInput.value='800';
  assert.equal(ctx.prepareTailResults([quote({price:600}),quote({id:2,price:900,observed_at:'2026-10-27T01:00:00Z'})]).length,0);
});
test('two flights to the same tail city both remain visible',()=>{
  const {ctx}=ui(); assert.equal(ctx.prepareTailResults([quote(),quote({itinerary_key:'two',first_leg_key:'CA1003'})]).length,2);
});
test('fresh verified and expired review partitions are separate',()=>{
  const {ctx,elements}=ui(); elements.tailTrustInput.value='verified';
  assert.equal(ctx.prepareTailResults([quote(),quote({itinerary_key:'old',expires_at:'2020-01-01T00:00:00Z'})]).length,1);
  elements.tailTrustInput.value='all';
  ctx.renderTailResults([quote(),quote({itinerary_key:'old',expires_at:'2020-01-01T00:00:00Z'})]);
  assert.match(elements.tailResults.innerHTML,/行程已校验/); assert.match(elements.tailResults.innerHTML,/待复核／历史报价/);
});
test('same first flight groups tail alternatives without mixing currencies',()=>{
  const {ctx}=ui(); let html=ctx.renderTailFlightGroups([quote(),quote({destination:'SZX',itinerary_key:'two'})]);
  assert.equal((html.match(/<h4>/g)||[]).length,1);
  html=ctx.renderTailFlightGroups([quote(),quote({currency:'USD',itinerary_key:'two'})]);
  assert.equal((html.match(/<h4>/g)||[]).length,2);
});
test('unknown terms never show guaranteed savings and include recheck action',()=>{
  const {ctx}=ui(); const html=ctx.renderTailResultCard(quote(),1);
  assert.match(html,/口径未确认，不计算节省金额/); assert.match(html,/data-tail-action="recheck"/);
  assert.match(html,/行李：未核实/); assert.match(html,/人数库存未校验/);
});
test('review links reject executable schemes and escape source text',()=>{
  const {ctx}=ui(); const html=ctx.renderTailResultCard(quote({review_url:'javascript:alert(1)',destination:'<img src=x>'}),1);
  assert.doesNotMatch(html,/href="javascript:/); assert.doesNotMatch(html,/<img src=x>/);
  assert.equal(ctx.safeTailReviewUrl('https://example.test/x'),'https://example.test/x');
});
