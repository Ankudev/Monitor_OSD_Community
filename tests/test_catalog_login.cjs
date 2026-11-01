const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync('web/script.js','utf8');
const start=source.indexOf("document.getElementById('adminAccess').addEventListener");
const listeners=source.slice(start,source.indexOf("window.addEventListener('pagehide'",start));
const elements=new Map();
let focused='';
function element(id){
  if(!elements.has(id)) elements.set(id,{value:'',textContent:'',className:'',disabled:false,open:false,handlers:{},
    classList:{remove(){}},addEventListener(name,callback){this.handlers[name]=callback;},
    reset(){element('adminPassword').value='';},focus(){focused=id;},showModal(){this.open=true;},
    close(){this.open=false;this.handlers.close?.();}});
  return elements.get(id);
}
let succeed=false, confirmed=true, complete;
const context=vm.createContext({document:{getElementById:element},
  fetch:async()=>new Promise(resolve=>{complete=()=>resolve({ok:succeed});}),
  readJsonResponse:async response=>{if(!response.ok)throw new Error('Nieprawidłowe hasło.');},
  clearPrivateObjects(){},resetObjectForm(){},adminFetch:async()=>({ok:true}),
});
vm.runInContext(`let adminCsrf='';async function refreshData(){adminCsrf=confirmed?'test':'';}
  ${listeners}`,context);
context.confirmed=confirmed;
const submit=()=>element('adminLogin').handlers.submit({preventDefault(){}});
(async()=>{
  element('adminAccess').handlers.click();
  assert(element('adminDialog').open);
  assert.equal(focused,'adminPassword');
  element('adminPassword').value='Hasło tylko w teście';
  element('adminDialogClose').handlers.click();
  assert(!element('adminDialog').open);
  assert.equal(element('adminPassword').value,'','Zamknięcie usuwa hasło');
  element('adminAccess').handlers.click();
  element('adminPassword').value='Błędne hasło testowe';
  let pending=submit();
  assert(element('adminLoginSubmit').disabled);
  complete();await pending;
  assert(element('adminDialog').open,'Błąd nie zamyka formularza');
  assert.equal(element('adminPassword').value,'');
  assert.equal(element('adminMessage').textContent,'Nieprawidłowe hasło.');
  assert(!element('adminLoginSubmit').disabled);
  succeed=true;context.confirmed=false;
  pending=submit();complete();await pending;
  assert(element('adminDialog').open,'Niepotwierdzona sesja nie może zamknąć logowania jako udanego');
  context.confirmed=true;
  pending=submit();complete();await pending;
  assert(!element('adminDialog').open);
  assert.equal(focused,'adminLogout','Udane logowanie przenosi fokus na widoczny przycisk konta');
  console.log('Logowanie: okno, fokus, czyszczenie hasła, błąd, zajęty formularz i potwierdzenie sesji - OK');
})().catch(error=>{console.error(error);process.exitCode=1;});
