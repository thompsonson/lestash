// A person's page (#263): who they are, what they made, what you did with it, and
// their items. One page serves a person, a channel and an AI; empty sections hide.

import { api } from '../../api.js';
import { escHtml, dateText } from '../../util.js';
import { renderCard } from '../item/card.js';
import { PERSON_KIND } from './card.js';
import { setSelfName } from './self.js';

const ROLE_WORDS = {
  author: 'wrote', creator: 'made', narrator: 'narrated', publisher: 'published', recipient: 'received',
};
const VERB_WORDS = {
  watched: 'You watched', viewed: 'You viewed', liked: 'You liked', disliked: 'You disliked',
  shared: 'You shared', prompted: 'You prompted', searched: 'You searched', listened: 'You listened to',
};
const PLURALS = {
  video: 'videos', post: 'posts', book: 'books', paper: 'papers', chat: 'chats', comment: 'replies',
  message: 'messages', document: 'documents', notebook: 'notebooks', note: 'notes', email: 'emails',
};
const PAGE = 20;

const monthYear = at => new Date(at).toLocaleDateString(undefined, { month: 'short', year: 'numeric' });

// "You watched 466 of their videos (502 times), since Jan 2022"
function activityLine(a, noun) {
  const items = a.items || a.count;
  const n = items === 1 ? noun.replace(/s$/, '').replace(/ie$/, 'y') : noun;
  const times = a.count > items ? ` (${a.count.toLocaleString()} times)` : '';
  const span = a.first_at ? `, since ${monthYear(a.first_at)}` : '';
  return `${VERB_WORDS[a.verb] || a.verb} ${items.toLocaleString()} of their ${n}${times}${span}`;
}

// Web and mail addresses only: an address from imported data must not run script
const safeUrl = url => url && /^(https?:|mailto:)/i.test(url) ? url : null;

export async function drawPerson(id, container, url) {
  container.innerHTML = '<div class="empty-state"><div class="spinner"></div></div>';
  let p;
  try {
    p = await api(`/api/people/${id}`);
  } catch (e) {
    if (location.pathname === `/people/${id}`) {
      container.innerHTML = `<div class="empty-state" style="padding:2rem">Person not found: ${escHtml(e.message)}</div>`;
    }
    return { failed: true };
  }
  if (location.pathname !== `/people/${id}`) return {};
  const kind = url.searchParams.get('kind') || '';
  const kindName = p.is_self ? 'You' : (PERSON_KIND[p.kind] || p.kind);
  const accounts = p.accounts.map(a => {
    const href = safeUrl(a.url);
    const label = `${a.platform}${a.display_name && a.display_name !== p.name ? `: ${a.display_name}` : ''}`;
    return href ? `<a href="${escHtml(href)}" target="_blank" rel="noopener">${escHtml(label)} ↗</a>` : `<span>${escHtml(label)}</span>`;
  }).join(' · ');

  // What they made, by kind; each is a filter for the list below
  const byKind = new Map();
  for (const m of p.made) {
    const k = byKind.get(m.kind) || { label: m.kind_label, count: 0, roles: new Set() };
    k.count += m.count;
    k.roles.add(ROLE_WORDS[m.role] || m.role);
    byKind.set(m.kind, k);
  }
  const chips = [...byKind].map(([k, v]) => {
    const on = k === kind;
    return `<a class="chip${on ? ' on' : ''}" href="/people/${p.id}${on ? '' : `?kind=${k}`}" aria-current="${on}">${escHtml(v.label)} ${v.count}</a>`;
  }).join('');
  // Their items' noun: "videos" for a channel that only makes videos, else "items"
  const kinds = new Set(p.made.map(m => m.kind));
  const noun = kinds.size === 1 ? (PLURALS[[...kinds][0]] || 'items') : 'items';
  const nounFor = verb => (verb === 'watched' ? 'videos' : noun);
  const activity = p.your_activity.map(a =>
    `<li>${escHtml(activityLine(a, nounFor(a.verb)))}${a.last_at ? `<span class="muted">, last on ${escHtml(dateText(a.last_at))}</span>` : ''}</li>`).join('');
  // You choose what bylines call you: "by Matt"
  const shortName = p.is_self ? `
    <form class="short-name filters" aria-label="What bylines call you">
      <label for="short-name-input" class="muted">Bylines call you</label>
      <input id="short-name-input" name="short" maxlength="40" value="${escHtml(p.short_name)}">
      <button type="submit">Save</button> <span class="short-name-status muted" aria-live="polite"></span>
    </form>` : '';

  container.innerHTML = `
    <div class="item-view-title"><span class="kind-icon" aria-hidden="true">●</span> ${escHtml(p.name)}</div>
    <div class="item-view-meta">${escHtml(kindName)} · ${p.items} ${p.items === 1 ? 'item' : 'items'}${p.mentioned_in ? ` · mentioned in ${p.mentioned_in}` : ''}</div>
    ${accounts ? `<div class="person-accounts">${accounts}</div>` : ''}
    ${shortName}
    ${activity ? `<div class="item-view-children"><div class="item-view-children-title">With you</div><ul class="person-activity">${activity}</ul></div>` : ''}
    ${chips ? `<div class="item-view-children"><div class="item-view-children-title">Made</div><div class="chips">${chips}</div></div>` : ''}
    <div class="item-list" id="person-items"></div>
    <button class="load-more" id="person-more" style="display:none">Load more</button>`;

  const form = container.querySelector('.short-name');
  if (form) {
    form.addEventListener('submit', async e => {
      e.preventDefault();
      const status = form.querySelector('.short-name-status');
      try {
        const saved = await api(`/api/people/${p.id}`, {
          method: 'PATCH', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ short_name: form.short.value }),
        });
        form.short.value = saved.short_name;
        setSelfName(saved.short_name);
        status.textContent = 'Saved';
      } catch (err) {
        status.textContent = `Could not save: ${err.message}`;
      }
    });
  }
  const list = container.querySelector('#person-items');
  const more = container.querySelector('#person-more');
  let offset = 0;
  const load = async () => {
    more.disabled = true;
    // Every kind, so the list adds up to the counts above it
    const q = new URLSearchParams({ person: p.id, limit: PAGE, offset });
    if (kind) q.set('kind', kind);
    try {
      const data = await api(`/api/items?${q}`);
      if (location.pathname !== `/people/${id}`) return;
      data.items.forEach(item => list.appendChild(renderCard(item)));
      offset += data.items.length;
      more.style.display = offset < data.total ? '' : 'none';
      if (!data.total) list.innerHTML = '<div class="empty-state">No items to show here.</div>';
    } catch (e) {
      list.insertAdjacentHTML('beforeend', `<div class="empty-state">Could not load their items: ${escHtml(e.message)}</div>`);
    } finally {
      more.disabled = false;
    }
  };
  more.addEventListener('click', load);
  await load();
  return {};
}
