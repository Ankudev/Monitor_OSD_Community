const assert = require('node:assert/strict');
require('../web/event-view.js');
// Minimalny DOM do kontroli interakcji i wyścigu odpowiedzi; wygląd sprawdzamy w przeglądarce.
class Element {
  constructor(tag) { this.tagName = tag; this.childNodes = []; this.dataset = {}; this.attributes = {}; this.handlers = {}; this.text = ''; }
  set textContent(text) { this.text = text; this.childNodes = []; }
  get textContent() { return this.text + this.childNodes.map(child => child.textContent).join(' '); }
  append(...children) {
    for (const child of children) {
      if (child.parent) child.parent.childNodes = child.parent.childNodes.filter(value => value !== child);
      child.parent = this;
      this.childNodes.push(child);
    }
  }
  replaceChildren(...children) { this.text = ''; this.childNodes = []; this.append(...children); }
  querySelector(tag) { return this.childNodes.find(child => child.tagName === tag) || this.childNodes.map(child => child.querySelector(tag)).find(Boolean); }
  setAttribute(name, value) { this.attributes[name] = value; }
  addEventListener(name, handler) { this.handlers[name] = handler; }
  focus() { globalThis.focused = this; }
}
globalThis.document = {createElement: tag => new Element(tag)};
require('../web/object-history.js');
const tick = () => new Promise(resolve => setImmediate(resolve));
function fixture(id) {
  const root = new Element('div');
  const heading = new Element('h4'); heading.textContent = 'Obiekt testowy'; root.append(heading);
  globalThis.OsdObjectHistory.mount(root, id);
  const tabs = root.childNodes.find(child => child.attributes.role === 'tablist');
  const panels = root.childNodes.filter(child => child.attributes.role === 'tabpanel');
  return {root, heading, tabs: tabs.childNodes, panels};
}
(async () => {
  let calls = [];
  globalThis.fetch = async (url, options) => {
    calls.push({url, options});
    return {ok:true, json:async () => ({demo:true, items:[0,1,2,3].map(i => ({object_id:'one', type:'outage', description:`<img src=x onerror=alert(${i})>`}))})};
  };
  const view = fixture('one & another=1');
  assert.equal(calls.length, 0, 'Nie pobieraj historii wszystkich obiektów przy starcie.');
  view.tabs[1].handlers.click();
  await tick();
  const query = new URL(calls[0].url, 'http://localhost').searchParams;
  assert.equal(query.get('object_id'), 'one & another=1');
  assert.equal(query.get('limit'), '3');
  assert.equal(query.get('latest_per_event'), '1');
  assert.equal(view.panels[1].querySelector('ol').childNodes.length, 3);
  assert.match(view.panels[1].textContent, /symulowane/);
  assert.equal(view.panels[1].querySelector('img'), undefined);
  assert.match(view.panels[1].textContent, /<img src=x/);
  assert.equal(view.heading.parent, view.root, 'Nazwa obiektu widoczna nad zakładkami.');
  assert.equal(view.panels[0].hidden, true);
  assert.equal(view.tabs[1].attributes['aria-selected'], 'true');
  let prevented = false;
  view.tabs[1].handlers.keydown({key:'ArrowLeft', preventDefault() { prevented = true; }});
  assert(prevented);
  assert.equal(globalThis.focused, view.tabs[0]);
  assert.equal(view.panels[1].hidden, true);

  let resolveOld;
  globalThis.fetch = () => new Promise(resolve => { resolveOld = resolve; });
  view.tabs[1].handlers.click();
  view.tabs[0].handlers.click();
  globalThis.fetch = async () => ({ok:true, json: async () => ({items:[]})});
  view.tabs[1].handlers.click();
  await tick();
  resolveOld({ok:true, json:async () => ({items:[{description:'stara odpowiedź'}]})});
  await tick();
  assert.match(view.panels[1].textContent, /Brak zapisanej historii/);
  assert(!view.panels[1].textContent.includes('stara odpowiedź'));

  globalThis.fetch = async () => ({ok:false});
  view.tabs[1].handlers.click();
  await tick();
  assert.match(view.panels[1].textContent, /Nie udało się/);
  assert.equal(view.panels[1].attributes['aria-busy'], 'false');
  globalThis.fetch = async () => ({ok:true, json:async () => ({items:[]})});
  await view.panels[1].querySelector('button').handlers.click();
  assert.match(view.panels[1].textContent, /Brak zapisanej historii/);
  assert.equal(view.tabs[0].attributes['aria-controls'], view.panels[0].id);
  assert.notEqual(fixture('two').panels[0].id, view.panels[0].id);
  console.log('Historia obiektu: limit, kodowanie ID, tekst bez HTML, demo, klawiatura, wyścig i ponowienie: OK');
})().catch(error => { console.error(error); process.exitCode = 1; });
