const REFRESH_SECONDS = 30;
const FAVORITES_KEY = "snut.favorites.v1"; // gitleaks:allow -- publiczny identyfikator localStorage, bez danych uwierzytelniających
const WARSAW_TIME_ZONE = "Europe/Warsaw";

let events = [];
let objects = [];
let statusPayload = null;
let activeView = "current";
let selectedDate = dayKey(new Date());
let secondsToRefresh = REFRESH_SECONDS;
let map;
let locationMap;
let objectLayer;
let outageLayer;
let previewMarker;
let locationResolveRevision = 0;
let autoLocationName = null;
let autoLocationKeyword = null;
let objectMarkers = new Map();
let pendingDeepLink = null;
let hasFocusedDeepLink = false;
let favoriteIds = loadFavorites();
let mainRefreshPending = false;
let pendingObjectRefresh = false;
let editingObjectId = "";
let mapEtag = "";
let lastMapPayload = null;
let geometries = {};
let showOperatorPoints = false;
let serverClockAtRead = null;
let clockReadAt = 0;
let sourceScheduleAvailable = false;
const openGeoportalReviews = new Set();
const busyGeoportalReviews = new Set();
const geoportalReviewErrors = new Map();

const typeLabels = {
  outage: "Awaria",
  planned_outage: "Planowane wyłączenie",
  mass_outage: "Awaria masowa",
  unknown: "Nieokreślony typ",
};

const typeColors = {
  outage: "#c9362b",
  planned_outage: "#d46c1f",
  mass_outage: "#802820",
  unknown: "#1769aa",
  cancelled: "#7b818a",
  upcoming: "#d46c1f",
  overdue: "#925000",
};

const timePhaseLabels = {
  upcoming: "Nadchodzące",
  ongoing: "Trwa według harmonogramu",
  overdue: "Po przewidywanym terminie, operator nadal publikuje",
  closed: "Zamknięte według operatora",
  unknown: "Termin nieznany",
};

const sourceStatusLabels = {
  active: "Aktywne",
  cancelled: "Odwołane",
  resolved: "Zakończone",
  unknown: "Brak danych",
};

const matchConfidenceLabels = {
  exact_address: "Zweryfikowany adres w wykazie operatora",
  address_geometry_conflict: "Adres i obszar operatora są sprzeczne",
  county_mass: "Komunikat dotyczący powiatu",
  exact_teryt: "Zgodny kod miejscowości lub ulicy",
  spatial_polygon: "Obiekt znajduje się w obszarze zdarzenia",
  spatial_radius: "W promieniu operatora; zasięg wyłączenia niepotwierdzony",
  spatial_nearby: "Obiekt znajduje się w pobliżu zdarzenia",
  text_keyword: "Dopasowanie na podstawie słowa kluczowego",
  text_address: "Zgodny tekst miejscowości lub ulicy",
};

function describeDataStatus(payload) {
  if (payload.demo) return {title: "Scenariusz demonstracyjny", message: "Zdarzenia są symulowane. Punkty miejscowości są orientacyjne.", connection: "Dane demonstracyjne"};
  const dataStatus = payload.data_status || "unknown";
  const pendingLocations = (payload.locations || []).filter(
    (location) => (location.matching_pending_sources || []).length > 0,
  ).length;
  const staleSources = (payload.sources || []).filter((source) => source.status !== "fresh").length;
  const missingSources = (payload.missing_sources || []).length;

  if (dataStatus === "fresh") {
    return { title: "", message: "", connection: "Dane aktualne" };
  }
  if (dataStatus === "partial" && pendingLocations > 0 && staleSources === 0 && missingSources === 0) {
    return {
      title: "Trwa aktualizowanie dopasowań.",
      message: `Wszystkie źródła operatorów są aktualne. Liczba lokalizacji oczekujących na przeliczenie: ${formatCount(pendingLocations)}. Dane zostaną uzupełnione po kolejnym automatycznym skanie.`,
      connection: "Trwa przeliczanie lokalizacji",
    };
  }
  if (dataStatus === "partial") {
    return {
      title: "Dane są częściowe.",
      message: "Co najmniej jedno źródło nie dostarczyło pełnych, aktualnych danych.",
      connection: "Dane częściowe",
    };
  }
  if (dataStatus === "stale") {
    return {
      title: "Dane są nieaktualne.",
      message: "Co najmniej jedno źródło nie zostało ostatnio poprawnie odczytane.",
      connection: "Dane nieaktualne",
    };
  }
  if (dataStatus === "unavailable") {
    return {
      title: "Dane są niedostępne.",
      message: "Żadne źródło nie ma aktualnego, potwierdzonego odczytu.",
      connection: "Dane niedostępne",
    };
  }
  return {
    title: "Nieznany stan danych.",
    message: "Aplikacja nie rozpoznała stanu danych zwróconego przez serwer.",
    connection: "Nieznany stan danych",
  };
}

function dayKey(date) {
  const year = date.getFullYear();
  const month = String(date.getMonth() + 1).padStart(2, "0");
  const day = String(date.getDate()).padStart(2, "0");
  return `${year}-${month}-${day}`;
}

function formatDate(value) {
  return OsdEventView.formatDate(value);
}

function formatCount(value) {
  return new Intl.NumberFormat("pl-PL").format(value || 0);
}

function truncateText(value, maxLength = 600) {
  const text = String(value || "").trim();
  if (text.length <= maxLength) return text;
  return `${text.slice(0, maxLength).trimEnd()}…`;
}

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function googleMapsDirectionsUrl(object) {
  const latitudeValue = object?.latitude;
  const longitudeValue = object?.longitude;
  if (latitudeValue === null || latitudeValue === undefined || latitudeValue === "") return "";
  if (longitudeValue === null || longitudeValue === undefined || longitudeValue === "") return "";
  const latitude = Number(latitudeValue);
  const longitude = Number(longitudeValue);
  if (!Number.isFinite(latitude) || !Number.isFinite(longitude)) return "";
  const destination = encodeURIComponent(`${latitude},${longitude}`);
  return `https://www.google.com/maps/dir/?api=1&destination=${destination}`;
}

function createElement(tag, className, text) {
  const element = document.createElement(tag);
  if (className) element.className = className;
  if (text !== undefined) element.textContent = text;
  return element;
}

function loadFavorites() {
  try {
    const parsed = JSON.parse(window.localStorage.getItem(FAVORITES_KEY) || "[]");
    return Array.isArray(parsed) ? [...new Set(parsed.map((item) => String(item)).filter(Boolean))].slice(0, 20) : [];
  } catch {
    return [];
  }
}

function saveFavorites(ids) {
  favoriteIds = [...new Set((ids || []).map((item) => String(item)).filter(Boolean))].slice(0, 20);
  try {
    window.localStorage.setItem(FAVORITES_KEY, JSON.stringify(favoriteIds));
  } catch {
    // Brak localStorage nie może zablokować mapy.
  }
}

function isClosed(event) {
  return Boolean(event.revoked || event.status === "cancelled" || event.status === "resolved" || event.time_phase === "closed");
}

function parseTime(value) {
  if (!value) return null;
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? null : date;
}

function validTimeWindows(event) {
  if (!Array.isArray(event.time_windows)) return [];
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

function warsawDayKey(value) {
  const date = parseTime(value);
  if (!date) return "";
  return new Intl.DateTimeFormat("en-CA", { timeZone: WARSAW_TIME_ZONE }).format(date);
}

function attentionReason(event) {
  if (isClosed(event)) return "";
  if (event.verification_pending) return "Weryfikacja";
  if (!pointConfirmed(event)) return "Objęcie punktu niepotwierdzone";
  if (event.time_phase === "ongoing") return "Trwa";
  if (event.time_phase === "overdue") return "Po przewidywanym terminie";
  if (!validTimeWindows(event).length || event.time_phase === "unknown") return "Nieznany czas";
  return "";
}

function isNext48(event) {
  if (isClosed(event) || event.verification_pending) return false;
  const now = new Date();
  const end = new Date(now.getTime() + 48 * 60 * 60 * 1000);
  return validTimeWindows(event).some((window) => window.start && window.start > now && window.start <= end);
}

function objectForEvent(event) {
  return objects.find((item) => String(item.id) === String(event.object_id));
}

function objectUrl(id, view = "current", date = "") {
  const params = new URLSearchParams({ object: String(id), view });
  if (date) params.set("date", date);
  return `/?${params.toString()}`;
}

function eventLabel(event) {
  return OsdEventView.eventLabel(event);
}

function eventColor(event) {
  if (event.verification_pending || ["cancelled", "resolved"].includes(event.status)) {
    return typeColors.cancelled;
  }
  return typeColors[event.type] || typeColors.unknown;
}

function safeSourceUrl(event) {
  return OsdEventView.safeUrl(event.source_url);
}

function rawEvent(event) {
  const raw = event.source_event?.raw;
  if (raw && Object.keys(raw).length) return raw;
  return event.pge_event || event.source_event || {};
}

function sourceLabel(event) {
  return event.operator || String(event.source || "OSD").toUpperCase();
}

function firstTeryt(event) {
  return event.matched_locations?.[0]?.teryt || {};
}

function addressText(event) {
  return OsdEventView.addressText(event);
}

function eventMatchesDate(event, dateKey) {
  return validTimeWindows(event).some((window) => {
    const startDay = window.start_at ? warsawDayKey(window.start_at) : "";
    const stopDay = window.stop_at ? warsawDayKey(window.stop_at) : startDay;
    return startDay <= dateKey && (!stopDay || stopDay >= dateKey);
  });
}

function visibleEvents() {
  const query = document.getElementById("eventSearch").value.trim().toLowerCase();
  const scope = document.getElementById("matchScope").value;
  return events.filter((event) => {
    if (scope === "confirmed" && !pointConfirmed(event)) return false;
    if (scope === "candidate" && pointConfirmed(event)) return false;
    let viewMatches = false;
    if (activeView === "planned") {
      viewMatches = event.type === "planned_outage" && !event.verification_pending && !isClosed(event) && eventMatchesDate(event, selectedDate);
    } else if (activeView === "attention") {
      viewMatches = Boolean(attentionReason(event));
    } else if (activeView === "next48") {
      viewMatches = isNext48(event);
    } else {
      viewMatches = event.time_phase !== "upcoming" && !isClosed(event);
    }
    const searchValue = [event.city, event.city_sym, event.configured_name, event.street_sym, event.operator, event.source, event.description, addressText(event)]
      .filter(Boolean)
      .join(" ")
      .toLowerCase();
    return viewMatches && searchValue.includes(query);
  });
}

function initializeMap() {
  map = L.map("map", { zoomControl: true }).setView([52.05, 19.2], 6);
  installMapLayers(map);
  localizeMapControls(map);
  objectLayer = L.layerGroup().addTo(map);
  outageLayer = L.layerGroup().addTo(map);
}

function initializeLocationMap() {
  locationMap = L.map("locationMap", { zoomControl: true }).setView([52.05, 19.2], 6);
  installMapLayers(locationMap, "bottomleft");
  localizeMapControls(locationMap);
  locationMap.on("click", (event) => resolvePickedPoint(event.latlng.lat, event.latlng.lng));
}

function localizeMapControls(target) {
  for (const [selector, label] of [[".leaflet-control-zoom-in", "Przybliż mapę"], [".leaflet-control-zoom-out", "Oddal mapę"], [".leaflet-control-layers-toggle", "Wybierz warstwy mapy"]]) {
    const element = target.getContainer().querySelector(selector);
    if (element) { element.title = label; element.setAttribute("aria-label", label); }
  }
  target.on("popupopen", event => {
    const inspector = event.popup.getElement()?.querySelector('.object-inspector');
    if (inspector) {
      // Leaflet aktualizuje rozmiar, zachowując węzły i obsługę zakładek.
      event.popup.setContent(inspector);
      globalThis.OsdObjectHistory.mount(inspector, inspector.dataset.objectId, () => event.popup.update());
    }
    const close = event.popup.getElement()?.querySelector(".leaflet-popup-close-button");
    if (close) { close.title = "Zamknij opis"; close.setAttribute("aria-label", "Zamknij opis"); }
  });
}

function installMapLayers(target, noticePosition = "topleft") {
  const ordinary = L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
    maxNativeZoom: 18, maxZoom: 20, attribution: "&copy; OpenStreetMap",
  }).addTo(target);
  const ortho = L.tileLayer("https://mapy.geoportal.gov.pl/wss/service/PZGIK/ORTO/WMTS/StandardResolution?SERVICE=WMTS&REQUEST=GetTile&VERSION=1.0.0&LAYER=ORTOFOTOMAPA&STYLE=default&FORMAT=image%2Fjpeg&TILEMATRIXSET=EPSG%3A3857&TILEMATRIX=EPSG%3A3857%3A{z}&TILEROW={y}&TILECOL={x}", {
    maxNativeZoom: 19, maxZoom: 20, bounds: [[49, 14], [55.1, 24.6]],
    attribution: 'Źródło: <a href="https://www.geoportal.gov.pl/" target="_blank" rel="noopener noreferrer">GUGiK / Geoportal</a>',
  });
  // Osobna warstwa pod markerami, bez przechwytywania wyboru punktu GPS.
  const energyPane = target.createPane("energyInfrastructure");
  energyPane.style.zIndex = "250";
  energyPane.style.pointerEvents = "none";
  const gesut = L.tileLayer.wms("https://integracja.gugik.gov.pl/cgi-bin/KrajowaIntegracjaUzbrojeniaTerenu", {
    layers: "przewod_elektroenergetyczny", format: "image/png", transparent: true, version: "1.3.0",
    minZoom: 20, maxZoom: 20, pane: "energyInfrastructure", bounds: [[49, 14], [55.1, 24.6]],
    attribution: 'Sieć: <a href="https://www.geoportal.gov.pl/pl/dane/uzbrojenie-terenu-gesut/" target="_blank" rel="noopener noreferrer">GUGiK / powiatowe GESUT</a>',
  });
  const pse = L.tileLayer.wms("https://mapy.geoportal.gov.pl/wss/ext/PSE", {
    layers: "linie,stacje", styles: ",", format: "image/png", transparent: true, version: "1.3.0",
    maxZoom: 20, pane: "energyInfrastructure", bounds: [[49, 14], [55.1, 24.6]],
    attribution: 'Sieć: <a href="https://mapy.geoportal.gov.pl/wss/ext/PSE" target="_blank" rel="noopener noreferrer">PSE / Geoportal</a>',
  });
  const gesutOverlay = L.layerGroup([gesut]);
  L.control.layers({ "Mapa": ordinary, "Ortofotomapa GUGiK": ortho }, {
    // Grupa pozwala włączyć nakładkę również poniżej progu zoomu.
    // Sam WMS nie pobiera wtedy kafelków, a panel pokazuje wskazówkę.
    "Sieć elektroenergetyczna GESUT": gesutOverlay,
    "Linie i stacje PSE": pse,
  }, { position: "topright" }).addTo(target);
  // Na głównej mapie komunikat nie może zasłaniać legendy na telefonie.
  const notice = L.control({ position: noticePosition });
  notice.onAdd = () => {
    notice.element = L.DomUtil.create("div", "ortho-warning");
    notice.element.setAttribute("role", "status");
    notice.element.hidden = true;
    return notice.element;
  };
  notice.addTo(target);
  L.DomEvent.disableClickPropagation(notice.element);
  L.DomEvent.disableScrollPropagation(notice.element);
  const energyErrors = new Map([[gesut, new Set()], [pse, new Set()]]);
  const failedTiles = new Set();
  const updateNotice = () => {
    const messages = [];
    if (target.hasLayer(ortho) && failedTiles.size) messages.push("Nie udało się pobrać ortofotomapy. Możesz wrócić do podkładu „Mapa”.");
    if (target.hasLayer(gesut) && target.getZoom() < 20) messages.push("GESUT: przybliż mapę do poziomu 20, aby zobaczyć przewody.");
    for (const [layer, label] of [[gesut, "GESUT"], [pse, "PSE"]]) {
      if (target.hasLayer(layer) && energyErrors.get(layer).size) messages.push(`${label}: nie udało się pobrać części mapy. Możesz wyłączyć nakładkę.`);
    }
    if (target.hasLayer(gesut) || target.hasLayer(pse)) messages.push("Mapa infrastruktury nie potwierdza zasilania obiektu. Brak linii na mapie nie oznacza braku sieci.");
    notice.element.textContent = messages.join(" ");
    notice.element.hidden = messages.length === 0;
  };
  for (const layer of [gesut, pse]) {
    layer.on("tileerror", (event) => { energyErrors.get(layer).add(event.tile); updateNotice(); });
    const clearEnergyError = (event) => { energyErrors.get(layer).delete(event.tile); updateNotice(); };
    layer.on("tileload", clearEnergyError);
    layer.on("tileunload", clearEnergyError);
  }
  ortho.on("tileerror", (event) => {
    failedTiles.add(event.tile);
    if (!event.tile.dataset.geoportalRetry) {
      event.tile.dataset.geoportalRetry = "1";
      setTimeout(() => {
        if (event.tile.isConnected && target.hasLayer(ortho)) event.tile.src += "&retry=1";
      }, 1000);
      return;
    }
    updateNotice();
  });
  const clearTileError = (event) => {
    failedTiles.delete(event.tile);
    updateNotice();
  };
  ortho.on("tileload", clearTileError);
  ortho.on("tileunload", clearTileError);
  target.on("baselayerchange overlayadd overlayremove zoomend", updateNotice);
  gesutOverlay.addTo(target);
  pse.addTo(target);
  updateNotice();
}

function renderLocationReview(container, review) {
  container.replaceChildren();
  const labels = {
    consistent: "Dane zgodne z rejestrem PRG; adres obiektu wymaga osobnego dowodu.",
    needs_review: "Wykryto rozbieżność. Sprawdź lokalizację.",
    inconclusive: "Dane nie wystarczają do rozstrzygnięcia.",
    not_checked: "Nie wykonano jeszcze porównania.",
    requires_review: "Dane obiektu zmieniono. Poprzednie porównanie jest nieaktualne.",
    unavailable: "Geoportal chwilowo niedostępny.",
  };
  container.append(createElement("p", "", labels[review.status] || "Wynik wymaga sprawdzenia."));
  if (review.checked_at) container.append(createElement("p", "", `Data porównania: ${formatDate(review.checked_at)}${review.expired ? "; wynik nieaktualny" : ""}`));
  for (const reason of review.reasons || []) container.append(createElement("p", "", reason));
  for (const candidate of review.commune?.candidates || []) {
    container.append(createElement("p", "", `Gmina według GPS: ${candidate.name || ""}, kod ${candidate.code}${candidate.near_boundary ? ", punkt przy granicy" : ""}`));
  }
  for (const candidate of review.address?.candidates || []) {
    container.append(createElement("p", "", `${candidate.city || ""}, ${candidate.street || ""} ${candidate.house_number}, ${new Intl.NumberFormat("pl-PL").format(candidate.distance_m)} m; SIMC ${candidate.city_sym}${candidate.address_equal ? "; zgodność zapisanego adresu" : "; kandydat"}`));
  }
  container.append(createElement("p", "", "Porównanie nie potwierdza objęcia wyłączeniem ani stanu zasilania."));
  const link = createElement("a", "", "Źródło: GUGiK / Geoportal");
  link.href = "https://www.geoportal.gov.pl/";
  link.target = "_blank";
  link.rel = "noopener noreferrer";
  container.append(link);
}

function powerSupplyLines(object) {
  return OsdEventView.powerSupplyLines(object);
}

function mapEventMessage(event, object) {
  const info = OsdEventView.summary(event, object);
  const explanation = matchExplanationRows(event, false);
  return `<div class="map-message"><strong>${escapeHtml(info.title)}</strong><br>${escapeHtml(info.operator)} · ${escapeHtml(info.phase)}<br>${info.periods.map(escapeHtml).join("<br>")}<p class="event-address">${escapeHtml(info.address)}</p><p>${escapeHtml(info.reason)}</p><details><summary>Dlaczego ten komunikat pasuje?</summary>${explanation.map(line => `<p>${escapeHtml(line)}</p>`).join("")}</details>${info.hasAdditionalDescription ? `<details><summary>Opis operatora</summary><p>${escapeHtml(info.description)}</p></details>` : ""}${info.sourceUrl ? `<a class="event-source-link" href="${escapeHtml(info.sourceUrl)}" target="_blank" rel="noopener noreferrer">Źródło operatora</a>` : ""}</div>`;
}

function renderMap(list) {
  if (!map) return;
  const openObject = [...(objectMarkers || [])].find(([, marker]) => marker.isPopupOpen?.())?.[0];
  objectLayer.clearLayers();
  outageLayer.clearLayers();
  objectMarkers = new Map();
  const byObject = new Map();
  for (const event of list) {
    const key = String(event.object_id);
    if (!byObject.has(key)) byObject.set(key, []);
    byObject.get(key).push(event);
  }

  for (const object of objects) {
    const point = validMapPoint(object.latitude, object.longitude);
    if (!point) continue;
    const [latitude, longitude] = point;
    const directionsUrl = googleMapsDirectionsUrl(object);
    const related = byObject.get(String(object.id)) || [];
    const confirmed = related.find(pointConfirmed);
    const highlighted = confirmed
      || related.find((event) => !event.verification_pending && ["outage", "mass_outage"].includes(event.type))
      || related.find((event) => !event.verification_pending)
      || related[0];
    const popup = [
      `<h4>${escapeHtml(object.name)}</h4>`,
      escapeHtml(object.city),
      object.house_number ? `Adres: ${escapeHtml(object.street || "")} ${escapeHtml(object.house_number)}` : "",
      `TERYT (SIMC): ${escapeHtml(object.city_sym)}`,
      locationWarning(object),
      "Położenie monitorowanego obiektu",
      ...powerSupplyLines(object).map(escapeHtml),
      `<a class="map-route-link" href="${escapeHtml(directionsUrl)}" target="_blank" rel="noopener noreferrer">Trasa w Google Maps</a>`,
      ...related.map((event) => mapEventMessage(event, object)),
    ].join("<br>");
    const marker = L.circleMarker([latitude, longitude], {
      radius: related.length ? 10 : 5,
      color: "#fff",
      weight: related.length ? 3 : 2,
      dashArray: related.length && !confirmed ? "3 3" : null,
      fillColor: highlighted ? eventColor(highlighted) : "#1769aa",
      fillOpacity: 1,
    })
      .bindPopup(`<div class="object-inspector" data-object-id="${escapeHtml(object.id)}">${popup}</div>`, {maxWidth: 340, maxHeight: 400})
      .addTo(objectLayer);
    objectMarkers.set(String(object.id), marker);
  }
  if (openObject) objectMarkers.get(openObject)?.openPopup();

  // Punkty operatora opisują cały komunikat, a nie GPS dopasowanego obiektu.
  if (!showOperatorPoints) {
    document.getElementById("operatorOverlayStatus").hidden = true;
    focusDeepLinkedObject();
    return;
  }
  const bySourceEvent = new Map();
  for (const event of list) {
    const key = `${event.source || ""}:${event.source_event_id || event.event_id || event.object_id || ""}`;
    if (!bySourceEvent.has(key)) bySourceEvent.set(key, []);
    bySourceEvent.get(key).push(event);
  }
  const drawnGeometries = new Set();
  let operatorPoints = 0, operatorAreas = 0, withoutGeometry = 0;
  for (const related of bySourceEvent.values()) {
    const event = related[0];
    const color = eventColor(event);
    const geometry = geometries[event.geometry_ref];
    const coordinateSource = related.flatMap((item) => {
      const shape = geometries[item.geometry_ref];
      return item.source === "pge" && shape?.type === "MultiPoint" && Array.isArray(shape.coordinates)
        ? shape.coordinates.map((pair) => ({ latitude: pair?.[1], longitude: pair?.[0] }))
        : item.coordinates || item.pge_event?.coordinates || [];
    });
    const coordinates = coordinateSource
      .map((point) => validMapPoint(point?.latitude, point?.longitude)).filter(Boolean);
    const names = [...new Set(related.map((item) => item.configured_name || item.city || String(item.object_id)))];
    const popup = `<strong>Komunikat operatora: ${escapeHtml(sourceLabel(event))}</strong><br>${escapeHtml(eventLabel(event))}<br>${escapeHtml(event.description || addressText(event))}<br>Punkty z komunikatu operatora mogą znajdować się poza monitorowanym obiektem.<br>Obiekty z dopasowaniem potwierdzonym lub wymagającym sprawdzenia: ${names.map(escapeHtml).join("; ")}`;
    if (validMapGeometry(geometry)) {
      if (!drawnGeometries.has(event.geometry_ref)) {
        L.geoJSON(geometry, { style: { color, weight: 2, fillColor: color, fillOpacity: 0.15 } })
          .bindPopup(`${popup}<br>Obszar przekazany przez operatora`).addTo(outageLayer);
        drawnGeometries.add(event.geometry_ref);
        operatorAreas += 1;
      }
    } else {
      const drawnPoints = new Set();
      for (const point of coordinates) {
        const key = point.join(",");
        if (drawnPoints.has(key)) continue;
        drawnPoints.add(key);
        L.circleMarker(point, { radius: 5, color, weight: 1, dashArray: "2 2", fillColor: color, fillOpacity: 0.15 })
          .bindPopup(`${popup}<br>Punkt przekazany przez operatora; nie oznacza położenia obiektu`).addTo(outageLayer);
        operatorPoints += 1;
      }
      if (!coordinates.length) withoutGeometry += 1;
    }
  }
  const overlayStatus = document.getElementById("operatorOverlayStatus");
  overlayStatus.hidden = false;
  overlayStatus.textContent = operatorPoints || operatorAreas
    ? `Pokazano ${OsdEventView.count(operatorPoints, ["punkt", "punkty", "punktów"])} i ${OsdEventView.count(operatorAreas, ["obszar", "obszary", "obszarów"])} operatorów.${withoutGeometry ? ` Komunikaty bez geometrii: ${withoutGeometry}.` : ""} Przewiń do mapy i przybliż wybraną okolicę.`
    : "W tym widoku nie ma punktów ani obszarów przekazanych przez operatorów.";
  focusDeepLinkedObject();
}

function validMapPoint(latitude, longitude) {
  const missing = (value) => value === null || value === undefined || (typeof value === "string" && !value.trim());
  if (missing(latitude) || missing(longitude)) return null;
  if (![latitude, longitude].every((value) => typeof value === "number" || typeof value === "string")) return null;
  if (![latitude, longitude].every((value) => typeof value !== "string" || /^[+-]?(?:\d+\.?\d*|\.\d+)(?:e[+-]?\d+)?$/i.test(value.trim()))) return null;
  const pair = [Number(latitude), Number(longitude)];
  return pair.every(Number.isFinite) && Math.abs(pair[0]) <= 90 && Math.abs(pair[1]) <= 180 ? pair : null;
}

function validMapGeometry(geometry) {
  if (!geometry || !["Polygon", "MultiPolygon"].includes(geometry.type) || !Array.isArray(geometry.coordinates)) return false;
  const polygons = geometry.type === "Polygon" ? [geometry.coordinates] : geometry.coordinates;
  return polygons.length > 0 && polygons.every((polygon) => Array.isArray(polygon) && polygon.length > 0 && polygon.every((ring) => (
    Array.isArray(ring) && ring.length >= 4 && ring.every((pair) => Array.isArray(pair) && pair.length >= 2 && pair.slice(0, 2).every((n) => typeof n === "number") && validMapPoint(pair[1], pair[0]))
    && ring[0][0] === ring[ring.length - 1][0] && ring[0][1] === ring[ring.length - 1][1]
  )));
}

function pointConfirmed(event) {
  return OsdEventView.pointConfirmed(event);
}

function matchExplanationRows(event, includeReason = true) {
  return typeof OsdMatchingView !== 'undefined' ? OsdMatchingView.rows(event, {includeReason})
    : [event.match_evidence?.reason || 'Brak szczegółowego wyjaśnienia starszego wpisu.'];
}

function matchExplanationElement(event, includeReason = true) {
  const details = createElement('details', 'full-data');
  details.append(createElement('summary', '', 'Dlaczego ten komunikat pasuje?'));
  for (const line of matchExplanationRows(event, includeReason)) details.append(createElement('p', '', line));
  return details;
}

function locationWarning(object) {
  return OsdEventView.locationWarning(object);
}

function parseInitialRoute() {
  const params = new URLSearchParams(window.location.search);
  const view = params.get("view");
  const object = params.get("object");
  const date = params.get("date");
  if (["current", "attention", "next48", "planned", "objects"].includes(view)) {
    activeView = view;
  }
  if (/^\d{4}-\d{2}-\d{2}$/.test(date || "")) {
    selectedDate = date;
  }
  if (object) {
    pendingDeepLink = String(object);
  }
}

function updateRoute(objectId = "") {
  const params = new URLSearchParams();
  if (objectId) params.set("object", String(objectId));
  params.set("view", activeView);
  if (activeView === "planned") params.set("date", selectedDate);
  const nextUrl = `${window.location.pathname}${params.toString() ? `?${params.toString()}` : ""}`;
  window.history.replaceState({}, "", nextUrl);
}

function focusObject(objectId, { update = true } = {}) {
  if (!map || !objectId) return false;
  const marker = objectMarkers.get(String(objectId));
  if (!marker) return false;
  const latlng = marker.getLatLng();
  map.setView(latlng, Math.max(map.getZoom(), 13));
  marker.openPopup();
  if (update) updateRoute(objectId);
  return true;
}

function focusDeepLinkedObject() {
  if (!pendingDeepLink || hasFocusedDeepLink) return;
  const objectExists = objects.some((item) => String(item.id) === String(pendingDeepLink));
  if (!objectExists) return;
  if (focusObject(pendingDeepLink, { update: false })) {
    hasFocusedDeepLink = true;
  }
}

function renderDateStrip() {
  const strip = document.getElementById("dateStrip");
  strip.replaceChildren();
  const today = new Date();
  for (let index = 0; index < 6; index += 1) {
    const date = new Date(today);
    date.setDate(today.getDate() + index);
    const key = dayKey(date);
    const button = createElement("button", `date-button${key === selectedDate ? " active" : ""}`);
    button.type = "button";
    button.dataset.date = key;
    button.append(createElement("strong", "", index === 0 ? "Dziś" : new Intl.DateTimeFormat("pl-PL", { weekday: "short" }).format(date)));
    button.append(createElement("span", "", new Intl.DateTimeFormat("pl-PL", { day: "2-digit", month: "2-digit", year: "numeric" }).format(date)));
    strip.append(button);
  }
}

function createMeta(label, value) {
  const wrapper = document.createElement("div");
  wrapper.append(createElement("dt", "", label));
  wrapper.append(createElement("dd", "", value));
  return wrapper;
}

function createDataBox(title, lines) {
  const box = createElement("div", "data-box");
  box.append(createElement("h4", "", title));
  for (const line of lines) box.append(createElement("p", "", line));
  return box;
}

function createEventCard(event) {
  const info = OsdEventView.summary(event, objectForEvent(event));
  const kindClass = event.status === "cancelled"
    ? "cancelled"
    : `${event.type} ${event.time_phase || "unknown"}`;
  const card = createElement("article", `event-card ${kindClass}`);
  const top = createElement("div", "event-topline");
  top.append(createElement("span", "event-badge", eventLabel(event)));
  const eventActions = createElement("div", "event-actions");
  eventActions.append(createElement("span", "event-operator", info.operator));
  if (event.object_id) {
    const objectLink = createElement("a", "event-object-link", "Pokaż obiekt");
    objectLink.href = objectUrl(event.object_id, activeView, activeView === "planned" ? selectedDate : "");
    eventActions.append(objectLink);
  }
  top.append(eventActions);
  card.append(top);
  card.append(createElement("p", "event-address", info.address));
  const timing = createElement("dl", "event-meta event-summary");
  info.periods.forEach((period, index) => timing.append(createMeta(info.periods.length > 1 ? `Termin ${index + 1}` : "Termin", period)));
  timing.append(createMeta("Stan komunikatu", `${info.status} · ${info.phase}`));
  card.append(timing);
  card.append(createElement("p", "location-warning", info.reason));
  card.append(matchExplanationElement(event, false));
  const sourceUrl = safeSourceUrl(event);
  if (sourceUrl) {
    const sourceLink = createElement("a", "event-source-link", "Źródło operatora");
    sourceLink.href = sourceUrl;
    sourceLink.target = "_blank";
    sourceLink.rel = "noopener noreferrer";
    card.append(sourceLink);
  }
  const details = createElement("details", "full-data");
  details.append(createElement("summary", "", "Dane obiektu i komunikatu"));
  details.append(createDataBox("Monitorowany obiekt", [
    info.objectName, info.objectCity, ...(info.objectAddress ? [`Adres obiektu: ${info.objectAddress}`] : []),
    info.teryt, ...info.powerLines, ...(info.locationWarning ? [info.locationWarning] : []),
  ]));
  if (info.hasAdditionalDescription) details.append(createElement("p", "event-description", `Opis operatora: ${info.description}`));
  const teryt = firstTeryt(event);
  const columns = createElement("div", "data-columns");
  columns.append(createDataBox("TERYT pierwszego dopasowanego adresu", [
    `Województwo: ${teryt.voivodeshipName || "-"} (${teryt.voivodeshipCode || "-"})`,
    `Powiat: ${teryt.countyName || "-"} (${teryt.countyCode || "-"})`,
    `Gmina: ${teryt.communeName || "-"} (${teryt.communeCode || "-"})`,
    `Miejscowość: ${teryt.cityName || event.city} (${teryt.citySym || event.city_sym})`,
    `Ulica: ${teryt.streetName || "-"} (${teryt.streetSymUl || "-"})`,
  ]));
  columns.append(createDataBox("Dane źródłowe", [
    `Operator: ${sourceLabel(event)}`,
    `ID komunikatu: ${event.source_event_id || event.pge_uuid || "bez ID"}`,
    `Dopasowanie: ${matchConfidenceLabels[event.match_confidence] || "Brak danych"}`,
    `Przedziały czasu: ${(event.time_windows || event.periods || []).length}`,
    `Współrzędne: ${(event.coordinates || []).length}`,
    `Dopasowane adresy: ${(event.matched_locations || []).length}`,
    `Aktualizacja: ${formatDate(event.source_updated_at || event.source_event?.updated_at)}`,
  ]));
  details.append(columns);
  const raw = rawEvent(event);
  if (Object.keys(raw).length) {
    const pre = createElement("pre", "json-view");
    pre.textContent = JSON.stringify(raw, null, 2);
    details.append(pre);
  } else {
    details.append(createElement("p", "", "Widok mapy nie zawiera pełnego rekordu operatora."));
  }
  card.append(details);
  return card;
}

function renderEvents() {
  const list = visibleEvents();
  const container = document.getElementById("eventsList");
  container.replaceChildren();
  document.getElementById("visibleCount").textContent = OsdEventView.count(list.length, ["wynik", "wyniki", "wyników"]);
  document.getElementById("mapResultLabel").textContent = `${OsdEventView.word(list.length, ["przypisanie", "przypisania", "przypisań"])} w widoku`;
  document.getElementById("mapResultCount").textContent = formatCount(list.length);
  document.getElementById("emptyState").classList.toggle("hidden", list.length !== 0);

  const groups = new Map();
  for (const event of list) {
    const key = String(event.object_id || event.city_sym);
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(event);
  }
  for (const group of groups.values()) {
    const first = group[0];
    const section = createElement("section", "location-group");
    const heading = createElement("div", "location-heading");
    heading.append(createElement("span", "", "-"));
    heading.append(createElement("h3", "", first.configured_name || first.city || first.city_sym));
    heading.append(createElement("span", "teryt-code", `TERYT ${first.city_sym}`));
    section.append(heading);
    for (const event of group) section.append(createEventCard(event));
    container.append(section);
  }

  document.getElementById("countAll").textContent = formatCount(list.length);
  const active = list.filter((event) => !event.verification_pending && !["cancelled", "resolved"].includes(event.status));
  document.getElementById("countOutage").textContent = formatCount(active.filter((event) => pointConfirmed(event) && event.type === "outage").length);
  document.getElementById("countPlanned").textContent = formatCount(active.filter((event) => pointConfirmed(event) && event.type === "planned_outage").length);
  document.getElementById("countMass").textContent = formatCount(active.filter((event) => pointConfirmed(event) && event.type === "mass_outage").length);
  document.getElementById("countCandidates").textContent = formatCount(list.filter((event) => !pointConfirmed(event)).length);
  document.getElementById("countAttention").textContent = formatCount(list.filter((event) => attentionReason(event)).length);
  renderMap(list);
}

function renderObjects() {
  const query = document.getElementById("objectSearch").value.trim().toLowerCase();
  const catalog = adminCsrf ? adminCatalog : objects;
  const filtered = catalog.filter((object) => [object.name, object.keyword, object.city, object.city_sym, object.street, object.street_sym].filter(Boolean).join(" ").toLowerCase().includes(query));
  const list = document.getElementById("objectList");
  list.replaceChildren();
  document.getElementById("navObjectCount").textContent = formatCount(objects.length);
  document.getElementById("catalogCount").textContent = OsdEventView.count(filtered.length, ["obiekt", "obiekty", "obiektów"]);
  document.getElementById("catalogMode").textContent = adminCsrf ? "TRYB ADMINISTRATORA" : "KATALOG PUBLICZNY";

  if (!filtered.length) list.append(createElement("p", "objects-note", "Brak obiektów spełniających wybrane kryteria."));

  for (const object of filtered) {
    const row = createElement("article", "object-row");
    row.append(createElement("span", "object-pin", "O"));
    const text = createElement("div", "");
    text.append(createElement("h4", "", object.name || object.city));
    const street = object.street ? ` | ${object.street}` : "";
    const keyword = object.keyword ? ` | słowo: ${object.keyword}` : "";
    const number = object.house_number ? ` ${object.house_number}` : "";
    text.append(createElement("p", "", `${object.city}${street}${number} | TERYT ${object.city_sym}${keyword}`));
    if (number && object.address_audit?.status !== "verified") {
      text.append(createElement("p", "location-warning", "Adres obiektu wymaga potwierdzenia dowodem."));
    }
    if (locationWarning(object)) {
      text.append(createElement("p", "location-warning", locationWarning(object)));
    }
    if (object.matching_pending_sources?.length) {
      text.append(createElement("p", "location-warning", "Oczekuje na przeliczenie dopasowań w automatycznym skanie."));
    }
    for (const line of powerSupplyLines(object)) text.append(createElement("p", "object-power-supply", line));
    row.append(text);
    const actions = createElement("div", "object-actions");
    const favoriteButton = createElement(
      "button",
      `secondary-button object-favorite-button${favoriteIds.includes(String(object.id)) ? " active" : ""}`,
      favoriteIds.includes(String(object.id)) ? "★" : "☆",
    );
    favoriteButton.type = "button";
    favoriteButton.title = favoriteIds.includes(String(object.id)) ? "Usuń z ulubionych" : "Dodaj do ulubionych";
    favoriteButton.setAttribute("aria-label", `${favoriteButton.title}: ${object.name || object.city}`);
    favoriteButton.setAttribute("aria-pressed", String(favoriteIds.includes(String(object.id))));
    favoriteButton.dataset.objectId = object.id;
    actions.append(favoriteButton);
    const directionsUrl = googleMapsDirectionsUrl(object);
    if (directionsUrl) {
      const mapButton = createElement("button", "secondary-button object-map-button", "Mapa");
      mapButton.type = "button";
      mapButton.dataset.objectId = object.id;
      actions.append(mapButton);
      const routeLink = createElement("a", "secondary-button object-route-link", "Trasa");
      routeLink.href = directionsUrl;
      routeLink.target = "_blank";
      routeLink.rel = "noopener noreferrer";
      routeLink.setAttribute("aria-label", `Trasa do ${object.name || object.city} w Google Maps`);
      actions.append(routeLink);
    }
    const editButton = createElement("button", "secondary-button object-edit-button", "Edytuj");
    editButton.classList.toggle('hidden', !adminCsrf);
    const geoButton = createElement("button", "secondary-button object-geoportal-button", "Porównaj z Geoportalem");
    geoButton.type = "button";
    geoButton.classList.toggle('hidden', !adminCsrf);
    geoButton.dataset.objectId = object.id;
    geoButton.disabled = busyGeoportalReviews.has(String(object.id));
    if (geoButton.disabled) geoButton.textContent = "Porównywanie…";
    actions.append(geoButton);
    const review = createElement("details", "geoportal-review");
    review.classList.toggle('hidden', !adminCsrf);
    review.append(createElement("summary", "", "Porównanie z Geoportalem"));
    const reviewBody = createElement("div", "geoportal-review-body");
    reviewBody.append(createElement("p", "", "Otwórz, aby odczytać zapisany wynik."));
    review.append(reviewBody);
    review.addEventListener("toggle", async () => {
      const id = String(object.id);
      if (!review.open) { openGeoportalReviews.delete(id); return; }
      openGeoportalReviews.add(id);
      if (busyGeoportalReviews.has(id)) {
        reviewBody.textContent = "Porównanie trwa…";
        return;
      }
      if (review.dataset.loaded) return;
      try {
        const response = await fetch(`/api/v1/objects/${encodeURIComponent(object.id)}/location-review`);
        const data = await readJsonResponse(response, "Nie udało się odczytać porównania.");
        renderLocationReview(reviewBody, data.item);
        if (geoportalReviewErrors.has(id)) reviewBody.prepend(createElement("p", "location-warning", geoportalReviewErrors.get(id)));
        review.dataset.loaded = "yes";
      } catch (error) { reviewBody.textContent = error.message; }
    });
    review.open = openGeoportalReviews.has(String(object.id));
    text.append(review);
    editButton.type = "button";
    editButton.title = "Edytuj obiekt";
    editButton.dataset.objectId = object.id;
    actions.append(editButton);
    const deleteButton = createElement("button", "danger-button object-delete-button", "Usuń");
    deleteButton.classList.toggle('hidden', !adminCsrf);
    deleteButton.type = "button";
    deleteButton.title = "Usuń obiekt";
    deleteButton.dataset.objectId = object.id;
    deleteButton.dataset.objectName = object.name;
    actions.append(deleteButton);
    row.append(actions);
    globalThis.OsdObjectHistory.mount(text, object.id);
    list.append(row);
  }
}

function setView(view) {
  activeView = view;
  document.body.classList.toggle('catalog-view', view === 'objects');
  document.querySelectorAll("button[data-view]").forEach((button) => {
    const selected = button.dataset.view === view;
    button.classList.toggle("active", selected);
    if (selected) button.setAttribute("aria-current", "page");
    else button.removeAttribute("aria-current");
  });
  globalThis.OsdHeader?.revealActive();
  document.getElementById("objectsView").classList.toggle("hidden", view !== "objects");
  document.getElementById("outagesView").classList.toggle("hidden", view === "objects");
  document.getElementById("dateStrip").classList.toggle("hidden", view !== "planned");

  const titles = {
    current: ["AKTUALNA SYTUACJA", "Aktualne komunikaty operatorów", "Mapa pokazuje zapisane obiekty. Kolor oznacza typ komunikatu; przerywany obrys oznacza niepotwierdzone objęcie punktu."],
    attention: ["DO SPRAWDZENIA", "Zdarzenia wymagające uwagi", "Komunikaty trwające, niepewne, nadal publikowane po przewidywanym terminie lub z niejednoznacznym czasem. Nie potwierdzają stanu zasilania."],
    next48: ["NAJBLIŻSZE 48 GODZIN", "Nadchodzące terminy zdarzeń", "Każdy poprawny przyszły przedział czasu jest pokazany osobno, także wtedy, gdy inny termin tego samego komunikatu już trwa."],
    planned: ["NAJBLIŻSZE DNI", "Planowane wyłączenia", "Wybierz dzień i sprawdź komunikaty oznaczone przez operatora jako planowane wyłączenia."],
    objects: ["KATALOG LOKALIZACJI", "Moje obiekty", "Przeglądaj lokalizacje, dodawaj je do ulubionych i sprawdzaj ich położenie na mapie."],
  };
  document.getElementById("viewEyebrow").textContent = titles[view][0];
  document.getElementById("viewTitle").textContent = titles[view][1];
  document.getElementById("viewDescription").textContent = titles[view][2];
  if (view === "objects") {
    renderObjects();
    setTimeout(() => locationMap.invalidateSize(), 0);
  } else {
    renderEvents();
    setTimeout(() => map.invalidateSize(), 0);
  }
  updateRoute(pendingDeepLink && !hasFocusedDeepLink ? pendingDeepLink : "");
}

function showConnection(ok, text) {
  const element = document.getElementById("connectionState");
  element.setAttribute('aria-label', text);
  element.classList.toggle("online", ok);
  element.classList.toggle("offline", !ok);
  element.querySelector("span:last-child").textContent = text;
}

function renderNotificationStatus(queue) {
  const queueLabel = document.getElementById("notificationStatus");
  if (queueLabel) {
    const count = queue.counts || {};
    queueLabel.textContent = queue.worker_state === "unavailable"
      ? "Powiadomienia: stan kolejki niedostępny."
      : queue.worker_state === "storage_error"
      ? "Powiadomienia: błąd zapisu lub odczytu kolejki."
      : !queue.enabled ? "Powiadomienia: wyłączone."
      : queue.worker_state === "stopped" ? "Powiadomienia: mechanizm wysyłki zatrzymany."
      : `Powiadomienia: oczekujące ${count.pending || 0}, wysyłane ${count.sending || 0}, przyjęte przez odbiorcę ${count.delivered || 0}, błędne ${count.failed || 0}, pominięte ${count.skipped || 0}.`;
  }
}

function renderSourceHealth(status) {
  const container = document.getElementById("sourceHealth");
  container.replaceChildren();
  for (const source of status.enabled_sources || []) {
    const health = status.sources?.[source] || {};
    const chip = createElement("article", `source-chip ${health.stale ? "error" : "ok"}`);
    chip.append(createElement("strong", "", health.label || source.toUpperCase()));
    if (status.demo) {
      chip.title = "Przykład formatu operatora w scenariuszu demonstracyjnym.";
      chip.append(createElement("span", "", "Dane scenariusza. Bez odczytu operatora."));
      container.append(chip);
      continue;
    }
    chip.title = `${health.error ? `${health.error}\n` : ""}Ostatni poprawny odczyt: ${formatDate(health.last_success_at)}`;
    const state = health.error
      ? (health.error_category === "maintenance" ? OsdEventView.sourceErrorText(health) : `Błąd odczytu: ${OsdEventView.sourceErrorText(health)}`)
      : health.stale
        ? "Dane wymagają odświeżenia"
      : health.last_success_at
        ? (health.running ? "Odczyt trwa" : "Dane aktualne")
        : "Oczekuje na pierwszy skan";
    chip.append(createElement("span", health.error ? "source-error" : "", state));
    if (health.error) {
      const details = createElement("details", "source-message");
      details.append(createElement("summary", "", "Komunikat operatora"));
      details.append(createElement("p", "", health.error));
      chip.append(details);
    }
    chip.append(createElement("small", "", `Ostatni poprawny odczyt: ${formatDate(health.last_success_at)}`));
    const publication = OsdEventView.publicationText({ ...health, source });
    if (publication) chip.append(createElement("small", "", publication));
    chip.dataset.source = source;
    container.append(chip);
  }
  updateSourceClocks();
}

function sourceClockState(health, now, available) {
  if (!available) return { time: "Termin niepotwierdzony", countdown: "Brak połączenia z aplikacją" };
  if (health.running) return { time: "Odczyt trwa", countdown: "Oczekiwanie na wynik operatora" };
  const next = health.next_scan_at ? Date.parse(health.next_scan_at) : NaN;
  if (!Number.isFinite(next)) return { time: "Oczekuje na pierwszy odczyt", countdown: "Termin ustala harmonogram serwera" };
  const remaining = Math.ceil(Math.max(0, next - now) / 1000);
  if (!remaining) return { time: "Oczekuje na rozpoczęcie", countdown: "Stan sprawdzamy przy odświeżeniu panelu" };
  const hours = Math.floor(remaining / 3600);
  const minutes = Math.floor((remaining % 3600) / 60);
  const seconds = remaining % 60;
  const duration = `${hours ? `${hours} godz. ` : ""}${minutes} min ${seconds} s`;
  const blocked = health.blocked_until ? Date.parse(health.blocked_until) : NaN;
  const label = blocked > now ? "Przerwa wskazana przez operatora" : health.error ? "Ponowna próba" : "Za";
  return { time: formatDate(health.next_scan_at), countdown: `${label}: ${duration}` };
}

function updateSourceClocks() {
  if (statusPayload?.demo) {
    document.getElementById("nextScanLabel").textContent = "Odnowienie scenariusza";
    document.getElementById("nextScanTime").textContent = "Co godzinę";
    document.getElementById("nextScanCountdown").textContent = "Rzeczywiste źródła są wyłączone";
    return;
  }
  const now = serverClockAtRead === null ? Date.now() : serverClockAtRead + performance.now() - clockReadAt;
  const sources = (statusPayload?.enabled_sources || []).map((source) => statusPayload.sources[source]);
  const state = fullScanClockState(sources, now, sourceScheduleAvailable);
  document.getElementById("nextScanLabel").textContent = state.label;
  document.getElementById("nextScanTime").textContent = state.time;
  document.getElementById("nextScanCountdown").textContent = state.countdown;
}

function fullScanClockState(sources, now, available) {
  const label = "Następny pełny skan";
  if (!available) return { label, ...sourceClockState({}, now, false) };
  if (!sources.length) return { label, time: "Brak aktywnych źródeł", countdown: "" };
  if (sources.some((source) => source.running)) return { label: "Skan operatorów", time: "Odczyt trwa", countdown: "Oczekiwanie na wyniki" };
  const times = sources.map((source) => Date.parse(source.next_scan_at || ""));
  if (times.some((time) => !Number.isFinite(time))) return { label, time: "Termin niepotwierdzony", countdown: "Oczekiwanie na harmonogram wszystkich źródeł" };
  const first = Math.min(...times);
  const last = Math.max(...times);
  if (last - first > 60000) {
    const next = sources[times.indexOf(first)];
    const state = sourceClockState(next, now, true);
    return { label: "Najbliższy odczyt API", ...state, countdown: `${state.countdown} · terminy operatorów są różne` };
  }
  const state = sourceClockState(sources[times.indexOf(last)], now, true);
  if (first <= now) return { label, time: "Oczekuje na rozpoczęcie", countdown: "Stan sprawdzamy przy odświeżeniu panelu" };
  return { label, ...state };
}

function applyMapPayload(payload) {
  geometries = payload.geometries || {};
  const locations = new Map(payload.locations.map(item => [String(item.object_id), {...item, id:String(item.object_id)}]));
  if (adminCsrf) {
    for (const item of adminCatalog) {
      const id = String(item.id);
      locations.set(id, {...locations.get(id), ...item, id});
    }
  }
  objects = [...locations.values()];
  events = payload.events.map((item) => ({
    ...item,
    event_key: item.event_id,
    status: item.source_status,
  }));
  const sources = Object.fromEntries(payload.sources.map((item) => [
    item.source,
    {
      ...item,
      stale: item.status !== "fresh",
      consecutive_errors: Number(item.consecutive_errors || 0),
    },
  ]));
  const dataStatus = describeDataStatus(payload);
  statusPayload = {
    demo: payload.demo === true,
    ok: payload.data_status === "fresh",
    last_success_at: payload.data_updated_at,
    error: dataStatus.message,
    noticeTitle: dataStatus.title,
    connectionText: dataStatus.connection,
    enabled_sources: payload.sources.map((item) => item.source),
    sources,
  };
}

async function fetchMapPayload() {
  await syncAdmin();
  const controller = new AbortController();
  const timeout = window.setTimeout(() => controller.abort(), 10000);
  try {
    const headers = mapEtag ? { "If-None-Match": mapEtag } : {};
    const response = await fetch("/api/v1/map", {
      cache: "no-cache",
      credentials: "omit",
      headers,
      signal: controller.signal,
    });
    const serverTime = Date.parse(response.headers.get("Date") || "");
    if (Number.isFinite(serverTime)) {
      serverClockAtRead = serverTime;
      clockReadAt = performance.now();
    }
    if (response.status === 304 && lastMapPayload) return lastMapPayload;
    if (!response.ok) {
      const error = new Error(`HTTP ${response.status}`);
      error.status = response.status;
      throw error;
    }
    const payload = await response.json();
    if (!payload || !Array.isArray(payload.locations) || !Array.isArray(payload.events) || !Array.isArray(payload.sources)) {
      throw new Error("Niepoprawny schemat odpowiedzi");
    }
    mapEtag = response.headers.get("ETag") || "";
    lastMapPayload = payload;
    return payload;
  } finally {
    window.clearTimeout(timeout);
  }
}

async function readJsonResponse(response, fallbackMessage) {
  const text = await response.text();
  let data = {};
  if (text) {
    try {
      data = JSON.parse(text);
    } catch {
      throw new Error(`Serwer zwrócił niepoprawną odpowiedź HTTP ${response.status}.`);
    }
  }
  if (!response.ok) throw new Error(OsdEventView.polishText(data.error?.message || data.error || fallbackMessage));
  return data;
}

async function fetchNotificationStatus() {
  const controller = new AbortController();
  const timeout = window.setTimeout(() => controller.abort(), 10000);
  try {
    const response = await fetch("/api/status", { cache: "no-store", signal: controller.signal });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const payload = await response.json();
    if (!payload.notifications || typeof payload.notifications !== "object") throw new Error("Brak stanu kolejki");
    return payload.notifications;
  } finally {
    window.clearTimeout(timeout);
  }
}

async function refreshData({ forceObjects = false } = {}) {
  if (mainRefreshPending) {
    pendingObjectRefresh ||= forceObjects;
    return;
  }
  mainRefreshPending = true;
  if (forceObjects) {
    mapEtag = "";
    lastMapPayload = null;
  }
  const requests = [["map", fetchMapPayload()], ["notifications", fetchNotificationStatus()]];
  const results = await Promise.allSettled(requests.map(([, promise]) => promise));
  const failures = [];
  results.forEach((result, index) => {
    const name = requests[index][0];
    if (result.status === "fulfilled") {
      if (name === "map") {
        applyMapPayload(result.value);
        sourceScheduleAvailable = true;
      }
      if (name === "notifications") renderNotificationStatus(result.value);
      return;
    }
    if (name === "notifications") {
      renderNotificationStatus({worker_state: "unavailable"});
      return;
    }
    if ([403, 409].includes(result.reason?.status)) {
      objects = []; events = []; geometries = {}; mapEtag = ""; lastMapPayload = null;
    }
    failures.push(`Mapa: ${result.reason?.name === "AbortError" ? "przekroczono czas oczekiwania" : result.reason?.message || "błąd"}`);
    sourceScheduleAvailable = false;
  });
  document.getElementById("loadingState").classList.add("hidden");
  if (statusPayload) {
    document.getElementById("lastUpdate").textContent = formatDate(statusPayload.last_success_at);
    renderSourceHealth(statusPayload);
    showConnection(statusPayload.ok && failures.length === 0, statusPayload.connectionText);
  } else {
    showConnection(false, "Brak połączenia");
  }
  const sourceError = statusPayload?.error ? `Źródła: ${statusPayload.error}` : "";
  const message = [...failures, sourceError].filter(Boolean).join(" | ");
  document.getElementById("errorBanner").classList.toggle("hidden", !message);
  document.getElementById("errorTitle").textContent = statusPayload?.noticeTitle || "Nie udało się pobrać danych.";
  document.getElementById("errorText").textContent = message;
  renderObjects();
  renderEvents();
  document.getElementById("mainContent").setAttribute("aria-busy", "false");
  document.getElementById("initialLoading").hidden = true;
  mainRefreshPending = false;
  secondsToRefresh = REFRESH_SECONDS;
  if (pendingObjectRefresh) {
    pendingObjectRefresh = false;
    await refreshData({ forceObjects: true });
  }
}

function resetObjectForm() {
  editingObjectId = "";
  document.getElementById("objectForm").reset();
  clearResolvedLocation();
  document.getElementById("objectFormEyebrow").textContent = "NOWA LOKALIZACJA";
  document.getElementById("objectFormTitle").textContent = "Dodaj obiekt do monitorowania";
  document.getElementById("saveObject").textContent = "Dodaj obiekt";
  document.getElementById("cancelObjectEdit").classList.add("hidden");
}

function startObjectEdit(object) {
  locationResolveRevision += 1;
  autoLocationName = null;
  autoLocationKeyword = null;
  editingObjectId = String(object.id);
  document.getElementById("objectName").value = object.name || "";
  document.getElementById("objectKeyword").value = object.keyword || "";
  document.getElementById("selectedCitySym").value = object.city_sym || "";
  document.getElementById("selectedStreetSym").value = object.street_sym || "";
  document.getElementById("objectHouseNumber").value = object.house_number || "";
  document.getElementById("objectGenerator").value = object.power_supply?.generator || "unknown";
  document.getElementById("objectSecondaryFeed").value = object.power_supply?.secondary_feed || "unknown";
  document.getElementById("objectPowerNotes").value = object.power_supply?.notes || "";
  document.getElementById("terytSearch").value = `${object.city || ""} - ${object.city_sym || ""}`;
  const streetInput = document.getElementById("streetSearch");
  streetInput.disabled = false;
  streetInput.value = object.street ? `${object.street} - ${object.street_sym || ""}` : "";
  if (object.has_coordinates) {
    setPickedCoordinates(Number(object.latitude), Number(object.longitude));
  } else {
    document.getElementById("objectLatitude").value = "";
    document.getElementById("objectLongitude").value = "";
    document.getElementById("coordinateText").textContent = "Nie wskazano";
  }
  document.getElementById("resolvedLocationName").textContent = object.street ? `${object.city}, ${object.street}` : object.city;
  document.getElementById("resolvedLocationCodes").textContent = `TERYT ${object.city_sym}${object.street_sym ? ` | ULIC ${object.street_sym}` : ""}`;
  document.getElementById("locationResult").classList.remove("hidden");
  document.getElementById("objectFormEyebrow").textContent = "EDYCJA LOKALIZACJI";
  document.getElementById("objectFormTitle").textContent = object.name || object.city;
  document.getElementById("saveObject").textContent = "Zapisz zmiany";
  document.getElementById("cancelObjectEdit").classList.remove("hidden");
  document.getElementById("formMessage").textContent = "Edytujesz istniejący obiekt. Jego identyfikator (ID) pozostanie bez zmian.";
  document.getElementById("objectForm").scrollIntoView({ behavior: "smooth", block: "start" });
}

function debounce(callback, wait = 250) {
  let timeout;
  return (...args) => {
    clearTimeout(timeout);
    timeout = setTimeout(() => callback(...args), wait);
  };
}

async function searchTeryt() {
  const input = document.getElementById("terytSearch");
  const suggestions = document.getElementById("terytSuggestions");
  const query = input.value.trim();
  document.getElementById("selectedCitySym").value = "";
  if (query.length < 2) {
    suggestions.classList.add("hidden");
    return;
  }
  const response = await fetch(`/api/teryt/search?q=${encodeURIComponent(query)}`);
  const data = await response.json();
  suggestions.replaceChildren();
  for (const item of data.results || []) {
    const button = createElement("button", "suggestion");
    button.type = "button";
    button.dataset.citySym = item.city_sym;
    button.dataset.city = item.city;
    button.append(createElement("strong", "", item.city));
    button.append(createElement("small", "", `TERYT ${item.city_sym} | woj. ${item.voivodeship_code}`));
    suggestions.append(button);
  }
  suggestions.classList.toggle("hidden", !suggestions.children.length);
}

async function searchStreets() {
  const citySym = document.getElementById("selectedCitySym").value;
  const input = document.getElementById("streetSearch");
  const suggestions = document.getElementById("streetSuggestions");
  document.getElementById("selectedStreetSym").value = "";
  if (!citySym) return;
  const response = await fetch(`/api/teryt/${citySym}/streets?q=${encodeURIComponent(input.value.trim())}`);
  const data = await response.json();
  suggestions.replaceChildren();
  for (const item of data.results || []) {
    const button = createElement("button", "suggestion");
    button.type = "button";
    button.dataset.streetSym = item.street_sym;
    button.dataset.street = item.street;
    button.append(createElement("strong", "", item.street));
    button.append(createElement("small", "", `ULIC ${item.street_sym}`));
    suggestions.append(button);
  }
  suggestions.classList.toggle("hidden", !suggestions.children.length);
}

function setPickedCoordinates(latitude, longitude, focusMap = true) {
  document.getElementById("objectLatitude").value = latitude.toFixed(6);
  document.getElementById("objectLongitude").value = longitude.toFixed(6);
  document.getElementById("coordinateText").textContent = `${latitude.toFixed(6)}, ${longitude.toFixed(6)}`;
  if (previewMarker) previewMarker.remove();
  previewMarker = L.marker([latitude, longitude], {alt: "Wybrany punkt", title: "Wybrany punkt"}).addTo(locationMap).bindPopup("Wybrany punkt");
  if (focusMap) {
    previewMarker.openPopup();
    locationMap.setView([latitude, longitude], 16);
  }
}

function clearLocationAddress() {
  for (const id of ["selectedCitySym", "selectedStreetSym", "terytSearch", "streetSearch", "objectHouseNumber"]) {
    document.getElementById(id).value = "";
  }
  document.getElementById("streetSearch").disabled = true;
  document.getElementById("terytSuggestions").classList.add("hidden");
  document.getElementById("streetSuggestions").classList.add("hidden");
  document.getElementById("locationResult").classList.add("hidden");
  const name = document.getElementById("objectName");
  const keyword = document.getElementById("objectKeyword");
  if (name.value === autoLocationName) name.value = "";
  if (keyword.value === autoLocationKeyword) keyword.value = "";
  autoLocationName = null;
  autoLocationKeyword = null;
}

function applyResolvedLocation(data, focusMap = true) {
  setPickedCoordinates(Number(data.latitude), Number(data.longitude), focusMap);
  document.getElementById("selectedCitySym").value = data.city_sym || "";
  document.getElementById("selectedStreetSym").value = data.street_sym || "";
  document.getElementById("terytSearch").value = `${data.city} - ${data.city_sym}`;
  const streetInput = document.getElementById("streetSearch");
  streetInput.disabled = false;
  streetInput.placeholder = "Wpisz nazwę ulicy";
  streetInput.value = data.street ? `${data.street} - ${data.street_sym}` : "";

  const result = document.getElementById("locationResult");
  result.classList.remove("hidden");
  document.getElementById("resolvedLocationName").textContent = data.street ? `${data.city}, ${data.street}` : data.city;
  document.getElementById("resolvedLocationCodes").textContent = `TERYT ${data.city_sym}${data.street_sym ? ` | ULIC ${data.street_sym}` : ""}`;

  const nameInput = document.getElementById("objectName");
  const keywordInput = document.getElementById("objectKeyword");
  if (!nameInput.value.trim()) {
    nameInput.value = data.street ? `${data.city} ${data.street}` : data.city;
    autoLocationName = nameInput.value;
  }
  if (!keywordInput.value.trim()) {
    keywordInput.value = data.street || data.city;
    autoLocationKeyword = keywordInput.value;
  }

  const message = document.getElementById("formMessage");
  message.className = data.warning ? "form-message warning" : "form-message success";
  message.textContent = OsdEventView.polishText(data.warning) || "Lokalizacja została rozpoznana. Możesz ją zapisać.";
}

async function fetchResolvedLocation(url, requestId = ++locationResolveRevision, focusMap = true) {
  const message = document.getElementById("formMessage");
  message.className = "form-message";
  message.textContent = "Sprawdzanie adresu i danych TERYT…";
  const response = await fetch(url);
  const responseText = await response.text();
  if (requestId !== locationResolveRevision) return;
  let data;
  try {
    data = JSON.parse(responseText);
  } catch {
    throw new Error(
      response.status === 404
        ? "Część serwerowa jest w starszej wersji. Uruchom ponownie aplikację w Pythonie."
        : `Serwer zwrócił niepoprawną odpowiedź HTTP ${response.status}.`,
    );
  }
  if (!response.ok) {
    if (data.resolution_status === "requires_review" && data.osm_result) {
      const point = validMapPoint(data.osm_result.latitude, data.osm_result.longitude);
      if (point) {
        document.getElementById("selectedCitySym").value = "";
        document.getElementById("selectedStreetSym").value = "";
        document.getElementById("terytSearch").value = "";
        document.getElementById("streetSearch").value = "";
        document.getElementById("streetSearch").disabled = true;
        document.getElementById("objectHouseNumber").value = "";
        setPickedCoordinates(...point, focusMap);
        document.getElementById("locationResult").classList.remove("hidden");
        document.getElementById("resolvedLocationName").textContent = data.osm_result.display_name || "Punkt rozpoznany przez OpenStreetMap";
        document.getElementById("resolvedLocationCodes").textContent = "Wybierz miejscowość w rejestrze TERYT i adres. Wynik OpenStreetMap ma charakter pomocniczy.";
        message.className = "form-message warning";
        message.textContent = "Rozpoznano punkt na mapie. Wybierz miejscowość lub ulicę, aby zapisać właściwy adres. " + (data.error || "");
        return;
      }
    }
    throw new Error(OsdEventView.polishText(data.error || "Nie udało się rozpoznać lokalizacji."));
  }
  applyResolvedLocation(data, focusMap);
}

async function resolvePickedPoint(latitude, longitude) {
  const requestId = ++locationResolveRevision;
  clearLocationAddress();
  setPickedCoordinates(latitude, longitude, false);
  try {
    await fetchResolvedLocation(`/api/location/reverse?lat=${encodeURIComponent(latitude)}&lon=${encodeURIComponent(longitude)}`, requestId, false);
  } catch (error) {
    if (requestId !== locationResolveRevision) return;
    const message = document.getElementById("formMessage");
    message.className = "form-message error";
    message.textContent = `Punkt został wybrany. ${error.message} Wybierz TERYT i ulicę ręcznie albo kliknij inne miejsce na mapie.`;
  }
}

async function resolveLocationQuery() {
  const input = document.getElementById("locationQuery");
  const button = document.getElementById("resolveLocation");
  const query = input.value.trim();
  if (!query || button.disabled) return;
  const requestId = ++locationResolveRevision;
  clearLocationAddress();
  button.disabled = true;
  button.textContent = "Wyszukiwanie…";
  try {
    await fetchResolvedLocation(`/api/location/resolve?q=${encodeURIComponent(query)}`, requestId);
  } catch (error) {
    if (requestId !== locationResolveRevision) return;
    const message = document.getElementById("formMessage");
    message.className = "form-message error";
    message.textContent = error.message;
  } finally {
    button.disabled = false;
    button.textContent = "Szukaj";
  }
}

function clearResolvedLocation() {
  locationResolveRevision += 1;
  clearLocationAddress();
  document.getElementById("selectedCitySym").value = "";
  document.getElementById("selectedStreetSym").value = "";
  document.getElementById("terytSearch").value = "";
  document.getElementById("streetSearch").value = "";
  document.getElementById("streetSearch").disabled = true;
  document.getElementById("objectLatitude").value = "";
  document.getElementById("objectLongitude").value = "";
  document.getElementById("coordinateText").textContent = "Nie wskazano";
  document.getElementById("locationResult").classList.add("hidden");
  document.getElementById("formMessage").textContent = "";
  if (previewMarker) {
    previewMarker.remove();
    previewMarker = null;
  }
}

function refreshTick() {
  if (document.hidden) return;
  secondsToRefresh = Math.max(0, secondsToRefresh - 1);
  document.getElementById("nextRefresh").textContent = `Odświeżenie panelu za ${secondsToRefresh} s`;
  updateSourceClocks();
  if (secondsToRefresh <= 0) refreshData();
}

document.querySelector(".view-navigation").addEventListener("click", (event) => {
  const button = event.target.closest("button[data-view]");
  if (button) setView(button.dataset.view);
});

document.getElementById("dateStrip").addEventListener("click", (event) => {
  const button = event.target.closest("button[data-date]");
  if (!button) return;
  selectedDate = button.dataset.date;
  renderDateStrip();
  renderEvents();
});

document.getElementById("eventSearch").addEventListener("input", renderEvents);
document.getElementById("clearEventSearch").addEventListener("click", () => {
  document.getElementById("eventSearch").value = "";
  renderEvents();
});
document.getElementById("objectSearch").addEventListener("input", renderObjects);
document.getElementById("objectName").addEventListener("input", () => { autoLocationName = null; });
document.getElementById("objectKeyword").addEventListener("input", () => { autoLocationKeyword = null; });
document.getElementById("matchScope").addEventListener("change", renderEvents);
document.getElementById("showOperatorPoints").addEventListener("change", (event) => {
  showOperatorPoints = event.target.checked;
  renderMap(visibleEvents());
});
document.getElementById("terytSearch").addEventListener("input", debounce(searchTeryt));
document.getElementById("terytSearch").addEventListener("invalid", event => {
  event.target.closest("details").open = true;
  event.target.setCustomValidity("Wybierz miejscowość w rejestrze TERYT.");
});
document.getElementById("terytSearch").addEventListener("input", event => event.target.setCustomValidity(""));
document.getElementById("streetSearch").addEventListener("input", debounce(searchStreets));
document.getElementById("resolveLocation").addEventListener("click", resolveLocationQuery);
document.getElementById("locationQuery").addEventListener("keydown", (event) => {
  if (event.key === "Enter") {
    event.preventDefault();
    resolveLocationQuery();
  }
});
document.getElementById("clearLocation").addEventListener("click", clearResolvedLocation);
document.getElementById("cancelObjectEdit").addEventListener("click", resetObjectForm);

document.getElementById("terytSuggestions").addEventListener("click", (event) => {
  const button = event.target.closest("button[data-city-sym]");
  if (!button) return;
  locationResolveRevision += 1;
  document.getElementById("selectedCitySym").value = button.dataset.citySym;
  document.getElementById("terytSearch").value = `${button.dataset.city} - ${button.dataset.citySym}`;
  document.getElementById("terytSuggestions").classList.add("hidden");
  const streetInput = document.getElementById("streetSearch");
  streetInput.disabled = false;
  streetInput.placeholder = "Wpisz nazwę ulicy";
});

document.getElementById("streetSuggestions").addEventListener("click", (event) => {
  const button = event.target.closest("button[data-street-sym]");
  if (!button) return;
  locationResolveRevision += 1;
  document.getElementById("selectedStreetSym").value = button.dataset.streetSym;
  document.getElementById("streetSearch").value = `${button.dataset.street} - ${button.dataset.streetSym}`;
  document.getElementById("streetSuggestions").classList.add("hidden");
});

document.getElementById("objectForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  const message = document.getElementById("formMessage");
  const payload = {
    name: document.getElementById("objectName").value.trim(),
    keyword: document.getElementById("objectKeyword").value.trim(),
    city_sym: document.getElementById("selectedCitySym").value,
    street_sym: document.getElementById("selectedStreetSym").value,
    house_number: document.getElementById("objectHouseNumber").value.trim() || null,
    power_supply: {
      generator: document.getElementById("objectGenerator").value,
      secondary_feed: document.getElementById("objectSecondaryFeed").value,
      notes: document.getElementById("objectPowerNotes").value.trim(),
    },
    latitude: document.getElementById("objectLatitude").value || null,
    longitude: document.getElementById("objectLongitude").value || null,
  };
  if (!payload.city_sym) {
    message.className = "form-message error";
    message.textContent = "Wybierz miejscowość z listy TERYT.";
    return;
  }
  if (!editingObjectId && (!payload.latitude || !payload.longitude)) {
    message.className = "form-message error";
    message.textContent = "Najpierw wyszukaj adres albo kliknij punkt na mapie.";
    return;
  }
  if (editingObjectId) {
    const original = objects.find((item) => String(item.id) === editingObjectId);
    for (const field of ["name", "keyword", "city_sym", "street_sym", "house_number", "latitude", "longitude"]) {
      const submitted = payload[field];
      const saved = original?.[field];
      const coordinate = field === "latitude" || field === "longitude";
      const equal = coordinate
        ? (submitted == null && saved == null) || (submitted != null && saved != null && Number(submitted) === Number(saved))
        : String(submitted ?? "") === String(saved ?? "");
      if (equal) delete payload[field];
    }
  }
  let data;
  try {
    const response = await adminFetch(
      editingObjectId ? `/api/admin/objects/${encodeURIComponent(editingObjectId)}` : "/api/admin/objects",
      {
      method: editingObjectId ? "PATCH" : "POST",
      redirect: "manual",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
      },
    );
    data = await readJsonResponse(response, editingObjectId ? "Nie udało się zmienić obiektu." : "Nie udało się dodać obiektu.");
  } catch (error) {
    message.className = "form-message error";
    message.textContent = error.message || "Nie udało się dodać obiektu.";
    return;
  }
  const wasEditing = Boolean(editingObjectId);
  resetObjectForm();
  message.className = "form-message success";
  message.textContent = wasEditing ? "Zmiany zostały zapisane." : "Obiekt został dodany.";
  await refreshData({ forceObjects: true });
  setView("objects");
});

document.getElementById("objectList").addEventListener("click", async (event) => {
  const geoButton = event.target.closest("button.object-geoportal-button");
  if (geoButton) {
    const id = String(geoButton.dataset.objectId);
    busyGeoportalReviews.add(id);
    openGeoportalReviews.add(id);
    geoportalReviewErrors.delete(id);
    const review = geoButton.closest("article").querySelector(".geoportal-review");
    const output = review.querySelector(".geoportal-review-body");
    geoButton.disabled = true;
    geoButton.textContent = "Porównywanie…";
    try {
      const response = await adminFetch("/api/location/check", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ object_id: geoButton.dataset.objectId }) });
      const data = await readJsonResponse(response, "Nie udało się porównać danych z Geoportalem.");
      review.dataset.loaded = "yes";
      renderLocationReview(output, data.item);
      review.open = true;
    } catch (error) {
      geoportalReviewErrors.set(id, error.message);
      review.dataset.loaded = "yes";
      output.replaceChildren(createElement("p", "location-warning", error.message));
      try {
        const previous = await fetch(`/api/v1/objects/${encodeURIComponent(geoButton.dataset.objectId)}/location-review`);
        const data = await readJsonResponse(previous, "Nie udało się odczytać poprzedniego wyniku.");
        const old = createElement("div", "");
        renderLocationReview(old, data.item);
        output.append(old);
      } catch (_) { /* Pierwotny błąd pozostaje widoczny. */ }
      review.open = true;
    } finally {
      busyGeoportalReviews.delete(id);
      geoButton.disabled = false;
      geoButton.textContent = "Porównaj z Geoportalem";
      renderObjects();
    }
    return;
  }
  const mapButton = event.target.closest("button.object-map-button");
  if (mapButton) {
    const object = objects.find((item) => String(item.id) === mapButton.dataset.objectId);
    if (object) {
      setView("current");
      focusObject(object.id);
    }
    return;
  }

  const favoriteButton = event.target.closest("button.object-favorite-button");
  if (favoriteButton) {
    const id = String(favoriteButton.dataset.objectId);
    if (favoriteIds.includes(id)) {
      saveFavorites(favoriteIds.filter((item) => item !== id));
    } else {
      saveFavorites([...favoriteIds, id]);
    }
    renderObjects();
    return;
  }

  const editButton = event.target.closest("button.object-edit-button");
  if (editButton) {
    const object = objects.find((item) => String(item.id) === editButton.dataset.objectId);
    if (object) startObjectEdit(object);
    return;
  }

  const deleteButton = event.target.closest("button.object-delete-button");
  if (!deleteButton) return;
  if (!window.confirm(`Usunąć obiekt "${deleteButton.dataset.objectName}"?`)) return;
  const message = document.getElementById("formMessage");
  try {
    const response = await adminFetch(`/api/admin/objects/${encodeURIComponent(deleteButton.dataset.objectId)}`, {
      method: "DELETE",
      redirect: "manual",
    });
    await readJsonResponse(response, "Nie udało się usunąć obiektu.");
    message.className = "form-message success";
    message.textContent = "Obiekt został usunięty.";
    await refreshData({ forceObjects: true });
  } catch (error) {
    message.className = "form-message error";
    message.textContent = error.message || "Nie udało się usunąć obiektu.";
  }
});

setInterval(refreshTick, 1000);
document.addEventListener("visibilitychange", () => {
  if (!document.hidden) {
    updateSourceClocks();
    refreshData();
  }
});

let adminCsrf = "";
let adminCatalog = [];
let adminRevision = '';
let adminSessionEpoch = 0;
function updateAdminControls() {
  const authenticated = Boolean(adminCsrf);
  document.body.classList.toggle('admin-authenticated', authenticated);
  document.getElementById('objectForm').classList.toggle('hidden', !authenticated);
  document.getElementById('adminAccess').hidden = authenticated;
  document.getElementById('adminLogout').hidden = !authenticated;
  document.getElementById('catalogAdminActions').hidden = !authenticated;
}
function clearPrivateObjects() {
  adminSessionEpoch += 1;
  adminCsrf = ''; adminCatalog = []; adminRevision = '';
  updateAdminControls();
  window.dispatchEvent(new CustomEvent('osd-admin-session', {detail:false}));
  if (lastMapPayload) applyMapPayload(lastMapPayload);
  else { objects = []; events = []; geometries = {}; }
  renderObjects(); renderEvents();
}
async function syncAdmin() {
  const epoch = adminSessionEpoch;
  const hadSession = Boolean(adminCsrf);
  let csrf = '', catalog = {};
  try {
    const response = await fetch('/api/admin/session', {cache:'no-store'});
    if (!response.ok) throw new Error('Nie udało się sprawdzić sesji.');
    const session = await response.json();
    csrf = session.authenticated ? (session.csrf_token || '') : '';
    if (csrf) {
      const response = await fetch('/api/admin/objects', {cache:'no-store'});
      if (!response.ok) throw new Error('Nie udało się pobrać pełnego katalogu.');
      catalog = await response.json();
      if (!Array.isArray(catalog.objects)) throw new Error('Niepoprawna odpowiedź katalogu.');
    }
  } catch { csrf = ''; catalog = {}; }
  // Odpowiedź rozpoczęta przed wylogowaniem nie może przywrócić prywatnych punktów.
  if (epoch !== adminSessionEpoch) return;
  adminCsrf = csrf;
  adminCatalog = catalog.objects || [];
  adminRevision = catalog.catalog_revision || '';
  if (hadSession && !adminCsrf) {
    resetObjectForm();
    clearPrivateObjects();
  }
  updateAdminControls();
  if (adminCsrf) document.getElementById('adminMessage').textContent = 'Tryb edycji jest aktywny.';
  window.dispatchEvent(new CustomEvent('osd-admin-session', {detail:Boolean(adminCsrf)}));
}
async function adminFetch(url, options = {}) {
  const response = await fetch(url, {...options, headers:{...options.headers, 'X-CSRF-Token':adminCsrf, 'X-Catalog-Revision':adminRevision}});
  if (response.status === 401) {
    clearPrivateObjects();
    document.getElementById('objectForm').classList.add('hidden');
    document.getElementById('adminMessage').textContent = 'Sesja wygasła. Zaloguj się ponownie przed zapisem.';
  }
  return response;
}
document.getElementById('objectForm').classList.add('hidden');
document.getElementById('adminAccess').addEventListener('click', () => {
  const dialog = document.getElementById('adminDialog');
  document.getElementById('adminLogin').reset();
  document.getElementById('adminMessage').className = 'form-message';
  document.getElementById('adminMessage').textContent = 'Wpisz hasło administratora.';
  dialog.showModal();
  document.getElementById('adminPassword').focus();
});
document.getElementById('adminDialogClose').addEventListener('click', () => document.getElementById('adminDialog').close());
document.getElementById('adminDialog').addEventListener('close', () => {
  document.getElementById('adminPassword').value = '';
});
document.getElementById('adminLogin').addEventListener('submit', async event => {
  event.preventDefault();
  const input = document.getElementById('adminPassword');
  const submit = document.getElementById('adminLoginSubmit');
  const message = document.getElementById('adminMessage');
  submit.disabled = true;
  message.className = 'form-message';
  message.textContent = 'Logowanie…';
  try {
    const response = await fetch('/api/admin/login', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({password:input.value})});
    input.value = '';
    await readJsonResponse(response, 'Nie udało się zalogować.');
    await refreshData({forceObjects:true});
    if (!adminCsrf) throw new Error('Nie udało się potwierdzić sesji i odczytać katalogu. Spróbuj ponownie.');
    document.getElementById('adminDialog').close();
    document.getElementById('adminLogout').focus();
  } catch (error) { input.value = ''; message.className = 'form-message error'; message.textContent = error.message; input.focus(); }
  finally { submit.disabled = false; }
});
document.getElementById('adminLogout').addEventListener('click', async () => {
  const button = document.getElementById('adminLogout');
  button.disabled = true;
  try {
    const response = await adminFetch('/api/admin/logout', {method:'POST'});
    await readJsonResponse(response, 'Nie udało się wylogować. Spróbuj ponownie.');
    clearPrivateObjects(); resetObjectForm();
    document.getElementById('adminMessage').textContent = 'Wylogowano. Odczyt publiczny.';
    await refreshData({forceObjects:true});
  } catch (error) {
    document.getElementById('errorTitle').textContent = 'Nie udało się wylogować.';
    document.getElementById('errorText').textContent = error.message;
    document.getElementById('errorBanner').classList.remove('hidden');
  } finally { button.disabled = false; }
});
window.addEventListener('pagehide', () => {
  document.getElementById('adminPassword').value = '';
  clearPrivateObjects(); resetObjectForm();
  document.getElementById('objectForm').classList.add('hidden');
});
window.addEventListener('pageshow', event => { if (event.persisted) refreshData({forceObjects:true}); });
initializeMap();
initializeLocationMap();
parseInitialRoute();
renderDateStrip();
setView(activeView);
refreshData({ forceObjects: true });
