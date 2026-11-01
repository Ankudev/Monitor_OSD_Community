const assert = require('node:assert/strict');
require('../web/event-view.js');
const {word, count, polishText, summary} = globalThis.OsdEventView;
const forms = ['obiekt', 'obiekty', 'obiektów'];
for (const [number, expected] of [[0, 'obiektów'], [1, 'obiekt'], [2, 'obiekty'], [4, 'obiekty'], [5, 'obiektów'],
  [11, 'obiektów'], [12, 'obiektów'], [14, 'obiektów'], [21, 'obiektów'], [22, 'obiekty'], [101, 'obiektów'], [112, 'obiektów'], [122, 'obiekty']]) {
  assert.equal(word(number, forms), expected, String(number));
}
assert.equal(count(1, ['wynik', 'wyniki', 'wyników']), '1 wynik');
assert.equal(count(null, forms), '— obiektów');
assert.equal(count(1, ['okno', 'okna', 'okien']), '1 okno');
const event = {description: 'Nie poprawiamy cache ani błędów w danych operatora.',
  match_evidence: {reason: 'Zgodny kod miejscowości lub ulicy; brak potwierdzenia konkretnego adresu/punktu.'}};
const before = JSON.stringify(event);
assert(summary(event).reason.includes('adresu lub punktu'));
assert.equal(summary(event).description, event.description);
assert.equal(summary(event).address, event.description);
assert.equal(JSON.stringify(event), before);
const notes = 'Nie redagujemy: generator 10kW, cache, OSD.';
const powerObject = {power_supply:{generator:'szr',secondary_feed:'manual',notes}};
const originalPower = JSON.stringify(powerObject);
assert.deepEqual(globalThis.OsdEventView.powerSupplyLines(powerObject), [
  'Agregat: Agregat z układem SZR', 'Dodatkowy tor zasilania: Tor przełączany ręcznie', `Uwagi eksploatacyjne: ${notes}`,
]);
assert.equal(JSON.stringify(powerObject), originalPower);
assert.equal(count(22, ['przedział czasu', 'przedziały czasu', 'przedziałów czasu']), '22 przedziały czasu');
assert(polishText('Nie udało się odczytać cache geokodera: SQLiteError').includes('pamięci podręcznej geokodera: SQLiteError'));
assert.equal(polishText('HTTP 429; Retry-After: 30; city_sym'), 'HTTP 429; Retry-After: 30; city_sym');
console.log('Polszczyzna: odmiana liczników, brak danych, zachowane dane operatora i szczegóły techniczne - OK');
