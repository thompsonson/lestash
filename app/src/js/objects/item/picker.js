// Picking an existing item to attach a share, an image or a link to (#324).
// Before you type it offers what you most likely mean: the item holding the shared
// link and items close to the shared text, then what you opened and worked on lately.
// While you type it searches as the Search tab does, with its kind chips.
//
// Elements by convention, for a prefix: {prefix}-search, {prefix}-results,
// {prefix}-selected. Used by the capture, image, multi-image and link dialogs.

import { api } from '../../api.js';
import { escHtml, sourceLabel } from '../../util.js';

const OPENED_KEY = 'lestash.recentlyOpened';
const OPENED_KEPT = 20;
const SHOWN = 6;          // per section, before you type
const SEARCH_LIMIT = 20;
const SIMILAR_MIN = 25;   // characters of shared text worth a similarity search
const SIMILAR_SHOWN = 3;  // so the recent sections stay in view

// ── Recently opened: kept on this device, newest first ─────────────────────────

function readOpened() {
  try {
    const list = JSON.parse(localStorage.getItem(OPENED_KEY) || '[]');
    return Array.isArray(list) ? list : [];
  } catch {
    return [];
  }
}

// Called when an item's page opens
export function rememberOpened(item) {
  if (!item || !item.id) return;
  const entry = {
    id: item.id,
    title: item.title || (item.content || '').slice(0, 80) || item.url || '',
    kind_label: item.kind_label || '',
    source_type: item.source_type || '',
    held: item.held !== false,
  };
  const list = [entry, ...readOpened().filter(e => e.id !== item.id)].slice(0, OPENED_KEPT);
  try {
    localStorage.setItem(OPENED_KEY, JSON.stringify(list));
  } catch {
    // storage full or blocked: recent items are a convenience
  }
}

// ── State per dialog ───────────────────────────────────────────────────────────

const states = {};
const stateOf = prefix => states[prefix] || (states[prefix] = { seq: 0, kind: '', context: {} });
const el = (prefix, part) => document.getElementById(`${prefix}-${part}`);

// Clear the picker. The context says what is being attached: the shared link and
// text, and the item open (never offered).
export function attachPickerReset(prefix, context = {}) {
  const st = stateOf(prefix);
  if (st.timer) clearTimeout(st.timer);
  Object.assign(st, { timer: null, selectedId: null, kind: '', seq: (st.seq || 0) + 1, context });
  const search = el(prefix, 'search');
  if (search) search.value = '';
  const results = el(prefix, 'results');
  if (results) { results.style.display = 'none'; results.innerHTML = ''; }
  const selected = el(prefix, 'selected');
  if (selected) selected.style.display = 'none';
}

// ── Rows ───────────────────────────────────────────────────────────────────────

function rowTitle(it) {
  return it.title || (it.content || '').slice(0, 80) || it.url || 'Untitled';
}

function rowHtml(prefix, it, why) {
  const title = rowTitle(it);
  const date = it.created_at ? new Date(it.created_at).toLocaleDateString() : '';
  const meta = [
    it.kind_label,
    sourceLabel(it.source_type) || it.source_type,
    it.held === false && it.kind !== 'stub' ? 'not held' : '',  // a stub's label says so
    date,
    why,
  ].filter(Boolean).map(escHtml).join(' · ');
  const on = stateOf(prefix).selectedId === it.id ? ' selected' : '';
  return `<div class="img-attach-result${on}" data-id="${it.id}" data-title="${escHtml(title)}" onclick="attachPickerSelect('${prefix}', ${it.id})">
    <div>${escHtml(title)}</div>
    <div class="result-meta">${meta}</div>
  </div>`;
}

const note = text => `<div class="picker-note">${escHtml(text)}</div>`;

function show(prefix, html) {
  const results = el(prefix, 'results');
  if (!results) return;
  results.innerHTML = html;
  results.style.display = html ? 'block' : 'none';
}

// ── Before you type ────────────────────────────────────────────────────────────

// The shared text without its links, for a similarity search
function sharedWords(text) {
  return (text || '').replace(/https?:\/\/\S+/g, ' ').replace(/\s+/g, ' ').trim();
}

export async function attachPickerSuggest(prefix) {
  const st = stateOf(prefix);
  const search = el(prefix, 'search');
  const typed = () => search && search.value.trim();
  if (typed()) return;
  const seq = ++st.seq;
  const ctx = st.context || {};
  const stale = () => seq !== st.seq || typed();  // reset, typed or asked again since

  const params = new URLSearchParams();
  if (ctx.url) params.set('url', ctx.url);
  if (ctx.exclude) params.set('exclude', String(ctx.exclude));
  const words = sharedWords(ctx.text);
  // Similar text can wait on the embedding model: the rest shows first, and it is
  // added when it comes
  const similarCall = words.length >= SIMILAR_MIN
    ? api(`/api/items/search?q=${encodeURIComponent(words.slice(0, 500))}&mode=semantic&limit=${SIMILAR_SHOWN}`)
      .catch(() => null)
    : null;
  const found = { suggested: null, similar: null };
  const draw = () => {
    const seen = new Set(ctx.exclude ? [Number(ctx.exclude)] : []);
    const fresh = list => (list || []).filter(it => !seen.has(it.id) && seen.add(it.id));
    const sameLink = fresh(found.suggested && found.suggested.same_link);
    const close = fresh(found.similar && (found.similar.items || found.similar)).slice(0, SIMILAR_SHOWN);
    const opened = fresh(readOpened()).slice(0, SHOWN);
    const worked = fresh(found.suggested && found.suggested.worked_on).slice(0, SHOWN);
    const section = (heading, list) => list.length
      ? `<div class="picker-heading">${escHtml(heading)}</div>${list.map(it => rowHtml(prefix, it, '')).join('')}`
      : '';
    const waiting = similarCall && !found.similar ? note('Looking for similar items…') : '';
    const html = section('Likely', [...sameLink, ...close]) + waiting
      + section('Recently opened', opened) + section('Recently worked on', worked);
    show(prefix, html || note('Nothing suggested yet: search above.'));
  };
  show(prefix, note('Finding likely items…'));
  found.suggested = await api(`/api/items/suggest?${params}`).catch(() => null);
  if (stale()) return;
  draw();
  if (similarCall) {
    found.similar = (await similarCall) || { items: [] };
    if (!stale()) draw();
  }
}

// ── While you type ─────────────────────────────────────────────────────────────

function chipsHtml(prefix, facets, kind) {
  if ((facets || []).length < 2 && !kind) return '';
  const chip = (k, text) => `<button type="button" class="chip${k === kind ? ' on' : ''}" onclick="attachPickerKind('${prefix}', '${escHtml(k)}')">${escHtml(text)}</button>`;
  return `<div class="picker-kinds">${chip('', 'All')}${facets.map(f => chip(f.kind, `${f.label} ${f.count}`)).join('')}</div>`;
}

async function runSearch(prefix, query) {
  const st = stateOf(prefix);
  const seq = ++st.seq;
  const kind = st.kind ? `&kind=${encodeURIComponent(st.kind)}` : '';
  try {
    const data = await api(`/api/items/search?q=${encodeURIComponent(query)}&limit=${SEARCH_LIMIT}${kind}`);
    if (seq !== st.seq) return;
    const skip = Number(st.context && st.context.exclude);
    const items = (data.items || data).filter(it => it.id !== skip);
    show(prefix, chipsHtml(prefix, data.facets, st.kind)
      + (items.length ? items.map(it => rowHtml(prefix, it, '')).join('') : note('No items found')));
  } catch {
    if (seq === st.seq) show(prefix, note('Search failed'));
  }
}

export function attachPickerSearch(prefix, query) {
  const st = stateOf(prefix);
  if (st.timer) clearTimeout(st.timer);
  const q = (query || '').trim();
  if (q.length < 2) {
    st.kind = '';
    attachPickerSuggest(prefix);
    return;
  }
  st.timer = setTimeout(() => runSearch(prefix, q), 300);
}

export function attachPickerKind(prefix, kind) {
  const st = stateOf(prefix);
  st.kind = kind;
  const q = (el(prefix, 'search') || {}).value || '';
  if (q.trim().length >= 2) runSearch(prefix, q.trim());
}

// ── Choosing ───────────────────────────────────────────────────────────────────

export function attachPickerSelect(prefix, itemId) {
  stateOf(prefix).selectedId = itemId;
  const results = el(prefix, 'results');
  results.querySelectorAll('.img-attach-result').forEach(r => r.classList.remove('selected'));
  const row = results.querySelector(`[data-id="${itemId}"]`);
  if (row) row.classList.add('selected');
  const label = el(prefix, 'selected');
  label.textContent = 'Attaching to: ' + (row ? row.dataset.title : `Item #${itemId}`);
  label.style.display = 'block';
}

export function attachPickerGetId(prefix) {
  return stateOf(prefix).selectedId ?? null;
}
