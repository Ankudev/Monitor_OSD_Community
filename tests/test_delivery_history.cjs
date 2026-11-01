const assert = require('node:assert/strict');
const history = require('../web/delivery-history.js');
assert.equal(history.date(null), 'Brak danych');
assert.ok(history.date(0).includes('1970'), 'Czas Unix 0 jest poprawną datą');
assert.ok(history.date(Date.parse('2026-10-23T08:00:00Z') / 1000).includes('10:00'), 'Daty są wyświetlane w czasie polskim');
console.log('Historia dostaw: daty, brak daty i czas polski - OK');
