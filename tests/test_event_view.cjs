const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
require('../web/event-view.js');
require('../web/match-explanation.js');
const {OsdEventView, OsdMatchingView} = globalThis;
assert.equal(OsdEventView.sourceErrorText({error: 'SourceMaintenanceError: Komunikat operatora'}), 'Strona operatora niedostępna (prace serwisowe)');
assert.equal(OsdEventView.sourceErrorText({error_category: 'maintenance', error: 'pełna treść'}), 'Strona operatora niedostępna (prace serwisowe)');
assert.equal(OsdEventView.sourceErrorText({error_category: 'maintenance', error: null}), '');
assert.equal(OsdEventView.sourceErrorText({error: 'SourceContractError: Nieznany HTML'}), 'SourceContractError: Nieznany HTML');
const window = {start_at: '2026-10-14T06:00:00Z', stop_at: '2026-10-14T16:00:00Z', valid: true};
const object = {id: 'test', name: 'Obiekt testowy', city: 'Miejscowość testowa', city_sym: '0000001',
  power_supply: {generator: 'manual', secondary_feed: 'none'}, location_audit: {status: 'verified'}};
const event = {object_id: 'test', operator: 'PGE Dystrybucja', type: 'planned_outage', status: 'active', time_phase: 'upcoming',
  description: 'Skrócony opis', time_windows: [window],
  matched_locations: [{numbers: ['3', '4', '5'], teryt: {}}, {numbers: 'Diagnostyka, Orange', teryt: {streetName: 'ul. Testowa'}}],
  match_evidence: {version: 2, scope: 'candidate', point_confirmed: false, reason: 'Zgodność miejscowości nie potwierdza adresu.'},
  match_explanation: {version: 1, confidence: 'low', reason: 'Zgodność miejscowości nie potwierdza adresu.', limitations: ['locality_only']},
  source_url: 'https://example.com/operator'};
const before = JSON.stringify({event, object});
const info = OsdEventView.summary(event, object);
assert.equal(info.address, 'cała miejscowość 3, 4, 5; ul. Testowa Diagnostyka, Orange');
assert(info.periods[0].includes('08:00') && info.periods[0].includes('18:00') && info.periods[0].includes('10 godz.'));
assert(info.title.includes('niepotwierdzone'));
assert(info.hasAdditionalDescription);
assert.equal(info.locationWarning, '');
assert.equal(OsdEventView.summary(event, object, window).periods[0], info.periods[0]);
assert.equal(JSON.stringify({event, object}), before);
assert(!OsdMatchingView.rows(event, {includeReason: false}).includes(info.reason));

const evidence = {version: 2, scope: 'confirmed_point', point_confirmed: true};
for (const override of [{verification_pending: true}, {location_review_required: true}, {match_evidence: {...evidence, version: 1}}, {match_evidence: {...evidence, scope: 'candidate'}}]) {
  assert.equal(OsdEventView.pointConfirmed({...event, match_evidence: evidence, ...override}), false);
}
assert.equal(OsdEventView.pointConfirmed({...event, match_evidence: evidence}), true);
assert.equal(OsdEventView.eventLabel({...event, status: 'cancelled'}), 'Komunikat odwołany');
assert.equal(OsdEventView.eventLabel({...event, status: 'resolved'}), 'Komunikat zakończony według operatora');
assert.equal(OsdEventView.safeUrl('javascript:alert(1)'), '');
assert.equal(OsdEventView.addressText({description: 'Opis bez wykazu adresów'}), 'Opis bez wykazu adresów');
assert.equal(OsdEventView.addressText({}), 'Brak szczegółowego adresu');
assert.equal(OsdEventView.summary({...event, time_windows: [window, {...window, start_at: '2026-10-15T06:00:00Z', stop_at: '2026-10-15T16:00:00Z'}]}, object).periods.length, 2);
const incomplete = OsdEventView.summary({...event, time_windows: [{...window, start_at: null}]}, object).periods[0];
assert(incomplete.includes('Początek niepodany'));
assert(!incomplete.includes('czas trwania'));
const missingEnd = OsdEventView.summary({...event, time_windows: [{...window, stop_at: null}]}, object).periods[0];
assert(missingEnd.includes('Początek: ') && missingEnd.includes('koniec niepodany'));
assert(!missingEnd.includes('czas trwania'));
assert(!/[—–]/u.test(info.periods[0]));
const sourceText = 'Opis operatora — zakres 1–5; napięcie 15 kV.';
assert.equal(OsdEventView.summary({...event, description: sourceText, matched_locations: [{text: sourceText}]}, object).description, sourceText);
assert.equal(OsdEventView.addressText({...event, matched_locations: [{text: sourceText}]}), sourceText);

const source = fs.readFileSync('web/script.js', 'utf8');
function element(tag, className, text) { return {tag, className, text, children: [], append(...items) {this.children.push(...items);}}; }
const context = vm.createContext({OsdEventView, OsdMatchingView, URLSearchParams, objects: [object], activeView: 'planned', selectedDate: '2026-10-14',
  document: {createElement: tag => element(tag)}, createElement: element, matchConfidenceLabels: {},
  sourceLabel: event => event.operator});
for (const name of ['escapeHtml', 'objectForEvent', 'objectUrl', 'eventLabel', 'safeSourceUrl', 'firstTeryt', 'rawEvent',
  'formatDate', 'matchExplanationRows', 'matchExplanationElement', 'createMeta', 'createDataBox', 'mapEventMessage', 'createEventCard']) {
  const start = source.indexOf(`function ${name}(`), end = source.indexOf('\nfunction ', start + 1);
  assert(start >= 0 && end > start, name);
  vm.runInContext(source.slice(start, end), context);
}
function texts(node) {return [node.text || '', ...node.children.flatMap(texts)];}
const popup = context.mapEventMessage(event, object);
const card = texts(context.createEventCard(event));
for (const field of [info.title, info.operator, info.address, info.reason, ...info.periods]) {
  assert(popup.includes(field), field);
  assert(card.includes(field), field);
}
assert.equal(card.filter(line => line === info.reason).length, 1);
assert(!card.includes('{}'));
const unsafe = context.mapEventMessage({...event, matched_locations: [{text: '<img src=x onerror=alert(1)>'}]}, object);
assert(!unsafe.includes('<img src='));
assert(unsafe.includes('&lt;img'));
console.log('Prezentacja zdarzeń: wspólne adresy, terminy, stan, ograniczenia, brak powtórzeń i bezpieczny tekst - OK');
