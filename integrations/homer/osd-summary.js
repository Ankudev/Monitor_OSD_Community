export const SOURCE_ORDER = ["pge", "tauron", "energa", "enea", "stoen"];
export function publicationText(row) {
  return globalThis.OsdEventView?.publicationText(row) || "";
}
// Ten sam mechanizm formatowania jest ładowany w OsdMonitor.vue i w głównym panelu.
export function matchExplanationRows(event) {
  return globalThis.OsdMatchingView?.rows(event) || ["Starszy wpis nie zawiera szczegółowego wyjaśnienia."];
}
export const FAVORITES_KEY = "snut.favorites.v1"; // gitleaks:allow -- publiczny identyfikator localStorage, bez danych uwierzytelniających
export const PLANNED_HORIZONS = [
  { hours: 48, label: "48 h" },
  { hours: 168, label: "7 dni" },
  { hours: 336, label: "14 dni" },
];

export const TYPE_LABELS = {
  planned_outage: "Planowane wyłączenie",
  outage: "Awaria",
  mass_outage: "Awaria masowa",
  unknown: "Nieokreślony typ",
};

export const TIME_LABELS = {
  upcoming: "Nadchodzące",
  ongoing: "Trwa według harmonogramu",
  overdue: "Po przewidywanym terminie",
  closed: "Zamknięte według operatora",
  unknown: "Termin nieznany",
};

export function isClosed(event) {
  return Boolean(
    event?.revoked ||
      event?.status === "cancelled" ||
      event?.status === "resolved" ||
      event?.time_phase === "closed",
  );
}

export function isInactive(event) {
  return Boolean(isClosed(event) || event?.verification_pending);
}

export function pointConfirmed(event) {
  return event?.match_evidence?.version === 2 && event?.match_evidence?.point_confirmed === true
    && event?.match_evidence?.scope === "confirmed_point" && !event?.verification_pending
    && !event?.location_review_required;
}

export function summarizeEvents(events) {
  const summary = {
    total: 0,
    ongoing: 0,
    overdue: 0,
    upcoming: 0,
    unknown: 0,
    inactive: 0,
    confirmed: 0,
    candidates: 0,
  };

  for (const event of Array.isArray(events) ? events : []) {
    summary.total += 1;
    if (pointConfirmed(event)) {
      summary.confirmed += 1;
    } else {
      summary.candidates += 1;
    }
    if (isInactive(event)) {
      summary.inactive += 1;
    } else if (event?.time_phase === "ongoing") {
      summary.ongoing += 1;
    } else if (event?.time_phase === "overdue") {
      summary.overdue += 1;
    } else if (event?.time_phase === "upcoming") {
      summary.upcoming += 1;
    } else {
      summary.unknown += 1;
    }
  }
  return summary;
}

export function parseTime(value) {
  if (!value) return null;
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? null : date;
}

export function validWindows(event) {
  if (!Array.isArray(event?.time_windows)) return [];
  return event.time_windows
    .filter((window) => window?.valid)
    .map((window) => ({
      start: parseTime(window.start_at),
      stop: parseTime(window.stop_at),
      start_at: window.start_at || null,
      stop_at: window.stop_at || null,
    }))
    .filter((window) => window.start || window.stop);
}

export function eventTypeLabel(event) {
  return TYPE_LABELS[event?.type] || TYPE_LABELS.unknown;
}

export function eventTimeLabel(event) {
  return TIME_LABELS[event?.time_phase] || TIME_LABELS.unknown;
}

export function objectName(event, objects = []) {
  const found = objects.find((object) => String(object.id) === String(event?.object_id));
  return event?.configured_name || found?.name || event?.city || event?.city_sym || event?.object_id || "Nieznany obiekt";
}

export function eventUrl(event, view = "current", startAt = null) {
  const params = new URLSearchParams();
  if (event?.object_id !== undefined && event?.object_id !== null && event?.object_id !== "") {
    params.set("object", String(event.object_id));
  }
  params.set("view", view);
  const date = parseTime(startAt);
  if (date) {
    params.set("date", new Intl.DateTimeFormat("en-CA", { timeZone: "Europe/Warsaw" }).format(date));
  }
  return `/${params.toString() ? `?${params.toString()}` : ""}`;
}

export function attentionReason(event) {
  if (isClosed(event)) return "";
  if (event?.verification_pending) return "Weryfikacja";
  if (event?.time_phase === "ongoing") return "Trwa";
  if (event?.time_phase === "overdue") return "Po przewidywanym terminie";
  if (!validWindows(event).length || event?.time_phase === "unknown") return "Nieznany czas";
  return "";
}

export function attentionEvents(events, objects = [], limit = 10) {
  const priority = { Trwa: 0, Weryfikacja: 1, "Po przewidywanym terminie": 2, "Nieznany czas": 3 };
  return (Array.isArray(events) ? events : [])
    .map((event) => ({
      event,
      reason: attentionReason(event),
      object: objectName(event, objects),
      description: eventDescription(event),
    }))
    .filter((row) => row.reason)
    .sort((a, b) => (
      priority[a.reason] - priority[b.reason] ||
      a.object.localeCompare(b.object, "pl") ||
      String(a.event.source_event_id || "").localeCompare(String(b.event.source_event_id || ""), "pl")
    ))
    .slice(0, limit);
}

export function eventDescription(event) {
  return String(event?.description || event?.source_event?.raw?.description || "").trim();
}

export function sourceUrl(event) {
  try {
    const url = new URL(event?.source_url || "");
    return ["http:", "https:"].includes(url.protocol) ? url.href : "";
  } catch {
    return "";
  }
}

export function formatDuration(start, stop) {
  if (!start || !stop || stop <= start) return "";
  const minutes = Math.round((stop - start) / 60000);
  if (minutes < 60) return `${minutes} min`;
  const hours = Math.floor(minutes / 60);
  const rest = minutes % 60;
  return rest ? `${hours} godz. ${rest} min` : `${hours} godz.`;
}

export function objectSearchText(object = {}) {
  return [
    object.name,
    object.city,
    object.city_sym,
    object.street,
    object.street_sym,
    object.keyword,
  ].filter(Boolean).join(" ");
}

export function eventSearchText(event, objects = []) {
  const found = objects.find((object) => String(object.id) === String(event?.object_id));
  const matched = (event?.matched_locations || [])
    .map((location) => [
      location.text,
      location.streetName,
      location.houseNumbers,
      location.teryt?.cityName,
      location.teryt?.streetName,
    ].filter(Boolean).join(" "))
    .join(" ");
  return [
    objectName(event, objects),
    objectSearchText(found),
    event?.city,
    event?.city_sym,
    event?.street_sym,
    event?.operator,
    event?.source,
    eventDescription(event),
    matched,
  ].filter(Boolean).join(" ").toLowerCase();
}

export function plannedWindowBucket(event, window, now = new Date(), hours = 168) {
  if (event?.type !== "planned_outage") return "skip";
  if (isClosed(event)) return "skip";
  if (event?.verification_pending) return "attention";
  if (!window?.start) return "invalid";
  if (window.stop && window.stop <= now) return "attention";
  if (window.start <= now) return window.stop ? "ongoing" : "attention";
  const end = new Date(now.getTime() + hours * 60 * 60 * 1000);
  return window.start <= end ? "future" : "outside";
}

export function plannedWindows(events, objects = [], now = new Date(), options = {}) {
  const {
    hours = 168,
    limit = 10,
    favorites = [],
    favoritesOnly = false,
    source = "all",
    query = "",
    includeInvalid = false,
  } = options;
  const favoriteSet = new Set((favorites || []).map((id) => String(id)));
  const normalizedQuery = String(query || "").trim().toLowerCase();
  const rows = [];
  const invalidRows = [];
  const seen = new Set();

  for (const event of Array.isArray(events) ? events : []) {
    if (event?.type !== "planned_outage" || isClosed(event) || event?.verification_pending) continue;
    if (source !== "all" && String(event?.source || "") !== source) continue;
    if (favoritesOnly && !favoriteSet.has(String(event?.object_id))) continue;
    if (normalizedQuery && !eventSearchText(event, objects).includes(normalizedQuery)) continue;

    const windows = validWindows(event);
    if (!windows.length) {
      invalidRows.push({ event, window: null, object: objectName(event, objects), description: eventDescription(event), bucket: "invalid" });
      continue;
    }

    for (const window of windows) {
      const bucket = plannedWindowBucket(event, window, now, hours);
      if (bucket === "invalid") {
        invalidRows.push({ event, window, object: objectName(event, objects), description: eventDescription(event), bucket });
        continue;
      }
      if (!["ongoing", "future"].includes(bucket)) continue;
      const key = [
        event.source || "",
        event.source_event_id || event.event_key || "",
        event.object_id || "",
        window.start_at || "",
        window.stop_at || "",
      ].join("|");
      if (seen.has(key)) continue;
      seen.add(key);
      rows.push({ event, window, object: objectName(event, objects), description: eventDescription(event), bucket });
    }
  }

  const sorted = rows.sort((a, b) => (
    (a.bucket === "ongoing" ? 0 : 1) - (b.bucket === "ongoing" ? 0 : 1) ||
    a.window.start - b.window.start ||
    a.object.localeCompare(b.object, "pl") ||
    String(a.event.object_id || "").localeCompare(String(b.event.object_id || ""), "pl")
  ));
  const uniqueObjects = new Set(sorted.map((row) => String(row.event.object_id || row.object))).size;
  return {
    rows: sorted.slice(0, limit),
    total: sorted.length,
    uniqueObjects,
    invalidRows: includeInvalid ? invalidRows.slice(0, limit) : [],
    invalidTotal: invalidRows.length,
  };
}

export function nextWindows(events, objects = [], now = new Date(), hours = 48, limit = 10) {
  const end = new Date(now.getTime() + hours * 60 * 60 * 1000);
  const rows = [];
  const seen = new Set();
  for (const event of Array.isArray(events) ? events : []) {
    if (isClosed(event) || event?.verification_pending) continue;
    for (const window of validWindows(event)) {
      if (!window.start || window.start <= now || window.start > end) continue;
      const key = [event.object_id, event.event_key || event.source_event_id, window.start_at, window.stop_at].join("|");
      if (seen.has(key)) continue;
      seen.add(key);
      rows.push({ event, window, object: objectName(event, objects) });
    }
  }
  return rows
    .sort((a, b) => a.window.start - b.window.start || a.object.localeCompare(b.object, "pl"))
    .slice(0, limit);
}

export function loadFavorites(storage = window.localStorage) {
  try {
    const parsed = JSON.parse(storage.getItem(FAVORITES_KEY) || "[]");
    return Array.isArray(parsed) ? [...new Set(parsed.map((item) => String(item)).filter(Boolean))].slice(0, 20) : [];
  } catch {
    return [];
  }
}

export function saveFavorites(ids, storage = window.localStorage) {
  const normalized = [...new Set((ids || []).map((item) => String(item)).filter(Boolean))].slice(0, 20);
  storage.setItem(FAVORITES_KEY, JSON.stringify(normalized));
  return normalized;
}

export function sourceRows(status) {
  const sources = status?.sources ?? {};
  return SOURCE_ORDER.filter((source) => sources[source]).map((source) => ({
    source,
    ...sources[source],
  }));
}

export function formatDate(value) {
  if (!value) return "brak odczytu";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "błędna data";
  return new Intl.DateTimeFormat("pl-PL", {
    dateStyle: "medium",
    timeStyle: "short",
    timeZone: "Europe/Warsaw",
  }).format(date);
}
