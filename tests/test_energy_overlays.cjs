const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync('web/script.js', 'utf8');
const start = source.indexOf('function installMapLayers(');
const end = source.indexOf('\nfunction renderLocationReview', start);
const layers = [], active = new Set(), events = {}, panes = {};
let zoom = 6, notice, choices;
const tileLayer = (url, options) => {
  const layer = { url, options, events: {}, addTo() { active.add(this); return this; },
    on(name, callback) { this.events[name] = callback; return this; } };
  layers.push(layer);
  return layer;
};
tileLayer.wms = tileLayer;
const control = () => ({ addTo() { notice = this.onAdd(); return this; } });
control.layers = (base, overlays) => { choices = { base, overlays }; return { addTo() {} }; };
const target = { hasLayer: layer => active.has(layer), getZoom: () => zoom,
  createPane(name) { return panes[name] = { style: {} }; },
  on(names, callback) { for (const name of names.split(' ')) events[name] = callback; } };
const context = { L: { tileLayer, control, layerGroup(layers) { return { layers, addTo() { active.add(this); layers.forEach(layer => active.add(layer)); return this; } }; },
  DomUtil: { create() { return { hidden: true, setAttribute() {} }; } },
  DomEvent: { disableClickPropagation() {}, disableScrollPropagation() {} } }, setTimeout() {} };
vm.createContext(context);
vm.runInContext(source.slice(start, end), context);
context.installMapLayers(target);
const [osm, ortho, gesut, pse] = layers;
assert(active.has(osm));
assert(active.has(gesut));
assert(active.has(pse));
assert(active.has(choices.overlays['Sieć elektroenergetyczna GESUT']));
assert.match(notice.textContent, /poziomu 20/);
assert.equal(gesut.options.minZoom, 20);
assert.equal(osm.options.maxNativeZoom, 18);
assert.equal(ortho.options.maxNativeZoom, 19);
assert.equal(osm.options.maxZoom, 20);
assert.equal(ortho.options.maxZoom, 20);
assert.equal(panes.energyInfrastructure.style.pointerEvents, 'none');
assert.equal(panes.energyInfrastructure.style.zIndex, '250');
assert.equal(Object.keys(choices.overlays).length, 2);

active.add(gesut); events.overlayadd();
assert.match(notice.textContent, /poziomu 20/);
zoom = 20; events.zoomend();
assert.doesNotMatch(notice.textContent, /poziomu 20/);
assert.match(notice.textContent, /nie potwierdza zasilania/);

// Udany kafelek nie ukrywa błędów innych kafelków ani drugiej nakładki.
active.add(pse);
const a = {}, b = {}, c = {};
gesut.events.tileerror({ tile: a }); gesut.events.tileerror({ tile: b });
pse.events.tileerror({ tile: c });
gesut.events.tileload({ tile: a });
assert.match(notice.textContent, /GESUT: nie udało/);
assert.match(notice.textContent, /PSE: nie udało/);
gesut.events.tileunload({ tile: b });
assert.doesNotMatch(notice.textContent, /GESUT: nie udało/);
assert.match(notice.textContent, /PSE: nie udało/);
active.delete(pse); events.overlayremove();
assert.doesNotMatch(notice.textContent, /PSE: nie udało/);
active.delete(gesut); events.overlayremove();
assert.equal(notice.hidden, true);
console.log('Nakładki energetyczne: domyślne włączenie, zoom, oddzielne błędy i ochrona kliknięć — OK');
