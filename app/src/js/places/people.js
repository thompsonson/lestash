// People (#263): people, channels and AIs, with a switch between them, and a page
// each at /people/<id>. "You" is /people/me.

import { api } from '../api.js';
import { route, navigate } from '../router.js';
import { escHtml } from '../util.js';
import { renderPersonCard } from '../objects/person/card.js';
import { drawPerson } from '../objects/person/page.js';

const SWITCHES = [['people', 'People'], ['channels', 'Channels'], ['ai', 'AI']];
const PAGE = 50;

let shown = null;

function shell() {
  const tab = document.getElementById('tab-people');
  if (tab.dataset.built) return tab;
  tab.dataset.built = '1';
  tab.innerHTML = `
    <nav class="switch" aria-label="Kind of person">${SWITCHES.map(([k, l]) => `<a href="/people?switch=${k}" data-switch="${k}">${l}</a>`).join('')}</nav>
    <form class="filters people-search" role="search">
      <input type="search" name="q" class="search-input" placeholder="Find by name" aria-label="Find by name">
    </form>
    <div class="people-count muted"></div>
    <div class="item-list people-list"></div>
    <button class="load-more people-more" style="display:none">Load more</button>`;
  tab.querySelector('.people-search').addEventListener('submit', e => {
    e.preventDefault();
    const params = new URLSearchParams(location.search);
    const q = e.target.q.value.trim();
    if (q) params.set('q', q); else params.delete('q');
    navigate(`/people?${params}`);
  });
  return tab;
}

async function drawList(url) {
  const tab = shell();
  const sw = url.searchParams.get('switch') || 'people';
  const q = url.searchParams.get('q') || '';
  tab.querySelectorAll('[data-switch]').forEach(a => {
    const on = a.dataset.switch === sw;
    a.classList.toggle('on', on);
    a.setAttribute('aria-current', on ? 'page' : 'false');
  });
  tab.querySelector('.people-search').q.value = q;
  const key = url.search;
  if (key === shown) return;
  shown = key;
  const list = tab.querySelector('.people-list');
  const more = tab.querySelector('.people-more');
  const count = tab.querySelector('.people-count');
  list.innerHTML = '<div class="empty-state"><div class="spinner"></div></div>';
  let offset = 0;
  const load = async () => {
    const params = new URLSearchParams({ switch: sw, limit: PAGE, offset });
    if (q) params.set('q', q);
    try {
      const data = await api(`/api/people?${params}`);
      if (shown !== key) return;
      if (offset === 0) list.innerHTML = '';
      data.people.forEach(p => list.appendChild(renderPersonCard(p)));
      offset += data.people.length;
      count.textContent = `${data.total.toLocaleString()} ${q ? 'found' : 'in all'}`;
      more.style.display = offset < data.total ? '' : 'none';
      if (!data.total) list.innerHTML = '<div class="empty-state">No one here yet.</div>';
    } catch (e) {
      if (shown !== key) return;
      shown = null;
      list.innerHTML = `<div class="empty-state">Could not load: ${escHtml(e.message)}</div>`;
    }
  };
  more.onclick = load;
  await load();
}

export function startPeople(show) {
  route(/^\/people$/, (m, url) => { show('tab-people', 'people'); return drawList(url); });
  route(/^\/people\/(me|\d+)$/, async ([, id], url) => {
    show('item-view', 'people');
    const container = document.getElementById('item-view-content');
    return drawPerson(id, container, url);
  });
}

export function redrawPeople() { shown = null; }
