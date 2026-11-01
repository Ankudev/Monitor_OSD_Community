<template>
  <div class="osd-event-details">
    <strong class="osd-event-title">{{ info.title }}</strong>
    <span>{{ info.operator }} · {{ info.phase }}</span>
    <span v-for="period in info.periods" :key="period" class="osd-event-period">{{ period }}</span>
    <p class="osd-event-address">{{ info.address }}</p>
    <p class="osd-event-reason">{{ info.reason }}</p>
    <details>
      <summary>Dlaczego ten komunikat pasuje?</summary>
      <p v-for="(line, index) in explanation" :key="index">{{ line }}</p>
    </details>
    <details>
      <summary>Dane obiektu i komunikatu</summary>
      <p>{{ info.objectCity }}<template v-if="info.objectAddress"> · {{ info.objectAddress }}</template></p>
      <p>{{ info.teryt }}</p>
      <p v-for="line in info.powerLines" :key="line">{{ line }}</p>
      <p v-if="info.locationWarning">{{ info.locationWarning }}</p>
      <p>Stan komunikatu: {{ info.status }}</p>
      <p v-if="info.hasAdditionalDescription">Opis operatora: {{ info.description }}</p>
    </details>
    <a v-if="info.sourceUrl" :href="info.sourceUrl" class="osd-event-source" target="_blank" rel="noopener noreferrer">Źródło operatora</a>
  </div>
</template>

<script>
import "./event-view.js";
import "./match-explanation.js";

export default {
  name: "OsdEventDetails",
  props: {event: {type: Object, required: true}, object: Object, window: Object},
  computed: {
    info() { return globalThis.OsdEventView.summary(this.event, this.object, this.window); },
    explanation() { return globalThis.OsdMatchingView.rows(this.event, {includeReason: false}); },
  },
};
</script>

<style scoped>
.osd-event-details { display: grid; gap: .3rem; margin-top: .35rem; font-size: .78rem; line-height: 1.45; overflow-wrap: anywhere; }
.osd-event-title { color: var(--text); }
.osd-event-period { font-weight: 600; }
.osd-event-details p { margin: 0; white-space: pre-line; }
.osd-event-address { padding-top: .15rem; }
.osd-event-reason { color: var(--text-secondary, var(--text)); }
.osd-event-details details { padding-top: .25rem; }
.osd-event-details summary { min-height: 44px; padding-block: 10px; cursor: pointer; color: var(--highlight-primary); font-weight: 700; }
.osd-event-details details p { margin-top: .4rem; }
.osd-event-source { min-height: 44px; padding-block: 10px; font-weight: 700; }
.osd-event-details :is(summary, a):focus-visible { outline: 2px solid var(--highlight-primary); outline-offset: 2px; }
</style>
