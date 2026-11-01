const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

const source = fs.readFileSync('web/script.js', 'utf8');
const start = source.indexOf('function installMapLayers(');
const end = source.indexOf('\nfunction renderLocationReview', start);
const layers = [];
const timers = [];
let notice;
const target = { active: true, hasLayer(layer) { return this.active && layer === layers[1]; }, on() {}, getZoom() { return 6; }, createPane() { return { style: {} }; } };
const control = () => ({ addTo() { if (this.onAdd) { notice = this.onAdd(); } return this; } });
control.layers = () => ({ addTo() {} });
const context = {
  L: {
    layerGroup(layers) { return { layers, addTo() { return this; } }; },
    tileLayer(url, options) {
      const layer = { url, options, events: {}, addTo() { return this; }, on(event, callback) { this.events[event] = callback; } };
      layers.push(layer);
      return layer;
    },
    control,
    DomUtil: { create() { return { hidden: true, setAttribute() {} }; } },
    DomEvent: { disableClickPropagation() {}, disableScrollPropagation() {} },
  },
  setTimeout(callback, delay) { timers.push({ callback, delay }); },
};
context.L.tileLayer.wms = context.L.tileLayer;
vm.createContext(context);
vm.runInContext(source.slice(start, end), context);
context.installMapLayers(target);
const ortho = layers[1];
const first = { dataset: {}, isConnected: true, src: 'https://mapy.geoportal.gov.pl/tile' };

// Jeden błąd jest ponawiany raz po odstępie, bez nieskończonej pętli.
ortho.events.tileerror({ tile: first });
assert.equal(timers.length, 1);
assert.equal(timers[0].delay, 1000);
timers.shift().callback();
assert.equal(first.src, 'https://mapy.geoportal.gov.pl/tile&retry=1');
ortho.events.tileerror({ tile: first });
assert.equal(timers.length, 0);
assert.equal(notice.hidden, false);

// Po udanym ponowieniu komunikat o błędzie znika.
ortho.events.tileload({ tile: first });
assert.equal(notice.hidden, true);

// Zmiana podkładu lub usunięcie kafelka zatrzymuje zaplanowane żądanie.
const removed = { dataset: {}, isConnected: false, src: 'unchanged' };
ortho.events.tileerror({ tile: removed });
timers.shift().callback();
assert.equal(removed.src, 'unchanged');
ortho.events.tileunload({ tile: removed });
const inactive = { dataset: {}, isConnected: true, src: 'unchanged' };
ortho.events.tileerror({ tile: inactive });
target.active = false;
timers.shift().callback();
assert.equal(inactive.src, 'unchanged');
console.log('Geoportal: ograniczone ponowienie kafelków i anulowanie po zmianie mapy — OK');
