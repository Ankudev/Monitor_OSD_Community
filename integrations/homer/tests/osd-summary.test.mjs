import assert from "node:assert/strict";
import test from "node:test";
import {
  attentionEvents,
  eventTimeLabel,
  eventTypeLabel,
  eventUrl,
  isInactive,
  nextWindows,
  plannedWindows,
  sourceUrl,
  sourceRows,
  summarizeEvents,
} from "../osd-summary.js";

test("classifies every event once", () => {
  const events = [
    { time_phase: "ongoing" },
    { time_phase: "overdue" },
    { time_phase: "upcoming", type: "outage" },
    { time_phase: "unknown" },
    { time_phase: "closed" },
    { status: "cancelled", time_phase: "ongoing" },
    { status: "resolved", time_phase: "upcoming" },
    { revoked: true, time_phase: "ongoing" },
    { verification_pending: true, time_phase: "ongoing" },
  ];

  assert.deepEqual(summarizeEvents(events), {
    total: 9,
    confirmed: 0,
    candidates: 9,
    ongoing: 1,
    overdue: 1,
    upcoming: 1,
    unknown: 1,
    inactive: 5,
  });
});

test("does not deduplicate records matched to multiple objects", () => {
  const events = [
    { source_event_id: "same", object_id: "one", time_phase: "ongoing" },
    { source_event_id: "same", object_id: "two", time_phase: "ongoing" },
  ];
  assert.equal(summarizeEvents(events).ongoing, 2);
});

test("accepts missing event data", () => {
  assert.deepEqual(summarizeEvents(null), {
    total: 0,
    confirmed: 0,
    candidates: 0,
    ongoing: 0,
    overdue: 0,
    upcoming: 0,
    unknown: 0,
    inactive: 0,
  });
  assert.equal(isInactive(null), false);
});

test("orders only configured sources", () => {
  const rows = sourceRows({
    sources: {
      stoen: { label: "Stoen" },
      pge: { label: "PGE" },
      custom: { label: "Custom" },
    },
  });
  assert.deepEqual(rows.map((row) => row.source), ["pge", "stoen"]);
});

test("selects attention events by operational priority", () => {
  const events = [
    { object_id: "4", time_phase: "unknown" },
    { object_id: "3", time_phase: "overdue" },
    { object_id: "2", verification_pending: true, time_phase: "ongoing" },
    { object_id: "1", time_phase: "ongoing" },
    { object_id: "5", status: "resolved", time_phase: "ongoing" },
  ];

  assert.deepEqual(
    attentionEvents(events).map((row) => row.reason),
    ["Trwa", "Weryfikacja", "Po przewidywanym terminie", "Nieznany czas"],
  );
});

test("uses explicit type and time labels", () => {
  assert.equal(eventTypeLabel({ type: "planned_outage" }), "Planowane wyłączenie");
  assert.equal(eventTypeLabel({ type: "outage" }), "Awaria");
  assert.equal(eventTypeLabel({ type: "mass_outage" }), "Awaria masowa");
  assert.equal(eventTypeLabel({ type: "future_surprise" }), "Nieokreślony typ");
  assert.equal(eventTimeLabel({ time_phase: "ongoing" }), "Trwa według harmonogramu");
  assert.equal(eventTimeLabel({ time_phase: "overdue" }), "Po przewidywanym terminie");
});

test("builds next 48 hour rows from every valid time window", () => {
  const now = new Date("2026-09-06T12:00:00+02:00");
  const events = [
    {
      object_id: "a",
      event_key: "multi",
      time_phase: "ongoing",
      time_windows: [
        { start_at: "2026-09-06T11:00:00+02:00", stop_at: "2026-09-06T13:00:00+02:00", valid: true },
        { start_at: "2026-09-07T08:00:00+02:00", stop_at: "2026-09-07T10:00:00+02:00", valid: true },
      ],
    },
    {
      object_id: "b",
      event_key: "too-late",
      time_phase: "upcoming",
      time_windows: [{ start_at: "2026-09-09T13:00:00+02:00", stop_at: null, valid: true }],
    },
    {
      object_id: "c",
      event_key: "bad",
      time_phase: "upcoming",
      time_windows: [{ start_at: "2026-09-07T09:00:00+02:00", stop_at: null, valid: false }],
    },
  ];

  const rows = nextWindows(events, [], now, 48);

  assert.equal(rows.length, 1);
  assert.equal(rows[0].event.event_key, "multi");
  assert.equal(rows[0].window.start_at, "2026-09-07T08:00:00+02:00");
});

test("selects planned windows without treating future outages as planned", () => {
  const now = new Date("2026-09-06T12:00:00+02:00");
  const events = [
    {
      object_id: "a",
      source: "pge",
      source_event_id: "planned",
      type: "planned_outage",
      description: "Prace planowe przy ulicy Leśnej",
      time_phase: "upcoming",
      time_windows: [{ start_at: "2026-09-07T08:00:00+02:00", stop_at: "2026-09-07T10:30:00+02:00", valid: true }],
    },
    {
      object_id: "b",
      source: "pge",
      source_event_id: "future-outage",
      type: "outage",
      time_phase: "upcoming",
      time_windows: [{ start_at: "2026-09-07T09:00:00+02:00", stop_at: null, valid: true }],
    },
  ];

  const result = plannedWindows(events, [], now, { hours: 168, limit: 10 });

  assert.equal(result.total, 1);
  assert.equal(result.uniqueObjects, 1);
  assert.equal(result.rows[0].event.source_event_id, "planned");
  assert.equal(result.rows[0].description, "Prace planowe przy ulicy Leśnej");
});

test("keeps ongoing planned windows before future windows and respects filters", () => {
  const now = new Date("2026-09-06T12:00:00+02:00");
  const objects = [
    { id: "a", name: "TSR Alfa", city: "Olsztyn" },
    { id: "b", name: "TSR Beta", city: "Gdańsk" },
    { id: "c", name: "TSR Gamma", city: "Kraków" },
  ];
  const events = [
    {
      object_id: "b",
      source: "tauron",
      source_event_id: "future",
      type: "planned_outage",
      description: "ulica Długa",
      time_phase: "upcoming",
      time_windows: [{ start_at: "2026-09-06T18:00:00+02:00", stop_at: "2026-09-06T19:00:00+02:00", valid: true }],
    },
    {
      object_id: "a",
      source: "pge",
      source_event_id: "ongoing",
      type: "planned_outage",
      description: "ulica Leśna",
      time_phase: "ongoing",
      time_windows: [{ start_at: "2026-09-06T10:00:00+02:00", stop_at: "2026-09-06T13:00:00+02:00", valid: true }],
    },
    {
      object_id: "c",
      source: "pge",
      source_event_id: "other",
      type: "planned_outage",
      description: "ulica Polna",
      time_phase: "upcoming",
      time_windows: [{ start_at: "2026-09-07T08:00:00+02:00", stop_at: "2026-09-07T10:00:00+02:00", valid: true }],
    },
  ];

  const all = plannedWindows(events, objects, now, { hours: 168, limit: 10 });
  const favorites = plannedWindows(events, objects, now, { hours: 168, limit: 10, favoritesOnly: true, favorites: ["b"] });
  const query = plannedWindows(events, objects, now, { hours: 168, limit: 10, query: "leśna" });

  assert.deepEqual(all.rows.map((row) => row.event.source_event_id), ["ongoing", "future", "other"]);
  assert.deepEqual(favorites.rows.map((row) => row.event.object_id), ["b"]);
  assert.deepEqual(query.rows.map((row) => row.event.source_event_id), ["ongoing"]);
});

test("separates planned records with invalid time from horizon rows", () => {
  const now = new Date("2026-09-06T12:00:00+02:00");
  const result = plannedWindows([
    {
      object_id: "a",
      source: "pge",
      source_event_id: "invalid",
      type: "planned_outage",
      description: "bez terminu",
      time_phase: "unknown",
      time_windows: [],
    },
  ], [], now, { hours: 168, limit: 10, includeInvalid: true });

  assert.equal(result.total, 0);
  assert.equal(result.invalidTotal, 1);
  assert.equal(result.invalidRows[0].bucket, "invalid");
});

test("accepts only http source links", () => {
  assert.equal(sourceUrl({ source_url: "https://example.test/path" }), "https://example.test/path");
  assert.equal(sourceUrl({ source_url: "javascript:alert(1)" }), "");
  assert.equal(sourceUrl({ source_url: "not a url" }), "");
});

test("creates deep links to the existing map", () => {
  assert.equal(
    eventUrl({ object_id: "42" }, "planned", "2026-09-07T08:00:00+02:00"),
    "/?object=42&view=planned&date=2026-09-07",
  );
});
