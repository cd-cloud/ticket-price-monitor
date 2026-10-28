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
    tailSearchScopeInput:{value:'open'},tailProviderSelect:{value:'feizhu'},tailModeInput:{value:'explore'},
    tailOpenScopeNotice:{hidden:true},tailBudgetCitiesInput:{value:'10'},tailTargetInput:{value:'2'},
    tailHybridScopeNotice:{hidden:true},tailFollowupBudgetInput:{value:'5'},tailCallBudgetInput:{value:'20'},
    candidates:{hidden:false},
  };
  const ctx = vm.createContext({URL, Date, document:{getElementById:id=>elements[id],querySelector:()=>elements.candidates},
    escapeHtml: x=>String(x??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])),
    airportCodeDisplayText:x=>x||'', providerLabel:x=>x, cabinLabel:x=>x||'', formatQueryDate:x=>x,
    tailResultConfidence:()=>({level:'medium',label:'需复核'}), tailResultConfidenceScore:()=>50,
    cleanVisibleText:()=>{},updateWorkspaceTabs:()=>{},renderTailSummary:()=>{},
  });
  for (const name of ['compareTailResults','tailQuoteFresh','tailQuoteVerified','prepareTailResults',
    'groupTailResultsByRoute','safeTailReviewUrl','tailQuoteTermsLabel','renderTailBaselines','renderTailFlightGroups',
    'renderTailResultCard','renderTailRouteGroup','renderTailResults','updateTailSearchScope','renderTailPriorities',
    'tailAttemptPhaseLabel']) {
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
test('open destination controls preserve hidden budgets but disable them and reset on unsupported platform',()=>{
  const {ctx,elements}=ui();
  ctx.updateTailSearchScope();
  assert.equal(elements.tailOpenScopeNotice.hidden,false);
  assert.equal(elements.candidates.hidden,true);
  assert.equal(elements.tailBudgetCitiesInput.disabled,true);
  assert.equal(elements.tailTargetInput.disabled,true);
  assert.equal(elements.tailBudgetCitiesInput.value,'10');
  elements.tailProviderSelect.value='ctrip';
  ctx.updateTailSearchScope();
  assert.equal(elements.tailSearchScopeInput.value,'candidates');
  assert.equal(elements.tailSearchScopeInput.disabled,true);
  assert.equal(elements.candidates.hidden,false);
  elements.tailProviderSelect.value='feizhu';elements.tailModeInput.value='refresh';
  elements.tailSearchScopeInput.value='open';
  ctx.updateTailSearchScope();
  assert.equal(elements.tailSearchScopeInput.value,'candidates');
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
test('CLI trial notice survives public metadata and is escaped, price scope stays unknown',()=>{
  const {ctx}=ui();
  const html=ctx.renderTailResultCard(quote({search_metadata:{system_message:'体验模式 <script>alert(1)</script>'}}),1);
  assert.match(html,/体验模式/);
  assert.doesNotMatch(html,/<script>/);
  assert.match(html,/整段总价未核实/);
  assert.match(html,/未返回不代表无航线/);
});
test('documented adultPrice source is displayed without asserting a total fare',()=>{
  const {ctx}=ui();
  const html=ctx.renderTailResultCard(quote({quote_terms:{source_price_field:'adultPrice'}}),1);
  assert.match(html,/平台 adultPrice · 整段总价未核实/);
});
test('inconclusive and legacy not_found rechecks do not assert delisting',()=>{
  const {ctx}=ui();
  for (const status of ['inconclusive','not_found']) {
    const html=ctx.renderTailResultCard(quote({recheck_outcome:{status}}),1);
    assert.match(html,/无法确认原行程/);
    assert.match(html,/可重试/);
    assert.match(html,/不代表已下架/);
    assert.doesNotMatch(html,/旧价已失效/);
  }
});
test('result cards distinguish first-leg and entire-itinerary filter scopes',()=>{
  const {ctx}=ui();
  assert.match(ctx.renderTailResultCard(quote({filter_scope:'first_leg'}),1),/舱位／航司仅约束首段/);
  assert.match(ctx.renderTailResultCard(quote({filter_scope:'all_legs'}),1),/舱位／航司约束全行程/);
});
test('hybrid controls retain seed cities and expose separate finite budgets',()=>{
  const {ctx,elements}=ui();
  elements.tailSearchScopeInput.value='hybrid';
  ctx.updateTailSearchScope();
  assert.equal(elements.candidates.hidden,false);
  assert.equal(elements.tailHybridScopeNotice.hidden,false);
  assert.equal(elements.tailOpenScopeNotice.hidden,true);
  assert.equal(elements.tailBudgetCitiesInput.disabled,true);
  assert.equal(elements.tailTargetInput.disabled,true);
  assert.equal(elements.tailFollowupBudgetInput.disabled,false);
  assert.equal(elements.tailCallBudgetInput.disabled,false);
  elements.tailProviderSelect.value='ctrip';
  ctx.updateTailSearchScope();
  assert.equal(elements.tailSearchScopeInput.value,'candidates');
  assert.equal(elements.tailCallBudgetInput.disabled,true);
  assert.equal(elements.tailFollowupBudgetInput.disabled,true);
});
test('priority explanations escape source data and do not promise bookability',()=>{
  const {ctx}=ui();
  const html=ctx.renderTailPriorities({ranking:'contextual_history_v1',
    provider_calls:{used:3,limit:20},next_candidates:[{destination:'CAN',reasons:['<img src=x onerror=x>','近期有效联程证据']}]});
  assert.match(html,/3 \/ 20/);
  assert.match(html,/非可售保证/);
  assert.match(html,/近期有效联程证据/);
  assert.doesNotMatch(html,/<img/);
  assert.equal(ctx.renderTailPriorities({}),'');
});
test('inconclusive search stays retryable and UI no longer offers absence cache bypass',()=>{
  const {ctx}=ui();
  assert.match(ctx.tailAttemptPhaseLabel('search_inconclusive'),/覆盖未确认，可重试/);
  assert.doesNotMatch(ctx.tailAttemptPhaseLabel('search_inconclusive'),/确认无航班/);
  assert.doesNotMatch(source,/忽略 6 小时无结果缓存/);
});
