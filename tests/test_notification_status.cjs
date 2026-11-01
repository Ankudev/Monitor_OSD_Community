const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const code = fs.readFileSync(require('node:path').join(__dirname, '../web/script.js'), 'utf8');
const slice = (start, end) => code.slice(code.indexOf(start), code.indexOf(end, code.indexOf(start)));
const label = {textContent: ''};
const context = vm.createContext({document: {getElementById: () => label}});
vm.runInContext(slice('function renderNotificationStatus(', 'function renderSourceHealth('), context);
context.renderNotificationStatus({enabled:false, worker_state:'disabled'});
assert.equal(label.textContent, 'Powiadomienia: wyłączone.');
context.renderNotificationStatus({enabled:true, worker_state:'running', counts:{pending:2, delivered:3, failed:1}});
assert.match(label.textContent, /oczekujące 2/);
assert.match(label.textContent, /przyjęte przez odbiorcę 3/);
context.renderNotificationStatus({worker_state:'storage_error'});
assert.match(label.textContent, /błąd zapisu lub odczytu kolejki/);
context.renderNotificationStatus({worker_state:'unavailable'});
assert.match(label.textContent, /niedostępny/);
// Całe odświeżenie: status jest osobnym odczytem, nawet gdy mapa pochodzi z ETag.
Object.assign(context, {mainRefreshPending:false, pendingObjectRefresh:false, mapEtag:'etag', lastMapPayload:{}, sourceScheduleAvailable:false,
  statusPayload:null, secondsToRefresh:0, REFRESH_SECONDS:30, Promise,
  fetchMapPayload:async()=>({data_status:'fresh'}), fetchNotificationStatus:async()=>({enabled:false,worker_state:'disabled'}),
  applyMapPayload:()=>{context.statusPayload={ok:true};}, formatDate:()=>'', renderSourceHealth:()=>{},
  showConnection:()=>{}, renderObjects:()=>{}, renderEvents:()=>{}});
context.document.getElementById = id => id==='notificationStatus' ? label : {setAttribute(){},classList:{add(){},toggle(){}},textContent:''};
vm.runInContext(slice('async function refreshData(', 'function resetObjectForm('), context);
(async()=>{
  await context.refreshData();
  assert.equal(label.textContent,'Powiadomienia: wyłączone.');
  context.fetchNotificationStatus=async()=>{throw new Error('offline');};
  await context.refreshData();
  assert.match(label.textContent,/niedostępny/);
  assert.equal(context.sourceScheduleAvailable,true);
  console.log('R7: status kolejki aktualizowany niezależnie od mapy i błędu statusu.');
})().catch(error=>{console.error(error);process.exitCode=1;});
