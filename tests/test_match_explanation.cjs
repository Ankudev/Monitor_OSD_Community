const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
require('../web/match-explanation.js');
const rows = globalThis.OsdMatchingView.rows;
assert.match(rows({})[0], /Starszy wpis/);
assert.match(rows({match_explanation:{version:99}})[0], /Starszy wpis/);
const event = {match_explanation:{version:1, confidence:'low', reason:'<img onerror="bad">',
  details:{proximity:{distance_m:429.9,limit_m:1000,limit_source:'application_config'},
    address:{rows:[{object_house_number:'12A',result:'matches',numbers:{original:'12, 12A'}}]},
    geometry:{result:'hole_boundary',polygon_index:0}}, limitations:['proximity_only']}};
const lines = rows(event);
assert(lines.includes('<img onerror="bad">'));
assert(lines.some(x=>x.includes('430 m') && x.includes('1000 m')));
assert(lines.some(x=>x.includes('12A') && x.includes('zgodność')));
assert(lines.some(x=>x.includes('granicy wewnętrznego wyłączenia')));
assert(lines.some(x=>x.includes('niska')));
const source=fs.readFileSync('web/script.js','utf8');
const start=source.indexOf('function matchExplanationRows('),end=source.indexOf('function locationWarning(',start);
function element(tag,cls,text) {return {tag,text,children:[],append(child){this.children.push(child);}};}
const context=vm.createContext({OsdMatchingView:globalThis.OsdMatchingView,createElement:element});
vm.runInContext(source.slice(start,end),context);
const output=context.matchExplanationElement(event);
assert.equal(output.tag,'details');
assert.equal(output.children[0].tag,'summary');
assert.equal(output.children[1].text,'<img onerror="bad">'); // Tekst, a nie innerHTML.
assert(source.includes('explanation.map(line => `<p>${escapeHtml(line)}</p>`'));
const vue=fs.readFileSync('integrations/homer/OsdEventDetails.vue','utf8');
assert(vue.includes('{{ line }}')); // Vue escapuje interpolowany tekst.
assert(vue.includes('import "./match-explanation.js"'));
console.log('R8: wspólny formatter, fallback, metry, adresy, geometria i bezpieczny tekst - OK');
