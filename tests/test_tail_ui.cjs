const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const sourceFiles = [
  '../static/app/api.js',
  '../static/app/store.js',
  '../static/app/common.js',
  '../static/app/route_form.js',
  '../static/app/dashboard.js',
  '../static/app/browser_profiles.js',
  '../static/app/tail_form.js',
  '../static/app/tail_jobs.js',
  '../static/app/tail_results.js',
  '../static/app/workspace.js',
  '../static/app.js',
];
const source = sourceFiles
  .map(file => fs.readFileSync(path.join(__dirname, file), 'utf8'))
  .join('\n');

const tailStateFields = {
  tailSafeBatchSize: ['form', 'safeBatchSize'],
  tailDefaultProfiles: ['form', 'defaultProfiles'],
  tailCandidateProfileOverride: ['form', 'candidateProfileOverride'],
  tailCandidatesManual: ['form', 'candidatesManual'],
  tailSavedCandidates: ['form', 'savedCandidates'],
  tailCandidatesLoaded: ['form', 'candidatesLoaded'],
  tailFormTouched: ['form', 'touched'],
  tailCandidateNames: ['form', 'candidateNames'],
  activeTailJobId: ['job', 'activeId'],
  activeTailJobTimer: ['job', 'activeTimer'],
  tailDiscoveryJobs: ['job', 'items'],
  tailHistoryJobs: ['job', 'historyItems'],
  tailSelectedJobId: ['job', 'selectedId'],
  tailSelectedJob: ['job', 'selected'],
  tailPollFailures: ['job', 'pollFailures'],
  tailDiscoveryResults: ['results', 'items'],
  tailResultView: ['results', 'view'],
  tailResultRequest: ['results', 'request'],
  tailResultOffset: ['results', 'offset'],
  tailHasMore: ['results', 'hasMore'],
  tailResultsLoaded: ['results', 'loaded'],
  tailDisclosureState: ['results', 'disclosureState'],
  tailConnectionUnknown: ['connection', 'unknown'],
  tailSubmitting: ['connection', 'submitting'],
};

function assignTailState(ctx, values) {
  const state = vm.runInContext('tailState', ctx);
  for (const [key, value] of Object.entries(values)) {
    const target = tailStateFields[key];
    if (target) state[target[0]][target[1]] = value;
    else ctx[key] = value;
  }
}

function ui() {
  const elements = {
    tailLatestOnlyInput: {checked:true}, tailSortSelect:{value:'price'}, tailTrustInput:{value:'all'},
    tailDateFilter:{value:''}, tailMaxPriceInput:{value:''}, tailResults:{innerHTML:''},
    tailSearchScopeInput:{value:'open'},tailProviderSelect:{value:'feizhu'},tailModeInput:{value:'explore'},
    tailOpenScopeNotice:{hidden:true},tailBudgetCitiesInput:{value:'10'},tailTargetInput:{value:'2'},
    tailHybridScopeNotice:{hidden:true},tailFollowupBudgetInput:{value:'5'},tailCallBudgetInput:{value:'20'},
    tailScheduleIntervalInput:{value:'2'},tailScheduleTimeInput:{value:'09:00'},
    tailScheduleSummary:{textContent:''},tailScheduleList:{innerHTML:''},
    tailHistoryPanel:{innerHTML:'',hidden:false},
    candidates:{hidden:false},
  };
  const ctx = vm.createContext({URL, URLSearchParams, Date, AbortController,
    window:{innerHeight:900}, localStorage:{getItem:()=>null},
    document:{getElementById:id=>elements[id],querySelector:()=>elements.candidates,querySelectorAll:()=>[]},
    escapeHtml: x=>String(x??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])),
    airportCodeDisplayText:x=>x||'', providerLabel:x=>x, cabinLabel:x=>x||'', formatQueryDate:x=>x,formatQueryTime:x=>x,
    tailResultConfidence:()=>({level:'medium',label:'需复核'}), tailResultConfidenceScore:()=>50,
    cleanVisibleText:()=>{},updateWorkspaceTabs:()=>{},renderTailSummary:()=>{},
    updateTailBusyControls:()=>{},
  });
  vm.runInContext(fs.readFileSync(path.join(__dirname, '../static/app/store.js'), 'utf8'), ctx);
  ctx.tailState = vm.runInContext('tailState', ctx);
  vm.runInContext(fs.readFileSync(path.join(__dirname, '../static/app/api.js'), 'utf8'), ctx);
  assignTailState(ctx, {tailResultView:'history', tailResultsLoaded:true, tailSelectedJobId:'job'});
  for (const name of ['compareTailResults','tailQuoteFresh','tailQuoteVerified','prepareTailResults',
    'groupTailResultsByRoute','safeTailReviewUrl','tailQuoteTermsLabel','renderTailBaselines','renderTailFlightGroups',
    'tailFlightGroupKey','groupTailFlightResults','buildTailRouteTimeSummaries','tailOverviewPriceLabel','renderTailRouteTimeOverview','renderTailRouteTimeItem','renderTailStaleResults','resetTailResultDisclosure',
    'tailFullRouteLabel','tailItineraryKey','renderTailItineraryGroups',
    'renderTailResultCard','renderTailRouteGroup','renderTailResults','updateTailSearchScope','renderTailPriorities',
    'tailAttemptPhaseLabel','tailPlaceLabel','replaceTailMarkup','tailConditionLabel',
    'tailQueryDiffers','updateTailRunAction','updateTailResultContext','updateTailResultTools','tailActiveContext','loadSelectedTailJobConditions',
    'renderTailSchedules','tailJobStatusLabel','tailHistoryScopeLabel','tailHistorySavedCount','tailHistoryEffectiveCandidates','tailHistoryResultState','tailHistoryCityName','tailHistoryDestinationPreview','tailHistoryConditionsSnapshot','tailHistorySortKey','sortTailHistoryJobs','tailHistoryPriceText','tailHistoryGroupKey','groupTailHistoryJobs','tailHistoryGroupState','renderTailHistoryGroup','renderTailHistoryJobs','renderTailHistoryJob',
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
test('fresh verified and earlier retrieval partitions are separate',()=>{
  const {ctx,elements}=ui(); elements.tailTrustInput.value='verified';
  assert.equal(ctx.prepareTailResults([quote(),quote({itinerary_key:'old',observed_at:'2020-01-01T00:00:00Z',expires_at:'2020-01-01T00:00:00Z'})]).length,1);
  elements.tailTrustInput.value='all';
  ctx.renderTailResults([quote(),quote({itinerary_key:'old',observed_at:'2020-01-01T00:00:00Z',expires_at:'2020-01-01T00:00:00Z'})]);
  assert.match(elements.tailResults.innerHTML,/航段已核对/); assert.match(elements.tailResults.innerHTML,/检索 2020-01-01T00:00:00Z/);
});
test('same first flight groups tail alternatives without mixing currencies',()=>{
  const {ctx}=ui(); let html=ctx.renderTailFlightGroups([quote(),quote({destination:'SZX',itinerary_key:'two'})]);
  assert.equal((html.match(/<h4>/g)||[]).length,1);
  html=ctx.renderTailFlightGroups([quote(),quote({currency:'USD',itinerary_key:'two'})]);
  assert.equal((html.match(/<h4>/g)||[]).length,2);
});
test('results default to collapsed route-time overview groups',()=>{
  const {ctx,elements}=ui();
  const rows=['CAN','SZX','KMG','XMN','TAO'].map((destination,index)=>quote({
    id:index+1,destination,first_leg_key:`CA${1000+index}`,itinerary_key:`route-${index}`,
  }));
  ctx.renderTailResults(rows);
  const html=elements.tailResults.innerHTML;
  assert.match(html,/路线价格总览 <span>5 组<\/span>/);
  assert.equal((html.match(/class="tail-overview-item"/g)||[]).length,5);
  assert.equal((html.match(/class="tail-flight-group"/g)||[]).length,5);
  assert.doesNotMatch(html,/class="tail-overview-item"[^>]* open/);
  assert.doesNotMatch(html,/tail-all-results/);
});
test('one first-leg overview keeps all itineraries inside its collapsed details',()=>{
  const {ctx,elements}=ui();
  const rows=['CAN','SZX','KMG','XMN'].map((destination,index)=>quote({
    id:index+1,destination,itinerary_key:`same-leg-${index}`,
  }));
  ctx.renderTailResults(rows);
  const html=elements.tailResults.innerHTML;
  assert.equal((html.match(/class="tail-overview-item"/g)||[]).length,1);
  assert.match(html,/CA1001 09:00 → 11:00/);
  assert.match(html,/4 条路线 · 4 条报价/);
  assert.equal((html.match(/class="tail-flight-group"/g)||[]).length,1);
  assert.equal((html.match(/class="tail-itinerary-option/g)||[]).length,4);
});
test('overview lists provider low prices separately instead of merging platforms',()=>{
  const {ctx,elements}=ui();
  ctx.renderTailResults([quote({id:1,provider:'ctrip',price:800,itinerary_key:'ctrip-route'}),
    quote({id:2,provider:'feizhu',price:700,itinerary_key:'feizhu-route'})]);
  const html=elements.tailResults.innerHTML;
  assert.match(html,/ctrip<\/small><strong>CNY 800/);
  assert.match(html,/feizhu<\/small><strong>CNY 700/);
  assert.doesNotMatch(html,/全网|合并最低价/);
});
test('all earlier-retrieved results use an earlier-retrieval overview with dates',()=>{
  const {ctx,elements}=ui();
  const rows=['CAN','SZX','KMG','XMN'].map((destination,index)=>quote({
    id:index+1,destination,first_leg_key:`OLD${index}`,itinerary_key:`old-${index}`,
    observed_at:'2020-01-01T00:00:00Z',expires_at:'2020-01-01T00:00:00Z',
  }));
  ctx.renderTailResults(rows);
  const html=elements.tailResults.innerHTML;
  assert.match(html,/较早检索 <span>4 组<\/span>/);
  assert.equal((html.match(/class="tail-overview-item"/g)||[]).length,4);
  assert.match(html,/检索 2020-01-01T00:00:00Z/);
  assert.doesNotMatch(html,/tail-all-results/);
});
test('explicit earlier-retrieval filter opens overview details',()=>{
  const {ctx,elements}=ui();
  elements.tailTrustInput.value='stale';
  ctx.renderTailResults([quote({id:1,observed_at:'2020-01-01T00:00:00Z',expires_at:'2020-01-01T00:00:00Z'})]);
  const html=elements.tailResults.innerHTML;
  assert.match(html,/class="tail-state-section tail-stale-group" data-disclosure="stale-results" open/);
  assert.match(html,/class="tail-overview-item" data-disclosure="[^"]+" open/);
  assert.match(html,/data-provider="feizhu"/);
});
test('new-query disclosure reset clears result expansion state',()=>{
  const {ctx}=ui();
  ctx.tailState.results.disclosureState.set('tail-more-results',true);
  ctx.tailState.results.disclosureState.set('stale-results',true);
  ctx.resetTailResultDisclosure();
  assert.equal(ctx.tailState.results.disclosureState.size,0);
});
test('selected task conditions can be loaded back into the query form',()=>{
  const {ctx,elements}=ui();
  const ids=['tailOriginInput','tailTransferInput','tailDateInput','tailProviderSelect','tailFeizhuScopeInput',
    'tailCabinSelect','tailQueryMaxPriceInput','tailDepartureTimeStartInput','tailDepartureTimeEndInput',
    'tailAirlineInput','tailFilterScopeInput','tailPassengersInput','tailSearchScopeInput','tailBudgetCitiesInput',
    'tailBudgetMinutesInput','tailTargetInput','tailBaselineBudgetInput','tailFollowupBudgetInput',
    'tailCallBudgetInput','tailCandidateProfileSelect','tailModeInput'];
  for (const id of ids) elements[id]={value:'',disabled:false};
  elements.tailQueryEditor={open:false};
  assignTailState(ctx,{tailSelectedJobId:'job',tailSelectedJob:{job_id:'job',payload:{
    origin:'JHG',transfer:'CTU',departure_date:'2027-01-03',provider:'feizhu',cabin:'economy',max_price:800,
    departure_time_start:'08:00',departure_time_end:'20:00',preferred_airlines:['MU'],filter_scope:'first_leg',
    passengers:1,search_scope:'candidates',candidates:['KMG','TAO'],max_candidates:6,max_minutes:10,
    target_results:0,max_baseline_queries:1,max_followup_candidates:0,max_provider_calls:0,
  }}});
  ctx.normalizeCode=value=>String(value||'').toUpperCase();
  for (const name of ['updateTailSearchScope','applyTailAirlineDefaults','updateTailCandidateSummary','updateTailFormSummary','saveTailForm','updateTailResultContext']) ctx[name]=()=>{};
  ctx.loadSelectedTailJobConditions();
  assert.equal(elements.tailOriginInput.value,'JHG');
  assert.equal(elements.tailTransferInput.value,'CTU');
  assert.equal(elements.tailDateInput.value,'2027-01-03');
  assert.equal(elements.tailProviderSelect.value,'feizhu');
  assert.equal(elements.tailQueryMaxPriceInput.value,'800');
  assert.equal(elements.tailAirlineInput.value,'MU');
  assert.equal(elements.tailBudgetCitiesInput.value,'6');
  assert.equal(elements.tailCallBudgetInput.value,'0');
  assert.deepEqual(Array.from(ctx.tailState.form.savedCandidates),['KMG','TAO']);
  assert.equal(elements.tailQueryEditor.open,true);
});
test('tail schedules render next run, last status and management actions safely',()=>{
  const {ctx,elements}=ui();
  loadFunctions(ctx,['tailFormFieldIds']);
  ctx.renderTailSchedules([{schedule_id:'<bad>',enabled:true,interval_days:2,run_time:'09:00',
    next_run_at:'2027-01-01T01:00:00Z',last_error:'<script>bad</script>',
    route:{origin:'BJS',transfer:'CTU',departure_date:'2027-01-01',provider:'feizhu'},
    last_job_status:'finished',last_job_saved:4}]);
  const html=elements.tailScheduleList.innerHTML;
  assert.equal(elements.tailScheduleSummary.textContent,'1 个计划');
  assert.match(html,/每 2 天 09:00/);
  assert.match(html,/上次 已完成 · 保存 4 条/);
  assert.match(html,/&lt;script&gt;bad&lt;\/script&gt;/);
  assert.doesNotMatch(html,/<script>bad/);
  assert.match(html,/data-tail-schedule-action="toggle"/);
  assert.match(html,/data-tail-schedule-action="delete"/);
  assert.ok(ctx.tailFormFieldIds().includes('tailScheduleIntervalInput'));
  assert.ok(ctx.tailFormFieldIds().includes('tailScheduleTimeInput'));
});
test('history jobs render route, date, status, progress and escaped details',()=>{
  const {ctx,elements}=ui();
  assignTailState(ctx,{tailHistoryJobs:[{job_id:'job-1',status:'finished',updated_at:'2027-01-02T01:00:00Z',min_price:580,
    payload:{origin:'BJS',transfer:'CTU',departure_date:'2027-01-03',provider:'both',search_scope:'candidates',candidates:['CAN','SHA']},
    progress:{saved:11,attempted:6,total:6,message:'<script>bad</script>'},result:null}]});
  ctx.renderTailHistoryJobs();
  const html=elements.tailHistoryPanel.innerHTML;
  assert.match(html,/历史查询记录/);
  assert.match(html,/BJS → CTU/);
  assert.match(html,/有报价 · 保存 11 条/);
  assert.match(html,/已完成 · 2 个尾段城市/);
  assert.match(html,/已查 6 \/ 6/);
  assert.match(html,/<strong>2027-01-03<\/strong>/);
  assert.match(html,/最低 ¥580/);
  assert.ok(html.indexOf('2027-01-03') < html.indexOf('有报价 · 保存 11 条'), 'date column before result column');
  assert.match(html,/查询条件/);
  assert.match(html,/尾段城市/);
  assert.match(html,/指定城市/);
  assert.match(html,/data-history-select-job="job-1"/);
  assert.match(html,/&lt;script&gt;bad&lt;\/script&gt;/);
  assert.doesNotMatch(html,/<script>bad/);
});
test('history jobs surface result state, preview and effective candidate count',()=>{
  const {ctx,elements}=ui();
  assignTailState(ctx,{tailHistoryJobs:[
    {job_id:'job-eff',status:'paused',updated_at:'2027-01-02T01:00:00Z',
      payload:{origin:'BJS',transfer:'CTU',departure_date:'2027-01-03',provider:'ctrip',search_scope:'candidates',
        candidates:['CAN','SHA','SZX','KMG','HGH','NKG','WUH'],cabin:'economy',passengers:2,max_price:1500},
      progress:{saved:4,attempted:6,total:6,effective_candidates:['CAN','SHA','SZX','KMG','HGH','NKG']},result:null},
    {job_id:'job-open',status:'cancelled',
      payload:{origin:'SHA',transfer:'CTU',departure_date:'2027-02-01',provider:'feizhu',search_scope:'open',candidates:[]},
      progress:{},result:null},
    {job_id:'job-none',status:'finished',
      payload:{origin:'SHA',transfer:'PEK',departure_date:'2027-03-01',provider:'ctrip',search_scope:'candidates',candidates:['CAN']},
      progress:{saved:0},result:null},
  ]});
  ctx.renderTailHistoryJobs();
  const html=elements.tailHistoryPanel.innerHTML;
  assert.match(html,/部分完成 · 保存 4 条/);
  assert.match(html,/实际查询 6 个城市/);
  assert.doesNotMatch(html,/7 个尾段城市/);
  assert.match(html,/尾段城市<\/dt><dd>CAN \/ SHA \/ SZX \+4/);
  assert.match(html,/暂无最低价/);
  assert.match(html,/economy/);
  assert.match(html,/2 人/);
  assert.match(html,/¥1500/);
  assert.match(html,/已取消 · 未保存报价/);
  assert.match(html,/开放目的地发现/);
  assert.match(html,/无报价/);
});
test('history jobs sort by departure date by default and support origin/transfer sort',()=>{
  const {ctx,elements}=ui();
  const job=(id,origin,transfer,date)=>({job_id:id,status:'finished',
    payload:{origin,transfer,departure_date:date,provider:'ctrip',search_scope:'candidates',candidates:[]},
    progress:{},result:null});
  assignTailState(ctx,{tailHistoryJobs:[
    job('j1','SHA','CTU','2027-03-01'),
    job('j2','BJS','SHA','2027-01-15'),
    job('j3','CAN','BJS','2027-02-01'),
    job('j4','BJS','CTU',''),
  ]});
  const order=()=>[...elements.tailHistoryPanel.innerHTML.matchAll(/data-job-id="([^"]+)"/g)].map(m=>m[1]);
  ctx.renderTailHistoryJobs();
  assert.match(elements.tailHistoryPanel.innerHTML,/data-history-sort="date" aria-pressed="true"/);
  assert.deepEqual(order(),['j2','j3','j1','j4']);
  ctx.tailState.results.historySort='origin';ctx.renderTailHistoryJobs();
  assert.match(elements.tailHistoryPanel.innerHTML,/data-history-sort="origin" aria-pressed="true"/);
  assert.deepEqual(order(),['j4','j2','j3','j1']);
  ctx.tailState.results.historySort='transfer';ctx.renderTailHistoryJobs();
  assert.deepEqual(order(),['j3','j4','j1','j2']);
});
test('history jobs sharing route and date merge into one expandable group',()=>{
  const {ctx,elements}=ui();
  const job=(id,date,status,saved,minPrice,updated)=>({job_id:id,status,updated_at:updated,min_price:minPrice,
    payload:{origin:'CTU',transfer:'JHG',departure_date:date,provider:'ctrip',search_scope:'candidates',candidates:['CAN']},
    progress:{saved,attempted:6,total:6},result:null});
  assignTailState(ctx,{tailHistoryJobs:[
    job('g1','2027-01-01','finished',2,890,'2027-01-04T02:00:00Z'),
    job('g2','2027-01-01','paused',4,610,'2027-01-04T03:00:00Z'),
    job('g3','2027-01-01','cancelled',0,null,'2027-01-04T01:00:00Z'),
    job('solo','2027-01-22','finished',0,null,'2027-01-04T04:00:00Z'),
  ]});
  ctx.renderTailHistoryJobs();
  const html=elements.tailHistoryPanel.innerHTML;
  assert.match(html,/tail-history-group/);
  assert.match(html,/3 次查询/);
  assert.match(html,/有报价 · 共保存 6 条/);
  assert.match(html,/最低 ¥610/);
  assert.match(html,/最近：已停止，可继续/);
  const groupHtml=html.slice(html.indexOf('tail-history-group'),html.indexOf('data-job-id="solo"'));
  assert.equal([...groupHtml.matchAll(/data-job-id="([^"]+)"/g)].length,3);
  const solo=html.slice(html.indexOf('data-job-id="solo"'));
  assert.doesNotMatch(solo,/次查询/);
});
test('unknown terms never show guaranteed savings and include recheck action',()=>{
  const {ctx}=ui(); const html=ctx.renderTailResultCard(quote(),1);
  assert.match(html,/口径未确认，不计算节省金额/); assert.match(html,/data-tail-action="recheck"/);
  assert.match(html,/行李：未核实/); assert.match(html,/人数库存未核实/);
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
  assert.match(ctx.renderTailResultCard(quote({filter_scope:'first_leg'}),1),/舱位／航司仅约束我要乘坐的航段/);
  assert.match(ctx.renderTailResultCard(quote({filter_scope:'all_legs'}),1),/舱位／航司约束完整购票行程/);
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
  const policy={ranking:'contextual_history_v1',
    provider_calls:{used:3,limit:20},next_candidates:[{destination:'CAN',reasons:['<img src=x onerror=x>','近期有效联程证据']}]};
  const html=ctx.renderTailPriorities(policy);
  assert.match(html,/3 \/ 20/);
  assert.match(html,/非可售保证/);
  assert.match(html,/近期有效联程证据/);
  assert.doesNotMatch(html,/<img/);
  assert.doesNotMatch(ctx.renderTailPriorities(policy,true),/<details/);
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
  assignTailState(ctx,{tailResultView:'current',tailSelectedJobId:'a',
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
  ctx.tailState.job.selectedId='loading';
  ctx.updateTailResultContext();
  assert.doesNotMatch(elements.tailResultIdentity.textContent,/BJS/);
  assert.match(elements.tailResultIdentity.textContent,/正在读取/);
  ctx.tailState.results.view='history';ctx.updateTailResultContext();
  assert.equal(elements.tailResultIdentity.textContent,'');
  assert.equal(elements.tailDraftNotice.hidden,true);
  assert.equal(elements.tailTaskConditions.textContent,'');
});

test('filter summary remains visible when controls close; clear restores latest-only',()=>{
  const {ctx,elements}=ui();
  elements.tailResultFilterSummary={};elements.tailViewResultsBtn={hidden:true};
  elements.tailDateFilter.value='2027-06-20';elements.tailMaxPriceInput.value='800';
  elements.tailLatestOnlyInput.checked=false;ctx.tailState.results.items=[quote()];
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
  assert.match(ctx.tailDecisionDifference(item),/比符合筛选条件的直飞价低 CNY 300/);
  const html=ctx.renderTailResultCard(item,1);
  assert.ok(html.indexOf('同口径参考')<html.indexOf('<details'),
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
  ]) assert.equal(ctx.tailDecisionDifference(item),'无法判断能省多少');
  const stale=comparableQuote({expires_at:'2020-01-01'});
  assert.match(ctx.tailDecisionDifference(stale),/价格比较待复核/);
  assert.doesNotMatch(ctx.renderTailResultCard(stale,1),/比同首段单买低/);
  for (const amount of [null,undefined,NaN,999]) {
    const item=comparableQuote();delete item.baselines.market;
    item.baselines.same_flight.savings_amount=amount;
    assert.equal(ctx.tailDecisionDifference(item),'无法判断能省多少');
  }
});

test('negative and zero differences are not presented as savings',()=>{
  const {ctx}=ui();const item=comparableQuote();delete item.baselines.market;
  item.baselines.same_flight.price=650;item.baselines.same_flight.savings_amount=-50;
  assert.match(ctx.tailDecisionDifference(item),/比同首段单买高 CNY 50/);
  item.baselines.same_flight.price=700;item.baselines.same_flight.savings_amount=0;
  assert.match(ctx.tailDecisionDifference(item),/与同首段单买相同/);
});

test('cards distinguish desired first leg, actual full route and visible unresolved risks',()=>{
  const {ctx}=ui();
  const item=quote({flight_details:[
    {origin:'BJS',destination:'SHA',departure_airport:'PEK',arrival_airport:'SHA',flight_no:'CA1001'},
    {origin:'SHA',destination:'CAN',departure_airport:'SHA',arrival_airport:'CAN'},
  ]});
  const html=ctx.renderTailFlightGroups([item]);
  assert.match(html,/我要乘坐 · BJS → SHA/);
  assert.match(html,/完整购票行程/);
  assert.match(html,/PEK → SHA → CAN/);
  assert.ok(html.indexOf('我要乘坐')<html.indexOf('完整购票行程'));
  assert.ok(html.indexOf('出票、行李与票规需复核')<html.indexOf('<details'));
  assert.match(html,/人数库存未核实/);
  assert.match(html,/航段已核对 · 价格未复核/);
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

test('dual provider controls retain Ctrip candidates and separate Fliggy scope and budgets',()=>{
  const {ctx,elements}=ui();
  elements.tailProviderSelect.value='both';
  elements.tailFeizhuScopeInput={value:'open'};
  elements.tailBothNotice={hidden:true};
  ctx.updateTailSearchScope();
  assert.equal(elements.tailSearchScopeInput.value,'candidates');
  assert.equal(elements.tailSearchScopeInput.disabled,true);
  assert.equal(elements.candidates.hidden,false);
  assert.equal(elements.tailBudgetCitiesInput.disabled,false);
  assert.equal(elements.tailTargetInput.disabled,false);
  assert.equal(elements.tailCallBudgetInput.disabled,false);
  assert.equal(elements.tailFeizhuScopeInput.disabled,false);
  assert.equal(elements.tailFollowupBudgetInput.disabled,true);
  assert.equal(elements.tailBothNotice.hidden,false);
  elements.tailFeizhuScopeInput.value='hybrid';ctx.updateTailSearchScope();
  assert.equal(elements.tailFollowupBudgetInput.disabled,false);
  elements.tailProviderSelect.value='ctrip';ctx.updateTailSearchScope();
  assert.equal(elements.tailFeizhuScopeInput.value,'hybrid');
  assert.equal(elements.tailFeizhuScopeInput.disabled,true);
  assert.equal(elements.tailCallBudgetInput.disabled,true);
});

test('dual submission builds one payload with independent provider settings',()=>{
  const {ctx,elements}=ui();
  loadFunctions(ctx,['tailFormFieldIds','buildTailQueryPayload']);
  for (const id of ctx.tailFormFieldIds()) elements[id] ||= {value:''};
  assignTailState(ctx,{normalizeCode:x=>x,selectedTailCandidates:()=>['CAN'],
    tailCandidateProfileOverride:''});
  elements.tailProviderSelect.value='both';elements.tailFeizhuScopeInput.value='open';
  elements.tailSearchScopeInput.value='candidates';
  elements.tailOriginInput.value='BJS';elements.tailTransferInput.value='SHA';
  elements.tailDateInput.value='2027-06-20';elements.tailCallBudgetInput.value='8';
  const draft=ctx.buildTailQueryPayload();
  assert.equal(draft.provider,'both');assert.equal(draft.feizhu_search_scope,'open');
  assert.equal(draft.search_scope,'candidates');
  assert.deepEqual(Array.from(draft.candidates),['CAN']);
  assert.equal(draft.max_candidates,10);
  assert.equal(draft.target_results,2);
  assert.equal(draft.max_provider_calls,8);
});

test('dual draft comparison detects Fliggy scope and its hybrid budget independently',()=>{
  const {ctx}=ui();
  const saved={provider:'both',search_scope:'candidates',feizhu_search_scope:'hybrid',max_followup_candidates:5};
  assert.equal(ctx.tailQueryDiffers(saved,{...saved,max_followup_candidates:6}),true);
  assert.equal(ctx.tailQueryDiffers(saved,{...saved,feizhu_search_scope:'open'}),true);
});

test('advanced budget summary counts only effective changes and retains values across providers',()=>{
  const {ctx,elements}=ui();
  for (const [id,value] of [['tailBudgetCitiesInput','10'],['tailBudgetMinutesInput',''],
    ['tailTargetInput','2'],['tailFollowupBudgetInput','5'],['tailCallBudgetInput','20'],['tailBaselineBudgetInput','3']]) {
    elements[id]={value,defaultValue:value};
  }
  elements.tailBudgetSummary={};elements.tailProviderSelect.value='both';
  elements.tailFeizhuScopeInput={value:'open'};elements.tailBothNotice={hidden:true};
  ctx.selectedTailCandidates=()=>['CAN'];loadFunctions(ctx,['updateTailFormSummary']);
  ctx.updateTailFormSummary();assert.equal(elements.tailBudgetSummary.textContent,'默认预算');
  elements.tailCallBudgetInput.value='21';ctx.updateTailFormSummary();
  assert.equal(elements.tailBudgetSummary.textContent,'1 项已调整');
  elements.tailProviderSelect.value='ctrip';ctx.updateTailSearchScope();ctx.updateTailFormSummary();
  assert.equal(elements.tailBudgetSummary.textContent,'默认预算');
  assert.equal(elements.tailCallBudgetInput.value,'21');
  elements.tailProviderSelect.value='both';ctx.updateTailSearchScope();ctx.updateTailFormSummary();
  assert.equal(elements.tailBudgetSummary.textContent,'1 项已调整');
});

test('complete itinerary groups align providers only with matching evidence and terms',()=>{
  const {ctx}=ui();ctx.tailState.job.selected={job_id:'both',payload:{provider:'both'}};
  const legs=[
    {flight_no:'CA1001',departure_airport:'PEK',arrival_airport:'SHA',departure_time:'09:00',arrival_time:'11:00',cabin:'economy'},
    {flight_no:'CA2002',departure_airport:'SHA',arrival_airport:'CAN',departure_time:'13:00',arrival_time:'15:00',cabin:'economy'},
  ];
  const first=quote({provider:'ctrip',itinerary_key:'ctrip-route',flight_details:legs,quote_terms:{price_basis:'per_person'}});
  const second=quote({id:2,provider:'feizhu',itinerary_key:'feizhu-route',price:650,flight_details:legs,quote_terms:{price_basis:'per_person'}});
  assert.equal(ctx.tailItineraryKey(first),ctx.tailItineraryKey(second));
  const html=ctx.renderTailItineraryGroups([first,second]).join('');
  assert.equal((html.match(/has-multiple-providers/g)||[]).length,1);
  assert.match(html,/同行程平台独立报价/);assert.doesNotMatch(html,/暂无同行程报价/);
  assert.ok(html.indexOf('data-provider="ctrip"')<html.indexOf('data-provider="feizhu"'));
  assert.match(html,/data-result-id="1"/);assert.match(html,/data-result-id="2"/);
  const single=ctx.renderTailItineraryGroups([first]).join('');
  assert.match(single,/feizhu暂无同行程报价/);
  ctx.tailState.job.selected.payload.provider='ctrip';
  assert.doesNotMatch(ctx.renderTailItineraryGroups([first]).join(''),/暂无同行程报价/);
  const different=quote({id:3,provider:'feizhu',destination:'XMN',flight_details:legs.map((leg,index)=>index?{...leg,arrival_airport:'XMN',destination:'XMN'}:leg)});
  assert.notEqual(ctx.tailItineraryKey(first),ctx.tailItineraryKey(different));
  assert.equal(ctx.renderTailItineraryGroups([first,different]).length,2);
  const partial=quote({id:4,provider:'feizhu',itinerary_key:'partial',flight_details:[legs[0]]});
  assert.ok(ctx.renderTailItineraryGroups([first,partial]).every(html=>!html.includes('has-multiple-providers')));
  assert.notEqual(ctx.tailItineraryKey(first),ctx.tailItineraryKey({...first,currency:'USD'}));
  assert.notEqual(ctx.tailItineraryKey(first),ctx.tailItineraryKey({...first,passengers:2}));
  assert.notEqual(ctx.tailItineraryKey(first),ctx.tailItineraryKey({...first,quote_terms:{}}));
});

test('provider filter does not deduplicate or price-compare different platforms as one quote',()=>{
  const {ctx,elements}=ui();
  const rows=[quote({id:1,provider:'ctrip'}),quote({id:2,provider:'feizhu',price:650})];
  assert.equal(ctx.prepareTailResults(rows).length,2);
  const html=ctx.renderTailFlightGroups(rows);
  assert.equal((html.match(/<h4>/g)||[]).length,1);
  assert.equal((html.match(/class="tail-result-card"/g)||[]).length,2);
  assert.match(html,/data-provider="ctrip"/);assert.match(html,/data-provider="feizhu"/);
  assert.match(html,/按完整行程比较/);assert.doesNotMatch(html,/同平台最低参考/);
  const differentBasis=ctx.renderTailFlightGroups([rows[0],quote({id:2,provider:'feizhu',quote_terms:{price_basis:'per_person'}})]);
  assert.equal((differentBasis.match(/<h4>/g)||[]).length,2);
  const quickButtons=['','ctrip','feizhu'].map(provider=>({dataset:{tailProvider:provider},textContent:'',attributes:{},
    setAttribute(name,value){this.attributes[name]=value;}}));
  elements.tailProviderQuickFilter={hidden:true,querySelectorAll:()=>quickButtons};
  ctx.tailState.results.items=rows;ctx.updateTailResultTools();
  assert.equal(elements.tailProviderQuickFilter.hidden,false);
  assert.deepEqual(quickButtons.map(button=>button.textContent),['全部 2','携程 1','飞猪 1']);
  elements.tailResultProviderFilter={value:'ctrip'};
  assert.deepEqual(Array.from(ctx.prepareTailResults(rows),r=>r.provider),['ctrip']);
  elements.tailResultFilterSummary={};ctx.updateTailResultTools();
  assert.match(elements.tailResultFilterSummary.textContent,/1 项/);
  assert.equal(quickButtons[1].attributes['aria-pressed'],'true');
  loadFunctions(ctx,['clearTailResultFilters']);ctx.clearTailResultFilters();
  assert.equal(elements.tailResultProviderFilter.value,'');
  assert.equal(quickButtons[0].attributes['aria-pressed'],'true');
  ctx.tailState.results.items=[rows[0]];ctx.updateTailResultTools();
  assert.equal(elements.tailProviderQuickFilter.hidden,true);
});

test('batch progress shows sibling success and failure, escapes errors and offers only valid recovery',()=>{
  const {ctx,elements}=ui();
  elements.tailJobSummary={innerHTML:''};
  assignTailState(ctx,{tailBusy:()=>false,tailJobStatusLabel:x=>x,renderTailJobCandidate:()=>'',providerLabel:x=>({ctrip:'携程',feizhu:'飞猪'}[x]||x),ACTIVE_TAIL_JOB_STATUSES:['running','queued','needs_verification']});
  loadFunctions(ctx,['renderTailJobProgress','renderTailBatchProgress']);
  const job={job_id:'batch',status:'partial',payload:{provider:'both'},progress:{saved:2,children:[
    {job_id:'c',provider:'ctrip',status:'finished',payload:{provider:'ctrip'},saved:2},
    {job_id:'f',provider:'feizhu',status:'failed',payload:{provider:'feizhu',search_scope:'open'},error:'<img src=x>'},
  ]}};
  ctx.renderTailJobProgress(job);
  const html=elements.tailJobSummary.innerHTML;
  assert.match(html,/携程 · finished/);assert.match(html,/飞猪 · failed/);
  assert.match(html,/重试飞猪/);
  assert.match(html,/class="primary compact" data-tail-action="retry-failed"/);
  assert.doesNotMatch(html,/data-tail-action="resume"/);
  assert.doesNotMatch(html,/<img src=x>/);
  assert.equal((html.match(/class="tail-provider-progress-card"/g)||[]).length,2);
  assert.equal((html.match(/data-disclosure="child-f"/g)||[]).length,1);
  assert.doesNotMatch(html,/class="tail-provider-progress-card"[^>]*open/);
  assert.match(html,/<summary><strong>飞猪 · failed<\/strong>[\s\S]*?class="tail-warning"/);
  job.status='running';ctx.renderTailJobProgress(job);
  assert.doesNotMatch(elements.tailJobSummary.innerHTML,/data-tail-action="resume"|data-tail-action="retry-failed"/);
  job.payload={...job.payload,origin:'BJS',transfer:'SHA',departure_date:'2027-06-20'};
  assert.match(ctx.tailActiveContext(job),/后台执行.*下方结果另有所属/);
  ctx.tailState.job.selectedId='batch';ctx.tailState.results.view='current';
  assert.doesNotMatch(ctx.tailActiveContext(job),/下方结果另有所属/);
  job.status='finished';assert.equal(ctx.tailActiveContext(job),'');
  job.status='needs_verification';ctx.renderTailJobProgress(job);
  assert.match(elements.tailJobSummary.innerHTML,/class="primary compact" data-tail-action="continue"/);
  assert.match(elements.tailJobSummary.innerHTML,/飞猪无需等待/);
});

test('dual recovery separates failed providers from pending work and escapes provider names',()=>{
  const {ctx,elements}=ui();elements.tailJobSummary={innerHTML:''};
  assignTailState(ctx,{tailBusy:()=>false,tailJobStatusLabel:x=>x,renderTailJobCandidate:()=>'',providerLabel:x=>({ctrip:'携程',feizhu:'飞猪'}[x]||x),ACTIVE_TAIL_JOB_STATUSES:['running','queued','needs_verification']});
  loadFunctions(ctx,['renderTailBatchProgress']);
  const job={job_id:'batch',status:'paused',payload:{provider:'both'},progress:{saved:2,children:[
    {job_id:'c',provider:'ctrip',status:'failed',saved:1,payload:{provider:'ctrip'}},
    {job_id:'f',provider:'feizhu',status:'paused',saved:1,payload:{provider:'feizhu'}},
  ]}};
  ctx.renderTailBatchProgress(job,elements.tailJobSummary);
  let html=elements.tailJobSummary.innerHTML;
  assert.match(html,/class="primary compact" data-tail-action="retry-failed"/);
  assert.match(html,/class="ghost compact" data-tail-action="resume"/);
  assert.match(html,/重试携程/);assert.match(html,/继续未查项/);
  assert.match(html,/重试只运行失败平台；继续只处理未查项/);
  job.progress.children[0].provider='ctrip<img src=x>';
  ctx.renderTailBatchProgress(job,elements.tailJobSummary);
  assert.doesNotMatch(elements.tailJobSummary.innerHTML,/<img src=x>/);
  job.progress.children=[{job_id:'f',provider:'feizhu',status:'paused',saved:1,payload:{provider:'feizhu'},
    progress:{queue:{items:[{destination:'SZX',status:'pending'}]}}}];
  ctx.renderTailBatchProgress(job,elements.tailJobSummary);
  html=elements.tailJobSummary.innerHTML;
  assert.match(html,/class="primary compact" data-tail-action="resume"/);
  assert.doesNotMatch(html,/data-tail-action="retry-failed"/);
  job.status='finished';ctx.renderTailBatchProgress(job,elements.tailJobSummary);
  assert.doesNotMatch(elements.tailJobSummary.innerHTML,/data-tail-action="resume"/);
});

test('new search is secondary to an existing task and clearly distinguishes edited conditions',()=>{
  const {ctx,elements}=ui();const classes=new Set(),viewClasses=new Set();
  elements.tailRunBtn={classList:{toggle:(name,force)=>force?classes.add(name):classes.delete(name)}};
  elements.tailViewResultsBtn={classList:{toggle:(name,force)=>force?viewClasses.add(name):viewClasses.delete(name)}};
  elements.tailRunHint={};elements.tailOriginInput={};
  const payload={origin:'BJS',transfer:'SHA',departure_date:'2027-06-20',provider:'ctrip'};
  assignTailState(ctx,{tailResultView:'current',tailSelectedJobId:'paused',tailSelectedJob:{job_id:'paused',payload},
    tailBusy:()=>false,normalizeCode:x=>x,buildTailQueryPayload:()=>({...payload}),tailDiscoveryResults:[quote()]});
  ctx.updateTailRunAction();
  assert.equal(elements.tailRunBtn.textContent,'使用表单条件重新查询');
  assert.equal(classes.has('ghost'),true);assert.equal(classes.has('primary'),false);
  assert.equal(viewClasses.has('primary'),false);
  ctx.buildTailQueryPayload=()=>({...payload,departure_date:'2027-06-21'});ctx.updateTailRunAction();
  assert.equal(elements.tailRunBtn.textContent,'使用新条件查询');
  assert.match(elements.tailRunHint.textContent,/原任务结果/);
  ctx.tailBusy=()=>true;ctx.updateTailRunAction();
  assert.equal(elements.tailRunBtn.textContent,'查询进行中');
  ctx.tailBusy=()=>false;ctx.tailState.results.view='history';ctx.updateTailRunAction();
  assert.match(elements.tailRunHint.textContent,/历史方案/);
  assert.equal(viewClasses.has('primary'),true);
  ctx.tailState.results.view='current';ctx.tailState.job.selected={job_id:'paused',payload,status:'finished'};ctx.updateTailRunAction();
  assert.equal(viewClasses.has('primary'),true);assert.equal(elements.tailViewResultsBtn.textContent,'查看方案 ↓');
  ctx.tailBusy=()=>true;ctx.updateTailRunAction();assert.equal(viewClasses.has('primary'),false);
  ctx.tailBusy=()=>false;ctx.tailState.results.view='history';ctx.updateTailRunAction();
  assert.match(elements.tailRunHint.textContent,/历史方案/);
  ctx.tailState.job.selectedId='';ctx.tailState.job.selected=null;ctx.tailState.results.view='current';ctx.updateTailRunAction();
  assert.equal(elements.tailRunBtn.textContent,'开始查询');assert.equal(classes.has('primary'),true);
});

test('empty summary distinguishes first use, filters, running, history and saved empty results',()=>{
  const {ctx,elements}=ui();elements.tailSummary={};
  assignTailState(ctx,{tailResultView:'current',tailSelectedJobId:'',tailResultsLoaded:true,activeTailJobId:''});
  loadFunctions(ctx,['renderTailSummary']);
  ctx.renderTailSummary([],[]);assert.equal(elements.tailSummary.textContent,'尚未开始查询');
  ctx.renderTailSummary([],[quote()]);assert.equal(elements.tailSummary.textContent,'当前筛选下暂无结果');
  ctx.tailState.job.selectedId='run';ctx.tailState.job.activeId='run';ctx.renderTailSummary([],[]);
  assert.match(elements.tailSummary.textContent,/查询进行中/);
  ctx.tailState.results.view='history';ctx.renderTailSummary([],[]);
  assert.match(elements.tailSummary.textContent,/历史报价/);
  ctx.tailState.results.view='current';ctx.tailState.job.activeId='';ctx.renderTailSummary([],[]);
  assert.match(elements.tailSummary.textContent,/暂未保存/);assert.doesNotMatch(elements.tailSummary.textContent,/暂无命中/);
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
  assert.match(html,/航段已核对/);
  assert.match(html,/税费未确认/);
  assert.match(html,/涉及换机场/);
  assert.doesNotMatch(html,/高可信|中可信|>complete</);
  assert.match(ctx.renderTailResultCard(quote({observed_at:'2020-01-01T00:00:00Z',expires_at:'2020-01-01'}),1),/检索 2020-01-01/);
});

test('empty states separate hidden results, first search, running and historical empty',()=>{
  const {ctx,elements}=ui();
  elements.tailMaxPriceInput.value='1'; ctx.renderTailResults([quote()]);
  assert.match(elements.tailResults.innerHTML,/被列表筛选隐藏/);
  elements.tailMaxPriceInput.value='';
  ctx.tailState.results.view='current';ctx.tailState.job.selectedId='';ctx.renderTailResults([]);
  assert.match(elements.tailResults.innerHTML,/先选择我要乘坐的行程/);
  ctx.tailState.job.selectedId='running';ctx.tailState.job.activeId='running';ctx.renderTailResults([]);
  assert.match(elements.tailResults.innerHTML,/正在查找/);
  ctx.tailState.results.view='history';ctx.renderTailResults([]);
  assert.match(elements.tailResults.innerHTML,/还没有已保存/);
});

test('manual candidates survive route/airline edits while newly excluded endpoints are unchecked',()=>{
  const {ctx}=ui();
  const boxes=[{value:'CAN',checked:true},{value:'SZX',checked:false},{value:'BJS',checked:true}];
  ctx.tailState.form.candidatesManual=true;
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
  ctx.tailState.form.candidatesManual=false;ctx.applyTailAirlineDefaults();
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
  assignTailState(ctx,{tailResultRequest:0,tailResultOffset:0,tailHasMore:false,tailDiscoveryResults:[],
    tailResultView:'current',tailSelectedJobId:'a/b',updateTailPagination:()=>{},updateTailDateFilter:()=>{},
    renderTailResults:()=>{},showTailError:()=>{},tailGetJson:async url=>{urls.push(url);return {results:[quote()],has_more:false,next_offset:1}}});
  loadFunctions(ctx,['fetchTailResults']);
  await ctx.fetchTailResults();
  assert.match(urls[0],/job_id=a%2Fb/);
  ctx.tailState.results.view='history';await ctx.fetchTailResults();
  assert.doesNotMatch(urls[1],/job_id/);
});

test('a delayed response from a previous tab cannot overwrite the new context',async()=>{
  const {ctx,elements}=ui();elements.tailLoadMoreBtn={disabled:false};
  let release;
  assignTailState(ctx,{tailResultRequest:0,tailResultOffset:0,tailHasMore:false,tailDiscoveryResults:[],
    tailResultView:'current',tailSelectedJobId:'a',updateTailPagination:()=>{},updateTailDateFilter:()=>{},
    renderTailResults:()=>{},showTailError:()=>{},tailGetJson:()=>new Promise(resolve=>{release=resolve})});
  loadFunctions(ctx,['fetchTailResults']);
  const old=ctx.fetchTailResults();
  const releaseOld=release;
  ctx.tailState.results.view='history';
  const newer=ctx.fetchTailResults();
  release({results:[quote({id:9})],next_offset:1});await newer;
  releaseOld({results:[quote({id:1})],next_offset:1});await old;
  assert.deepEqual(Array.from(ctx.tailState.results.items,r=>r.id),[9]);
});

test('failed polling retains active identity, prevents duplicate starts, and backs off',async()=>{
  const {ctx,elements}=ui();elements.tailConnectionStatus={};
  const timers=[];let busyUpdates=0;
  assignTailState(ctx,{activeTailJobId:'running',activeTailJobTimer:null,tailPollFailures:0,tailConnectionUnknown:false,
    tailGetJson:async()=>{throw new Error('offline')},setTimeout:(fn,delay)=>{timers.push({fn,delay});return timers.length},
    clearTimeout:()=>{},updateTailBusyControls:()=>{busyUpdates++}});
  loadFunctions(ctx,['pollTailDiscoveryJob','tailBusy']);ctx.tailState.connection.submitting=false;
  ctx.pollTailDiscoveryJob('running',true);
  await timers[0].fn();
  assert.equal(ctx.tailState.job.activeId,'running');assert.equal(ctx.tailState.connection.unknown,true);
  assert.equal(ctx.tailBusy(),true);assert.ok(busyUpdates);
  assert.equal(timers[1].delay,7000);assert.match(elements.tailConnectionStatus.textContent,/后台状态待确认/);
});

test('running polling refreshes saved results before task completion; quote failure does not stop job',async()=>{
  const {ctx,elements}=ui();elements.tailConnectionStatus={};
  const timers=[];let resultCalls=0;
  assignTailState(ctx,{activeTailJobId:'running',activeTailJobTimer:null,tailSelectedJobId:'running',tailResultView:'current',
    tailPollFailures:0,tailConnectionUnknown:false,ACTIVE_TAIL_JOB_STATUSES:['queued','running','needs_verification'],
    tailGetJson:async()=>({job_id:'running',status:'running'}),
    setTimeout:(fn,delay)=>{timers.push({fn,delay});return timers.length},clearTimeout:()=>{},
    renderTailJobProgress:()=>{},updateTailCancelButton:()=>{},updateTailBusyControls:()=>{},
    fetchTailResults:async()=>{resultCalls++;throw new Error('results offline')},fetchTailJobs:async()=>{}});
  loadFunctions(ctx,['pollTailDiscoveryJob']);
  ctx.pollTailDiscoveryJob('running',true);await timers[0].fn();
  assert.equal(resultCalls,1);assert.equal(ctx.tailState.job.activeId,'running');
  assert.equal(ctx.tailState.connection.unknown,false);assert.equal(timers.length,2);
});

test('404 is distinguished from temporary disconnect and releases vanished job',async()=>{
  const {ctx,elements}=ui();elements.tailConnectionStatus={};let timer;
  assignTailState(ctx,{activeTailJobId:'gone',activeTailJobTimer:null,tailPollFailures:0,
    setTimeout:fn=>{timer=fn},clearTimeout:()=>{},updateTailCancelButton:()=>{},
    tailGetJson:async()=>{throw Object.assign(new Error('missing'),{status:404})}});
  loadFunctions(ctx,['pollTailDiscoveryJob']);
  ctx.pollTailDiscoveryJob('gone',true);await timer();
  assert.equal(ctx.tailState.job.activeId,'');assert.equal(ctx.tailState.connection.unknown,false);
  assert.match(elements.tailConnectionStatus.textContent,/找不到此任务/);
});

test('completed jobs do not offer meaningless resume and failed jobs get targeted retry',()=>{
  const {ctx,elements}=ui();elements.tailJobSummary={innerHTML:''};
  assignTailState(ctx,{tailBusy:()=>false,tailJobStatusLabel:x=>x,renderTailJobCandidate:()=>'',providerLabel:x=>({ctrip:'携程',feizhu:'飞猪'}[x]||x),ACTIVE_TAIL_JOB_STATUSES:['running','queued']});
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
  assignTailState(ctx,{tailSubmitting:false,tailConnectionUnknown:false,
    fetch:async()=>{posts++;throw new Error('connection lost')},
    setTimeout:(fn,delay)=>{timers.push({fn,delay});return timers.length},clearTimeout:()=>{},
    initializeTailWorkspace:async()=>{}});
  loadFunctions(ctx,['tailPostJob','tailBusy']);
  await assert.rejects(ctx.tailPostJob('/api/tail-discovery/run'),/connection lost/);
  assert.equal(posts,1);assert.equal(ctx.tailState.connection.submitting,false);
  assert.equal(ctx.tailState.connection.unknown,true);assert.equal(ctx.tailBusy(),true);
  assert.equal(timers[1].delay,3500);
  assert.match(elements.tailConnectionStatus.textContent,/不会自动重复提交/);
});

test('authoritative input rejection does not trap the form in an unknown state',async()=>{
  const {ctx,elements}=ui();elements.tailConnectionStatus={};let scheduled=0;
  assignTailState(ctx,{tailSubmitting:false,tailConnectionUnknown:false,
    fetch:async()=>({ok:false,json:async()=>({detail:'bad input'})}),
    setTimeout:()=>{scheduled++;return 1},clearTimeout:()=>{}});
  loadFunctions(ctx,['tailPostJob','tailBusy']);
  await assert.rejects(ctx.tailPostJob('/api/tail-discovery/run'),/bad input/);
  assert.equal(ctx.tailBusy(),false);assert.equal(scheduled,1); // timeout only; no reconciliation needed.
});

test('refresh preserves paginated depth and append removes duplicate IDs',async()=>{
  const {ctx,elements}=ui();elements.tailLoadMoreBtn={disabled:false};
  const calls=[];
  assignTailState(ctx,{tailResultRequest:0,tailResultOffset:400,tailHasMore:true,tailDiscoveryResults:[],
    tailResultView:'current',tailSelectedJobId:'a',updateTailPagination:()=>{},updateTailDateFilter:()=>{},
    renderTailResults:()=>{},showTailError:()=>{},tailGetJson:async url=>{
      const offset=Number(new URL('http://local'+url).searchParams.get('offset'));calls.push(offset);
      return {results:[quote({id:offset+1})],has_more:true,next_offset:offset+200};
    }});
  loadFunctions(ctx,['fetchTailResults']);
  await ctx.fetchTailResults({background:true});
  assert.deepEqual(calls,[0,200]);assert.equal(ctx.tailState.results.items.length,2);
  ctx.tailGetJson=async()=>({results:[quote({id:201}),quote({id:401})],has_more:false,next_offset:402});
  await ctx.fetchTailResults({append:true});
  assert.deepEqual(Array.from(ctx.tailState.results.items,r=>r.id),[1,201,401]);
});

test('saved conditions use a fixed allowlist and ignore credential-like fields',()=>{
  const {ctx,elements}=ui();
  elements.tailOriginInput={value:'BJS'};elements.tailCandidateProfileSelect={value:''};
  let saved;
  assignTailState(ctx,{tailCandidatesManual:true,tailCandidateProfileOverride:'',tailSavedCandidates:null,
    selectedTailCandidates:()=>['CAN'],localStorage:{setItem:(key,value)=>{saved=JSON.parse(value)},
      getItem:()=>JSON.stringify({fields:{tailOriginInput:'SHA',api_key:'not-a-credential'},manual:true,candidates:['SZX']})}});
  loadFunctions(ctx,['tailFormFieldIds','saveTailForm','restoreTailForm']);
  ctx.saveTailForm();assert.equal(saved.fields.tailOriginInput,'BJS');
  assert.equal(saved.fields.api_key,undefined);
  ctx.restoreTailForm();assert.equal(elements.tailOriginInput.value,'SHA');
  assert.equal(ctx.tailState.form.candidatesManual,true);
  assert.deepEqual(Array.from(ctx.tailState.form.savedCandidates),['SZX']);
});

test('refresh does not silently clear a date filter absent from the latest batch',()=>{
  const {ctx,elements}=ui();
  elements.tailDateFilter.value='2027-06-21';
  loadFunctions(ctx,['updateTailDateFilter']);
  ctx.updateTailDateFilter([quote()]);
  assert.equal(elements.tailDateFilter.value,'2027-06-21');
  assert.match(elements.tailDateFilter.innerHTML,/2027-06-21/);
});

test('tail errors preserve the page message and show a non-navigating warning toast',()=>{
  const {ctx,elements}=ui();elements.tailActionError={hidden:true};
  const notices=[];
  assignTailState(ctx,{notifyComplete:(message,options)=>notices.push({message,options})});
  loadFunctions(ctx,['showTailError']);
  ctx.showTailError('模拟复核失败');
  assert.equal(elements.tailActionError.textContent,'模拟复核失败');
  assert.equal(elements.tailActionError.hidden,false);
  assert.equal(notices.length,1);
  assert.equal(notices[0].message,'模拟复核失败');
  assert.equal(notices[0].options.kind,'warning');
  assert.equal(notices[0].options.durationMs,5200);
  ctx.showTailError('');
  assert.equal(elements.tailActionError.hidden,true);
  assert.equal(notices.length,1);
});
