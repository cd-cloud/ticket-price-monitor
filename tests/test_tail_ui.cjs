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
  const ctx = vm.createContext({URL, URLSearchParams, Date, AbortController,
    tailResultView:"history", tailResultsLoaded:true, tailSelectedJobId:"job", activeTailJobId:"",tailConnectionUnknown:false,
    tailDisclosureState:new Map(), window:{innerHeight:900}, tailDiscoveryResults:[],
    document:{getElementById:id=>elements[id],querySelector:()=>elements.candidates,querySelectorAll:()=>[]},
    escapeHtml: x=>String(x??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])),
    airportCodeDisplayText:x=>x||'', providerLabel:x=>x, cabinLabel:x=>x||'', formatQueryDate:x=>x,formatQueryTime:x=>x,
    tailResultConfidence:()=>({level:'medium',label:'需复核'}), tailResultConfidenceScore:()=>50,
    cleanVisibleText:()=>{},updateWorkspaceTabs:()=>{},renderTailSummary:()=>{},
    updateTailBusyControls:()=>{},
  });
  for (const name of ['compareTailResults','tailQuoteFresh','tailQuoteVerified','prepareTailResults',
    'groupTailResultsByRoute','safeTailReviewUrl','tailQuoteTermsLabel','renderTailBaselines','renderTailFlightGroups',
    'renderTailResultCard','renderTailRouteGroup','renderTailResults','updateTailSearchScope','renderTailPriorities',
    'tailAttemptPhaseLabel','tailPlaceLabel','replaceTailMarkup','tailConditionLabel',
    'tailQueryDiffers','updateTailResultContext','updateTailResultTools',
    'tailComparableBaseline','tailDecisionDifference']) {
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
  assert.match(elements.tailResults.innerHTML,/行程已校验/); assert.match(elements.tailResults.innerHTML,/已过期 \/ 时间未知/);
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
test('unknown date evidence is distinguished from a conflicting date',()=>{
  const {ctx}=ui();
  assert.match(ctx.tailAttemptPhaseLabel('date_unconfirmed'),/证据不足，可重试/);
  assert.match(ctx.tailAttemptPhaseLabel('date_mismatch'),/不一致/);
  assert.doesNotMatch(ctx.tailAttemptPhaseLabel('date_unconfirmed'),/无航班/);
});

test('query comparison normalizes code lists and numbers and detects advanced edits',()=>{
  const {ctx}=ui();
  ctx.normalizeCode=value=>String(value).trim().toUpperCase();
  const saved={origin:'BJS',transfer:'SHA',provider:'feizhu',departure_date:'2027-06-20',
    preferred_airlines:['CA','ZH'],candidates:['CAN','SZX'],max_price:null,max_minutes:10};
  const draft={...saved,origin:' bjs ',preferred_airlines:'ZH, CA,CA',candidates:['SZX','CAN'],
    max_price:0,max_minutes:'10'};
  assert.equal(ctx.tailQueryDiffers(saved,draft),false);
  for (const patch of [{departure_date:'2027-06-21'},{max_minutes:11},{candidates:['CAN']},
    {preferred_airlines:'MU'},{provider:'ctrip'}]) {
    assert.equal(ctx.tailQueryDiffers(saved,{...draft,...patch}),true);
  }
  assert.equal(ctx.tailQueryDiffers({origin:'BJS'},{origin:'BJS',passengers:2}),false,
    'missing legacy settings must not be guessed');
});

test('query comparison ignores unsupported and open-scope controls, not effective budgets',()=>{
  const {ctx}=ui();ctx.normalizeCode=x=>x;
  assert.equal(ctx.tailQueryDiffers({provider:'ctrip',max_provider_calls:0},
    {provider:'ctrip',max_provider_calls:20}),false);
  const saved={provider:'feizhu',search_scope:'open',candidates:['CAN'],max_candidates:10,
    target_results:2,max_minutes:15,max_provider_calls:20};
  assert.equal(ctx.tailQueryDiffers(saved,{...saved,candidates:[],max_candidates:0,target_results:0}),false);
  assert.equal(ctx.tailQueryDiffers(saved,{...saved,max_minutes:20}),true);
  assert.equal(ctx.tailQueryDiffers(saved,{...saved,max_provider_calls:10}),true);
});

test('result identity stays tied to selected task while draft changes, history has separate context',()=>{
  const {ctx,elements}=ui();
  for (const id of ['tailResultIdentity','tailDraftNotice','tailTaskConditions']) elements[id]={};
  const payload={origin:'BJS',transfer:'SHA',departure_date:'2027-06-20',provider:'ctrip'};
  let draft={...payload};
  Object.assign(ctx,{tailResultView:'current',tailSelectedJobId:'a',
    tailSelectedJob:{job_id:'a',payload},tailFormTouched:true,normalizeCode:x=>x,
    buildTailQueryPayload:()=>draft});
  ctx.updateTailResultContext();
  assert.equal(elements.tailDraftNotice.hidden,true);
  const identity=elements.tailResultIdentity.textContent;
  draft.departure_date='2027-06-21';
  ctx.updateTailResultContext();
  assert.equal(elements.tailResultIdentity.textContent,identity);
  assert.match(elements.tailDraftNotice.textContent,/条件已修改.*所选任务/);
  assert.equal(elements.tailDraftNotice.hidden,false);
  ctx.tailSelectedJobId='loading';
  ctx.updateTailResultContext();
  assert.doesNotMatch(elements.tailResultIdentity.textContent,/BJS/);
  assert.match(elements.tailResultIdentity.textContent,/正在读取/);
  ctx.tailResultView='history';ctx.updateTailResultContext();
  assert.match(elements.tailResultIdentity.textContent,/跨任务/);
  assert.match(elements.tailDraftNotice.textContent,/不会改变历史报价/);
  assert.equal(elements.tailTaskConditions.textContent,'');
});

test('filter summary remains visible when controls close; clear restores latest-only',()=>{
  const {ctx,elements}=ui();
  elements.tailResultFilterSummary={};elements.tailViewResultsBtn={hidden:true};
  elements.tailDateFilter.value='2027-06-20';elements.tailMaxPriceInput.value='800';
  elements.tailLatestOnlyInput.checked=false;ctx.tailDiscoveryResults=[quote()];
  ctx.updateTailResultTools();
  assert.match(elements.tailResultFilterSummary.textContent,/3 项/);
  assert.equal(elements.tailViewResultsBtn.hidden,false);
  loadFunctions(ctx,['clearTailResultFilters']);
  ctx.clearTailResultFilters();
  assert.equal(elements.tailLatestOnlyInput.checked,true);
  assert.equal(elements.tailResultFilterSummary.textContent,'筛选');
});

function comparableQuote(extra={}) {
  const terms={currency:'CNY',price_basis:'per_person',tax_included:true,priced_passengers:1,
    inventory_verified:true,fare_conditions:'same-flex'};
  return quote({provider:'ctrip',quote_terms:terms,baselines:{
    market:{status:'comparable',price:1000,savings_amount:300,quote_terms:{...terms}},
    same_flight:{status:'comparable',price:1100,savings_amount:400,quote_terms:{...terms}},
  },...extra});
}

test('decision difference prefers same-flight comparison and distinctly labels market fallback',()=>{
  const {ctx}=ui(); const item=comparableQuote();
  assert.match(ctx.tailDecisionDifference(item),/比同首段单买低 CNY 400/);
  delete item.baselines.same_flight;
  assert.match(ctx.tailDecisionDifference(item),/比符合条件的直飞最低价低 CNY 300/);
  const html=ctx.renderTailResultCard(item,1);
  assert.ok(html.indexOf('可比参考价差')<html.indexOf('<details'),
    'comparable difference must be visible without opening details');
});

test('stale, unverified, unknown, inconsistent and legacy savings never become current decision claims',()=>{
  const {ctx}=ui();
  for (const item of [
    comparableQuote({quote_terms:{}}),
    comparableQuote({validation:{status:'needs_review'}}),
    comparableQuote({savings_amount:999,baselines:{}}),
    comparableQuote({currency:'USD'}),
    comparableQuote({passengers:2}),
  ]) assert.equal(ctx.tailDecisionDifference(item),'暂无可比价差');
  const stale=comparableQuote({expires_at:'2020-01-01'});
  assert.match(ctx.tailDecisionDifference(stale),/历史报价/);
  assert.doesNotMatch(ctx.renderTailResultCard(stale,1),/比同首段单买低/);
  for (const amount of [null,undefined,NaN,999]) {
    const item=comparableQuote();delete item.baselines.market;
    item.baselines.same_flight.savings_amount=amount;
    assert.equal(ctx.tailDecisionDifference(item),'暂无可比价差');
  }
});

test('negative and zero differences are not presented as savings',()=>{
  const {ctx}=ui();const item=comparableQuote();delete item.baselines.market;
  item.baselines.same_flight.price=650;item.baselines.same_flight.savings_amount=-50;
  assert.match(ctx.tailDecisionDifference(item),/比同首段单买高 CNY 50/);
  item.baselines.same_flight.price=700;item.baselines.same_flight.savings_amount=0;
  assert.match(ctx.tailDecisionDifference(item),/参考价相同/);
});

test('cards distinguish desired first leg, actual full route and visible unresolved risks',()=>{
  const {ctx}=ui();
  const item=quote({flight_details:[
    {origin:'BJS',destination:'SHA',departure_airport:'PEK',arrival_airport:'SHA',flight_no:'CA1001'},
    {origin:'SHA',destination:'CAN',departure_airport:'SHA',arrival_airport:'CAN'},
  ]});
  const html=ctx.renderTailFlightGroups([item]);
  assert.match(html,/想乘坐的首段 · BJS → SHA/);
  assert.match(html,/完整购票行程/);
  assert.match(html,/PEK → SHA → CAN/);
  assert.ok(html.indexOf('价格及出票需复核')<html.indexOf('<details'));
  assert.match(ctx.renderTailResultCard(quote(),1),/候选购票行程 · 待核实/);
});

test('full itinerary preserves an airport change rather than silently skipping its departure airport',()=>{
  const {ctx}=ui();
  const html=ctx.renderTailResultCard(quote({flight_details:[
    {departure_airport:'PEK',arrival_airport:'SHA'},
    {departure_airport:'PVG',arrival_airport:'CAN'},
  ],suitability:{airport_change:true}}),1);
  assert.match(html,/PEK → SHA → PVG → CAN/);
  assert.match(html,/涉及换机场/);
});

function loadFunctions(ctx, names) {
  for (const name of names) {
    const pattern = new RegExp(`^(?:async )?function ${name}\\(`, 'm');
    const start = source.search(pattern);
    assert.ok(start >= 0, name);
    const next = source.slice(start + 1).search(/\n(?:async )?function /);
    vm.runInContext(source.slice(start, next < 0 ? undefined : start + 1 + next), ctx);
  }
}

test('review and expired filters are distinct and do not upgrade unknown data',()=>{
  const {ctx,elements}=ui();
  const rows=[quote(),quote({id:2,itinerary_key:'partial',validation:{status:'needs_review'}}),
    quote({id:3,itinerary_key:'old',expires_at:'2020-01-01'})];
  elements.tailTrustInput.value='review';
  assert.deepEqual(Array.from(ctx.prepareTailResults(rows),r=>r.id),[2]);
  elements.tailTrustInput.value='stale';
  assert.deepEqual(Array.from(ctx.prepareTailResults(rows),r=>r.id),[3]);
});

test('cards show explicit state, taxes and airport-change warnings without raw quality or confidence scores',()=>{
  const {ctx}=ui();
  const html=ctx.renderTailResultCard(quote({detail_quality:'complete',suitability:{airport_change:true}}),1);
  assert.match(html,/行程已校验/);
  assert.match(html,/税费口径未确认/);
  assert.match(html,/涉及换机场/);
  assert.doesNotMatch(html,/高可信|中可信|>complete</);
  assert.match(ctx.renderTailResultCard(quote({expires_at:'2020-01-01'}),1),/已过期 \/ 时间未知/);
});

test('empty states separate hidden results, first search, running and historical empty',()=>{
  const {ctx,elements}=ui();
  elements.tailMaxPriceInput.value='1'; ctx.renderTailResults([quote()]);
  assert.match(elements.tailResults.innerHTML,/被列表筛选隐藏/);
  elements.tailMaxPriceInput.value='';
  ctx.tailResultView='current';ctx.tailSelectedJobId='';ctx.renderTailResults([]);
  assert.match(elements.tailResults.innerHTML,/先告诉我们/);
  ctx.tailSelectedJobId='running';ctx.activeTailJobId='running';ctx.renderTailResults([]);
  assert.match(elements.tailResults.innerHTML,/正在查找/);
  ctx.tailResultView='history';ctx.renderTailResults([]);
  assert.match(elements.tailResults.innerHTML,/还没有已保存/);
});

test('manual candidates survive route/airline edits while newly excluded endpoints are unchecked',()=>{
  const {ctx}=ui();
  const boxes=[{value:'CAN',checked:true},{value:'SZX',checked:false},{value:'BJS',checked:true}];
  ctx.tailCandidatesManual=true;
  ctx.document.querySelectorAll=()=>boxes;
  ctx.tailDefaultCodesForAirlines=()=>['SZX','BJS'];
  ctx.excludedTailDestinationCodes=()=>new Set(['BJS']);
  ctx.updateTailCandidateSummary=()=>{};
  loadFunctions(ctx,['applyTailAirlineDefaults']);
  ctx.applyTailAirlineDefaults();
  assert.equal(boxes[0].checked,true);
  assert.equal(boxes[1].checked,false);
  assert.equal(boxes[2].checked,false);
  assert.equal(boxes[2].disabled,true);
  ctx.tailCandidatesManual=false;ctx.applyTailAirlineDefaults();
  assert.equal(boxes[0].checked,false);assert.equal(boxes[1].checked,true);
});

test('platform-specific inputs hide labels but retain budget values',()=>{
  const {ctx,elements}=ui();
  for (const id of ['tailSearchScopeInput','tailFollowupBudgetInput','tailCallBudgetInput',
    'tailBudgetCitiesInput','tailTargetInput']) {
    elements[id].label={hidden:false};
    elements[id].closest=()=>elements[id].label;
  }
  elements.tailProviderSelect.value='ctrip';ctx.updateTailSearchScope();
  assert.equal(elements.tailCallBudgetInput.label.hidden,true);
  assert.equal(elements.tailFollowupBudgetInput.label.hidden,true);
  assert.equal(elements.tailSearchScopeInput.label.hidden,true);
  assert.equal(elements.tailCallBudgetInput.value,'20');
});

test('disclosure state including explicitly closed sections survives replacement',()=>{
  const {ctx}=ui();
  let children=[{dataset:{disclosure:'quote-1'},open:true},{dataset:{disclosure:'quote-2'},open:false}];
  const container={_html:'old',querySelectorAll:q=>q.startsWith('details')?children:[],
    contains:()=>false,get innerHTML(){return this._html},
    set innerHTML(v){this._html=v;children=[{dataset:{disclosure:'quote-1'},open:false},{dataset:{disclosure:'quote-2'},open:true}]}};
  ctx.replaceTailMarkup(container,'new');
  assert.equal(children[0].open,true);
  assert.equal(children[1].open,false);
});

test('task result requests include explicit job scope and history requests do not',async()=>{
  const {ctx,elements}=ui();const urls=[];
  elements.tailLoadMoreBtn={disabled:false};
  Object.assign(ctx,{tailResultRequest:0,tailResultOffset:0,tailHasMore:false,tailDiscoveryResults:[],
    tailResultView:'current',tailSelectedJobId:'a/b',updateTailPagination:()=>{},updateTailDateFilter:()=>{},
    renderTailResults:()=>{},showTailError:()=>{},tailGetJson:async url=>{urls.push(url);return {results:[quote()],has_more:false,next_offset:1}}});
  loadFunctions(ctx,['fetchTailResults']);
  await ctx.fetchTailResults();
  assert.match(urls[0],/job_id=a%2Fb/);
  ctx.tailResultView='history';await ctx.fetchTailResults();
  assert.doesNotMatch(urls[1],/job_id/);
});

test('a delayed response from a previous tab cannot overwrite the new context',async()=>{
  const {ctx,elements}=ui();elements.tailLoadMoreBtn={disabled:false};
  let release;
  Object.assign(ctx,{tailResultRequest:0,tailResultOffset:0,tailHasMore:false,tailDiscoveryResults:[],
    tailResultView:'current',tailSelectedJobId:'a',updateTailPagination:()=>{},updateTailDateFilter:()=>{},
    renderTailResults:()=>{},showTailError:()=>{},tailGetJson:()=>new Promise(resolve=>{release=resolve})});
  loadFunctions(ctx,['fetchTailResults']);
  const old=ctx.fetchTailResults();
  const releaseOld=release;
  ctx.tailResultView='history';
  const newer=ctx.fetchTailResults();
  release({results:[quote({id:9})],next_offset:1});await newer;
  releaseOld({results:[quote({id:1})],next_offset:1});await old;
  assert.deepEqual(Array.from(ctx.tailDiscoveryResults,r=>r.id),[9]);
});

test('failed polling retains active identity, prevents duplicate starts, and backs off',async()=>{
  const {ctx,elements}=ui();elements.tailConnectionStatus={};
  const timers=[];let busyUpdates=0;
  Object.assign(ctx,{activeTailJobId:'running',activeTailJobTimer:null,tailPollFailures:0,tailConnectionUnknown:false,
    tailGetJson:async()=>{throw new Error('offline')},setTimeout:(fn,delay)=>{timers.push({fn,delay});return timers.length},
    clearTimeout:()=>{},updateTailBusyControls:()=>{busyUpdates++}});
  loadFunctions(ctx,['pollTailDiscoveryJob','tailBusy']);ctx.tailSubmitting=false;
  ctx.pollTailDiscoveryJob('running',true);
  await timers[0].fn();
  assert.equal(ctx.activeTailJobId,'running');assert.equal(ctx.tailConnectionUnknown,true);
  assert.equal(ctx.tailBusy(),true);assert.ok(busyUpdates);
  assert.equal(timers[1].delay,7000);assert.match(elements.tailConnectionStatus.textContent,/后台状态待确认/);
});

test('running polling refreshes saved results before task completion; quote failure does not stop job',async()=>{
  const {ctx,elements}=ui();elements.tailConnectionStatus={};
  const timers=[];let resultCalls=0;
  Object.assign(ctx,{activeTailJobId:'running',activeTailJobTimer:null,tailSelectedJobId:'running',tailResultView:'current',
    tailPollFailures:0,tailConnectionUnknown:false,ACTIVE_TAIL_JOB_STATUSES:['queued','running','needs_verification'],
    tailGetJson:async()=>({job_id:'running',status:'running'}),
    setTimeout:(fn,delay)=>{timers.push({fn,delay});return timers.length},clearTimeout:()=>{},
    renderTailJobProgress:()=>{},updateTailCancelButton:()=>{},updateTailBusyControls:()=>{},
    fetchTailResults:async()=>{resultCalls++;throw new Error('results offline')},fetchTailJobs:async()=>{}});
  loadFunctions(ctx,['pollTailDiscoveryJob']);
  ctx.pollTailDiscoveryJob('running',true);await timers[0].fn();
  assert.equal(resultCalls,1);assert.equal(ctx.activeTailJobId,'running');
  assert.equal(ctx.tailConnectionUnknown,false);assert.equal(timers.length,2);
});

test('404 is distinguished from temporary disconnect and releases vanished job',async()=>{
  const {ctx,elements}=ui();elements.tailConnectionStatus={};let timer;
  Object.assign(ctx,{activeTailJobId:'gone',activeTailJobTimer:null,tailPollFailures:0,
    setTimeout:fn=>{timer=fn},clearTimeout:()=>{},updateTailCancelButton:()=>{},
    tailGetJson:async()=>{throw Object.assign(new Error('missing'),{status:404})}});
  loadFunctions(ctx,['pollTailDiscoveryJob']);
  ctx.pollTailDiscoveryJob('gone',true);await timer();
  assert.equal(ctx.activeTailJobId,'');assert.equal(ctx.tailConnectionUnknown,false);
  assert.match(elements.tailConnectionStatus.textContent,/找不到此任务/);
});

test('completed jobs do not offer meaningless resume and failed jobs get targeted retry',()=>{
  const {ctx,elements}=ui();elements.tailJobSummary={innerHTML:''};
  Object.assign(ctx,{tailBusy:()=>false,tailJobStatusLabel:x=>x,renderTailJobCandidate:()=>'',ACTIVE_TAIL_JOB_STATUSES:['running','queued']});
  loadFunctions(ctx,['renderTailJobProgress']);
  ctx.renderTailJobProgress({job_id:'done',status:'finished',progress:{saved:3}});
  assert.doesNotMatch(elements.tailJobSummary.innerHTML,/data-tail-action="resume"/);
  assert.match(elements.tailJobSummary.innerHTML,/重新查价/);
  assert.ok(elements.tailJobSummary.innerHTML.indexOf('<details') <
    elements.tailJobSummary.innerHTML.indexOf('data-tail-action="explore"'),
    'secondary completed-task operations are collapsed');
  assert.match(elements.tailJobSummary.innerHTML,/重新查询此任务/);
  ctx.renderTailJobProgress({job_id:'bad',status:'failed',progress:{failed:2}});
  assert.match(elements.tailJobSummary.innerHTML,/data-tail-action="retry-failed"/);
  assert.ok(elements.tailJobSummary.innerHTML.indexOf('data-tail-action="retry-failed"') <
    elements.tailJobSummary.innerHTML.indexOf('<details'),
    'failure recovery must remain visible');
});

test('lost POST response locks competing actions and reconciles rather than resubmitting',async()=>{
  const {ctx,elements}=ui();elements.tailConnectionStatus={};const timers=[];let posts=0;
  Object.assign(ctx,{tailSubmitting:false,tailConnectionUnknown:false,
    fetch:async()=>{posts++;throw new Error('connection lost')},
    setTimeout:(fn,delay)=>{timers.push({fn,delay});return timers.length},clearTimeout:()=>{},
    initializeTailWorkspace:async()=>{}});
  loadFunctions(ctx,['tailPostJob','tailBusy']);
  await assert.rejects(ctx.tailPostJob('/api/tail-discovery/run'),/connection lost/);
  assert.equal(posts,1);assert.equal(ctx.tailSubmitting,false);
  assert.equal(ctx.tailConnectionUnknown,true);assert.equal(ctx.tailBusy(),true);
  assert.equal(timers[1].delay,3500);
  assert.match(elements.tailConnectionStatus.textContent,/不会自动重复提交/);
});

test('authoritative input rejection does not trap the form in an unknown state',async()=>{
  const {ctx,elements}=ui();elements.tailConnectionStatus={};let scheduled=0;
  Object.assign(ctx,{tailSubmitting:false,tailConnectionUnknown:false,
    fetch:async()=>({ok:false,json:async()=>({detail:'bad input'})}),
    setTimeout:()=>{scheduled++;return 1},clearTimeout:()=>{}});
  loadFunctions(ctx,['tailPostJob','tailBusy']);
  await assert.rejects(ctx.tailPostJob('/api/tail-discovery/run'),/bad input/);
  assert.equal(ctx.tailBusy(),false);assert.equal(scheduled,1); // timeout only; no reconciliation needed.
});

test('refresh preserves paginated depth and append removes duplicate IDs',async()=>{
  const {ctx,elements}=ui();elements.tailLoadMoreBtn={disabled:false};
  const calls=[];
  Object.assign(ctx,{tailResultRequest:0,tailResultOffset:400,tailHasMore:true,tailDiscoveryResults:[],
    tailResultView:'current',tailSelectedJobId:'a',updateTailPagination:()=>{},updateTailDateFilter:()=>{},
    renderTailResults:()=>{},showTailError:()=>{},tailGetJson:async url=>{
      const offset=Number(new URL('http://local'+url).searchParams.get('offset'));calls.push(offset);
      return {results:[quote({id:offset+1})],has_more:true,next_offset:offset+200};
    }});
  loadFunctions(ctx,['fetchTailResults']);
  await ctx.fetchTailResults({background:true});
  assert.deepEqual(calls,[0,200]);assert.equal(ctx.tailDiscoveryResults.length,2);
  ctx.tailGetJson=async()=>({results:[quote({id:201}),quote({id:401})],has_more:false,next_offset:402});
  await ctx.fetchTailResults({append:true});
  assert.deepEqual(Array.from(ctx.tailDiscoveryResults,r=>r.id),[1,201,401]);
});

test('saved conditions use a fixed allowlist and ignore credential-like fields',()=>{
  const {ctx,elements}=ui();
  elements.tailOriginInput={value:'BJS'};elements.tailCandidateProfileSelect={value:''};
  let saved;
  Object.assign(ctx,{tailCandidatesManual:true,tailCandidateProfileOverride:'',tailSavedCandidates:null,
    selectedTailCandidates:()=>['CAN'],localStorage:{setItem:(key,value)=>{saved=JSON.parse(value)},
      getItem:()=>JSON.stringify({fields:{tailOriginInput:'SHA',api_key:'not-a-credential'},manual:true,candidates:['SZX']})}});
  loadFunctions(ctx,['tailFormFieldIds','saveTailForm','restoreTailForm']);
  ctx.saveTailForm();assert.equal(saved.fields.tailOriginInput,'BJS');
  assert.equal(saved.fields.api_key,undefined);
  ctx.restoreTailForm();assert.equal(elements.tailOriginInput.value,'SHA');
  assert.equal(ctx.tailCandidatesManual,true);
  assert.deepEqual(Array.from(ctx.tailSavedCandidates),['SZX']);
});

test('refresh does not silently clear a date filter absent from the latest batch',()=>{
  const {ctx,elements}=ui();
  elements.tailDateFilter.value='2027-06-21';
  loadFunctions(ctx,['updateTailDateFilter']);
  ctx.updateTailDateFilter([quote()]);
  assert.equal(elements.tailDateFilter.value,'2027-06-21');
  assert.match(elements.tailDateFilter.innerHTML,/2027-06-21/);
});
