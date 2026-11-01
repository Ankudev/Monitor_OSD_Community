/* Chroniony edytor decyzji. Dokumenty i treści użytkownika wstawiamy jako tekst. */
(() => {
  let state, csrf;
  const labels = {name:"Nazwa", city_sym:"Kod miejscowości", street_sym:"Kod ulicy", house_number:"Numer obiektu", latitude:"Szerokość geograficzna", longitude:"Długość geograficzna"};
  const sourceLabels = {project:"Opracowanie własne",demo_locations:"Lokalizacje demonstracyjne",pge:"PGE Dystrybucja",tauron:"TAURON Dystrybucja",energa:"ENERGA-OPERATOR",enea:"ENEA Operator",stoen:"Stoen Operator"};
  const reviewLabels = {pending:"Wymaga wyjaśnienia",approved:"Zakres zatwierdzony",restricted:"Wykorzystanie ograniczone"};
  const modeLabels = {public:"Zakres publiczny",restricted:"Dostęp ograniczony",links_only:"Odnośniki do źródła"};
  const element = (tag, text) => { const result = document.createElement(tag); if (text !== undefined) result.textContent = text; return result; };
  const message = value => { document.getElementById("policyMessage").textContent = value; };
  const selected = () => state.objects.find(row => row.id === document.getElementById("publicationObject").value);
  function render() {
    const obj = selected();
    if (!obj) return;
    const decision = state.decisions[obj.id] || {visibility:"private",fields:{}};
    document.getElementById("publicationVisibility").value = decision.visibility;
    document.getElementById("publicationReason").value = decision.reason || "";
    const fields = document.getElementById("publicationFields"); fields.replaceChildren();
    for (const [key, label] of Object.entries(labels)) {
      if (!(key in obj)) continue;
      const approval = decision.fields[key] || {};
      const box = element("fieldset"); box.dataset.field = key; box.append(element("legend",label));
      const enabledLabel = element("label"); const enabled = element("input"); enabled.type="checkbox"; enabled.dataset.role="enabled"; enabled.checked=Boolean(decision.fields[key]);
      enabledLabel.append(enabled,document.createTextNode(` Publikuj wartość: ${obj[key] ?? "brak"}`)); box.append(enabledLabel);
      const sourceLabel = element("label","Źródło "); const source = element("select"); source.dataset.role="source";
      for (const [id, entry] of Object.entries(state.sources)) {
        if (entry.actions.publish) { const option=element("option",sourceLabels[id] || id); option.value=id; source.append(option); }
      }
      source.value=approval.source_id || "project"; sourceLabel.append(source); box.append(sourceLabel);
      for (const [role, title, value] of [["url","Adres dowodu źródła",approval.source_url || ""],["method","Metoda i dowód ustalenia",approval.method || ""]]) {
        const fieldLabel=element("label",title+" "); const input=element("input"); input.dataset.role=role; input.value=value; input.maxLength=role === "url" ? 2048 : 1500;
        input.type=role === "url" ? "url" : "text"; fieldLabel.append(input); box.append(fieldLabel);
      }
      fields.append(box);
    }
  }
  async function load() {
    const session=await (await fetch("/api/admin/session",{cache:"no-store"})).json(); csrf=session.csrf_token;
    if (!csrf) throw new Error("Zaloguj się w katalogu obiektów.");
    const response=await fetch("/api/admin/publication",{cache:"no-store"});
    if (!response.ok) throw new Error("Nie udało się wczytać decyzji.");
    state=await response.json();
    const select=document.getElementById("publicationObject"); const previous=select.value; select.replaceChildren();
    for (const obj of state.objects) {const option=element("option",obj.name || obj.id); option.value=obj.id; select.append(option);}
    if (state.objects.some(obj=>obj.id === previous)) select.value=previous;
    const sources=document.getElementById("publicationSources"); sources.replaceChildren();
    for (const [id, value] of Object.entries(state.sources)) sources.append(element("p",`${sourceLabels[id] || id}: ${reviewLabels[value.review_status] || "Stan nieznany"}; ${modeLabels[value.publication_mode] || "Tryb nieznany"}. ${value.basis}`));
    render(); message(`Wczytano rewizję ${state.revision}.`);
  }
  document.getElementById("publicationObject").addEventListener("change",render);
  document.getElementById("publicationReload").addEventListener("click",()=>load().catch(error=>message(error.message)));
  document.getElementById("publicationForm").addEventListener("submit",async event=>{
    event.preventDefault();
    try {
      const obj=selected(); const fields={};
      for (const box of document.querySelectorAll("[data-field]")) {
        const value=role=>box.querySelector(`[data-role="${role}"]`);
        if (!value("enabled").checked) continue;
        const original=state.decisions[obj.id]?.fields?.[box.dataset.field] || {};
        const same=original.source_id===value("source").value && original.source_url===value("url").value && original.method===value("method").value;
        fields[box.dataset.field]={...original,source_id:value("source").value,source_url:value("url").value,
          method:value("method").value,value_sha256:state.field_hashes[obj.id][box.dataset.field],
          acquired_at:same ? original.acquired_at ?? null : null,modified:same ? original.modified ?? true : true};
      }
      const decision={...state.decisions[obj.id],visibility:document.getElementById("publicationVisibility").value,fields,reason:document.getElementById("publicationReason").value};
      const response=await fetch(`/api/admin/publication/object/${encodeURIComponent(obj.id)}`,{method:"PUT",headers:{"Content-Type":"application/json","X-CSRF-Token":csrf},body:JSON.stringify({revision:state.revision,decision})});
      if (!response.ok) throw new Error("Nie zapisano decyzji. Sprawdź dowody, sesję i aktualną rewizję.");
      await load(); message("Decyzja została zapisana.");
    } catch(error) {message(error.message);}
  });
  load().catch(error=>message(error.message));
})();
