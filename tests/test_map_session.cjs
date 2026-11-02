const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync('web/script.js', 'utf8');
const projection = source.slice(source.indexOf('function applyMapPayload('), source.indexOf('\nasync function fetchMapPayload('));
const session = source.slice(source.indexOf('let adminCsrf ='), source.indexOf("\ndocument.getElementById('objectForm').classList.add", source.indexOf('let adminCsrf =')));
const publicMap = {locations:[{object_id:'public',name:'Publiczny',latitude:52,longitude:21}],events:[],sources:[]};
const privateCatalog = [{id:'public',name:'Publiczny',latitude:52.1,longitude:21.1,keyword:'PRIVATE'},
  {id:'secret',name:'PRIVATE-NAME',latitude:53,longitude:22,power_supply:{notes:'PRIVATE'}}];
let lastRendered;
const context = vm.createContext({
  document:{body:{classList:{toggle(){}}},getElementById:()=>({classList:{toggle(){},add(){}},hidden:false,textContent:''})},
  window:{dispatchEvent(){}},CustomEvent:class{},resetObjectForm(){},renderObjects(){},
  renderEvents(){lastRendered = JSON.stringify(context.state());},
  describeDataStatus:()=>({}),publicMap,privateCatalog,
});
vm.runInContext(`let objects=[],events=[],geometries={},statusPayload=null,lastMapPayload=publicMap;
  ${projection}\n${session}
  function state(){return {objects,events,geometries,adminCsrf,adminCatalog,statusPayload};}
  function signIn(){adminCsrf='test';adminCatalog=privateCatalog;applyMapPayload(publicMap);}`,context);
const response = (data,ok=true)=>({ok,status:ok?200:401,json:async()=>data});
(async()=>{
  context.applyMapPayload({...publicMap,demo:true});
  assert.equal(context.state().statusPayload.demo,true,'Tryb symulacji musi dotrzeć do zegarów i stanu źródeł');
  context.applyMapPayload(publicMap);
  assert.equal(context.state().statusPayload.demo,false,'Zwykły odczyt nie może zachować poprzedniego trybu demo');
  assert.equal(context.state().objects.length,1);
  context.signIn();
  assert.equal(context.state().objects.length,2);
  assert.equal(context.state().objects[0].latitude,52.1);
  context.applyMapPayload(publicMap);
  assert.equal(context.state().objects.length,2,'Powtórne odświeżenie nie tworzy duplikatów');
  assert(!JSON.stringify(publicMap).includes('PRIVATE'),'Publiczna migawka nie może zostać zmodyfikowana');
  context.clearPrivateObjects();
  assert.equal(context.state().objects.length,1);
  assert.equal(context.state().objects[0].latitude,52);
  assert(!lastRendered.includes('PRIVATE'),'Wylogowanie przerysowuje mapę i usuwa prywatne pola');
  let finish;
  context.fetch = async url => url.endsWith('/session') ? response({authenticated:true,csrf_token:'late'}) :
    new Promise(resolve=>{finish=()=>resolve(response({objects:privateCatalog}));});
  const running=context.syncAdmin();
  await new Promise(resolve=>setImmediate(resolve));
  context.clearPrivateObjects();
  finish();await running;
  context.applyMapPayload(publicMap);
  assert(!JSON.stringify(context.state()).includes('PRIVATE'),'Spóźniony odczyt nie przywraca sesji po wylogowaniu');
  for (const failure of ['expired','denied','offline']) {
    context.signIn();
    context.fetch = async url => {
      if(failure==='offline') throw new Error('offline');
      return url.endsWith('/session') ? response({authenticated:failure!=='expired',csrf_token:'test'}) : response({},false);
    };
    await context.syncAdmin();
    assert(!JSON.stringify(context.state()).includes('PRIVATE'),failure);
    assert(!lastRendered.includes('PRIVATE'),failure);
  }
  context.signIn();vm.runInContext('lastMapPayload=null',context);context.clearPrivateObjects();
  assert.equal(context.state().objects.length,0,'Brak publicznej migawki musi wyczyścić mapę');
  console.log('Mapa: pełny katalog po logowaniu, wylogowanie, wygaśnięcie i wyścig odpowiedzi - OK');
})().catch(error=>{console.error(error);process.exitCode=1;});
