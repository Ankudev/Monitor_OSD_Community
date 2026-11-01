<template>
  <div>
    <header class="topbar osd-header">
      <div class="topbar-inner">
        <div class="brand-mark" aria-hidden="true">OSD</div>
        <div class="brand-title">
          <p class="eyebrow">MONITOR ZDARZEŃ OSD</p>
          <h1>Monitor OSD Community</h1>
        </div>
      </div>
    </header>
    <nav class="view-navigation osd-navigation" aria-label="Główne widoki">
      <div class="view-navigation-inner">
        <a class="view-tab" href="/"><span class="tab-icon" aria-hidden="true">!</span>Komunikaty bieżące</a>
        <a class="view-tab" href="/?view=attention"><span class="tab-icon" aria-hidden="true">?</span>Wymaga uwagi</a>
        <a class="view-tab" href="/?view=next48"><span class="tab-icon" aria-hidden="true">48</span>48 godzin</a>
        <a class="view-tab" href="/?view=planned"><span class="tab-icon" aria-hidden="true">P</span>Planowane wyłączenia</a>
        <a class="view-tab" href="/?view=objects"><span class="tab-icon" aria-hidden="true">O</span>Moje obiekty</a>
        <a class="view-tab active" href="/snut/" aria-current="page"><span class="tab-icon" aria-hidden="true">S</span>SNUT</a>
        <a class="view-tab" href="/readme"><span class="tab-icon" aria-hidden="true">R</span>README</a>
      </div>
    </nav>
    <div class="osd-tools" role="group" aria-label="Narzędzia pulpitu SNUT">
      <div class="osd-tools-inner">
        <span class="osd-tools-label">Pulpit SNUT</span>
        <slot />
      </div>
    </div>
  </div>
</template>

<script>
import "../assets/app-header.css";
import "../assets/app-header.js";

export default {
  name: "OsdHeader",
  mounted() {
    globalThis.OsdHeader.revealActive();
  },
};
</script>
