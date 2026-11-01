// Odtwarza bieżącą funkcję mapy offline. To diagnostyka, nie odbiór poprawionej mapy.
const fs = require('node:fs');
const path = require('node:path');
require('../web/event-view.js');
require('../web/match-explanation.js');
const OsdEventView = globalThis.OsdEventView, OsdMatchingView = globalThis.OsdMatchingView;
const source = fs.readFileSync(path.join(__dirname, '..', 'web', 'script.js'), 'utf8');
const start = source.indexOf('function renderMap(list)');
const end = source.indexOf('\nfunction ', start + 1);
if (start < 0 || end < 0) throw new Error('Nie znaleziono funkcji renderMap');
let objects = [], objectMarkers, observed = [], geometries = {}, showOperatorPoints = true;
const overlayStatus = {};
const document = {getElementById() { return overlayStatus; }};
const map = {}, objectLayer = { clearLayers() {} }, outageLayer = { clearLayers() {} };
const L = {
  circleMarker(points, options) { const value={type:'marker',points,options}; observed.push(value); return {bindPopup(popup) {value.popup=popup;return this;}, addTo() {return this;}}; },
  polygon(points) { observed.push({type: 'polygon', points}); return {bindPopup() {return this;}, addTo() {return this;}}; },
  geoJSON(geometry) { observed.push({type: 'geoJSON', geometry}); return {bindPopup() {return this;}, addTo() {return this;}}; },
};
const googleMapsDirectionsUrl = () => '', escapeHtml = value => String(value || '');
const colorsStart = source.indexOf('const typeColors = ') + 'const typeColors = '.length;
const typeColors = new Function(`return (${source.slice(colorsStart,source.indexOf(';',colorsStart))})`)();
const eventLabel = () => '', addressText = () => '', sourceLabel = event => event.source || '', focusDeepLinkedObject = () => {};
eval(source.slice(start, end));
for (const name of ['validMapPoint', 'validMapGeometry', 'pointConfirmed', 'locationWarning', 'eventColor', 'powerSupplyLines', 'mapEventMessage', 'matchExplanationRows']) {
  const first = source.indexOf(`function ${name}(`);
  const last = source.indexOf('\nfunction ', first + 1);
  eval(source.slice(first, last));
}
const outer = [[20,50],[22,50],[22,52],[20,52],[20,50]];
const hole = [[20.5,50.5],[21.5,50.5],[21.5,51.5],[20.5,51.5],[20.5,50.5]];
const cases = [
  {name:'missing_object_gps', objects:[{id:'one',latitude:null,longitude:null}], events:[{object_id:'one',coordinates:[]}]},
  {name:'null_event_point', objects:[], events:[{coordinates:[{latitude:null,longitude:null}]}]},
  {name:'empty_event_point', objects:[], events:[{coordinates:[{latitude:'',longitude:''}]}]},
  {name:'incomplete_event_point', objects:[], events:[{coordinates:[{latitude:52,longitude:null}]}]},
  {name:'valid_zero', objects:[], events:[{coordinates:[{latitude:0,longitude:20}]}]},
  {name:'out_of_range', objects:[], events:[{coordinates:[{latitude:100,longitude:200}]}]},
  {name:'two_independent_points', objects:[], events:[{coordinates:[{latitude:52,longitude:20},{latitude:53,longitude:21}]}]},
  {name:'three_independent_points', objects:[], events:[{coordinates:[{latitude:52,longitude:20},{latitude:53,longitude:21},{latitude:54,longitude:22}]}]},
  {name:'boolean_point', objects:[], events:[{coordinates:[{latitude:true,longitude:20}]}]},
  {name:'polygon_hole_once', objects:[], geometries:{shape:{type:'Polygon',coordinates:[outer,hole]}},events:[{geometry_ref:'shape'},{geometry_ref:'shape'}]},
  {name:'multipolygon', objects:[], geometries:{shape:{type:'MultiPolygon',coordinates:[[outer,hole],[outer]]}},events:[{geometry_ref:'shape'}]},
  {name:'object_scope_default', operatorPoints:false, objects:[{id:'one',name:'Lokalizacja testowa',latitude:51.1,longitude:20.1}],events:[{object_id:'one',source:'tauron',type:'outage',source_event_id:'first',coordinates:[{latitude:50.8444,longitude:17.8681},{latitude:50.9741,longitude:17.9777}]}]},
  {name:'many_messages_one_object', operatorPoints:false, objects:[{id:'one',latitude:52,longitude:20}],events:[{object_id:'one',source_event_id:'one'},{object_id:'one',source_event_id:'two'}]},
  {name:'shared_operator_points', objects:[],events:[{object_id:'one',source:'tauron',source_event_id:'shared',configured_name:'Obiekt A',coordinates:[{latitude:52,longitude:20}]},{object_id:'two',source:'tauron',source_event_id:'shared',configured_name:'Obiekt B',coordinates:[{latitude:52,longitude:20},{latitude:53,longitude:21}]}]},
  {name:'unknown_object_no_marker', operatorPoints:false,objects:[{id:'one',latitude:null,longitude:null}],events:[{object_id:'one',coordinates:[{latitude:52,longitude:20}]}]},
  {name:'confirmed_object', operatorPoints:false,objects:[{id:'one',latitude:52,longitude:20}],events:[{object_id:'one',match_evidence:{version:2,scope:'confirmed_point',point_confirmed:true}}]},
  {name:'pending_confirmation', operatorPoints:false,objects:[{id:'one',latitude:52,longitude:20}],events:[{object_id:'one',verification_pending:true,match_evidence:{version:2,scope:'confirmed_point',point_confirmed:true}}]},
  {name:'legacy_confirmation', operatorPoints:false,objects:[{id:'one',latitude:52,longitude:20}],events:[{object_id:'one',match_evidence:{version:1,scope:'confirmed_point',point_confirmed:true}}]},
];
const results = cases.map(test => {
  objects = test.objects; geometries = test.geometries || {}; showOperatorPoints = test.operatorPoints !== false; observed = []; renderMap(test.events);
  return {case:test.name, input:test, drawn:observed, overlayStatus:{...overlayStatus}};
});
if (require.main === module) console.log(JSON.stringify(results, null, 2));
module.exports = {results, validMapPoint, locationWarning};
