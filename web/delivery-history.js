"use strict";
(() => {
  const labels = {pending: "Oczekuje", sending: "Wysyłanie", delivered: "Przyjęte przez odbiorcę", failed: "Niepowodzenie", skipped: "Pominięte"};
  const actions = {new: "Nowy komunikat", updated: "Aktualizacja", resolved: "Zakończenie", cancelled: "Odwołanie", not_published: "Brak komunikatu w źródle", out_of_scope: "Komunikat poza zakresem obiektu"};
  function date(value) { return value == null ? "Brak danych" : new Date(value * 1000).toLocaleString("pl-PL", {timeZone: "Europe/Warsaw"}); }
  if (typeof module !== "undefined") module.exports = {labels, actions, date};
  if (typeof document === "undefined") return;
  const panel = document.getElementById("deliveryHistory");
  const output = document.getElementById("deliveryHistoryOutput");
  const notice = document.getElementById("deliveryHistoryStatus");
  const previous = document.getElementById("deliveryHistoryPrevious");
  const next = document.getElementById("deliveryHistoryNext");
  const reload = document.getElementById("deliveryHistoryReload");
  let offset = 0, authenticated = false, epoch = 0, pending = null;
  function session(value) {
    if (authenticated === value) return;
    authenticated = value; epoch++;
    panel.hidden = !value;
    if (!value) { panel.open = false; offset = 0; output.replaceChildren(); notice.textContent = ""; }
  }
  function line(parent, text) { const node = document.createElement("p"); node.textContent = text; parent.append(node); }
  async function load() {
    if (!authenticated || pending === epoch) return;
    pending = epoch; reload.disabled = true;
    const captured = epoch;
    notice.textContent = "Pobieranie historii wysyłki powiadomień…";
    try {
      const response = await fetch(`/api/admin/notifications?limit=20&offset=${offset}`, {cache: "no-store", signal: AbortSignal.timeout(10000)});
      if (captured !== epoch || !authenticated) return;
      if (response.status === 401) { session(false); return; }
      if (!response.ok) throw new Error("Odczyt niedostępny");
      const page = await response.json();
      if (captured !== epoch || !authenticated) return;
      output.replaceChildren();
      notice.textContent = `Historia wysyłki powiadomień: ${OsdEventView.count(page.total, ["zadanie", "zadania", "zadań"])}. Strona ${Math.floor(offset / 20) + 1}.`;
      if (!page.items.length) line(output, "Brak zapisanych zadań wysyłki.");
      for (const row of page.items) {
        const details = document.createElement("details"), summary = document.createElement("summary");
        summary.textContent = `${labels[row.status] || "Nieznany status"}. ${row.source.toUpperCase()}, obiekt ${row.object_id}`;
        details.append(summary);
        line(details, `Zdarzenie: ${actions[row.action] || "Inna zmiana"}. Utworzono: ${date(row.created_at)}.`);
        line(details, `Próby: ${row.attempts} z ${row.max_attempts}. Ostatnia próba: ${date(row.last_attempt_at)}.`);
        line(details, `Przyjęcie przez odbiorcę: ${date(row.delivered_at)}. Zakończenie: ${date(row.finished_at)}.`);
        if (row.status === "pending") line(details, `Następna próba: ${date(row.next_attempt_at)}.`);
        line(details, `Kod HTTP: ${row.last_http_status ?? "Brak"}. Kod błędu: ${row.last_error_code || "Brak"}.`);
        line(details, `Identyfikator dostawy: ${row.job_id}. Identyfikator zmiany: ${row.change_id}.`);
        if (row.last_decision_reason) line(details, `Decyzja administratora: ${row.last_decision_reason}. Czas: ${date(row.last_decision_at)}.`);
        output.append(details);
      }
      previous.disabled = offset === 0;
      next.disabled = offset + page.items.length >= page.total;
    } catch {
      if (captured === epoch && authenticated) notice.textContent = "Nie udało się pobrać historii wysyłki powiadomień. Spróbuj ponownie.";
    } finally { if (pending === captured) { pending = null; reload.disabled = false; } }
  }
  window.addEventListener("osd-admin-session", event => session(Boolean(event.detail)));
  panel.addEventListener("toggle", () => { if (panel.open) load(); });
  reload.addEventListener("click", load);
  previous.addEventListener("click", () => { if (pending !== epoch) { offset = Math.max(0, offset - 20); load(); } });
  next.addEventListener("click", () => { if (pending !== epoch) { offset += 20; load(); } });
  session(typeof adminCsrf !== "undefined" && Boolean(adminCsrf));
})();
