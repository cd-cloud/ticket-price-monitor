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
