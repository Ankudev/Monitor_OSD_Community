const fs = require('node:fs');
const assert = require('node:assert/strict');
require('../web/event-view.js');
const source = fs.readFileSync(require('node:path').join(__dirname,'../web/script.js'),'utf8');
function extract(name) {
  const start = new RegExp(`(?:^|\\n)(?:async )?function ${name}\\(`).exec(source);
  assert(start, name);
  const first = start.index + (source[start.index] === '\n' ? 1 : 0);
  const following = /\n(?:async )?function /g;
  following.lastIndex = first + start[0].length;
  const end = following.exec(source);
  return source.slice(first,end ? end.index : source.indexOf('\ndocument.',first));
}
const names = ['validMapPoint','setPickedCoordinates','clearLocationAddress','applyResolvedLocation',
  'fetchResolvedLocation','resolvePickedPoint','clearResolvedLocation'];
function harness(fetch) {
  const fields = new Map();
  const document = {getElementById(id) {
    if (!fields.has(id)) fields.set(id,{value:'',textContent:'',disabled:false,className:'',classList:{add(){},remove(){}}});
    return fields.get(id);
  }};
  const markers = [], zooms = [];
  const map = {setView(...args){zooms.push(args);}};
  const L = {marker(point) {
    const marker={point,removed:false,addTo(){return this;},bindPopup(){return this;},openPopup(){return this;},remove(){this.removed=true;}};
    markers.push(marker);return marker;
  }};
  const api = new Function('document','fetch','L','locationMap',
    `let previewMarker,locationResolveRevision=0,autoLocationName=null,autoLocationKeyword=null;\n`
    +names.map(extract).join('\n')+ '\nreturn {pick:resolvePickedPoint,clear:clearResolvedLocation};')(document,fetch,L,map);
  return {api,fields,document,markers,zooms,value:id=>document.getElementById(id).value};
}
const success = (lat,lon,city,sym) => ({ok:true,status:200,text:async()=>JSON.stringify({latitude:lat,longitude:lon,city,city_sym:sym,street:'',street_sym:''})});
(async () => {
  // Drugi klik działa nawet gdy geokoder odpowie błędem; stary adres nie pozostaje.
  let count=0;
  const a=harness(async()=>++count===1?success(51,20,'Pierwsze','0000001'):{ok:false,status:429,text:async()=>JSON.stringify({error:'Geokoder zajęty'})});
  await a.api.pick(51,20);
  assert.equal(a.value('objectName'),'Pierwsze');
  a.document.getElementById('objectHouseNumber').value='12';
  await a.api.pick(52,21);
  assert.equal(a.value('objectLatitude'),'52.000000');
  assert.equal(a.value('objectLongitude'),'21.000000');
  for(const id of ['selectedCitySym','objectHouseNumber','objectName','objectKeyword']) assert.equal(a.value(id),'');
  assert.match(a.document.getElementById('formMessage').textContent,/Punkt został wybrany/);
  assert.equal(a.zooms.length,0);
  assert(a.markers.slice(0,-1).every(m=>m.removed));

  // Automatyczne nazwy po kolejnym poprawnym odczycie dotyczą nowego punktu.
  let city=0;
  const labels=harness(async()=>++city===1?success(51,20,'Pierwsze','0000001'):success(52,21,'Drugie','0000002'));
  await labels.api.pick(51,20);await labels.api.pick(52,21);
  assert.equal(labels.value('objectName'),'Drugie');
  assert.equal(labels.value('objectKeyword'),'Drugie');

  // Starsza odpowiedź zakończona później nie nadpisuje drugiego punktu ani nazw.
  const pending=[];
  const b=harness(url=>new Promise(resolve=>pending.push({url,resolve})));
  const first=b.api.pick(51,20),second=b.api.pick(52,21);
  assert.equal(b.value('objectLatitude'),'52.000000');
  pending[1].resolve(success(52,21,'Drugie','0000002'));await second;
  pending[0].resolve(success(51,20,'Pierwsze','0000001'));await first;
  assert.equal(b.value('selectedCitySym'),'0000002');
  assert.equal(b.value('objectName'),'Drugie');
  assert.deepEqual(b.markers.at(-1).point,[52,21]);

  // Czyszczenie unieważnia trwający odczyt i nie usuwa ręcznie wpisanej nazwy.
  b.document.getElementById('objectName').value='Mój ręczny opis';
  b.document.getElementById('objectKeyword').value='własne słowo';
  const third=b.api.pick(53,22);
  assert.equal(b.value('objectName'),'Mój ręczny opis');
  assert.equal(b.value('objectKeyword'),'własne słowo');
  b.api.clear();pending[2].resolve(success(53,22,'Trzecie','0000003'));await third;
  assert.equal(b.value('objectLatitude'),'');
  assert.equal(b.value('selectedCitySym'),'');
  assert(b.markers.every(m=>m.removed));
  console.log('Wybór mapy: kolejne kliknięcia, błąd geokodera, kolejność odpowiedzi, czyszczenie i ręczne nazwy - OK');
})().catch(error=>{console.error(error);process.exitCode=1;});
