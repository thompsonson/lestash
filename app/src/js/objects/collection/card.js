// A collection's card: its name, description, item count, and an "On Kobo" badge when
// it is shown on the Kobo (#315). Cards are links to /collections/<id>.

import { escHtml, relTime } from '../../util.js';

export function renderCollectionCard(c) {
  const card = document.createElement('a');
  card.className = 'item-card';
  card.href = `/collections/${c.id}`;
  const onKobo = c.kobo && c.kobo.shown ? '<span class="chip on collection-badge">On Kobo</span>' : '';
  card.innerHTML = `
    <div class="item-card-header">
      <span class="item-title">${escHtml(c.name)}</span>
      <span class="item-date">${relTime(c.created_at)}</span>
    </div>
    ${c.description ? `<div class="item-preview">${escHtml(c.description)}</div>` : ''}
    <div class="item-footer">
      <span>${c.item_count} item${c.item_count !== 1 ? 's' : ''}</span>${onKobo}
    </div>`;
  return card;
}
