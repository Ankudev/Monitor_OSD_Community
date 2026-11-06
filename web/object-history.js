/* Historia obiektu korzysta wyłącznie z publicznego, filtrowanego API. */
(() => {
  let nextId = 0;
  const historyObjects = new Set();
  const actions = {
    new: 'Nowe dopasowanie', updated: 'Zmienione dane', cancelled: 'Odwołany komunikat',
    not_published: 'Operator przestał publikować komunikat', out_of_scope: 'Komunikat przestał pasować',
    snapshot: 'Pierwszy zapis w archiwum',
  };
  function element(tag, text, className = '') {
    const node = document.createElement(tag);
    node.textContent = text;
    node.className = className;
    return node;
  }
  function mount(container, objectId, onLayout = () => {}) {
    if (container.dataset.historyMounted) return;
    container.dataset.historyMounted = 'true';
    const id = `object-history-${++nextId}`;
    const details = element('div', '', 'object-details-panel');
    details.append(...container.childNodes);
    // Nazwa obiektu pozostaje widoczna nad obiema zakładkami katalogu.
    const heading = details.querySelector('h4');
    if (heading) container.append(heading);
    const history = element('div', '', 'object-history-panel');
    const panels = [details, history];
    const tabs = element('div', '', 'object-detail-tabs');
    tabs.setAttribute('role', 'tablist');
    tabs.setAttribute('aria-label', 'Informacje o obiekcie');
    const buttons = ['Szczegóły', 'Historia'].map((title, index) => {
      const button = element('button', title);
      button.type = 'button';
      button.id = `${id}-tab-${index}`;
      button.setAttribute('role', 'tab');
      button.setAttribute('aria-controls', `${id}-panel-${index}`);
      panels[index].id = `${id}-panel-${index}`;
      panels[index].setAttribute('role', 'tabpanel');
      panels[index].setAttribute('aria-labelledby', button.id);
      panels[index].tabIndex = 0;
      tabs.append(button);
      return button;
    });
    container.append(tabs, ...panels);
    let controller;
    let generation = 0;
    async function readHistory() {
      controller?.abort();
      const current = ++generation;
      controller = new AbortController();
      const signal = controller.signal;
      const timeout = setTimeout(() => controller?.signal === signal && controller.abort(), 10000);
      const status = element('p', 'Wczytywanie historii…', 'object-history-status');
      status.setAttribute('role', 'status');
      history.replaceChildren(status);
      history.setAttribute('aria-busy', 'true');
      onLayout();
      try {
        const query = new URLSearchParams({object_id: String(objectId), limit: '3', latest_per_event: '1'});
        const response = await fetch(`/api/v1/history?${query}`, {signal, cache: 'no-store'});
        if (!response.ok) throw new Error('history_unavailable');
        const data = await response.json();
        if (!Array.isArray(data.items)) throw new Error('invalid_history');
        if (current !== generation) return;
        status.textContent = data.items.length ? 'Ostatnie zapisane zmiany maksymalnie trzech komunikatów.' : 'Brak zapisanej historii dla tego obiektu.';
        if (data.demo) history.append(element('p', 'Dane demonstracyjne. Historia zawiera zdarzenia symulowane.', 'object-history-notice'));
        const list = element('ol', '', 'object-history-list');
        for (const item of data.items.slice(0, 3)) {
          const info = globalThis.OsdEventView.summary(item);
          const row = element('li', '', 'object-history-entry');
          row.append(element('strong', `${info.title} · ${info.operator}`));
          row.append(element('p', `Zapisano: ${globalThis.OsdEventView.formatDate(item.recorded_at)}`));
          row.append(element('p', actions[item.action] || 'Zapisana zmiana komunikatu'));
          for (const period of info.periods) row.append(element('p', period));
          if (info.description) row.append(element('p', info.description));
          row.append(element('p', info.reason, 'object-history-note'));
          list.append(row);
        }
        history.append(list, element('p', 'Historia obejmuje dostępne zapisy aplikacji. Komunikat i jego dopasowanie nie potwierdzają rzeczywistego stanu zasilania.', 'object-history-note'));
      } catch {
        if (current !== generation) return;
        status.textContent = 'Nie udało się wczytać historii. Spróbuj ponownie.';
        const retry = element('button', 'Ponów próbę', 'secondary-button');
        retry.type = 'button';
        retry.addEventListener('click', readHistory);
        history.append(retry);
      } finally {
        clearTimeout(timeout);
        if (current === generation) {
          history.setAttribute('aria-busy', 'false');
          onLayout();
        }
      }
    }
    function select(index) {
      ++generation;
      controller?.abort();
      if (index === 1) historyObjects.add(String(objectId));
      else historyObjects.delete(String(objectId));
      buttons.forEach((button, position) => {
        button.setAttribute('aria-selected', String(position === index));
        button.tabIndex = position === index ? 0 : -1;
        panels[position].hidden = position !== index;
      });
      if (index === 1) readHistory();
      onLayout();
    }
    buttons.forEach((button, index) => {
      button.addEventListener('click', () => select(index));
      button.addEventListener('keydown', event => {
        const target = {ArrowLeft: 1 - index, ArrowRight: 1 - index, Home: 0, End: 1}[event.key];
        if (target === undefined) return;
        event.preventDefault();
        select(target);
        buttons[target].focus();
      });
    });
    select(historyObjects.has(String(objectId)) ? 1 : 0);
  }
  globalThis.OsdObjectHistory = {mount};
})();
