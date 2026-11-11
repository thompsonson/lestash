// The Library (#264): your items on shelves by what they are, Books to Notes, with
// filters by person, topic and year counted by the server, and a sort. Lists are
// filtered before paging, so every page is full.

import { api } from '../api.js';
import { route, navigate } from '../router.js';
import { escHtml, openFiltersWhenWide } from '../util.js';
import { renderCard } from '../objects/item/card.js';

const PAGE = 30;
const SORTS = [['recent', 'Newest'], ['oldest', 'Oldest'], ['title', 'Title']];

let shelves = null;  // [{shelf, label, kinds, count}]
let shown = null;

function shell() {
  const tab = document.getElementById('tab-library');
  if (tab.dataset.built) return tab;
  tab.dataset.built = '1';
  tab.innerHTML = `
    <nav class="switch shelves" aria-label="Shelves"></nav>
    <details class="fold-filters">
      <summary>Filters</summary>
      <div class="filters library-filters">
        <select data-facet="person" aria-label="Made by"></select>
        <select data-facet="tag" aria-label="Topic"></select>
        <select data-facet="year" aria-label="Year"></select>
        <select data-facet="sort" aria-label="Sort">${SORTS.map(([v, l]) => `<option value="${v}">${l}</option>`).join('')}</select>
      </div>
    </details>
    <div class="library-count muted"></div>
    <div class="item-list library-list"></div>
    <button class="load-more library-more" style="display:none">Load more</button>`;
  openFiltersWhenWide(tab);
  tab.querySelectorAll('[data-facet]').forEach(sel => sel.addEventListener('change', () => {
    const params = new URLSearchParams(location.search);
    for (const s of tab.querySelectorAll('[data-facet]')) {
      if (s.value && !(s.dataset.facet === 'sort' && s.value === 'recent')) params.set(s.dataset.facet, s.value);
      else params.delete(s.dataset.facet);
    }
    navigate(`/library?${params}`);
  }));
  return tab;
}

function options(select, label, list, value, toValue, toText) {
  const opts = [`<option value="">${escHtml(label)}</option>`]
    .concat(list.map(x => `<option value="${escHtml(toValue(x))}">${escHtml(toText(x))}</option>`));
  // A value from the address that the counts don't list still shows, so the
  // filter on screen matches the list
  if (value && !list.some(x => String(toValue(x)) === value)) {
    opts.push(`<option value="${escHtml(value)}">${escHtml(value)}</option>`);
  }
  select.innerHTML = opts.join('');
  select.value = value || '';
}

async function drawShelf(url) {
  const tab = shell();
  if (!shelves) {
    try {
      shelves = await api('/api/library');
    } catch (e) {
      tab.querySelector('.library-list').innerHTML = `<div class="empty-state">Could not load the shelves: ${escHtml(e.message)}</div>`;
      return;
    }
  }
  const params = url.searchParams;
  const current = shelves.find(s => s.shelf === params.get('shelf')) || shelves[0];
  tab.querySelector('.shelves').innerHTML = shelves.map(s => {
    const on = s === current;
    return `<a href="/library?shelf=${s.shelf}" class="${on ? 'on' : ''}" aria-current="${on ? 'page' : 'false'}">${escHtml(s.label)} <span class="muted">${s.count.toLocaleString()}</span></a>`;
  }).join('');
  const key = current.shelf + '?' + params.toString();
  if (key === shown) return;
  shown = key;

  const sel = name => tab.querySelector(`[data-facet="${name}"]`);
  sel('sort').value = params.get('sort') || 'recent';
  const list = tab.querySelector('.library-list');
  const more = tab.querySelector('.library-more');
  const count = tab.querySelector('.library-count');
  list.innerHTML = '<div class="empty-state"><div class="spinner"></div></div>';

  api(`/api/library/${current.shelf}/facets`).then(f => {
    if (shown !== key) return;
    options(sel('person'), 'Anyone', f.people, params.get('person'), p => p.id, p => `${p.name} (${p.count})`);
    options(sel('tag'), 'Any topic', f.topics, params.get('tag'), t => t.name, t => `${t.name} (${t.count})`);
    options(sel('year'), 'Any year', f.years, params.get('year'), y => y.name, y => `${y.name} (${y.count})`);
    const active = ['person', 'tag', 'year'].filter(k => params.get(k)).length;
    tab.querySelector('.fold-filters summary').textContent = active ? `Filters (${active})` : 'Filters';
  }).catch(() => {});

  let offset = 0;
  const load = async () => {
    more.disabled = true;
    const q = new URLSearchParams({ kind: current.kinds.join(','), limit: PAGE, offset });
    for (const k of ['person', 'tag', 'year', 'sort']) if (params.get(k)) q.set(k, params.get(k));
    try {
      const data = await api(`/api/items?${q}`);
      if (shown !== key) return;
      if (offset === 0) list.innerHTML = '';
      data.items.forEach(item => list.appendChild(renderCard(item)));
      offset += data.items.length;
      count.textContent = `${data.total.toLocaleString()} ${data.total === 1 ? 'item' : 'items'}`;
      more.style.display = offset < data.total ? '' : 'none';
      if (!data.total) list.innerHTML = '<div class="empty-state">Nothing on this shelf with these filters.</div>';
    } catch (e) {
      if (shown !== key) return;
      shown = null;  // coming back tries again
      list.innerHTML = `<div class="empty-state">Could not load this shelf: ${escHtml(e.message)}</div>`;
    } finally {
      more.disabled = false;
    }
  };
  more.onclick = load;
  await load();
}

export function startLibrary(show) {
  route(/^\/library$/, (m, url) => { show('tab-library', 'library'); return drawShelf(url); });
}

// After saving or syncing, counts and lists load again
export function redrawLibrary() { shelves = null; shown = null; }
