// A person's page (#263): who they are, what they made, what you did with it, and
// their items. One page serves a person, a channel and an AI; empty sections hide.

import { api } from '../../api.js';
import { escHtml, dateText } from '../../util.js';
import { renderCard } from '../item/card.js';
import { PERSON_KIND } from './card.js';

const ROLE_WORDS = {
  author: 'wrote', creator: 'made', narrator: 'narrated', publisher: 'published', recipient: 'received',
};
const VERB_WORDS = {
  watched: 'You watched', viewed: 'You viewed', liked: 'You liked', disliked: 'You disliked',
  shared: 'You shared', prompted: 'You prompted', searched: 'You searched', listened: 'You listened to',
};
const PAGE = 20;

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
  const activity = p.your_activity.map(a =>
    `<li>${escHtml(VERB_WORDS[a.verb] || a.verb)} ${a.count} of their items${a.last_at ? `, last on ${escHtml(dateText(a.last_at))}` : ''}</li>`).join('');

  container.innerHTML = `
    <div class="item-view-title"><span class="kind-icon" aria-hidden="true">●</span> ${escHtml(p.name)}</div>
    <div class="item-view-meta">${escHtml(kindName)} · ${p.items} ${p.items === 1 ? 'item' : 'items'}${p.mentioned_in ? ` · mentioned in ${p.mentioned_in}` : ''}</div>
    ${accounts ? `<div class="person-accounts">${accounts}</div>` : ''}
    ${activity ? `<div class="item-view-children"><div class="item-view-children-title">With you</div><ul class="person-activity">${activity}</ul></div>` : ''}
    ${chips ? `<div class="item-view-children"><div class="item-view-children-title">Made</div><div class="chips">${chips}</div></div>` : ''}
    <div class="item-list" id="person-items"></div>
    <button class="load-more" id="person-more" style="display:none">Load more</button>`;

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
