const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const apiSource = fs.readFileSync(path.join(__dirname, '../static/app/api.js'), 'utf8');
const storeSource = fs.readFileSync(path.join(__dirname, '../static/app/store.js'), 'utf8');

function storeContext(savedTab = null) {
  return vm.createContext({
    localStorage: {getItem: key => key === 'activeWorkspaceTab' ? savedTab : null},
    window: {tailCandidateNames: {}},
  });
}

test('store provides independent initial dashboard and browser profile state', () => {
  const ctx = storeContext();
  vm.runInContext(storeSource, ctx);
  assert.equal(vm.runInContext('dashboardState.activeTaskView', ctx), 'all');
  assert.equal(vm.runInContext('dashboardState.data.tasks.length', ctx), 0);
  assert.equal(vm.runInContext('browserProfileState.data.providers.length', ctx), 0);
  assert.equal(vm.runInContext('dashboardState.pollInFlight', ctx), false);
});

test('workspace state restores the saved tab and falls back to routes', () => {
  const restored = storeContext('tail');
  vm.runInContext(storeSource, restored);
  assert.equal(vm.runInContext('workspaceState.activeTab', restored), 'tail');
  const initial = storeContext();
  vm.runInContext(storeSource, initial);
  assert.equal(vm.runInContext('workspaceState.activeTab', initial), 'routes');
});

test('fetchApi passes ordinary requests through unchanged', async () => {
  let call;
  const response = {ok: true};
  const ctx = vm.createContext({
    fetch: async (...args) => { call = args; return response; },
  });
  vm.runInContext(apiSource, ctx);
  const result = await vm.runInContext('fetchApi("/api/test", {method:"POST"})', ctx);
  assert.equal(result, response);
  assert.equal(call[0], '/api/test');
  assert.equal(call[1].method, 'POST');
  assert.deepEqual(Object.keys(call[1]), ['method']);
});

test('fetchApi applies a timeout signal without leaking timeoutMs and clears the timer', async () => {
  let call, timerCallback, cleared = false;
  const ctx = vm.createContext({
    AbortController,
    fetch: async (...args) => { call = args; return {ok: true}; },
    setTimeout: callback => { timerCallback = callback; return 'timer'; },
    clearTimeout: timer => { cleared = timer === 'timer'; },
  });
  vm.runInContext(apiSource, ctx);
  await vm.runInContext('fetchApi("/api/test", {method:"POST", timeoutMs:1500})', ctx);
  assert.equal(call[1].method, 'POST');
  assert.equal(Object.hasOwn(call[1], 'timeoutMs'), false);
  assert.ok(call[1].signal instanceof AbortSignal);
  assert.equal(typeof timerCallback, 'function');
  assert.equal(cleared, true);
});

test('responseErrorMessage preserves JSON detail, error and plain text fallbacks', async () => {
  const ctx = vm.createContext({});
  vm.runInContext(apiSource, ctx);
  const cases = [
    ['{"detail":"保存失败"}', 'fallback', '保存失败'],
    ['{"error":"读取失败"}', 'fallback', '读取失败'],
    ['plain failure', 'fallback', 'plain failure'],
    ['', 'fallback', 'fallback'],
  ];
  for (const [body, fallback, expected] of cases) {
    const actual = await vm.runInContext(
      `responseErrorMessage({text:async()=>${JSON.stringify(body)}}, ${JSON.stringify(fallback)})`, ctx,
    );
    assert.equal(actual, expected);
  }
});

test('tail state exposes independent form, job, results and connection groups', () => {
  const ctx = storeContext();
  vm.runInContext(storeSource, ctx);
  assert.equal(vm.runInContext('tailState.form.safeBatchSize', ctx), 3);
  assert.equal(vm.runInContext('tailState.job.activeId', ctx), '');
  assert.equal(vm.runInContext('tailState.results.view', ctx), 'current');
  assert.equal(vm.runInContext('tailState.connection.unknown', ctx), false);
  assert.equal(vm.runInContext('tailState.form !== tailState.job && tailState.job !== tailState.results', ctx), true);
});

test('tail candidate names retain their window compatibility mirror', () => {
  const ctx = vm.createContext({
    localStorage: {getItem: () => null},
    window: {tailCandidateNames: {CA: '中国国航'}},
  });
  vm.runInContext(storeSource, ctx);
  assert.equal(vm.runInContext('tailState.form.candidateNames.CA', ctx), '中国国航');
  assert.equal(vm.runInContext('tailState.form.candidateNames === window.tailCandidateNames', ctx), true);
});

test('fetchApi relies on the HttpOnly session cookie and never reads URL or web storage tokens', async () => {
  let call;
  const ctx = vm.createContext({
    window: {
      get location() { throw new Error('location token access is forbidden'); },
      get sessionStorage() { throw new Error('session token access is forbidden'); },
    },
    fetch: async (...args) => { call = args; return {ok: true}; },
  });
  vm.runInContext(apiSource, ctx);
  await vm.runInContext('fetchApi("/api/dashboard")', ctx);
  assert.equal(call[0], '/api/dashboard');
  assert.deepEqual(Object.keys(call[1]), []);
});

test('dashboard render functions escape untrusted route and snapshot text', () => {
  const commonSource = fs.readFileSync(path.join(__dirname, '../static/app/common.js'), 'utf8');
  const dashboardSource = fs.readFileSync(path.join(__dirname, '../static/app/dashboard.js'), 'utf8');
  const createdElements = [];
  function makeElement(tag) {
    return {
      tagName: tag,
      className: '',
      innerHTML: '',
      value: '',
      textContent: '',
      classList: {add() {}, toggle() {}},
      appendChild(child) { createdElements.push(child); },
    };
  }
  const elementsById = {
    providerCheckboxes: makeElement('div'),
    providerFilter: makeElement('select'),
    routeList: makeElement('div'),
  };
  const ctx = vm.createContext({
    window: {},
    Intl,
    Date,
    JSON,
    Number,
    String,
    Boolean,
    Array,
    Set,
    Map,
    document: {
      getElementById: id => elementsById[id] || null,
      createElement: tag => makeElement(tag),
    },
    dashboardState: {routeRenderSignature: '', activeTaskView: 'all', data: {tasks: []}},
    airportCodeDisplayText: code => String(code || ''),
  });
  vm.runInContext(commonSource, ctx);
  vm.runInContext(dashboardSource, ctx);

  vm.runInContext('renderProviderOptions(["ctrip\\"><img src=x onerror=alert(1)>"])', ctx);
  assert.ok(!createdElements[0].innerHTML.includes('<img src=x'));

  const row = vm.runInContext(`renderTaskRow(${JSON.stringify({
    status: 'ok',
    provider: 'ctrip',
    route_key: 'SHA|PEK|2099-01-01"><script>alert(1)</script>',
    origin: 'SHA',
    destination: 'PEK',
    departure_date: '2099-01-01',
    cabin: '<b>evil</b>',
    transfer_policy: '<i>evil</i>',
    preferred_airlines: ['<svg onload=1>'],
    latest_price: 500,
    currency: '<img src=x onerror=1>',
  })})`, ctx);
  assert.ok(!row.innerHTML.includes('<script>'));
  assert.ok(!row.innerHTML.includes('<svg onload=1>'));
  assert.ok(!row.innerHTML.includes('<img src=x onerror=1>'));

  vm.runInContext(`renderRoutes([${JSON.stringify({
    route_key: 'SHA|PEK"><img src=x onerror=1>',
    route_type: 'one_way',
    origin: 'SHA',
    destination: 'PEK',
    departure_date: '2099-01-01',
    cabin: 'economy',
    transfer_policy: 'any',
    passengers: '<script>1</script>',
    providers: ['<b>ctrip</b>'],
    preferred_airlines: ['<i>CA</i>'],
    auto_query_enabled: false,
  })}])`, ctx);
  const card = createdElements[createdElements.length - 1];
  assert.ok(!card.innerHTML.includes('<img src=x onerror=1>'));
  assert.ok(!card.innerHTML.includes('<b>ctrip</b>'));
  assert.ok(!card.innerHTML.includes('<i>CA</i>'));
});

test('dashboard task groups never compare different currencies or quote terms', () => {
  const commonSource = fs.readFileSync(path.join(__dirname, '../static/app/common.js'), 'utf8');
  const dashboardSource = fs.readFileSync(path.join(__dirname, '../static/app/dashboard.js'), 'utf8');
  const ctx = vm.createContext({
    window: {},
    Intl,
    Date,
    JSON,
    Number,
    String,
    Boolean,
    Array,
    Set,
    Map,
  });
  vm.runInContext(commonSource, ctx);
  vm.runInContext(dashboardSource, ctx);

  const result = vm.runInContext(`(() => {
    const base = {
      currency: 'CNY', passengers: 1, cabin: 'economy', departure_date: '2099-01-01',
      route_type: 'one_way', transfer_policy: 'any', preferred_airlines: [],
      quote_terms: {price_basis: 'per_person', tax_included: true, priced_passengers: 1},
    };
    const differentCurrency = summarizeTaskGroup({tasks: [
      {...base, latest_price: 100, currency: 'USD'},
      {...base, latest_price: 1000},
    ]});
    const differentPassengers = summarizeTaskGroup({tasks: [
      {...base, latest_price: 100},
      {...base, latest_price: 90, passengers: 2, quote_terms: {...base.quote_terms, priced_passengers: 2}},
    ]});
    const comparable = summarizeTaskGroup({tasks: [
      {...base, latest_price: 100},
      {...base, latest_price: 90},
    ]});
    return {
      differentCurrency: {best: differentCurrency.best, comparable: differentCurrency.comparable},
      differentPassengers: {best: differentPassengers.best, comparable: differentPassengers.comparable},
      comparable: {best: comparable.best.latest_price, comparable: comparable.comparable},
    };
  })()`, ctx);

  assert.equal(result.differentCurrency.best, null);
  assert.equal(result.differentCurrency.comparable, false);
  assert.equal(result.differentPassengers.best, null);
  assert.equal(result.differentPassengers.comparable, false);
  assert.equal(result.comparable.best, 90);
  assert.equal(result.comparable.comparable, true);
});

test('workspace tab persistence writes and reads the same localStorage key with ARIA tabs', () => {
  const workspaceSource = fs.readFileSync(path.join(__dirname, '../static/app/workspace.js'), 'utf8');
  const saved = new Map();
  const makeNode = () => ({
    hidden: false,
    dataset: {},
    attrs: {},
    classList: {add() {}, toggle() {}},
    setAttribute(k, v) { this.attrs[k] = v; },
    addEventListener() {},
    querySelector() { return null; },
    querySelectorAll() { return []; },
  });
  const panels = {
    routes: makeNode(),
    tasks: makeNode(),
    tail: makeNode(),
    profiles: makeNode(),
    insights: makeNode(),
    experiments: makeNode(),
  };
  const ctx = vm.createContext({
    localStorage: {
      getItem: key => saved.get(key) ?? null,
      setItem: (key, value) => saved.set(key, value),
    },
    window: {tailCandidateNames: {}},
    document: {
      body: {dataset: {}},
      getElementById: id => ({
        tailDiscoveryPanel: panels.tail,
        browserProfilePanel: panels.profiles,
        airchinaExperimentPanel: panels.experiments,
      }[id] || null),
      querySelector: sel => ({
        '.route-grid': panels.routes,
        '.log-workspace': panels.insights,
      }[sel] || null),
      querySelectorAll: () => [],
    },
  });
  vm.runInContext(storeSource, ctx);
  vm.runInContext(workspaceSource, ctx);
  vm.runInContext('setWorkspaceTab("tail")', ctx);
  assert.equal(saved.get('activeWorkspaceTab'), 'tail');
  // Re-initialize store against the same storage to verify round-trip restore.
  const reloaded = vm.createContext({
    localStorage: {getItem: key => saved.get(key) ?? null},
    window: {tailCandidateNames: {}},
  });
  vm.runInContext(storeSource, reloaded);
  assert.equal(vm.runInContext('workspaceState.activeTab', reloaded), 'tail');
});

test('renderPriceHistorySparkline renders SVG trend line and escapes labels', () => {
  const commonSource = fs.readFileSync(path.join(__dirname, '../static/app/common.js'), 'utf8');
  const dashboardSource = fs.readFileSync(path.join(__dirname, '../static/app/dashboard.js'), 'utf8');
  const ctx = vm.createContext({window: {}, Intl, Date, JSON, Number, String, Boolean, Array, Set, Map});
  vm.runInContext(commonSource, ctx);
  vm.runInContext(dashboardSource, ctx);
  const html = vm.runInContext(`renderPriceHistorySparkline([
    {observed_at: '2099-01-01T00:00:00Z', price: 900, currency: 'CNY', flight_no: 'CA1501'},
    {observed_at: '2099-01-02T00:00:00Z', price: 780, currency: 'CNY', flight_no: '<img src=x>'}
  ], 800)`, ctx);
  assert.ok(html.includes('<svg'));
  assert.ok(html.includes('history-chart-line'));
  assert.ok(html.includes('history-target-line'));
  assert.ok(!html.includes('<img src=x>'));
});

