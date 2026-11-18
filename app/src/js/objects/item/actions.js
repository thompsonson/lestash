// The item page's actions (#323): one main action that fits the item's kind, then
// three menus. Add: note, link, tag, collection. Go: the post it answers or its
// parent, its day, the original. More: edit, history, copy, share.
// The buttons carry data-action, which the item page's click listener handles, so
// they work the same in a menu; a menu closes once something in it is chosen.

import { api } from '../../api.js';
import { escHtml } from '../../util.js';
import { FILE_TYPES } from './add-files.js';

const pad = n => String(n).padStart(2, '0');

// The local day an item was made, for /day/<date>
export function dayOf(item) {
  const when = item.created_at ? new Date(item.created_at) : null;
  if (!when || Number.isNaN(when.getTime())) return '';
  return `${when.getFullYear()}-${pad(when.getMonth() + 1)}-${pad(when.getDate())}`;
}

// Your LinkedIn reply to a named author is titled by the post it answers, not by
// its own opening words, which show in full below: "Reply to robnewby's post".
// Others' comments, and a title of your own (set with Edit), keep their title.
// Returns '' when the title stays.
const OPENING = 20;  // characters of the text a title made from it starts with
export function replyTitle(item) {
  if (item.kind !== 'comment' || item.source_type !== 'linkedin') return '';
  const raw = (item.metadata && item.metadata.raw) || {};
  const who = item.author_display || '';
  if (!raw.owner || raw.owner !== raw.actor || !who || who === '-' || who.startsWith('urn:')) return '';
  // Comments are stored without a title (the page shows a preview of the text); one
  // set with Edit, not the text's opening, stays
  const opening = (item.content || '').trim().slice(0, OPENING);
  const stored = (item.title || '').trim();
  if (!opening || (stored && !stored.startsWith(opening))) return '';
  return `Reply to ${who}'s post`;
}

const external = (href, text, cls = '') =>
  `<a${cls ? ` class="${cls}"` : ''} href="${escHtml(href)}" target="_blank" rel="noopener">${escHtml(text)}</a>`;

// The one action that fits the kind. A reply's post may only be known from its
// reply link, loaded later: the button waits hidden until then (setAnsweredPost).
function mainAction(item) {
  if (item.kind === 'comment') {
    const post = item.parent_id;
    return `<a class="btn-main" id="item-main-action" href="${post ? `/items/${post}` : '#'}"${post ? '' : ' hidden'}>Open the post</a>`;
  }
  if (item.kind === 'video' && item.url) return external(item.url, 'Watch', 'btn-main');
  if (item.url) return external(item.url, 'Open original', 'btn-main');
  return '';
}

const menu = (label, entries) => {
  const list = entries.filter(Boolean).join('');
  return list
    ? `<details class="item-menu"><summary class="sync-btn">${escHtml(label)}</summary><div class="item-menu-list">${list}</div></details>`
    : '';
};
const action = (name, text, id) =>
  `<button type="button" data-action="${name}"${id ? ` data-id="${id}"` : ''}>${escHtml(text)}</button>`;
// An entry with a short line under it saying what it adds
const hinted = (name, text, hint, id) =>
  `<button type="button" data-action="${name}"${id ? ` data-id="${id}"` : ''}>${escHtml(text)}<small>${escHtml(hint)}</small></button>`;

// ── A stub: a post LeStash knows only by its address (#322) ──────────────────────

// "linkedin/urn:li:activity:7526…", linking to the post: shorter than its address
export function shortLink(item) {
  const url = item.url || '';
  const urn = url.match(/urn:li:[A-Za-z]+:[\w(),:-]+/);
  if (item.source_type === 'linkedin' && urn) return `linkedin/${urn[0]}`;
  return url.replace(/^https?:\/\/(www\.)?/, '').replace(/[?#].*$/, '').replace(/\/$/, '');
}

// Keep a copy: its text pasted, or a screenshot. Each is a child of the stub, so it
// moves to the post if the post is ever imported.
function stubActionsHtml(item) {
  const day = dayOf(item);
  const add = menu('+ Add', [
    action('show-copy-form', 'Its text (paste)'),
    action('add-screenshot', 'A screenshot'),
    action('show-note-form', 'Note'),
    action('focus-tag', 'Tag'),
    `<button type="button" onclick="addToCollection(${item.id})">Collection</button>`,
  ]);
  const go = menu('Go', [
    day ? `<a href="/day/${day}">Its day</a>` : '',
    item.url ? external(item.url, 'Original') : '',
  ]);
  const more = menu('⋯', [
    item.source_type === 'linkedin' ? action('fetch-preview', 'Fetch its preview', item.id) : '',
    action('copy-link', 'Copy link', item.id),
  ]);
  return `<div class="item-detail-actions" id="item-detail-actions">
    <button type="button" class="btn-main" data-action="show-copy-form">Keep a copy</button>${add}${go}${more}
    <select id="add-to-coll-select" style="display:none" onchange="confirmAddToCollection(this)"></select>
  </div>
  <div id="copy-form" class="copy-form" hidden>
    <textarea id="copy-text" rows="6" placeholder="Paste the post's text"></textarea>
    <div class="copy-form-actions">
      <button class="sync-btn" type="button" data-action="save-copy" data-id="${item.id}">Keep</button>
      <button class="sync-btn" type="button" data-action="cancel-copy">Cancel</button>
    </div>
  </div>
  <input type="file" id="copy-image-input" accept="image/*" hidden>`;
}

export async function saveCopy(itemId, text) {
  return api(`/api/items/${itemId}/copies`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ content: text }),
  });
}

// A screenshot: the copy and its file in one request, so a failed upload leaves none
export async function saveScreenshot(itemId, file) {
  const form = new FormData();
  form.append('file', file, file.name || 'screenshot.png');
  return api(`/api/items/${itemId}/copies/screenshot`, { method: 'POST', body: form });
}

export async function fetchPreview(itemId) {
  return api(`/api/items/${itemId}/preview`, { method: 'POST' });
}

export function actionsHtml(item) {
  if (item.held === false) return stubActionsHtml(item);
  const day = dayOf(item);
  const add = menu('+ Add', [
    hinted('show-note-form', 'Note', 'your own text, under this item'),
    hinted('add-link', 'New link', 'a page or video not yet in LeStash'),
    hinted('add-images', 'Images', 'one or more, each its own item'),
    hinted('add-files', 'File', 'PDF, web page, text or Markdown'),
    '<hr class="item-menu-sep">',
    hinted('link-item', 'Link to an item', 'one already in LeStash', item.id),
    action('focus-tag', 'Tag'),
    `<button type="button" onclick="addToCollection(${item.id})">Collection</button>`,
  ]);
  const go = menu('Go', [
    item.kind === 'comment'
      ? `<a id="go-answered" href="${item.parent_id ? `/items/${item.parent_id}` : '#'}"${item.parent_id ? '' : ' hidden'}>The post it answers</a>`
      : (item.parent_id ? `<a href="/items/${item.parent_id}">Its parent</a>` : ''),
    day ? `<a href="/day/${day}">Its day</a>` : '',
    item.url ? external(item.url, 'Original') : '',
  ]);
  const more = menu('⋯', [
    action('edit-item', 'Edit', item.id),
    action('toggle-history', 'History', item.id),
    action('copy', 'Copy text', item.id),
    action('copy-link', 'Copy link', item.id),
    // Micro.blog takes what you wrote, not your replies to others
    item.is_own_content && item.kind !== 'comment' && item.kind !== 'reaction' ? `<button type="button" onclick="openMicroblogCompose(${item.id})">Share to Micro.blog</button>` : '',
  ]);
  return `<div class="item-detail-actions" id="item-detail-actions">
    ${mainAction(item)}${add}${go}${more}
    <select id="add-to-coll-select" style="display:none" onchange="confirmAddToCollection(this)"></select>
  </div>
  <input type="file" id="add-images-input" accept="image/*" multiple hidden>
  <input type="file" id="add-files-input" accept="${FILE_TYPES}" multiple hidden>`;
}

// A top-level reply's post, once its reply link has loaded
export function setAnsweredPost(postId) {
  for (const id of ['item-main-action', 'go-answered']) {
    const link = document.getElementById(id);
    if (!link) continue;
    link.href = `/items/${postId}`;
    link.hidden = false;
  }
}

// Menus: one open at a time, closed after a choice. "Tag" moves to the tag field.
export function wireItemActions(container) {
  const bar = container.querySelector('#item-detail-actions');
  if (!bar) return;
  bar.addEventListener('toggle', e => {
    if (!e.target.open) return;
    bar.querySelectorAll('details.item-menu[open]').forEach(d => { if (d !== e.target) d.open = false; });
    // Opens leftwards when it would run off the screen
    const list = e.target.querySelector('.item-menu-list');
    list.classList.remove('to-left');
    if (list.getBoundingClientRect().right > document.documentElement.clientWidth - 8) {
      list.classList.add('to-left');
    }
  }, true);
  bar.addEventListener('click', e => {
    const chosen = e.target.closest('.item-menu-list > *');
    if (!chosen) return;
    chosen.closest('details').open = false;
    if (chosen.dataset.action === 'focus-tag') {
      const input = container.querySelector('[data-tag-input]');
      if (input) { input.scrollIntoView({ block: 'center' }); input.focus(); }
    }
  });
}
