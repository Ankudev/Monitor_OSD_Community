"use strict";
(() => {
  const panel = document.getElementById("diagnosticsPanel");
  if (!panel) return;
  const output = document.getElementById("diagnosticsOutput");
  const button = document.getElementById("diagnosticsReload");
  const labels = { pge: "PGE", tauron: "TAURON", energa: "ENERGA", enea: "ENEA", stoen: "Stoen" };
  const results = { running: "odczyt trwa", success: "zapisano poprawny odczyt", failed: "błąd", interrupted: "brak wyniku końcowego" };
  const stages = { fetch: "pobieranie", parse: "analiza odpowiedzi", match: "dopasowanie", persist: "zapis" };
  const date = value => value ? new Date(value).toLocaleString("pl-PL") : "brak";
  function line(parent, text) {
    const element = document.createElement("p");
    element.textContent = text;
    parent.append(element);
  }
  async function json(path) {
    const response = await fetch(path);
    if (!response.ok) throw new Error("Diagnostyka jest chwilowo niedostępna.");
    return response.json();
  }
  async function load() {
    if (button.disabled) return;
    button.disabled = true;
    output.replaceChildren();
    try {
      const [scans, quality] = await Promise.all([
        json("/api/v1/diagnostics/scans?limit=10"),
        json("/api/v1/diagnostics/quality?limit=10"),
      ]);
      line(output, "Historia ostatnich 10 prób odczytu. Otwarcie tego panelu nie wysyła zapytań do operatorów.");
      if (scans.diagnostics_degraded) line(output, "Uwaga: rejestr może być nieaktualny. Zapis lub odczyt diagnostyki wymaga sprawdzenia.");
      if (!scans.items.length) line(output, "Rejestr jest pusty. Pierwsze wpisy pojawią się podczas automatycznego skanu.");
      for (const run of scans.items) {
        const details = document.createElement("details");
        const summary = document.createElement("summary");
        summary.textContent = `${labels[run.source] || run.source}: ${results[run.status] || "nieznany wynik"}; początek: ${date(run.started_at)}`;
        details.append(summary);
        line(details, `Etap: ${stages[run.stage] || "brak"}. Koniec: ${date(run.finished_at)}.`);
        line(details, `Żądania: ${run.request_count_complete === false ? "co najmniej " : ""}${run.request_count}; ponowienia: ${run.retry_count}; rekordy: ${run.source_event_count ?? "nieustalone"}; przypisania: ${run.matched_event_count ?? "nieustalone"}.`);
        line(details, `Potwierdzone objęcie punktu: ${run.confirmed_count ?? "nieustalone"}; dopasowania do sprawdzenia: ${run.candidate_count ?? "nieustalone"}. Odczyt: ${run.completeness === "complete" ? "zgodny z kontraktem adaptera" : run.completeness === "incomplete" ? "niepełny" : "kompletność nieustalona"}. Nie jest to potwierdzenie pełnego pokrycia obiektów.`);
        for (const response of run.responses || []) {
          line(details, `Odpowiedź HTTP ${response.http_status}: ${response.content_type || "typ treści niepodany"}; odczytano ${response.body_bytes ?? "nieustaloną liczbę"} bajtów po dekompresji. ${response.body_complete ? "Treść odpowiedzi odczytana w całości." : "Brak potwierdzenia odczytu całej treści odpowiedzi."}`);
          if (response.body_sha256) line(details, `SHA-256 odczytanej treści odpowiedzi: ${response.body_sha256}.`);
        }
        if (run.message) line(details, run.message);
        if (run.warnings?.includes("record_count_drop")) line(details, "Odczytano znacznie mniej rekordów niż poprzednio. To ostrzeżenie, a nie dowód awarii źródła.");
        if (run.warnings?.includes("unknown_event_types")) line(details, `Nierozpoznane typy komunikatów: ${run.unknown_type_count}. Sprawdź format odpowiedzi operatora.`);
        if (run.warnings?.includes("energa_mass_active_missing")) line(details, "ENERGA nie podała pola aktywności awarii masowej. Zachowano dotychczasową interpretację; kontrakt wymaga potwierdzenia.");
        if (run.warnings?.includes("enea_time_conflict")) line(details, "ENEA podała sprzeczne terminy końca. Termin pozostaje do sprawdzenia.");
        output.append(details);
      }
      line(output, `Jakość katalogu: ${OsdEventView.count(quality.object_count, ["obiekt", "obiekty", "obiektów"])}, ${OsdEventView.count(quality.issue_count, ["uwaga", "uwagi", "uwag"])}. Brak dowodu nie oznacza błędnej lokalizacji.`);
      for (const issue of quality.items) line(output, `${issue.object_id}: ${OsdEventView.polishText(issue.message)} ${issue.next_step}`);
    } catch (error) {
      line(output, error.message);
    } finally {
      button.disabled = false;
    }
  }
  panel.addEventListener("toggle", () => { if (panel.open) load(); });
  button.addEventListener("click", load);
})();
