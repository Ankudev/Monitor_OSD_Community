/* Pokazuj aktywną zakładkę także na wąskim ekranie. */
globalThis.OsdHeader = {
  revealActive() {
    const strip = document.querySelector('.osd-navigation .view-navigation-inner');
    const active = strip?.querySelector('[aria-current="page"]');
    if (!active) return;
    const bounds = strip.getBoundingClientRect();
    const tab = active.getBoundingClientRect();
    if (tab.left < bounds.left || tab.right > bounds.right) {
      strip.scrollLeft += tab.left - bounds.left - (strip.clientWidth - tab.width) / 2;
    }
  },
};
function showDataAttribution() {
  if (document.getElementById('osd-data-attribution')) return;
  const note = document.createElement('p');
  note.id = 'osd-data-attribution';
  note.className = 'osd-data-attribution';
  const link = (label, href) => {
    const a = document.createElement('a'); a.textContent = label; a.href = href; return a;
  };
  note.append('Monitor OSD Community · ', link('Źródła i ograniczenia', '/readme'), '. Mapa: ', link('© OpenStreetMap i autorzy, ODbL', 'https://www.openstreetmap.org/copyright'), '.');
  document.body.append(note);
}
document.addEventListener('DOMContentLoaded', () => { globalThis.OsdHeader.revealActive(); showDataAttribution(); });
if (document.readyState !== 'loading') showDataAttribution();
window.addEventListener('resize', () => globalThis.OsdHeader.revealActive());

async function showCommunityMode() {
  try {
    const response = await fetch('/api/community', {cache: 'no-store'});
    if (!response.ok) return;
    const mode = await response.json();
    if (mode.demo && !document.getElementById('community-demo-notice')) {
      const note = document.createElement('aside');
      note.id = 'community-demo-notice';
      note.setAttribute('role', 'note');
      note.textContent = mode.notice + ' Przykładowe punkty miejscowości; przypisania operatorów są częścią scenariusza.';
      document.body.prepend(note);
      document.body.classList.add('community-demo');
      const lastRead = document.getElementById('lastReadLabel');
      if (lastRead) lastRead.textContent = 'Ostatnie odnowienie scenariusza';
    }
  } catch { /* Mapa i lista pozostają dostępne przy błędzie informacji o profilu. */ }
}
if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', showCommunityMode);
else showCommunityMode();
