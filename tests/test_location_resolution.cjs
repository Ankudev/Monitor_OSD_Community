const assert = require('node:assert/strict');
require('../web/event-view.js');
const fs = require('node:fs');
const source = fs.readFileSync(require('node:path').join(__dirname, '../web/script.js'), 'utf8');
const extract = (name) => {
  const first = source.indexOf(`function ${name}(`);
  const last = source.indexOf('\nfunction ', first + 1);
  const asyncFirst = source.slice(Math.max(0, first - 6), first) === 'async ' ? first - 6 : first;
  return source.slice(asyncFirst, last);
};
const elements = new Map();
const document = {getElementById(id) {
  if (!elements.has(id)) elements.set(id, {value:'old',textContent:'',disabled:false,classList:{remove(){}}});
  return elements.get(id);
}};
let picked, applied = false;
const make = (response) => new Function('document','fetch','setPickedCoordinates','applyResolvedLocation',
  `let locationResolveRevision=0;\n${extract('validMapPoint')}\n${extract('fetchResolvedLocation')}\nreturn fetchResolvedLocation;`)(
    document, async()=>response, (...point)=>{picked=point;}, ()=>{applied=true;});
(async () => {
  const preview = {error:'Niejednoznaczna miejscowość',resolution_status:'requires_review',
    osm_result:{latitude:52,longitude:21,display_name:'Pomocniczy wynik OSM'}};
  await make({ok:false,status:400,text:async()=>JSON.stringify(preview)})('/example');
  assert.deepEqual(picked,[52,21,true]);
  assert.equal(applied,false);
  for (const id of ['selectedCitySym','selectedStreetSym','objectHouseNumber']) assert.equal(elements.get(id).value,'');
  assert.equal(elements.get('streetSearch').disabled,true);
  assert.match(elements.get('formMessage').textContent,/Wybierz miejscowość/);
  assert.match(elements.get('resolvedLocationName').textContent,/Pomocniczy/);
  await assert.rejects(()=>make({ok:false,status:503,text:async()=>JSON.stringify({error:'Limit geokodera'})})('/example'),/Limit geokodera/);
  console.log('OSM: podgląd bez odgadywania TERYT, stare pola wyczyszczone, błędy dostawcy zachowane - OK');
})().catch(error=>{console.error(error);process.exitCode=1;});
