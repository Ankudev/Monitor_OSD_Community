const fs = require('node:fs');
const assert = require('node:assert/strict');
require('../web/event-view.js');
const source = fs.readFileSync(require('node:path').join(__dirname, '../web/script.js'), 'utf8');
const first = source.indexOf('function addressText(');
const last = source.indexOf('\nfunction ', first + 1);
const addressText = new Function(`${source.slice(first, last)}; return addressText;`)();
for (const numbers of ['110', ['110']]) {
  assert.equal(addressText({matched_locations:[{teryt:{streetName:'Testowa'}, numbers}]}), 'Testowa 110');
}
assert.equal(addressText({matched_locations:[{teryt:{streetName:'Rolnicza'}, numbers:'23b, 23c, dz. 911/3'}]}), 'Rolnicza 23b, 23c, dz. 911/3');
console.log('Opisy PGE: tekst i tablice numerów zachowane.');
