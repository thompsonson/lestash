// Parts of the item page that depend on what the item is (#261): the header with its
// "up" link, the player that stays in view, transcript times that seek it, a post's
// thread and reactions, and parts grouped by kind. legacy.js's renderItemView calls
// these; the rest of the page (tags, actions, edit, links, people) is still there.

import { api, getApiBase } from '../../api.js';
import { escHtml, dateText, sourceLabel } from '../../util.js';
import { renderCard, byName } from './card.js';
import { kindIcon, kindText, personName, generationSettings } from './kinds.js';
import { selfName, selfFullName } from '../person/self.js';

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
  html += `<div class="item-view-title" id="item-title-display"><span class="kind-icon" aria-hidden="true">${kindIcon(item)}</span> ${escHtml(title)}</div>`;
  const parts = [kindText(item)];
  const when = dateText(item.created_at || (item.metadata && item.metadata.last_at));
  if (when) parts.push(when);
  if (item.kind === 'video' && item.metadata && item.metadata.channel_title) parts.push(item.metadata.channel_title);
  const meta = parts.filter(Boolean).map(escHtml);
  meta.splice(1, 0, whereFrom(item));
  // "by Matt", linked to the person it is by
  const by = item.kind === 'video' ? '' : byName(item);
  if (by) meta.push(item.by_id ? `by <a href="/people/${item.by_id}">${escHtml(by)}</a>` : `by ${escHtml(by)}`);
  if (item.url) meta.push(`<a href="${escHtml(item.url)}" target="_blank" rel="noopener">open original</a>`);
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
    sorted.forEach(c => list.appendChild(renderCard(c, { within: item })));
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
