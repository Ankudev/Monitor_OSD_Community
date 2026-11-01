const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

const source = fs.readFileSync('web/script.js', 'utf8');
const refresh = source.slice(source.indexOf('async function refreshData('), source.indexOf('\nfunction resetObjectForm('));
const tickStart = source.indexOf('function refreshTick(');
const tick = source.slice(tickStart, source.indexOf('\ndocument.', tickStart));
const elements = new Map();
const element = id => {
  if (!elements.has(id)) elements.set(id, { textContent: '', hidden: false, attributes: {},
    setAttribute(name, value) { this.attributes[name] = value; }, classList: { add() {}, toggle() {} } });
  return elements.get(id);
};

(async () => {
  let release;
  let calls = 0;
  let active = 0;
  let maximumActive = 0;
  let fail = false;
  const first = new Promise(resolve => { release = resolve; });
  const context = vm.createContext({
    document: { hidden: false, getElementById: element },
    fetchMapPayload: () => {
      calls++;
      active++;
      maximumActive = Math.max(maximumActive, active);
      const response = fail ? Promise.reject(new Error('offline')) : calls === 1 ? first : Promise.resolve({});
      return response.finally(() => { active--; });
    },
    fetchNotificationStatus: () => Promise.resolve({}),
    applyMapPayload() {}, renderNotificationStatus() {}, renderSourceHealth() {},
    showConnection() {}, renderObjects() {}, renderEvents() {}, updateSourceClocks() {},
  });
  vm.runInContext(`let mainRefreshPending = false, pendingObjectRefresh = false;
    let mapEtag = 'old', lastMapPayload = {}, sourceScheduleAvailable = false;
    let secondsToRefresh = 1; const REFRESH_SECONDS = 30; let statusPayload = null;
    ${refresh}\n${tick}
    function state() { return { mainRefreshPending, pendingObjectRefresh, mapEtag, secondsToRefresh }; }`, context);
  const running = context.refreshData();
  await context.refreshData();
  await context.refreshData({ forceObjects: true });
  await context.refreshData({ forceObjects: true });
  assert.equal(calls, 1, 'Requests must not overlap');
  release({});
  await running;
  assert.equal(calls, 2, 'All pending catalogue refreshes should coalesce into one fresh read');
  assert.equal(maximumActive, 1);
  assert.equal(context.state().mainRefreshPending, false);
  assert.equal(element('mainContent').attributes['aria-busy'], 'false');
  assert.equal(element('initialLoading').hidden, true);
  assert.equal(context.state().mapEtag, '');
  fail = true;
  await context.refreshData();
  assert.equal(context.state().mainRefreshPending, false, 'Network error must allow the next read');
  assert.equal(element('mainContent').attributes['aria-busy'], 'false', 'Błąd musi zakończyć stan ładowania');
  fail = false;
  await context.refreshData();
  const before = calls;
  context.document.hidden = true;
  context.refreshTick();
  assert.equal(calls, before, 'Hidden tab must not poll');
  context.document.hidden = false;
  vm.runInContext('secondsToRefresh = 0', context);
  context.refreshTick();
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(calls, before + 1);
  const vue = fs.readFileSync('integrations/homer/OsdMonitor.vue', 'utf8');
  const start = vue.indexOf('    async refresh(force = false) {');
  const method = vue.slice(start, vue.indexOf('\n    },', start) + 6).replace('async refresh(', 'async function refresh(');
  const visibility = { hidden: true };
  const snutRefresh = new Function('document', 'window', method + '; return refresh;')(
    visibility, { setTimeout: () => 1, clearTimeout() {} });
  let snutRelease;
  let snutCalls = 0;
  const snutWait = new Promise(resolve => { snutRelease = resolve; });
  const snut = { refreshPending: false, endpointState: { status: {}, events: {}, objects: {} },
    fetch: () => { snutCalls++; return snutCalls === 1 ? snutWait : Promise.resolve({ count: 2 }); },
    validateMapPayload: value => value, applyMapPayload(value) { this.eventsPayload = value; } };
  await snutRefresh.call(snut);
  assert.equal(snutCalls, 0, 'Hidden SNUT must not poll');
  visibility.hidden = false;
  const snutRunning = snutRefresh.call(snut);
  await snutRefresh.call(snut, true);
  assert.equal(snutCalls, 1, 'SNUT requests must not overlap');
  snutRelease({ count: 1 });
  await snutRunning;
  visibility.hidden = true;
  await snutRefresh.call(snut, true);
  assert.equal(snutCalls, 2, 'Explicit refresh still works');
  const saved = snut.eventsPayload;
  snut.fetch = () => Promise.reject(new Error('offline'));
  await snutRefresh.call(snut, true);
  assert.equal(snut.eventsPayload, saved, 'Error must preserve the last correct data');
  assert.equal(snut.refreshPending, false);
  assert.match(snut.errorMessage, /offline/);
  snut.fetch = () => Promise.reject(Object.assign(new Error('HTTP 409'), {status: 409}));
  await snutRefresh.call(snut, true);
  assert.equal(snut.eventsPayload, null, 'A policy change must clear the previous SNUT response');
  console.log('Mapa: brak równoległych pobrań, odświeżenie po edycji, błąd i ukryta karta - OK');
})().catch(error => { console.error(error); process.exitCode = 1; });
