/* Wspólna prezentacja tekstowa dla panelu, mapy i SNUT. */
(() => {
  const levels = {high: "wysoka", medium: "ograniczona", low: "niska", unknown: "nieustalona"};
  const limitations = {
    location_review_required: "Lokalizacja wymaga przeglądu.",
    location_not_audited: "Brak aktualnego audytu GPS.",
    address_not_verified: "Nie zweryfikowano adresu do porównania numerów.",
    house_number_missing: "Nie zapisano numeru domu.",
    numbers_unparsed: "Wykaz adresów lub numerów jest niejednoznaczny.",
    locality_only: "Zgodność miejscowości nie potwierdza konkretnego punktu.",
    street_only: "Zgodność ulicy nie potwierdza numeru domu.",
    proximity_only: "Bliskość punktu nie określa obszaru wyłączenia.",
    radius_extent_unverified: "Nie potwierdzono, że promień operatora określa zasięg wyłączenia.",
    address_geometry_conflict: "Adres i geometria są sprzeczne.",
    legacy_explanation_missing: "Starszy wpis nie zawiera szczegółowego wyjaśnienia.",
    details_truncated: "Zakres szczegółów ograniczono; pełny rekord znajduje się w danych operatora.",
    variant_bases_differ: "Warianty komunikatu mają różne podstawy dopasowania.",
  };
  function rows(event, {includeReason = true} = {}) {
    const value = event?.match_explanation;
    if (value?.version !== 1) return [limitations.legacy_explanation_missing];
    const result = [
      ...(includeReason ? [globalThis.OsdEventView?.polishText(value.reason) || value.reason || "Brak uzasadnienia dopasowania."] : []),
      `Ocena dowodu dopasowania: ${levels[value.confidence] || levels.unknown}. To nie jest pomiar napięcia.`,
    ];
    const d = value.details || {};
    if (d.object_city_sym) result.push(`Miejscowość obiektu (SIMC): ${d.object_city_sym}${d.object_street_sym ? `; ulica (ULIC): ${d.object_street_sym}` : ""}.`);
    for (const row of d.address?.rows || []) {
      if (!row.numbers) continue;
      const original = Array.isArray(row.numbers.original) ? row.numbers.original.join(", ") : String(row.numbers.original || "");
      const labels = {matches:"zgodność",does_not_match:"wykluczenie",unknown:"brak rozstrzygnięcia"};
      result.push(`Numer domu: ${row.object_house_number || "brak"}; wykaz operatora: ${original}; wynik: ${labels[row.result] || labels.unknown}.`);
    }
    if (d.address?.unknown_row_count) result.push(`Nierozstrzygnięte wiersze adresów: ${d.address.unknown_row_count}.`);
    const p = d.proximity;
    if (p && Number.isFinite(p.distance_m) && Number.isFinite(p.limit_m)) result.push(`Odległość od punktu operatora: ${Math.round(p.distance_m)} m; limit: ${p.limit_m} m (${p.limit_source === "operator_radius" ? "promień operatora" : "ustawienie aplikacji"}).`);
    const g = d.geometry;
    if (g) {
      const names = {inside:"wewnątrz obszaru",outside:"poza obszarem",hole:"w wewnętrznym wyłączeniu obszaru (dziurze)",outer_boundary:"na zewnętrznym brzegu",hole_boundary:"na granicy wewnętrznego wyłączenia obszaru"};
      result.push(`Położenie punktu względem obszaru: ${names[g.result] || "brak rozstrzygnięcia"}${Number.isInteger(g.polygon_index) ? `; część ${g.polygon_index + 1}` : ""}.`);
    }
    if (d.text?.fragment) result.push(`Porównany tekst: ${d.text.fragment}`);
    if (d.county_code) result.push(`Porównany kod powiatu: ${d.county_code}.`);
    if (d.variants) result.push(`Dopasowane warianty komunikatu: ${d.variants.length}.`);
    for (const code of value.limitations || []) result.push(limitations[code] || "Dodatkowe ograniczenie wymagające sprawdzenia.");
    return result;
  }
  globalThis.OsdMatchingView = {rows};
})();
