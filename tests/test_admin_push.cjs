const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const code = fs.readFileSync(require('node:path').join(__dirname, '../web/admin-push.js'), 'utf8');

function setup(authorized, error) {
  const elements = {pushSubscribe: {}, pushStatus: {}, pushDevice: {}};
  elements.pushSubscribe.addEventListener = (_, handler) => { elements.click = handler; };
  const calls = {scripts: [], init: [], permission: 0, optIn: 0};
  const sdk = {
    init: async options => { calls.init.push(options); },
    Notifications: {permission: false, requestPermission: async () => { calls.permission++; }},
    User: {PushSubscription: {id: null, optedIn: false,
      addEventListener() {}, optIn: async () => { calls.optIn++; }}},
  };
  const window = {};
  const context = vm.createContext({window, AbortSignal,
    fetch: async () => ({ok: authorized, json: async () => ({app_id: 'public-app-id', error})}),
    document: {getElementById: id => elements[id], createElement: () => ({}),
      head: {append: script => {
        calls.scripts.push(script.src);
        Promise.resolve().then(() => window.OneSignalDeferred.shift()(sdk));
      }}},
  });
  vm.runInContext(code, context);
  return {elements, calls, sdk};
}

(async () => {
  const denied = setup(false);
  await denied.elements.click();
  assert.equal(denied.calls.scripts.length, 0);
  assert.match(denied.elements.pushStatus.textContent, /sesję administratora/);
  assert.equal(denied.elements.pushSubscribe.disabled, false);

  const migrated = setup(false, 'Powiadomienia wymagają konfiguracji OneSignal dla nowego adresu i ponownego zapisania urządzenia.');
  await migrated.elements.click();
  assert.match(migrated.elements.pushStatus.textContent, /nowego adresu/);
  assert.equal(migrated.calls.scripts.length, 0);
  assert.equal(migrated.calls.permission, 0);

  const page = setup(true);
  assert.equal(page.calls.scripts.length, 0, 'Brak kontaktu z dostawcą przed kliknięciem');
  await page.elements.click();
  assert.equal(page.calls.init.length, 1);
  assert.equal(page.calls.init[0].welcomeNotification.disable, true);
  assert.equal(page.calls.init[0].serviceWorkerParam.scope, '/osd-push/');
  assert.equal(page.calls.optIn, 0, 'Odmowa uprawnienia nie uruchamia subskrypcji');
  assert.match(page.elements.pushStatus.textContent, /Brak aktywnej subskrypcji/);

  page.sdk.Notifications.permission = true;
  page.sdk.User.PushSubscription.id = 'device-id';
  page.sdk.User.PushSubscription.optedIn = true;
  await page.elements.click();
  assert.equal(page.calls.init.length, 1, 'Ponowienie nie inicjalizuje SDK drugi raz');
  assert.equal(page.calls.scripts.length, 1);
  assert.equal(page.calls.optIn, 1);
  assert.equal(page.elements.pushDevice.textContent, 'device-id');
  assert.match(page.elements.pushStatus.textContent, /Odbiorca musi być wskazany/);
  console.log('Push: prywatna konfiguracja, zgoda, jeden SDK i jawny odbiorca.');
})().catch(error => { console.error(error); process.exitCode = 1; });
