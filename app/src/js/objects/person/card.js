// A person's card: a person, a channel or an AI, with how many items they have and
// where their accounts are. Cards are links to /people/<id>.

import { escHtml } from '../../util.js';

export const PERSON_KIND = { person: 'Person', group: 'Group', organization: 'Channel', software: 'AI' };
const ICON = { person: '●', group: '◎', organization: '▶', software: '✦' };

export function renderPersonCard(p) {
  const a = document.createElement('a');
  a.className = 'item-card person-card';
  a.href = `/people/${p.id}`;
  const kind = p.is_self ? 'You' : (PERSON_KIND[p.kind] || p.kind);
  const where = (p.platforms || []).join(' · ');
  a.innerHTML = `
    <div class="item-card-header">
      <span class="kind-icon" aria-hidden="true">${ICON[p.kind] || '●'}</span>
      <span class="item-title">${escHtml(p.name)}</span>
    </div>
    <div class="item-card-line">${escHtml([kind, where].filter(Boolean).join(' · '))}</div>
    <div class="item-card-counts">${p.items} ${p.items === 1 ? 'item' : 'items'}</div>`;
  return a;
}
