const assert = require('node:assert/strict');
require('../web/event-view.js');
const text = globalThis.OsdEventView.publicationText;
assert.equal(text({source: 'pge'}), '');
assert.match(text({source: 'energa'}), /oczekuje na poprawny odczyt/);
for (const status of ['missing', 'invalid']) {
  assert.equal(text({source: 'energa', publication_time_status: status}), 'Operator nie podał poprawnej daty aktualizacji dokumentu.');
}
const health = {source: 'energa', publication_time_status: 'available', publication_updated_at: '2026-10-17T18:30:15Z'};
assert.match(text(health), /20:30/);
assert.match(text(health), /niezależna od czasu pobrania/);
assert(!text(health).includes('2026-10-18'));
assert(!text({...health, publication_updated_at: '<img src=x>'}).includes('<img'));
console.log('Daty publikacji: osobny czas dokumentu, brak daty i zgodność starej odpowiedzi — OK.');

const fs = require('node:fs');
for (const name of ['index', 'readme', 'examples/locations', 'examples/osd-display']) {
  const html = fs.readFileSync(`web/${name}.html`, 'utf8');
  const urls = [...html.matchAll(/\/static\/(event-view\.js|script\.js|match-explanation\.js|diagnostics\.js|style\.css)\?v=([^"\s]+)/g)];
  assert(urls.length > 0);
  const versions = {'style.css':'community-0-1-alpha', 'script.js':'object-history-20261106', 'event-view.js':'community-0-1-alpha', 'match-explanation.js':'community-0-1-alpha', 'diagnostics.js':'community-0-1-alpha'};
  assert(urls.every(match => match[2] === versions[match[1]]), `Nieaktualny adres zasobu: ${name}`);
}
