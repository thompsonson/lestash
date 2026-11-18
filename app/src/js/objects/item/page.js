// Parts of the item page that depend on what the item is (#261): the header with its
// "up" link, the player that stays in view, transcript times that seek it, a post's
// thread and reactions, and parts grouped by kind. legacy.js's renderItemView calls
// these; the rest of the page (tags, actions, edit, links, people) is still there.

import { api, getApiBase } from '../../api.js';
import { escHtml, dateText, sourceLabel } from '../../util.js';
import { renderCard, byName } from './card.js';
import { kindText, personName, generationSettings } from './kinds.js';
import { selfName, selfFullName } from '../person/self.js';
import { shortLink } from './actions.js';

// ── Header ──────────────────────────────────────────────────────────────────────

// A post's title is often its first words, cut off, which the text then repeats.
// Returns the title to show once: the whole first line when it is short, else the
// kind ("Post on LinkedIn"); and whether the text should drop that first line.
export function titleOnce(item, title) {
  const same = { title, dropFirstLine: false };
  if (!['post', 'comment', 'note', 'message'].includes(item.kind)) return same;
  const text = (item.content || '').trim();
  const stem = (title || '').replace(/(\.\.\.|…)\s*$/, '').trim();
  if (!stem || !text.startsWith(stem)) return same;
  const first = text.split('\n')[0].trim();
  if (first.length <= 120) return { title: first, dropFirstLine: true };
  return { title: `${kindText(item)} on ${sourceLabel(item.source_type)}`, dropFirstLine: false };
}

export function itemHeader(item, title) {
  let html = '';
  if (item.parent_id) {
    const label = item.parent_kind ? (KIND_WORDS[item.parent_kind] || 'Part of') : 'Part of';
    html += `<a class="item-up" href="/items/${item.parent_id}">↑ ${escHtml(label)}: ${escHtml(item.parent_title || 'the item this is part of')}</a>`;
  }
  html += `<div class="item-view-title" id="item-title-display">${escHtml(title)}</div>`;
  const parts = [kindText(item)];
  const when = dateText(item.created_at || (item.metadata && item.metadata.last_at));
  if (when) parts.push(when);
  if (item.kind === 'video' && item.metadata && item.metadata.channel_title) parts.push(item.metadata.channel_title);
  const meta = parts.filter(Boolean).map(escHtml);
  meta.splice(1, 0, whereFrom(item));
  // "by Matt", linked to the person it is by
  const by = item.kind === 'video' ? '' : byName(item);
  if (by) meta.push(item.by_id ? `by <a href="/people/${item.by_id}">${escHtml(by)}</a>` : `by ${escHtml(by)}`);
  // A stub: by its post's author when its preview names one, and its short address
  else if (item.held === false && item.author_display && item.author_display !== '-') meta.push(`by ${escHtml(item.author_display)}`);
  if (item.url && item.held === false) {
    meta.push(`<a class="item-short-link" href="${escHtml(item.url)}" target="_blank" rel="noopener">${escHtml(shortLink(item))}</a>`);
  } else if (item.url) {
    meta.push(`<a href="${escHtml(item.url)}" target="_blank" rel="noopener">open original</a>`);
  }
  meta.push(escHtml(goneFromKobo(item)));
  html += `<div class="item-view-meta">${meta.filter(Boolean).join(' · ')}</div>`;
  html += frontMatterHtml(item);
  const gen = generationSettings(item);
  if (gen.settings.length) {
    html += `<div class="item-settings">${gen.settings.map(([k, v]) => `<span><b>${escHtml(k)}</b> ${escHtml(v)}</span>`).join('')}</div>`;
  }
  // A long prompt folds to two lines, so the player stays near the top
  if (gen.prompt) {
    html += `<details class="item-prompt"><summary><b>Prompt</b> <span class="clamp">${escHtml(gen.prompt)}</span></summary></details>`;
  }
  return html;
}

// A Kobo book no longer on the Kobo (#314): "not on the Kobo since 14 Nov 2026".
// The time is UTC, written as SQLite writes it.
export function goneFromKobo(item) {
  const since = item.source_type === 'kobo' && item.metadata && item.metadata.removed_from_device;
  if (typeof since !== 'string') return '';
  const when = dateText(since.replace(' ', 'T') + 'Z');
  return when ? `not on the Kobo since ${when}` : 'not on the Kobo';
}

// Where the item comes from: the service, and for a file in a repository, the
// repository and the file, linked: "GitHub · thompsonson/atomicguard · x.md"
export function whereFrom(item) {
  const m = item.metadata || {};
  if (typeof m.repo === 'string' && typeof m.path === 'string') {
    const ref = typeof m.commit === 'string' && m.commit ? m.commit : 'HEAD';
    const repo = `https://github.com/${m.repo}`;
    return `GitHub · <a href="${escHtml(repo)}" target="_blank" rel="noopener">${escHtml(m.repo)}</a>`
      + ` · <a href="${escHtml(`${repo}/blob/${ref}/${m.path}`)}" target="_blank" rel="noopener" title="${escHtml(m.path)}">${escHtml(m.path.split('/').pop())}</a>`;
  }
  return escHtml(sourceLabel(item.source_type));
}

// A Markdown file's front matter: the block between "---" lines at the top.
// Returns its fields, [key, value] with a list's lines joined, and the text after it.
export function splitFrontMatter(text) {
  const match = /^\s*---\r?\n([\s\S]*?)\r?\n---[ \t]*(\r?\n|$)/.exec(text || '');
  if (!match) return { fields: [], body: text || '' };
  const fields = [];
  for (const line of match[1].split(/\r?\n/)) {
    const field = /^([A-Za-z_][\w -]*):\s*(.*)$/.exec(line);
    if (field) { fields.push([field[1].trim(), field[2]]); continue; }
    if (!line.trim()) continue;
    // Anything but "key: value" and its indented or "- " lines: not front matter
    if (!fields.length || !/^(\s|- )/.test(line)) return { fields: [], body: text };
    {
      const last = fields[fields.length - 1];
      last[1] = [last[1], line.trim().replace(/^- /, '')].filter(Boolean).join('\n');
    }
  }
  const unquote = v => v.trim().replace(/^(["'])(.*)\1$/, '$2');
  return {
    fields: fields.map(([k, v]) => [k, v.split('\n').map(unquote).join('\n')]).filter(([, v]) => v),
    body: text.slice(match[0].length),
  };
}

// The front matter folded under the header, without a title or date the header
// already shows the same
function frontMatterHtml(item) {
  const { fields } = splitFrontMatter(item.content);
  const same = ([k, v]) => {
    const key = k.toLowerCase();
    if (key === 'title') return v.trim() === (item.title || '').trim();
    if (key === 'date') return dateText(v) && dateText(v) === dateText(item.created_at);
    return false;
  };
  const shown = fields.filter(f => !same(f));
  if (!shown.length) return '';
  return `<details class="item-front"><summary>File details <span class="muted">· ${shown.map(([k]) => escHtml(k)).join(', ')}</span></summary>
    <dl>${shown.map(([k, v]) => `<dt>${escHtml(k)}</dt><dd>${escHtml(v).replace(/\n/g, '<br>')}</dd>`).join('')}</dl></details>`;
}

const KIND_WORDS = {
  post: 'Reply to the post', notebook: 'In the notebook', book: 'In the book', chat: 'In the chat',
  project: 'In the project', email: 'In the thread', video: 'Of the video', note: 'On the note',
};

// The content without what the header already shows: a heading repeating the title,
// and an artifact's "NotebookLM audio overview" line and raw **option:** lines
export function cleanContent(item, title, { dropFirstLine = false } = {}) {
  let text = splitFrontMatter(item.content || '').body;
  const lines = text.trim().split('\n');
  if (dropFirstLine) lines.shift();
  const first = lines.findIndex(l => l.trim());
  // The title as stored, without the emoji the header puts before it
  const bare = (item.title || title || '').trim();
  if (first >= 0 && /^#{1,3}\s+/.test(lines[first]) && lines[first].replace(/^#{1,3}\s+/, '').trim() === bare) {
    lines.splice(first, 1);
  }
  text = lines.join('\n');
  if (item.kind === 'artifact') {
    // Only the lines the importer wrote from this item's own settings, which the
    // header shows in words; other bold labels in a report's text stay
    const options = item.metadata && item.metadata.options;
    const keys = Object.keys((options && options.generationOptions) || {});
    text = text.replace(/^NotebookLM [a-z ]+$/m, '');
    for (const key of keys) {
      text = text.replace(new RegExp(`^\\*\\*${key}:\\*\\* .*$`, 'gm'), '');
    }
  }
  return text.replace(/\n{3,}/g, '\n\n').trim();
}

// ── Player ──────────────────────────────────────────────────────────────────────

// The first audio or video file, in a player that stays at the top of the page while
// the transcript scrolls under it. Returns { html, ids } (the media shown).
export function itemPlayer(item) {
  const m = (item.media || []).find(x => (x.media_type === 'audio' || x.media_type === 'video') && x.available && !x.superseded_at);
  if (!m) return { html: '', ids: new Set() };
  const src = `${getApiBase()}${escHtml(m.serve_url)}`;
  const tag = m.media_type === 'video'
    ? `<video id="item-player" controls preload="metadata" src="${src}"></video>`
    : `<audio id="item-player" controls preload="metadata" src="${src}"></audio>`;
  return { html: `<div class="item-player">${tag}</div>`, ids: new Set([m.id]) };
}

const TIME = /\((\d{1,2}:)?\d{1,2}:\d{2}\)|^(\d{1,2}:)?\d{1,2}:\d{2}$/;

function seconds(text) {
  return text.replace(/[()]/g, '').split(':').reduce((t, n) => t * 60 + Number(n), 0);
}

// Transcript times, "(2:14)" after a speaker and "## 12:34" headings, become buttons
// that move the player there
export function wireTranscript(container) {
  const player = container.querySelector('#item-player');
  const content = container.querySelector('#item-content-display');
  if (!player || !content) return;
  const walker = document.createTreeWalker(content, NodeFilter.SHOW_TEXT);
  const hits = [];
  for (let n = walker.nextNode(); n; n = walker.nextNode()) {
    if (TIME.test(n.nodeValue.trim()) || /\((\d{1,2}:)?\d{1,2}:\d{2}\)/.test(n.nodeValue)) hits.push(n);
  }
  for (const node of hits) {
    const frag = document.createDocumentFragment();
    const parts = node.nodeValue.split(/(\((?:\d{1,2}:)?\d{1,2}:\d{2}\)|^\s*(?:\d{1,2}:)?\d{1,2}:\d{2}\s*$)/);
    for (const part of parts) {
      if (!part) continue;
      if (TIME.test(part.trim())) {
        const b = document.createElement('button');
        b.type = 'button';
        b.className = 'seek';
        b.textContent = part.trim();
        b.dataset.t = String(seconds(part.trim()));
        b.title = `Play from ${part.trim().replace(/[()]/g, '')}`;
        frag.appendChild(b);
      } else {
        frag.appendChild(document.createTextNode(part));
      }
    }
    node.parentNode.replaceChild(frag, node);
  }
  content.addEventListener('click', e => {
    const b = e.target.closest('button.seek');
    if (!b) return;
    player.currentTime = Number(b.dataset.t);
    player.play().catch(() => {});
  });
}

// ── Reactions ───────────────────────────────────────────────────────────────────

// One line, "♥ 14 reactions", that opens the list of who reacted
export function reactionsLine(item) {
  if (!item.reaction_count) return '';
  const n = item.reaction_count;
  return `<div class="item-reactions" data-item-id="${item.id}">
    <button type="button" class="reactions-toggle" aria-expanded="false">♥ ${n} ${n === 1 ? 'reaction' : 'reactions'}</button>
    <div class="reactions-list" hidden></div>
  </div>`;
}

export function wireReactions(container, item) {
  const box = container.querySelector('.item-reactions');
  if (!box) return;
  const button = box.querySelector('.reactions-toggle');
  const list = box.querySelector('.reactions-list');
  button.addEventListener('click', async () => {
    const open = button.getAttribute('aria-expanded') === 'true';
    button.setAttribute('aria-expanded', String(!open));
    list.hidden = open;
    if (open || list.dataset.loaded) return;
    list.textContent = 'Loading…';
    try {
      const reactions = [];
      for (let offset = 0; ; offset += 100) {
        const page = await api(`/api/items?parent_id=${item.id}&kind=reaction&limit=100&offset=${offset}`);
        reactions.push(...page.items);
        if (page.items.length < 100 || reactions.length >= page.total) break;
      }
      list.dataset.loaded = '1';
      list.innerHTML = reactions.map(r => {
        const who = r.is_own_content ? selfName() : personName(r.actor_display, r.source_type) || 'Someone';
        const how = (r.metadata && r.metadata.reaction_type || 'like').toLowerCase();
        return `<a href="/items/${r.id}">${escHtml(who)}</a> <span class="muted">${escHtml(how)}</span>`;
      }).join(' · ');
    } catch {
      list.textContent = 'Could not load the reactions';
    }
  });
}

// ── Parts ───────────────────────────────────────────────────────────────────────

const KIND_PLURALS = {
  highlight: 'Highlights', note: 'Notes', chat: 'Chats', turn: 'Turns', document: 'Documents',
  comment: 'Replies', message: 'Messages', image: 'Images', transcript: 'Transcripts',
  video: 'Videos', post: 'Posts', paper: 'Papers', book: 'Books', stub: 'Not held',
};

// Web addresses in escaped text become links that open in a new tab
function linkify(escaped) {
  return escaped.replace(/\bhttps?:\/\/[^\s<]+[^\s<.,;:!?)\]'"]/g,
    url => `<a href="${url}" target="_blank" rel="noopener">${url}</a>`);
}

// The children request: reactions are a count on the post, never cards
export const PARTS_QUERY = '&exclude_kind=reaction';

// ── Parts: order and filter (#365) ──
// Newest first by default. "Oldest first" is kept per item, on this device; the kind
// filter lasts while the item stays open. Long groups show their first few parts.

// The items shown oldest first, the latest choice last; newest first is the default,
// so it needs no entry
const ORDER_KEY = 'lestash-parts-oldest-first';
const ORDERS_KEPT = 500;  // items whose order is kept; the oldest choice goes first
const GROUP_SHOWN = 4;

function oldestFirst() {
  try {
    const ids = JSON.parse(localStorage.getItem(ORDER_KEY) || '[]');
    return Array.isArray(ids) ? ids : [];
  } catch { return []; }
}

export function partsOrder(itemId) {
  return oldestFirst().includes(itemId) ? 'oldest' : 'newest';
}

function keepOrder(itemId, order) {
  const ids = oldestFirst().filter(id => id !== itemId);
  if (order === 'oldest') ids.push(itemId);
  try { localStorage.setItem(ORDER_KEY, JSON.stringify(ids.slice(-ORDERS_KEPT))); } catch { /* private mode */ }
}

// A part's time as a number; one with no time sorts last either way
function when(c) {
  const t = Date.parse(String(c.created_at || '').replace(' ', 'T').replace(/(\.\d{3})\d+/, '$1'));
  return Number.isNaN(t) ? null : t;
}

export function sortParts(children, order) {
  const sign = order === 'oldest' ? 1 : -1;
  return children.slice().sort((a, b) => {
    const ta = when(a), tb = when(b);
    if (ta === null || tb === null) return (ta === null) - (tb === null);
    return sign * (ta - tb) || sign * (a.id - b.id);
  });
}

const groupLabel = c => c.kind === 'artifact' ? 'Generated' : (KIND_PLURALS[c.kind] || c.kind_label || c.kind);

// What a drawn list of parts shows: the kind it is filtered to, and the groups opened
// with "show all". Kept on the list, so each item page starts with All, folded.
const viewOf = list => list._view || (list._view = { label: null, open: new Set() });
// While the order menu is open: the listener that closes it on a tap elsewhere
let closeOnTapAway = null;

function stopTapAway() {
  if (closeOnTapAway) document.removeEventListener('click', closeOnTapAway, true);
  closeOnTapAway = null;
}

// A part added while the item is open (a note, a linked item): drawn in its place
// among the others, keeping what is open. A chat's turns are appended, as before.
export function addPart(list, child, item) {
  if (list.querySelector(':scope > .chat-thread')) {
    list.appendChild(renderCard(child));
    return;
  }
  const state = list._parts || { item: item || { id: Number(list.dataset.itemId) }, children: [] };
  // You see the new part: a filter that would hide it is lifted, and its group opened
  const view = viewOf(list);
  const label = groupLabel(child);
  if (view.label && view.label !== label) view.label = null;
  view.open.add(label);
  state.children.push(child);
  renderParts(state.item, state.children, list);
}

function orderMenu(order) {
  const opt = (value, text) =>
    `<button type="button" data-parts-order="${value}"${value === order ? ' aria-current="true"' : ''}>${value === order ? '✓ ' : ''}${text}</button>`;
  return `<details class="item-menu parts-order"><summary>${order === 'oldest' ? 'Oldest first' : 'Newest first'} ▾</summary>`
    + `<div class="item-menu-list to-left">${opt('newest', 'Newest first')}${opt('oldest', 'Oldest first')}`
    + '<hr class="item-menu-sep"><small class="parts-order-note">Kept for this item</small></div></details>';
}

// A post's replies as a thread, oldest first; other items' parts grouped by kind,
// newest or oldest first, with chips to show one kind
export const PARTS_LOAD_FAILED = '<span class="parts-load-failed" style="color:var(--muted);font-size:0.8rem">Could not load these items</span>';

export function renderParts(item, children, list) {
  // A failed load is said above whatever parts were added since
  list.innerHTML = list._loadFailed ? PARTS_LOAD_FAILED : '';
  list._parts = { item, children };
  stopTapAway();
  const head = list.closest('.item-view-children')?.querySelector('.parts-order-slot');
  if (head) head.innerHTML = '';
  if (!children.length) return;
  if (children.every(c => c.kind === 'comment')) {
    list.appendChild(thread(children));
    return;
  }
  const view = viewOf(list);
  const order = partsOrder(item.id);
  const byLabel = new Map();
  for (const c of sortParts(children, order)) {
    const label = groupLabel(c);
    if (!byLabel.has(label)) byLabel.set(label, []);
    byLabel.get(label).push(c);
  }
  // Generated parts go by type (audio, slides …), each in the chosen order
  const generated = byLabel.get('Generated');
  if (generated) generated.sort((a, b) => String(a.kind_detail).localeCompare(String(b.kind_detail)));
  // Largest group first, so the chips and groups stay put when the order changes
  const groups = new Map([...byLabel].sort((a, b) => b[1].length - a[1].length || a[0].localeCompare(b[0])));
  if (!groups.has(view.label)) view.label = null;
  const redraw = () => renderParts(item, children, list);

  if (head) {
    head.innerHTML = orderMenu(order);
    const menu = head.querySelector('details');
    // A tap anywhere else closes it; one listener, only while it is open
    menu.addEventListener('toggle', () => {
      stopTapAway();
      if (!menu.open) return;
      closeOnTapAway = e => { if (!menu.contains(e.target)) menu.open = false; };
      document.addEventListener('click', closeOnTapAway, true);
    });
    head.querySelector('.item-menu-list').addEventListener('click', e => {
      const value = e.target.closest('[data-parts-order]')?.dataset.partsOrder;
      if (!value) return;
      keepOrder(item.id, value);
      redraw();
    });
  }
  if (groups.size > 1) {
    const chips = document.createElement('div');
    chips.className = 'chips parts-chips';
    const chip = (label, n) =>
      `<button type="button" class="chip${view.label === label ? ' on' : ''}" aria-pressed="${view.label === label}"`
      + ` data-kind="${escHtml(label ?? '')}">${escHtml(label ?? 'All')} <span class="chip-count">${n}</span></button>`;
    chips.innerHTML = chip(null, children.length) + [...groups].map(([label, items]) => chip(label, items.length)).join('');
    chips.addEventListener('click', e => {
      const btn = e.target.closest('[data-kind]');
      if (!btn) return;
      view.label = btn.dataset.kind || null;
      redraw();
    });
    list.appendChild(chips);
  }
  const card = c => renderCard(c, { within: item });
  if (view.label) {
    // One kind: every part of it, without a group heading
    groups.get(view.label).forEach(c => list.appendChild(card(c)));
    return;
  }
  for (const [label, items] of groups) {
    const h = document.createElement('div');
    h.className = 'parts-group-title';
    h.textContent = `${label} (${items.length})`;
    list.appendChild(h);
    const folded = items.length > GROUP_SHOWN && !view.open.has(label);
    (folded ? items.slice(0, GROUP_SHOWN) : items).forEach(c => list.appendChild(card(c)));
    if (folded) {
      const more = document.createElement('button');
      more.type = 'button';
      more.className = 'parts-more';
      more.textContent = `+ ${items.length - GROUP_SHOWN} more ${label.toLowerCase()} · show all`;
      more.addEventListener('click', () => {
        view.open.add(label);
        redraw();
      });
      list.appendChild(more);
    }
  }
}

// Replies as a conversation: each author's initial, their name once per run of
// replies, and their words. A name a reply starts with (whom it answers) is marked.
function thread(replies) {
  const box = document.createElement('div');
  box.className = 'thread';
  const sorted = replies.slice().sort((a, b) => String(a.created_at).localeCompare(String(b.created_at)));
  const who = r => r.by_name || (r.is_own_content ? selfName() : personName(r.actor_display, r.source_type) || personName(r.author_display, r.source_type) || 'Someone');
  // Names a reply may start with: the repliers' and yours in full
  const names = [...new Set([...sorted.map(who), selfFullName()])]
    .filter(n => n && n.split(' ').length > 1).sort((a, b) => b.length - a.length);
  let last = null;
  for (const r of sorted) {
    const name = who(r);
    const row = document.createElement('div');
    row.className = 'thread-row' + (name === last ? ' same-author' : '');
    row.style.setProperty('--hue', String([...name].reduce((h, c) => (h * 31 + c.charCodeAt(0)) % 360, 7)));
    let text = r.content || '';
    const answered = names.find(n => n !== name && !(r.by_self && n === selfFullName()) && text.startsWith(n));
    let lead = '';
    if (answered) {
      lead = `<span class="mention">${escHtml(answered)}</span>`;
      text = text.slice(answered.length);
    }
    const person = r.by_id ? `<a href="/people/${r.by_id}">${escHtml(name)}</a>` : escHtml(name);
    row.innerHTML = `
      <span class="avatar" aria-hidden="true">${escHtml((name.match(/\p{L}|\p{N}/u) || ['?'])[0].toUpperCase())}</span>
      <div class="thread-body">
        <div class="thread-head">${person} <a class="muted" href="/items/${r.id}">${escHtml(dateText(r.created_at))}</a></div>
        <div class="thread-text">${lead}${linkify(escHtml(text)).replace(/\n/g, '<br>')}</div>
      </div>`;
    box.appendChild(row);
    last = name;
  }
  return box;
}
