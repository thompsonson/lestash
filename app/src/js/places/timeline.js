// The Timeline, home of the app (#262): what you did and what you made, one day at a
// time. Busy verbs (a day of watching) fold into one line that opens; your posts,
// notes, chats and notebooks stand out. It replaces the Feed: "/" opens today.

import { api } from '../api.js';
import { route, navigate } from '../router.js';
import { escHtml } from '../util.js';
import { kindIcon } from '../objects/item/kinds.js';
import { openFiltersWhenWide } from '../util.js';

// Verbs that fold into one line once a day has more than FOLD_AT of one kind
const FOLDING = new Set(['watched', 'viewed', 'liked', 'disliked', 'shared', 'searched']);
const FOLD_AT = 3;

const VERB_WORDS = {
  watched: 'watched', viewed: 'viewed', liked: 'liked', disliked: 'disliked', shared: 'shared',
  searched: 'searched for', prompted: 'prompted', listened: 'listened to', read: 'read',
};
const MADE_WORDS = {
  post: 'posted', comment: 'replied', note: 'wrote a note', chat: 'chatted', notebook: 'made a notebook',
  artifact: 'generated', project: 'started a project', email: 'emailed',
};
const PLURALS = {
  video: 'videos', post: 'posts', search: 'searches', chat: 'chats', note: 'notes', book: 'books',
  paper: 'papers', document: 'documents', notebook: 'notebooks', comment: 'replies', stub: 'things',
  artifact: 'generated items', reaction: 'reactions', message: 'messages', other: 'things',
};

const VERB_FILTERS = [
  ['', 'Everything'], ['made', 'Made by you'], ['watched', 'Watched'], ['viewed', 'Viewed'],
  ['liked', 'Liked'], ['prompted', 'Prompted'], ['searched', 'Searched'], ['shared', 'Shared'],
];
const KIND_FILTERS = [
  ['', 'All kinds'], ['video', 'Videos'], ['post', 'Posts'], ['chat', 'Chats'], ['notebook', 'Notebooks'],
  ['note', 'Notes'], ['book', 'Books'], ['paper', 'Papers'], ['document', 'Documents'],
];

const pad = n => String(n).padStart(2, '0');
const isoDay = d => `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;

function parseDay(text) {
  const [y, m, d] = text.split('-').map(Number);
  const day = new Date(y, m - 1, d);
  return isNaN(day) ? null : day;
}

function dayAddress(day, params) {
  const today = isoDay(new Date());
  const qs = params.toString();
  const path = isoDay(day) === today ? '/' : `/day/${isoDay(day)}`;
  return qs ? `${path}?${qs}` : path;
}

function shell() {
  const tab = document.getElementById('tab-timeline');
  if (tab.dataset.built) return tab;
  tab.dataset.built = '1';
  tab.innerHTML = `
    <form class="timeline-search" role="search">
      <input type="search" name="q" class="search-input" placeholder="Search your knowledge base..." aria-label="Search">
    </form>
    <div class="timeline-nav">
      <a class="day-step" data-step="previous" href="#" aria-label="Previous day with activity">‹</a>
      <label class="day-label"><span class="day-text"></span>
        <input type="date" class="day-pick" aria-label="Go to a day"></label>
      <a class="day-step" data-step="next" href="#" aria-label="Next day with activity">›</a>
      <a class="day-today" href="/">Today</a>
    </div>
    <details class="fold-filters"><summary>Filters</summary>
      <div class="filters timeline-filters">
        <select class="verb-filter" aria-label="What you did">${VERB_FILTERS.map(([v, l]) => `<option value="${v}">${l}</option>`).join('')}</select>
        <select class="kind-filter" aria-label="Kind of item">${KIND_FILTERS.map(([v, l]) => `<option value="${v}">${l}</option>`).join('')}</select>
      </div>
    </details>
    <div class="timeline-list" aria-live="polite"></div>`;
  openFiltersWhenWide(tab);
  tab.querySelector('.timeline-search').addEventListener('submit', e => {
    e.preventDefault();
    const q = e.target.q.value.trim();
    navigate(q ? `/search?q=${encodeURIComponent(q)}` : '/search');
  });
  const refilter = () => {
    const params = new URLSearchParams();
    const verb = tab.querySelector('.verb-filter').value;
    const kind = tab.querySelector('.kind-filter').value;
    if (verb) params.set('verb', verb);
    if (kind) params.set('kind', kind);
    navigate(dayAddress(parseDay(tab.dataset.day), params));
  };
  tab.querySelector('.verb-filter').addEventListener('change', refilter);
  tab.querySelector('.kind-filter').addEventListener('change', refilter);
  tab.querySelector('.day-pick').addEventListener('change', e => {
    const day = parseDay(e.target.value);
    if (day) navigate(dayAddress(day, new URLSearchParams(location.search)));
  });
  return tab;
}

function entryTitle(e) {
  const item = e.item;
  if (!item) return (e.details && e.details.query) || '(nothing named)';
  return item.title;
}

function line(e) {
  const time = new Date(e.at).toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit' });
  const item = e.item;
  const verb = e.verb === 'made' ? (MADE_WORDS[item && item.kind] || 'made') : (VERB_WORDS[e.verb] || e.verb);
  const icon = item ? kindIcon(item) : '·';
  const title = escHtml(entryTitle(e));
  const what = item ? `<a href="/items/${item.id}">${title}</a>` : title;
  const extra = [];
  if (item && item.channel) extra.push(escHtml(item.channel));
  if (item && item.kind_detail) extra.push(escHtml(item.kind_detail));
  if (e.context) extra.push(`with <a href="/items/${e.context.id}">${escHtml(e.context.title)}</a>`);
  const landmark = e.verb === 'made' ? ' landmark' : '';
  const asked = e.prompt ? `<span class="t-prompt">“${escHtml(e.prompt)}”</span>` : '';
  return `<li class="timeline-entry${landmark} kind-${item ? item.kind : 'other'}">
    <span class="t-time">${time}</span>
    <span class="t-verb">${escHtml(verb)}</span>
    <span class="t-icon" aria-hidden="true">${icon}</span>
    <span class="t-what">${what}${extra.length ? ` <span class="muted">· ${extra.join(' · ')}</span>` : ''}${asked}</span>
  </li>`;
}

// One prompt under its chat: the time and what you asked
function promptLine(e) {
  const time = new Date(e.at).toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit' });
  return `<li class="timeline-entry prompt-entry">
    <span class="t-time">${time}</span>
    <span class="t-what">${e.prompt ? escHtml(e.prompt) : '<span class="muted">(what you asked is not held)</span>'}</span>
  </li>`;
}

// What an entry folds under: busy verbs by kind; prompts by their chat
function foldKey(e) {
  if (e.verb === 'prompted' && e.item) return `prompted|${e.item.id}`;
  return FOLDING.has(e.verb) ? `${e.verb}|${e.item ? e.item.kind : ''}` : null;
}

// Entries newest first, with busy verbs folded into one line at their latest time,
// and a chat's prompts under one line for the chat
function fold(entries) {
  const groups = new Map();
  for (const e of entries) {
    const key = foldKey(e);
    if (!key) continue;
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(e);
  }
  const folded = new Set([...groups]
    .filter(([k, list]) => list.length > (k.startsWith('prompted|') ? 1 : FOLD_AT)).map(([k]) => k));
  const out = [];
  const placed = new Set();
  for (const e of entries) {
    const key = foldKey(e);
    if (!folded.has(key)) { out.push({ entry: e }); continue; }
    if (placed.has(key)) continue;
    placed.add(key);
    out.push({ group: groups.get(key) });
  }
  return out;
}

const SHOWN_IN_GROUP = 200;

// "prompted Planning my return to France · 13 prompts", opening to what you asked
function promptGroupHtml(list) {
  const first = list[0];
  const from = new Date(list[list.length - 1].at).toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit' });
  const to = new Date(first.at).toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit' });
  return `<li class="timeline-group kind-${first.item.kind}"><details>
    <summary><span class="t-time">${to}</span> <span class="t-verb">prompted</span>
      <a href="/items/${first.item.id}">${escHtml(first.item.title)}</a>
      <b>${list.length} prompts</b> <span class="muted">${from}–${to}</span></summary>
    <ul class="timeline-sub">${list.slice(0, SHOWN_IN_GROUP).map(promptLine).join('')}</ul>
  </details></li>`;
}

function groupHtml(list) {
  const first = list[0];
  if (first.verb === 'prompted') return promptGroupHtml(list);
  const kind = first.item ? first.item.kind : '';
  const n = list.length;
  // "searched 9 times", not "searched for 9 searches"
  const what = first.verb === 'searched' ? 'times' : (PLURALS[kind] || 'things');
  const verb = first.verb === 'searched' ? 'searched' : (VERB_WORDS[first.verb] || first.verb);
  const from = new Date(list[list.length - 1].at).toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit' });
  const to = new Date(first.at).toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit' });
  const rows = list.slice(0, SHOWN_IN_GROUP).map(line).join('');
  const more = n > SHOWN_IN_GROUP ? `<li class="muted">and ${n - SHOWN_IN_GROUP} more</li>` : '';
  return `<li class="timeline-group"><details>
    <summary><span class="t-time">${to}</span> <span class="t-verb">${escHtml(verb)}</span>
      <b>${n} ${escHtml(what)}</b> <span class="muted">${from}–${to}</span></summary>
    <ul class="timeline-sub">${rows}${more}</ul>
  </details></li>`;
}

let shown = null;  // the address drawn, so a same-address render doesn't refetch

async function drawDay(day, url) {
  const tab = shell();
  const params = url.searchParams;
  const key = isoDay(day) + '?' + params.toString();
  tab.dataset.day = isoDay(day);
  tab.querySelector('.day-text').textContent = day.toLocaleDateString(undefined,
    { weekday: 'short', day: 'numeric', month: 'short', year: 'numeric' });
  tab.querySelector('.day-pick').value = isoDay(day);
  tab.querySelector('.verb-filter').value = params.get('verb') || '';
  tab.querySelector('.kind-filter').value = params.get('kind') || '';
  const active = ['verb', 'kind'].filter(k => params.get(k)).length;
  tab.querySelector('.fold-filters summary').textContent = active ? `Filters (${active})` : 'Filters';
  tab.querySelector('.day-today').hidden = isoDay(day) === isoDay(new Date());
  if (key === shown) return;
  shown = key;
  const list = tab.querySelector('.timeline-list');
  list.innerHTML = '<div class="empty-state"><div class="spinner"></div></div>';
  const start = new Date(day.getFullYear(), day.getMonth(), day.getDate());
  const end = new Date(day.getFullYear(), day.getMonth(), day.getDate() + 1);
  const q = new URLSearchParams({ start: start.toISOString(), end: end.toISOString() });
  if (params.get('verb')) q.set('verb', params.get('verb'));
  if (params.get('kind')) q.set('kind', params.get('kind'));
  try {
    const data = await api(`/api/timeline?${q}`);
    if (shown !== key) return;  // the user moved on meanwhile
    for (const [step, when] of [['previous', data.previous], ['next', data.next]]) {
      const a = tab.querySelector(`[data-step="${step}"]`);
      if (when) {
        a.href = dayAddress(new Date(when), params);
        a.removeAttribute('aria-disabled');
      } else {
        a.removeAttribute('href');
        a.setAttribute('aria-disabled', 'true');
      }
    }
    if (!data.entries.length) {
      const last = data.previous
        ? ` <a href="${dayAddress(new Date(data.previous), params)}">Go to ${escHtml(new Date(data.previous).toLocaleDateString(undefined, { day: 'numeric', month: 'short', year: 'numeric' }))}</a>, the last day with activity.`
        : '';
      list.innerHTML = `<div class="empty-state">Nothing on this day.${last}</div>`;
      return;
    }
    const parts = fold(data.entries).map(p => p.group ? groupHtml(p.group) : line(p.entry));
    list.innerHTML = `<ul class="timeline">${parts.join('')}</ul>`
      + (data.truncated ? '<div class="muted">Only the newest entries are shown.</div>' : '');
  } catch (e) {
    if (shown !== key) return;
    shown = null;  // coming back tries again
    list.innerHTML = `<div class="empty-state">Could not load this day: ${escHtml(e.message)}</div>`;
  }
}

export function startTimeline(markTab) {
  route(/^\/$/, (m, url) => {
    // The Feed's old filters (?source=, ?tag=, ?own=) mean nothing here
    if (['source', 'tag', 'own'].some(k => url.searchParams.has(k))) return navigate('/', { replace: true });
    markTab();
    return drawDay(new Date(), url);
  });
  route(/^\/day\/(\d{4}-\d{2}-\d{2})$/, ([, d], url) => {
    const day = parseDay(d);
    if (!day) return navigate('/', { replace: true });
    markTab();
    return drawDay(day, url);
  });
}

// For tests and Reconnect: draw the same day again
export function redrawTimeline() { shown = null; }
