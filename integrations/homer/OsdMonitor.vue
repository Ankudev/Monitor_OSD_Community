<template>
  <div class="card osd-card">
    <div class="card-content osd-card-content">
      <div class="osd-dashboard" :aria-busy="!initialLoadComplete" @click.stop>
        <div class="osd-heading">
          <div>
            <p class="title is-4">{{ item.name }}</p>
            <p class="subtitle is-6">Panel operacyjny monitorowanych lokalizacji</p>
          </div>
          <div class="osd-actions">
            <button class="osd-refresh" type="button" :disabled="refreshPending" @click="refresh(true)">
              {{ refreshPending ? "Odświeżanie widoku…" : "Odśwież widok" }}
            </button>
            <a class="osd-map-link" :href="item.url || '/'">Mapa</a>
          </div>
        </div>

        <p v-if="!initialLoadComplete" class="osd-loading" role="status">Wczytywanie zapisanych danych…</p>
        <div class="osd-body" :class="{ 'osd-body-pending': !initialLoadComplete }">
        <p v-if="warningMessage" class="osd-alert" role="status">
          {{ hasData ? "Wyświetlane są ostatnie poprawne dane." : "Brak danych." }} {{ warningMessage }}
        </p>

        <p v-if="objectRows.some((object) => object.matching_pending_sources?.length)" class="osd-alert" role="status">
          Dopasowania zmienionych obiektów zostaną przeliczone podczas automatycznego skanu. Liczniki obejmują tylko aktualne dopasowania.
        </p>

        <div class="osd-stats" aria-label="Podsumowanie dopasowań">
          <div><strong>{{ valueOrDash(status?.configured_locations) }}</strong><span>{{ noun(status?.configured_locations, ["obiekt", "obiekty", "obiektów"]) }}</span></div>
          <div><strong>{{ valueOrDash(eventCount) }}</strong><span>{{ noun(eventCount, ["dopasowanie", "dopasowania", "dopasowań"]) }}</span></div>
          <div><strong>{{ valueOrDash(summary.confirmed) }}</strong><span>{{ noun(summary.confirmed, ["potwierdzone objęcie punktu", "potwierdzone objęcia punktu", "potwierdzonych objęć punktu"]) }}</span></div>
          <div><strong>{{ valueOrDash(summary.candidates) }}</strong><span>{{ noun(summary.candidates, ["dopasowanie do sprawdzenia", "dopasowania do sprawdzenia", "dopasowań do sprawdzenia"]) }}</span></div>
        </div>
        <details class="osd-stat-details">
          <summary>Więcej liczników</summary>
          <div class="osd-stats osd-stats-secondary" aria-label="Dodatkowe liczniki">
          <div><strong>{{ valueOrDash(summary.ongoing) }}</strong><span>trwające</span></div>
          <div><strong>{{ valueOrDash(summary.overdue) }}</strong><span>po terminie</span></div>
          <div><strong>{{ valueOrDash(attentionTotal) }}</strong><span>wymaga uwagi</span></div>
          <div><strong>{{ valueOrDash(plannedTotal) }}</strong><span>planowane</span></div>
          <div><strong>{{ valueOrDash(favoriteIds.length) }}</strong><span>ulubione</span></div>
          </div>
        </details>

        <p class="osd-explanation">
          Liczniki pokazują komunikaty operatorów. Zgodność miejscowości, bliskość punktu i niezweryfikowany promień nie potwierdzają objęcia punktu wyłączeniem. To nie jest pomiar napięcia. „Trwające” opisuje harmonogram komunikatu.
          Operatorzy przyłączeń obiektów nie zostali potwierdzeni w katalogu. Brak komunikatu nie potwierdza stanu zasilania.
        </p>

        <div class="osd-grid">
          <section class="osd-panel">
            <div class="osd-panel-heading">
              <h3>Wymaga uwagi</h3>
              <span>{{ valueOrDash(attentionTotal) }}</span>
            </div>
            <ol v-if="attentionRows.length" class="osd-list">
              <li v-for="row in attentionRows" :key="rowKey(row.event, row.reason)">
                <a :href="eventUrl(row.event, 'current')">
                  <strong>{{ row.object }}</strong>
                </a>
                <OsdEventDetails :event="row.event" :object="objectForEvent(row.event)" />
              </li>
            </ol>
            <p v-else class="osd-empty">Brak komunikatów wymagających uwagi w ostatnim odczycie.</p>
          </section>

          <section class="osd-panel osd-planned-panel">
            <div class="osd-panel-heading">
              <h3>Planowane wyłączenia</h3>
              <span>{{ count(plannedTotal, ["przedział czasu", "przedziały czasu", "przedziałów czasu"]) }} · {{ count(plannedObjectTotal, ["obiekt", "obiekty", "obiektów"]) }}</span>
            </div>
            <div class="osd-filters">
              <div class="osd-segmented" aria-label="Zakres planowanych wyłączeń">
                <button
                  v-for="option in horizonOptions"
                  :key="option.hours"
                  type="button"
                  :class="{ active: plannedHorizonHours === option.hours }"
                  @click="plannedHorizonHours = option.hours"
                >
                  {{ option.label }}
                </button>
              </div>
              <select v-model="plannedSource" aria-label="Operator planowanych wyłączeń">
                <option value="all">Wszyscy operatorzy</option>
                <option v-for="row in operatorRows" :key="row.source" :value="row.source">
                  {{ row.label || row.source.toUpperCase() }}
                </option>
              </select>
              <label class="osd-checkbox">
                <input v-model="plannedFavoritesOnly" type="checkbox">
                <span>Tylko ulubione</span>
              </label>
              <input v-model="plannedQuery" type="search" aria-label="Szukaj planowanych wyłączeń" placeholder="Szukaj obiektu, ulicy lub opisu">
            </div>
            <ol v-if="plannedRows.length" class="osd-list osd-planned-list">
              <li v-for="row in plannedRows" :key="rowKey(row.event, row.window.start_at)">
                <a :href="eventUrl(row.event, 'planned', row.window.start_at)">
                  <strong>{{ row.object }}</strong>
                </a>
                <OsdEventDetails :event="row.event" :object="objectForEvent(row.event)" :window="row.window" />
              </li>
            </ol>
            <p v-else class="osd-empty">Brak planowanych wyłączeń w wybranym okresie.</p>
            <div v-if="plannedInvalidRows.length" class="osd-invalid">
              <h4>Planowane wyłączenia - termin do sprawdzenia: {{ valueOrDash(plannedInvalidTotal) }}</h4>
              <ol class="osd-list">
                <li v-for="row in plannedInvalidRows" :key="rowKey(row.event, 'invalid')">
                  <a :href="eventUrl(row.event, 'attention')">
                    <strong>{{ row.object }}</strong>
                  </a>
                  <OsdEventDetails :event="row.event" :object="objectForEvent(row.event)" />
                </li>
              </ol>
            </div>
          </section>
        </div>

        <section class="osd-panel osd-favorites">
          <div class="osd-panel-heading">
            <h3>Ulubione obiekty</h3>
            <span>w tej przeglądarce</span>
          </div>
          <div class="osd-favorite-form">
            <select v-model="favoriteCandidate" aria-label="Obiekt do dodania do ulubionych" :disabled="!objectRows.length || favoriteIds.length >= 20">
              <option value="">Dodaj obiekt</option>
              <option v-for="object in objectRows" :key="object.id" :value="String(object.id)">
                {{ object.name || object.city }} · TERYT {{ object.city_sym }}
              </option>
            </select>
            <button class="osd-refresh" type="button" :disabled="!favoriteCandidate" @click="addFavorite">
              Dodaj
            </button>
          </div>
          <div v-if="favoriteRows.length" class="osd-favorite-list">
            <div v-for="row in favoriteRows" :key="row.id" class="osd-favorite-row">
              <a :href="objectUrl(row.id)">
                <strong>{{ row.name }}</strong>
                <span>{{ row.description }}</span>
              </a>
              <button type="button" @click="removeFavorite(row.id)">Usuń</button>
            </div>
          </div>
          <p v-else class="osd-empty">Ulubione obiekty są zapisywane tylko w tej przeglądarce.</p>
        </section>

        <div v-if="operatorRows.length" class="osd-sources">
          <div v-for="row in operatorRows" :key="row.source" class="osd-source">
            <span class="osd-dot" :class="sourceState(row)" aria-hidden="true"></span>
            <div>
              <strong>{{ row.label || row.source.toUpperCase() }}</strong>
              <small>Ostatni poprawny odczyt: {{ formatDate(row.last_success_at) }}</small>
              <small v-if="publicationText(row)">{{ publicationText(row) }}</small>
            </div>
            <div class="osd-source-count">
              <strong>{{ valueOrDash(row.matched_event_count) }}</strong>
              <small>{{ sourceLabel(row) }}</small>
            </div>
            <details v-if="row.error" class="osd-source-message">
              <summary>Komunikat operatora</summary>
              <p>{{ row.error }}</p>
            </details>
          </div>
        </div>

        <p class="osd-footer">
          API: status {{ endpointText('status') }}, zdarzenia {{ endpointText('events') }}, obiekty {{ endpointText('objects') }}.
          Widok odświeżono: {{ refreshedAt ? formatDate(refreshedAt) : "oczekiwanie" }}
        </p>
        </div>
      </div>
    </div>
  </div>
</template>

<script>
import "./match-explanation.js";
import OsdEventDetails from "./OsdEventDetails.vue";
import service from "@/mixins/service.js";
import {
  attentionEvents,
  eventUrl,
  formatDate,
  publicationText,
  loadFavorites,
  PLANNED_HORIZONS,
  plannedWindows,
  saveFavorites,
  sourceRows,
  summarizeEvents,
} from "./osd-summary.js";

export default {
  name: "OsdMonitor",
  components: {OsdEventDetails},
  mixins: [service],
  props: { item: Object },
  data: () => ({
    status: null,
    eventsPayload: null,
    objectsPayload: null,
    endpointState: {
      status: { lastSuccess: null, error: "" },
      events: { lastSuccess: null, error: "" },
      objects: { lastSuccess: null, error: "" },
    },
    errorMessage: "",
    refreshedAt: null,
    favoriteIds: [],
    favoriteCandidate: "",
    horizonOptions: PLANNED_HORIZONS,
    plannedHorizonHours: 168,
    plannedSource: "all",
    plannedFavoritesOnly: false,
    plannedQuery: "",
    clockNow: new Date(),
    clockTimer: null,
    refreshPending: false,
    initialLoadComplete: false,
    abortController: null,
  }),
  computed: {
    hasData() {
      return Boolean(this.status || this.eventsPayload || this.objectsPayload);
    },
    eventList() {
      return Array.isArray(this.eventsPayload?.events) ? this.eventsPayload.events : [];
    },
    objectRows() {
      return (Array.isArray(this.objectsPayload?.objects) ? this.objectsPayload.objects : [])
        .map((object) => ({ ...object, id: String(object.id) }))
        .sort((a, b) => String(a.name || a.city || a.id).localeCompare(String(b.name || b.city || b.id), "pl"));
    },
    eventCount() {
      if (Number.isFinite(this.eventsPayload?.count)) return this.eventsPayload.count;
      return this.eventsPayload ? this.eventList.length : null;
    },
    summary() {
      return summarizeEvents(this.eventList);
    },
    attentionRows() {
      return attentionEvents(this.eventList, this.objectRows, 10);
    },
    attentionTotal() {
      return attentionEvents(this.eventList, this.objectRows, Number.MAX_SAFE_INTEGER).length;
    },
    plannedResult() {
      return plannedWindows(this.eventList, this.objectRows, this.clockNow, {
        hours: this.plannedHorizonHours,
        limit: Number.MAX_SAFE_INTEGER,
        favorites: this.favoriteIds,
        favoritesOnly: this.plannedFavoritesOnly,
        source: this.plannedSource,
        query: this.plannedQuery,
        includeInvalid: true,
      });
    },
    plannedRows() {
      return this.plannedResult.rows;
    },
    plannedTotal() {
      return this.plannedResult.total;
    },
    plannedObjectTotal() {
      return this.plannedResult.uniqueObjects;
    },
    plannedInvalidTotal() {
      return this.plannedResult.invalidTotal;
    },
    plannedInvalidRows() {
      return this.plannedResult.invalidRows;
    },
    favoriteRows() {
      const byId = new Map(this.objectRows.map((object) => [String(object.id), object]));
      return this.favoriteIds.map((id) => {
        const object = byId.get(String(id));
        if (!object) {
          return { id: String(id), name: `Obiekt ${id}`, description: "Obiektu nie ma w ostatnim poprawnym odczycie" };
        }
        const street = object.street ? ` · ${object.street}` : "";
        return {
          id: String(id),
          name: object.name || object.city || `Obiekt ${id}`,
          description: `${object.city || "brak miejscowości"}${street} · TERYT ${object.city_sym || "-"}`,
        };
      });
    },
    operatorRows() {
      return sourceRows(this.status);
    },
    warningMessage() {
      const sources = this.operatorRows
        .filter((row) => row.error || row.stale || !row.last_success_at)
        .map((row) => {
          const reason = globalThis.OsdEventView.sourceErrorText(row) || "dane nieaktualne";
          return `${row.label || row.source.toUpperCase()}: ${reason} (ostatni poprawny odczyt: ${formatDate(row.last_success_at)})`;
        });
      const sourceWarning = sources.length ? `Źródła wymagają uwagi: ${sources.join("; ")}` : "";
      return [this.errorMessage, sourceWarning].filter(Boolean).join("; ");
    },
  },
  created() {
    this.favoriteIds = loadFavorites();
    this.autoUpdateMethod = this.refresh;
    this.refresh(true);
    window.addEventListener("storage", this.syncFavorites);
    document.addEventListener("visibilitychange", this.onVisibilityChange);
    this.clockTimer = window.setInterval(this.syncClock, 60000);
  },
  beforeUnmount() {
    this.abortController?.abort();
    window.removeEventListener("storage", this.syncFavorites);
    document.removeEventListener("visibilitychange", this.onVisibilityChange);
    if (this.clockTimer) window.clearInterval(this.clockTimer);
  },
  methods: {
    noun(value, forms) { return globalThis.OsdEventView.word(value, forms); },
    count(value, forms) { return globalThis.OsdEventView.count(value, forms); },
    objectForEvent(event) {
      return this.objectRows.find(object => String(object.id) === String(event.object_id));
    },
    formatDate,
    publicationText,
    eventUrl,
    valueOrDash(value) {
      return Number.isFinite(value) ? new Intl.NumberFormat("pl-PL").format(value) : "—";
    },
    syncClock() {
      if (!document.hidden) this.clockNow = new Date();
    },
    onVisibilityChange() {
      this.syncClock();
      if (!document.hidden) this.refresh();
    },
    rowKey(event, suffix) {
      return `${event?.object_id || "object"}:${event?.event_key || event?.source_event_id || "event"}:${suffix}`;
    },
    objectUrl(id) {
      const params = new URLSearchParams({ object: String(id), view: "current" });
      return `/?${params.toString()}`;
    },
    sourceState(row) {
      if (row.stale || row.error || !row.last_success_at) return "error";
      if (row.running) return "running";
      return "ok";
    },
    sourceLabel(row) {
      if (row.error_category === "maintenance" && row.error) return "prace serwisowe";
      if (row.error) return "błąd";
      if (row.stale || !row.last_success_at) return "nieaktualne";
      if (row.running) return "skanowanie";
      return this.noun(row.matched_event_count, ["dopasowanie", "dopasowania", "dopasowań"]);
    },
    endpointText(name) {
      const state = this.endpointState[name];
      if (state.error) return "błąd";
      if (state.lastSuccess) return "OK";
      return "oczekuje";
    },
    syncFavorites() {
      this.favoriteIds = loadFavorites();
    },
    persistFavorites(ids) {
      try {
        this.favoriteIds = saveFavorites(ids);
      } catch {
        this.favoriteIds = [...new Set((ids || []).map((id) => String(id)).filter(Boolean))].slice(0, 20);
      }
    },
    addFavorite() {
      if (!this.favoriteCandidate) return;
      this.persistFavorites([...this.favoriteIds, this.favoriteCandidate]);
      this.favoriteCandidate = "";
    },
    removeFavorite(id) {
      this.persistFavorites(this.favoriteIds.filter((value) => String(value) !== String(id)));
    },
    validateMapPayload(payload) {
      if (!payload || typeof payload !== "object") throw new Error("Mapa: niepoprawna odpowiedź JSON");
      if (!Array.isArray(payload.sources)) throw new Error("Mapa: brak listy źródeł");
      if (!Array.isArray(payload.events)) throw new Error("Mapa: brak tablicy zdarzeń");
      if (!Array.isArray(payload.locations)) throw new Error("Mapa: brak tablicy lokalizacji");
      return payload;
    },
    applyMapPayload(payload) {
      const sources = Object.fromEntries(payload.sources.map((row) => [row.source, {
        ...row,
        stale: row.status !== "fresh",
        consecutive_errors: Number(row.consecutive_errors || 0),
      }]));
      this.status = {
        ok: payload.data_status === "fresh",
        configured_locations: payload.counts?.locations ?? payload.locations.length,
        last_success_at: payload.data_updated_at,
        sources,
      };
      this.eventsPayload = {
        count: payload.events.length,
        events: payload.events.map((event) => ({
          ...event,
          event_key: event.event_id,
          status: event.source_status,
        })),
      };
      this.objectsPayload = {
        count: payload.locations.length,
        objects: payload.locations.map((object) => ({ ...object, id: object.object_id })),
      };
    },
    async refresh(force = false) {
      if (this.refreshPending || (document.hidden && !force)) return;
      this.refreshPending = true;
      this.abortController = new AbortController();
      const timeout = window.setTimeout(() => this.abortController?.abort(), 10000);
      const request = { cache: "no-cache", signal: this.abortController.signal };
      try {
        const payload = this.validateMapPayload(await this.fetch("/v1/map", request));
        this.applyMapPayload(payload);
        const success = { lastSuccess: new Date().toISOString(), error: "" };
        this.endpointState.status = success;
        this.endpointState.events = success;
        this.endpointState.objects = success;
        this.refreshedAt = new Date().toISOString();
        this.errorMessage = "";
      } catch (error) {
        if ([403, 409].includes(error?.status) || /\b(403|409)\b/.test(error?.message || "")) {
          this.status = null;
          this.eventsPayload = null;
          this.objectsPayload = null;
        }
        const text = error?.name === "AbortError" ? "przekroczono czas oczekiwania" : error?.message || "błąd odczytu";
        for (const name of ["status", "events", "objects"]) {
          this.endpointState[name] = { ...this.endpointState[name], error: text };
        }
        this.errorMessage = `Mapa: ${text}`;
      }
      window.clearTimeout(timeout);
      this.abortController = null;
      this.refreshPending = false;
      this.initialLoadComplete = true;
    },
  },
};
</script>

<style scoped lang="scss">
.osd-card-content { height: auto; min-height: 85px; padding: 1.25rem; }
.osd-dashboard { position: relative; width: 100%; min-height: calc(100svh - 8rem); color: var(--text); }
.osd-body-pending { visibility: hidden; }
.osd-loading { position: absolute; margin-top: 1rem; color: var(--text-subtitle); }
.osd-heading { display: flex; align-items: flex-start; justify-content: space-between; gap: 1rem; }
.osd-heading .title { margin-bottom: .2rem; }
.osd-heading .subtitle { margin-bottom: 0; }
.osd-heading > div { min-width: 0; }
.osd-heading .title { overflow-wrap: anywhere; }
.osd-actions { display: flex; flex-wrap: wrap; gap: .5rem; justify-content: flex-end; }
.osd-map-link, .osd-refresh { min-height: 2.25rem; padding: .5rem .8rem; border-radius: .45rem; font-weight: 700; white-space: nowrap; }
.osd-map-link { color: #fff; background: var(--highlight-primary); }
.osd-refresh { color: var(--text-title); background: var(--background); border: 1px solid rgba(105, 115, 134, .35); cursor: pointer; }
.osd-refresh { min-width: 164px; }
.osd-refresh:disabled { cursor: not-allowed; opacity: .55; }
.osd-alert { margin-top: 1rem; padding: .7rem .85rem; color: #7b271f; background: #fff0ed; border-left: 4px solid #c9362b; border-radius: .3rem; }
.osd-stats { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: .65rem; margin-top: 1rem; }
.osd-stats-secondary { grid-template-columns: repeat(5, minmax(0, 1fr)); margin-top: .4rem; }
.osd-stat-details { margin-top: .35rem; }
.osd-stat-details summary { min-height: 2.25rem; padding: .4rem .2rem; color: var(--text-subtitle); font-size: .8rem; font-weight: 600; cursor: pointer; }
.osd-stat-details summary:focus-visible { outline: 2px solid var(--highlight-primary); outline-offset: 2px; }
.osd-stats div { padding: .75rem .6rem; text-align: center; background: var(--background); border-radius: .45rem; }
.osd-stats strong, .osd-stats span { display: block; }
.osd-stats strong { color: var(--text-title); font-size: 1.4rem; }
.osd-stats span { margin-top: .15rem; font-size: .72rem; }
.osd-explanation, .osd-footer, .osd-empty { margin-top: .75rem; color: var(--text-subtitle); font-size: .76rem; }
.osd-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: .65rem; margin-top: 1rem; }
.osd-panel { min-width: 0; padding: .85rem; background: var(--background); border: 1px solid rgba(105, 115, 134, .18); border-radius: .5rem; }
.osd-panel-heading { display: flex; align-items: center; justify-content: space-between; gap: .75rem; margin-bottom: .55rem; }
.osd-panel-heading h3 { margin: 0; color: var(--text-title); font-size: .9rem; }
.osd-panel-heading span { color: var(--text-subtitle); font-size: .72rem; }
.osd-list { display: grid; gap: .35rem; margin: 0; padding-left: 1.1rem; }
.osd-list li::marker { color: var(--highlight-primary); font-weight: 800; }
.osd-list a, .osd-favorite-row a { display: grid; gap: .08rem; color: var(--text); text-decoration: none; }
.osd-list strong, .osd-favorite-row strong { overflow-wrap: anywhere; color: var(--text-title); font-size: .8rem; }
.osd-list span, .osd-favorite-row span { color: var(--text-subtitle); font-size: .7rem; }
.osd-list em { display: -webkit-box; overflow: hidden; color: var(--text); font-size: .7rem; font-style: normal; -webkit-box-orient: vertical; -webkit-line-clamp: 2; overflow-wrap: anywhere; }
.osd-planned-panel { grid-column: span 1; }
.osd-filters { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: .45rem; margin-bottom: .65rem; }
.osd-filters select, .osd-filters input[type="search"] { min-width: 0; min-height: 2rem; padding: .35rem .5rem; color: var(--text); background: var(--card-background); border: 1px solid rgba(105, 115, 134, .35); border-radius: .4rem; font-size: .72rem; }
.osd-segmented { display: flex; min-width: 0; overflow: hidden; border: 1px solid rgba(105, 115, 134, .35); border-radius: .4rem; }
.osd-segmented button { flex: 1; min-height: 2rem; padding: .25rem .35rem; color: var(--text-subtitle); background: var(--card-background); border: 0; border-right: 1px solid rgba(105, 115, 134, .22); font-size: .68rem; font-weight: 800; }
.osd-segmented button:last-child { border-right: 0; }
.osd-segmented button.active { color: #fff; background: var(--highlight-primary); }
.osd-checkbox { display: flex; align-items: center; gap: .35rem; min-height: 2rem; color: var(--text-subtitle); font-size: .72rem; font-weight: 700; }
.osd-checkbox input { margin: 0; }
.osd-planned-list { max-height: min(72vh, 58rem); overflow-y: auto; padding-right: .35rem; scrollbar-gutter: stable; }
.osd-planned-list li { padding-bottom: .25rem; }
.osd-source-link { display: inline-block; margin-top: .18rem; color: var(--highlight-primary); font-size: .7rem; font-weight: 700; text-decoration: none; }
.osd-source-link:hover { text-decoration: underline; }
.osd-description { margin-top: .2rem; }
.osd-description summary { color: var(--highlight-primary); font-size: .7rem; font-weight: 700; cursor: pointer; }
.osd-description p { margin: .25rem 0 0; color: var(--text); font-size: .72rem; overflow-wrap: anywhere; }
.osd-invalid { margin-top: .65rem; padding-top: .6rem; border-top: 1px solid rgba(105, 115, 134, .18); }
.osd-invalid h4 { margin: 0 0 .45rem; color: var(--text-title); font-size: .76rem; }
.osd-favorites { margin-top: .65rem; }
.osd-favorite-form { display: flex; gap: .45rem; }
.osd-favorite-form select { flex: 1; min-width: 0; min-height: 2.25rem; color: var(--text); background: var(--card-background); border: 1px solid rgba(105, 115, 134, .35); border-radius: .4rem; }
.osd-favorite-list { display: grid; gap: .35rem; margin-top: .65rem; }
.osd-favorite-row { display: grid; grid-template-columns: minmax(0, 1fr) auto; align-items: center; gap: .5rem; padding: .45rem .55rem; background: var(--card-background); border-radius: .4rem; }
.osd-favorite-row button { color: #9f2f27; background: transparent; border: 0; font-size: .7rem; font-weight: 700; cursor: pointer; }
.osd-sources { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: .5rem; margin-top: .85rem; }
.osd-source { display: grid; grid-template-columns: auto minmax(0, 1fr) auto; align-items: center; gap: .6rem; padding: .65rem .7rem; background: var(--background); border-radius: .45rem; }
.osd-source strong, .osd-source small { display: block; }
.osd-source small { color: var(--text-subtitle); font-size: .68rem; }
.osd-source-count { text-align: right; }
.osd-source-message { grid-column: 2 / -1; min-width: 0; overflow-wrap: anywhere; white-space: normal; text-align: left; }
.osd-source-message summary { cursor: pointer; }
.osd-dot { width: .65rem; height: .65rem; border-radius: 50%; background: #7b818a; }
.osd-dot.ok { background: #18794e; box-shadow: 0 0 0 3px rgba(24, 121, 78, .15); }
.osd-dot.running { background: #d46c1f; }
.osd-dot.error { background: #c9362b; box-shadow: 0 0 0 3px rgba(201, 54, 43, .15); }
@media (max-width: 760px) {
  .osd-heading { align-items: stretch; flex-direction: column; gap: .55rem; }
  .osd-actions, .osd-map-link { justify-content: stretch; text-align: center; }
  .osd-stats, .osd-grid, .osd-sources { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .osd-filters { grid-template-columns: 1fr; }
}
@media (max-width: 520px) {
  .osd-card-content { padding: .75rem; }
  .osd-heading .subtitle { font-size: .9rem; }
  .osd-actions { justify-content: flex-start; }
  .osd-stats { grid-template-columns: repeat(2, minmax(0, 1fr)); gap: .4rem; margin-top: .65rem; }
  .osd-stats div { padding: .5rem .4rem; }
  .osd-stats strong { font-size: 1.25rem; }
  .osd-stats span { overflow-wrap: anywhere; }
  .osd-stats-secondary { margin-top: .25rem; }
  .osd-explanation { margin-top: .35rem; }
  .osd-grid, .osd-sources { grid-template-columns: 1fr; }
  .osd-favorite-form { flex-direction: column; }
}
</style>
