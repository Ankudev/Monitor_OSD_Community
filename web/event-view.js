/* Wspólne dane prezentacji komunikatu: mapa, lista zdarzeń i SNUT. */
(() => {
  const types = {outage: "Awaria", planned_outage: "Planowane wyłączenie", mass_outage: "Awaria masowa", unknown: "Nieokreślony typ"};
  const phases = {upcoming: "Termin w przyszłości", ongoing: "Trwa według harmonogramu", overdue: "Po przewidywanym terminie; operator nadal publikuje komunikat", closed: "Zakończony według operatora", unknown: "Termin nieznany"};
  const statuses = {active: "Aktywny", cancelled: "Odwołany", resolved: "Zakończony", unknown: "Brak danych"};
  const dataStatuses = {fresh: "dane aktualne", partial: "dane częściowe", stale: "dane nieaktualne", unavailable: "dane niedostępne", unknown: "stan nieznany"};
  // Tylko teksty własne aplikacji. Nie stosować do opisów operatorów ani uwag użytkownika.
  const ownText = [
    ["konkretnego adresu/punktu", "konkretnego adresu lub punktu"],
    ["oczekuje na naturalny skan", "oczekuje na automatyczny skan"],
    ["Brak aktualnego dowodu punktu GPS.", "Brak aktualnego potwierdzenia położenia GPS."],
    ["nieprawidłowy city_sym", "nieprawidłowy kod miejscowości (city_sym)"],
    ["Uwagi do zasilania muszą być tekstem do 1000 znaków", "Uwagi dotyczące zasilania muszą być tekstem o długości do 1000 znaków"],
    ["cache geokodera", "pamięci podręcznej geokodera"],
    ["OSM odpowiedziało, ale nie udało się jednoznacznie wybrać TERYT.", "Serwis OpenStreetMap odpowiedział, ale nie udało się jednoznacznie wybrać miejscowości w rejestrze TERYT."],
    ["powód nie podany", "nie podano powodu"],
  ];

  function polishText(value) {
    let text = String(value || "");
    for (const [original, edited] of ownText) text = text.replaceAll(original, edited);
    return text;
  }

  function word(value, forms) {
    if (value === 1) return forms[0];
    return Number.isInteger(value) && value % 10 >= 2 && value % 10 <= 4 && !(value % 100 >= 12 && value % 100 <= 14) ? forms[1] : forms[2];
  }

  function count(value, forms) {
    return `${Number.isFinite(value) ? new Intl.NumberFormat("pl-PL").format(value) : "—"} ${word(value, forms)}`;
  }

  function dataStatus(value) {
    return dataStatuses[value] || dataStatuses.unknown;
  }

  function sourceErrorText(health) {
    if (!health?.error) return "";
    if (health.error_category === "maintenance" || health.error.startsWith("SourceMaintenanceError:")) {
      return "Strona operatora niedostępna (prace serwisowe)";
    }
    return health.error;
  }

  function pointConfirmed(event) {
    return event?.match_evidence?.version === 2 && event.match_evidence.scope === "confirmed_point"
      && event.match_evidence.point_confirmed === true && !event.location_review_required && !event.verification_pending;
  }

  function eventLabel(event) {
    if (event.verification_pending) return "Brak komunikatu w ostatnim odczycie - trwa weryfikacja";
    if (event.status === "resolved") return "Komunikat zakończony według operatora";
    if (event.status === "cancelled") return "Komunikat odwołany";
    const type = types[event.type] || types.unknown;
    return pointConfirmed(event) ? type : `${type} - niepotwierdzone objęcie punktu`;
  }

  function formatDate(value) {
    if (!value) return "Brak danych";
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return String(value);
    return new Intl.DateTimeFormat("pl-PL", {dateStyle: "medium", timeStyle: "short", timeZone: "Europe/Warsaw"}).format(date);
  }

  function addressText(event) {
    const parts = [];
    for (const address of event.matched_locations || []) {
      if (address.text) { parts.push(address.text); continue; }
      if (address.streetName || address.houseNumbers) {
        parts.push(`${address.streetName || ""} ${address.houseNumbers || ""}`.trim());
        continue;
      }
      const street = address.teryt?.streetName || "cała miejscowość";
      const numbers = (Array.isArray(address.numbers) ? address.numbers.join(", ") : typeof address.numbers === "string" ? address.numbers : "").trim();
      parts.push(`${street}${numbers ? ` ${numbers}` : ""}`);
    }
    return [...new Set(parts)].join("; ") || description(event) || "Brak szczegółowego adresu";
  }

  function description(event) {
    return String(event.description || event.source_event?.raw?.description || event.pge_event?.description || "").trim();
  }

  function powerSupplyLines(object) {
    if (!object?.power_supply) return [];
    const power = object?.power_supply || {};
    const generator = {unknown: "Nie uzupełniono", none: "Brak agregatu", manual: "Agregat uruchamiany ręcznie", szr: "Agregat z układem SZR"};
    const feed = {unknown: "Nie uzupełniono", none: "Brak dodatkowego toru", manual: "Tor przełączany ręcznie", szr: "Tor z układem SZR"};
    return [`Agregat: ${generator[power.generator] || generator.unknown}`, `Dodatkowy tor zasilania: ${feed[power.secondary_feed] || feed.unknown}`,
      ...(power.notes ? [`Uwagi eksploatacyjne: ${power.notes}`] : [])];
  }

  function locationWarning(object) {
    const audit = object?.location_audit;
    if (!audit || !Object.keys(audit).length) return "Brak zapisanego audytu lokalizacji.";
    if (audit.review_reason === "location_changed") return "Lokalizacja została zmieniona. Poprzedni audyt GPS i TERYT jest nieważny; wymagany przegląd.";
    return audit.requires_review || audit.status !== "verified" ? "Lokalizacja lub przypisanie TERYT wymaga sprawdzenia." : "";
  }

  function safeUrl(value) {
    try {
      const url = new URL(value || "");
      return ["http:", "https:"].includes(url.protocol) ? url.href : "";
    } catch { return ""; }
  }

  function periodText(window) {
    const start = window.start_at ? `Początek: ${formatDate(window.start_at)}` : "Początek niepodany";
    const stop = window.stop_at ? `koniec: ${formatDate(window.stop_at)}` : "koniec niepodany";
    const minutes = window.start_at && window.stop_at ? Math.round((new Date(window.stop_at) - new Date(window.start_at)) / 60000) : NaN;
    let duration = "";
    if (Number.isFinite(minutes) && minutes > 0) {
      const hours = Math.floor(minutes / 60), rest = minutes % 60;
      duration = hours ? `${hours} godz.${rest ? ` ${rest} min` : ""}` : `${minutes} min`;
    }
    return `${start}; ${stop}${duration ? ` · przewidywany czas trwania: ${duration}` : ""}`;
  }

  function summary(event, object = {}, selectedWindow = null) {
    object = object || {};
    let windows = selectedWindow ? [selectedWindow] : (event.time_windows || []).filter(window => window.valid);
    if (!windows.length) windows = [{start_at: event.start_at || event.pge_event?.startAt, stop_at: event.stop_at || event.pge_event?.stopAt}];
    const address = addressText(event);
    const operatorDescription = description(event);
    return {
      title: eventLabel(event),
      operator: event.operator || String(event.source || "OSD").toUpperCase(),
      address,
      description: operatorDescription,
      hasAdditionalDescription: Boolean(operatorDescription && operatorDescription !== address),
      reason: polishText(event.match_evidence?.reason || "Brak dowodu objęcia punktu."),
      confirmed: pointConfirmed(event),
      periods: [...new Set(windows.map(periodText))],
      phase: phases[event.time_phase] || phases.unknown,
      status: statuses[event.source_status || event.status || "unknown"] || "Inny stan",
      objectName: event.configured_name || object.name || event.city || String(event.object_id || "Nieznany obiekt"),
      objectCity: object.city || event.city || "Brak miejscowości",
      objectAddress: [object.street, object.house_number].filter(Boolean).join(" "),
      teryt: `TERYT (SIMC): ${object.city_sym || event.city_sym || "Brak danych"}${object.street_sym || event.street_sym ? `; ULIC: ${object.street_sym || event.street_sym}` : ""}`,
      powerLines: powerSupplyLines(object),
      locationWarning: locationWarning(object),
      sourceUrl: safeUrl(event.source_url),
    };
  }
  function publicationText(health) {
    if (health?.source !== "energa") return "";
    if (!health.publication_time_status) return "Aktualizacja dokumentu operatora: oczekuje na poprawny odczyt z metadanymi.";
    const value = health.publication_updated_at;
    if (health.publication_time_status === "available" && value && Number.isFinite(Date.parse(value))) {
      return `Aktualizacja dokumentu operatora: ${formatDate(value)}. Data dokumentu jest niezależna od czasu pobrania.`;
    }
    return "Operator nie podał poprawnej daty aktualizacji dokumentu.";
  }
  globalThis.OsdEventView = {summary, addressText, description, eventLabel, formatDate, pointConfirmed, powerSupplyLines, locationWarning, safeUrl, word, count, dataStatus, sourceErrorText, polishText, publicationText};
})();
