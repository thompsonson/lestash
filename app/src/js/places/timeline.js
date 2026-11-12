// The Timeline, home of the app (#262): what you did and what you made, one day at a
// time. Busy verbs (a day of watching) fold into one line that opens; your posts,
// notes, chats and notebooks stand out. It replaces the Feed: "/" opens today.

import { api } from '../api.js';
import { route, navigate } from '../router.js';
import { escHtml, sourceLabel } from '../util.js';
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
  swipeDays(tab);
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

// ── Rows ────────────────────────────────────────────────────────────────────────
// Every row has one shape (#277): the service and what you did on the first line,
// with the time at the right; the title, cut to one line, under it. Groups open to
// their items, each a short row of time and title.

const clock = at => new Date(at).toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit' });
const span = list => {
  const from = clock(list[list.length - 1].at), to = clock(list[0].at);
  return from === to ? to : `${from}–${to}`;
};

// "a video", for a single activity's line
const A_KIND = {
  video: 'a video', post: 'a post', chat: 'a chat', note: 'a note', book: 'a book', paper: 'a paper',
  document: 'a document', notebook: 'a notebook', comment: 'a reply', artifact: 'a generated item',
  message: 'a message', email: 'an email', image: 'an image',
};
// "wrote 4 notes", for a group of things you made
const MADE_GROUP = {
  post: 'wrote', comment: 'wrote', note: 'wrote', notebook: 'made', artifact: 'generated',
  project: 'started', email: 'sent', document: 'wrote',
};

// Swipe left for the next day with activity, right for the one before, as the
// arrows do. A swipe from the screen's edge is the system's Back, and a mostly
// upright one is a scroll, so both are left alone; so is anything over an open item.
const EDGE = 24, MIN_DX = 70;
function swipeDays(tab) {
  let start = null;
  tab.addEventListener('touchstart', e => {
    const t = e.touches[0];
    const edge = t.clientX < EDGE || t.clientX > window.innerWidth - EDGE;
    const typing = e.target.closest('input, select, textarea');
    start = e.touches.length === 1 && !edge && !typing && !document.body.classList.contains('sheet-open')
      ? { x: t.clientX, y: t.clientY, at: Date.now() } : null;
  }, { passive: true });
  tab.addEventListener('touchend', e => {
    if (!start) return;
    const t = e.changedTouches[0];
    const dx = t.clientX - start.x, dy = t.clientY - start.y;
    const quick = Date.now() - start.at < 800;
    start = null;
    if (!quick || Math.abs(dx) < MIN_DX || Math.abs(dx) < 1.5 * Math.abs(dy)) return;
    const step = tab.querySelector(`[data-step="${dx < 0 ? 'next' : 'previous'}"]`);
    const href = step && step.getAttribute('href');
    if (!href || href === '#') return;
    const list = tab.querySelector('.timeline-list');
    list.classList.remove('slide-next', 'slide-previous');
    void list.offsetWidth;  // restart the animation
    list.classList.add(dx < 0 ? 'slide-next' : 'slide-previous');
    navigate(href);
  }, { passive: true });
  tab.addEventListener('touchcancel', () => { start = null; }, { passive: true });
}

function entryTitle(e) {
  const item = e.item;
  if (!item) return (e.details && e.details.query) || '(nothing named)';
  // A search's title repeats the verb: "Searched YouTube: sleep science"
  if (item.kind === 'search' && item.title) return item.title.replace(/^Searched( for)?( [A-Z][\w ]*?:)?\s*/, '');
  return item.title || '(untitled)';
}

// The service, with the item's kind icon: "▶ YouTube"
function source(e) {
  const item = e.item;
  const icon = item ? kindIcon(item) : '·';
  const name = item ? sourceLabel(item.source_type) : '';
  return `<span class="t-src"><span class="t-icon" aria-hidden="true">${icon}</span>${escHtml(name)}</span>`;
}

function head(e, act, time) {
  return `<span class="t-head">${source(e)}<span class="t-act">${act}</span><span class="t-time">${time}</span></span>`;
}

// The title line: the title, linked, and what it's with (a channel, a notebook)
function titleLine(e, { prompt = true } = {}) {
  const item = e.item;
  const title = escHtml(entryTitle(e));
  const what = item ? `<a href="/items/${item.id}">${title}</a>` : title;
  const extra = [];
  if (item && item.channel) extra.push(escHtml(item.channel));
  if (item && item.kind_detail) extra.push(escHtml(item.kind_detail));
  if (e.context) extra.push(`with <a href="/items/${e.context.id}">${escHtml(e.context.title)}</a>`);
  const asked = prompt && e.prompt ? `<span class="t-prompt">“${escHtml(e.prompt)}”</span>` : '';
  return `<span class="t-title">${what}${extra.length ? ` <span class="muted">· ${extra.join(' · ')}</span>` : ''}</span>${asked}`;
}

function verbFor(e) {
  const kind = e.item && e.item.kind;
  if (e.verb === 'made') return MADE_WORDS[kind] || 'made';
  if (e.verb === 'searched') return 'searched for';
  const verb = VERB_WORDS[e.verb] || e.verb;
  return A_KIND[kind] ? `${verb} ${A_KIND[kind]}` : verb;
}

function line(e) {
  const kind = e.item ? e.item.kind : 'other';
  const landmark = e.verb === 'made' ? ' landmark' : '';
  return `<li class="t-row${landmark} kind-${kind}">${head(e, escHtml(verbFor(e)), clock(e.at))}${titleLine(e)}</li>`;
}

// A row inside an opened group: the time and the title, or what you asked
function subLine(e) {
  const body = e.verb === 'prompted'
    ? `<span class="t-title t-asked">${e.prompt ? escHtml(e.prompt) : '<span class="muted">(what you asked is not held)</span>'}</span>`
    : (e.verb === 'made' && e.item && e.item.kind === 'chat'
      ? '<span class="t-title muted">started the chat</span>'
      : titleLine(e, { prompt: false }));
  return `<li class="t-sub"><span class="t-time">${clock(e.at)}</span>${body}</li>`;
}

// What an entry folds under: a chat's start and its prompts under the chat; busy
// verbs and things you made by kind and service
function foldKey(e) {
  const item = e.item;
  if (item && (e.verb === 'prompted' || (e.verb === 'made' && item.kind === 'chat'))) return `chat|${item.id}`;
  if (!item) return FOLDING.has(e.verb) ? `${e.verb}||` : null;
  if (FOLDING.has(e.verb) || e.verb === 'made') return `${e.verb}|${item.kind}|${item.source_type}`;
  return null;
}

// Entries newest first, folded at their latest time: a chat with any prompts, and
// more than FOLD_AT of one verb, kind and service
function fold(entries) {
  const groups = new Map();
  for (const e of entries) {
    const key = foldKey(e);
    if (!key) continue;
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(e);
  }
  const folds = ([k, list]) => k.startsWith('chat|') ? list.some(e => e.verb === 'prompted') : list.length > FOLD_AT;
  const folded = new Set([...groups].filter(folds).map(([k]) => k));
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

function groupHtml(list) {
  const first = list[0];
  const kind = first.item ? first.item.kind : 'other';
  const n = list.length;
  let act, titles, landmark = '';
  if (first.item && foldKey(first).startsWith('chat|')) {
    const prompts = list.filter(e => e.verb === 'prompted').length;
    const started = list.some(e => e.verb === 'made');
    act = `${started ? 'new chat' : 'chat'} · ${prompts} ${prompts === 1 ? 'prompt' : 'prompts'}`;
    titles = `<span class="t-title"><a href="/items/${first.item.id}">${escHtml(entryTitle(first))}</a></span>`;
    if (started) landmark = ' landmark';
  } else {
    const plural = PLURALS[kind] || 'things';
    if (first.verb === 'searched') act = `searched ${n} times`;
    else if (first.verb === 'made') { act = `${MADE_GROUP[kind] || 'made'} ${n} ${plural}`; landmark = ' landmark'; }
    else act = `${VERB_WORDS[first.verb] || first.verb} ${n} ${plural}`;
    titles = `<span class="t-title t-titles">${list.slice(0, 3).map(e => escHtml(entryTitle(e))).join(' · ')}</span>`;
  }
  const rows = list.slice(0, SHOWN_IN_GROUP).map(subLine).join('');
  const more = n > SHOWN_IN_GROUP ? `<li class="muted">and ${n - SHOWN_IN_GROUP} more</li>` : '';
  return `<li class="t-row t-group${landmark} kind-${kind}"><details>
    <summary>${head(first, escHtml(act), span(list))}${titles}</summary>
    <ul class="t-subs">${rows}${more}</ul>
  </details></li>`;
}

// The day at a glance: how many of each kind, each a filter. Its dots carry the
// kind colours the rows' edges use.
function summaryHtml(entries, params) {
  const byKind = new Map();
  for (const e of entries) {
    if (!e.item) continue;
    if (!byKind.has(e.item.kind)) byKind.set(e.item.kind, new Set());
    byKind.get(e.item.kind).add(e.item.id);
  }
  const counts = [...byKind].map(([k, ids]) => [k, ids.size]).sort((a, b) => b[1] - a[1]);
  if (!counts.length) return '';
  const day = parseDay(document.getElementById('tab-timeline').dataset.day);
  const chips = counts.map(([k, n]) => {
    const p = new URLSearchParams(params);
    p.set('kind', k);
    const word = n === 1 ? k : (PLURALS[k] || `${k}s`);
    return `<a class="t-chip kind-${k}" href="${dayAddress(day, p)}"><span class="t-dot" aria-hidden="true"></span>${n} ${escHtml(word)}</a>`;
  });
  return `<nav class="t-summary" aria-label="This day by kind">${chips.join('')}</nav>`;
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
    const kindless = new URLSearchParams(params);
    kindless.delete('kind');
    list.innerHTML = (params.get('kind') ? '' : summaryHtml(data.entries, kindless))
      + `<ul class="timeline">${parts.join('')}</ul>`
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
