// Parts of the item page that depend on what the item is (#261): the header with its
// "up" link, the player that stays in view, transcript times that seek it, a post's
// thread and reactions, and parts grouped by kind. legacy.js's renderItemView calls
// these; the rest of the page (tags, actions, edit, links, people) is still there.

import { api, getApiBase } from '../../api.js';
import { escHtml, dateText, sourceLabel } from '../../util.js';
import { renderCard } from './card.js';
import { kindIcon, kindText, personName, generationSettings } from './kinds.js';

// ── Header ──────────────────────────────────────────────────────────────────────

export function itemHeader(item, title) {
  let html = '';
  if (item.parent_id) {
    const label = item.parent_kind ? (KIND_WORDS[item.parent_kind] || 'Part of') : 'Part of';
    html += `<a class="item-up" href="/items/${item.parent_id}">↑ ${escHtml(label)}: ${escHtml(item.parent_title || 'the item this is part of')}</a>`;
  }
  html += `<div class="item-view-title" id="item-title-display"><span class="kind-icon" aria-hidden="true">${kindIcon(item)}</span> ${escHtml(title)}</div>`;
  const parts = [kindText(item), sourceLabel(item.source_type)];
  const when = dateText(item.created_at || (item.metadata && item.metadata.last_at));
  if (when) parts.push(when);
  const by = item.is_own_content ? 'You' : personName(item.author_display, item.source_type);
  if (by && item.kind !== 'video') parts.push(`by ${by}`);
  if (item.kind === 'video' && item.metadata && item.metadata.channel_title) parts.push(item.metadata.channel_title);
  const link = item.url ? ` · <a href="${escHtml(item.url)}" target="_blank" rel="noopener">open original</a>` : '';
  html += `<div class="item-view-meta">${parts.filter(Boolean).map(escHtml).join(' · ')}${link}</div>`;
  const gen = generationSettings(item);
  if (gen.settings.length) {
    html += `<div class="item-settings">${gen.settings.map(([k, v]) => `<span><b>${escHtml(k)}</b> ${escHtml(v)}</span>`).join('')}</div>`;
  }
  if (gen.prompt) html += `<div class="item-prompt"><b>Prompt</b> ${escHtml(gen.prompt)}</div>`;
  return html;
}

const KIND_WORDS = {
  post: 'Reply to the post', notebook: 'In the notebook', book: 'In the book', chat: 'In the chat',
  project: 'In the project', email: 'In the thread', video: 'Of the video', note: 'On the note',
};

// The content without what the header already shows: a heading repeating the title,
// and an artifact's "NotebookLM audio overview" line and raw **option:** lines
export function cleanContent(item, title) {
  let text = item.content || '';
  const lines = text.split('\n');
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
        const who = r.is_own_content ? 'You' : personName(r.actor_display, r.source_type) || 'Someone';
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

// A post's replies as a thread, oldest first; other items' parts grouped by kind
export function renderParts(item, children, list) {
  list.innerHTML = '';
  if (!children.length) return;
  if (children.every(c => c.kind === 'comment')) {
    list.appendChild(thread(children));
    return;
  }
  const groups = new Map();
  for (const c of children) {
    const label = c.kind === 'artifact' ? 'Generated' : (KIND_PLURALS[c.kind] || c.kind_label || c.kind);
    if (!groups.has(label)) groups.set(label, []);
    groups.get(label).push(c);
  }
  for (const [label, items] of groups) {
    const h = document.createElement('div');
    h.className = 'parts-group-title';
    h.textContent = `${label} (${items.length})`;
    list.appendChild(h);
    const sorted = label === 'Generated'
      ? items.slice().sort((a, b) => String(a.kind_detail).localeCompare(String(b.kind_detail)))
      : items;
    sorted.forEach(c => list.appendChild(renderCard(c)));
  }
}

function thread(replies) {
  const box = document.createElement('div');
  box.className = 'thread';
  const sorted = replies.slice().sort((a, b) => String(a.created_at).localeCompare(String(b.created_at)));
  for (const r of sorted) {
    const who = r.is_own_content ? 'You' : personName(r.actor_display, r.source_type) || personName(r.author_display, r.source_type) || 'Someone';
    const row = document.createElement('div');
    row.className = 'thread-row';
    row.innerHTML = `
      <div class="thread-head"><a href="/items/${r.id}">${escHtml(who)}</a> <span class="muted">${escHtml(dateText(r.created_at))}</span></div>
      <div class="thread-text">${linkify(escHtml(r.content || '')).replace(/\n/g, '<br>')}</div>`;
    box.appendChild(row);
  }
  return box;
}
