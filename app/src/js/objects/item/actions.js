// The item page's actions (#323): one main action that fits the item's kind, then
// three menus. Add: note, link, tag, collection. Go: the post it answers or its
// parent, its day, the original. More: edit, history, copy, share.
// The buttons carry data-action, which the item page's click listener handles, so
// they work the same in a menu; a menu closes once something in it is chosen.

import { escHtml } from '../../util.js';

const pad = n => String(n).padStart(2, '0');

// The local day an item was made, for /day/<date>
export function dayOf(item) {
  const when = item.created_at ? new Date(item.created_at) : null;
  if (!when || Number.isNaN(when.getTime())) return '';
  return `${when.getFullYear()}-${pad(when.getMonth() + 1)}-${pad(when.getDate())}`;
}

// Your LinkedIn reply to a named author is titled by the post it answers, not by
// its own opening words, which show in full below: "Reply to robnewby's post".
// Others' comments, and a title that isn't the reply's opening (one you edited),
// keep their title. Returns '' when the title stays.
const OPENING = 20;  // characters of the text a title made from it starts with
export function replyTitle(item) {
  if (item.kind !== 'comment' || item.source_type !== 'linkedin') return '';
  const raw = (item.metadata && item.metadata.raw) || {};
  const who = item.author_display || '';
  if (!raw.owner || raw.owner !== raw.actor || !who || who === '-' || who.startsWith('urn:')) return '';
  const opening = (item.content || '').trim().slice(0, OPENING);
  if (!opening || !(item.title || '').trim().startsWith(opening)) return '';
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

export function actionsHtml(item) {
  const day = dayOf(item);
  const add = menu('+ Add', [
    action('show-note-form', 'Note'),
    action('link-item', 'Link to an item', item.id),
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
  </div>`;
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
