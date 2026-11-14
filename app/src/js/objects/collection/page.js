// A collection's page: its items in reading order (#317), with a way to move each, and
// the Kobo panel (#315): a "Show on Kobo" switch and how far the Kobo is from it.
// The Kobo is written at the next plug-in, so the switch only says what should happen.

import { api } from '../../api.js';
import { escHtml, dateText } from '../../util.js';
import { renderCard } from '../item/card.js';

// SQLite writes UTC without a zone; read it as UTC
function utc(text) {
  return text && !/[zZ]|[+-]\d\d:?\d\d$/.test(text) ? text.replace(' ', 'T') + 'Z' : text;
}

function koboPanel(detail) {
  const k = detail.kobo;
  const shown = !!(k && k.shown);
  let status;
  if (!k) status = 'Not on the Kobo.';
  else if (!shown) status = k.waiting ? 'Taken off the Kobo at the next plug-in.' : 'Not on the Kobo.';
  else status = k.status + (k.waiting ? ' (at the next plug-in)' : '');
  const when = k && k.last_written ? ` Last written ${escHtml(dateText(utc(k.last_written)))}.` : '';
  const error = k && k.error ? `<div class="collection-kobo-error">${escHtml(k.error)}</div>` : '';
  return `
    <div class="collection-kobo">
      <label class="debug-switch"><input type="checkbox" id="collection-kobo-switch"${shown ? ' checked' : ''}> Show on Kobo</label>
      <div class="collection-kobo-status muted">${escHtml(status)}${when}</div>
      ${error}
    </div>`;
}

export function renderCollectionPage(container, detail, { toast, reload }) {
  let html = `<div class="item-view-title">${escHtml(detail.name)}</div>`;
  if (detail.description) html += `<div class="item-view-meta">${escHtml(detail.description)}</div>`;
  html += koboPanel(detail);
  html += `<div class="item-view-children-title">${detail.items.length} items, in reading order</div>`;
  html += '<ol class="collection-items" id="collection-items-list"></ol>';
  container.innerHTML = html;

  container.querySelector('#collection-kobo-switch').addEventListener('change', async (e) => {
    try {
      await api(`/api/collections/${detail.id}/kobo`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ shown: e.target.checked }),
      });
      toast(e.target.checked ? 'On the Kobo from the next plug-in' : 'Off the Kobo at the next plug-in', 'ok');
      reload();
    } catch (err) {
      e.target.checked = !e.target.checked;
      toast('Failed: ' + err.message, 'error');
    }
  });

  const list = container.querySelector('#collection-items-list');
  detail.items.forEach((item, i) => {
    const li = document.createElement('li');
    li.className = 'collection-item';
    const moves = document.createElement('div');
    moves.className = 'collection-moves';
    for (const [label, to, title] of [['↑', i, 'Earlier'], ['↓', i + 2, 'Later']]) {
      const b = document.createElement('button');
      b.type = 'button';
      b.textContent = label;
      b.title = title;
      b.setAttribute('aria-label', `${title}: ${item.title || 'item'}`);
      b.disabled = to < 1 || to > detail.items.length;
      b.addEventListener('click', async () => {
        try {
          await api(`/api/collections/${detail.id}/items/${item.id}`, {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ position: to }),
          });
          reload();
        } catch (err) {
          toast('Failed: ' + err.message, 'error');
        }
      });
      moves.appendChild(b);
    }
    li.appendChild(moves);
    li.appendChild(renderCard(item));
    list.appendChild(li);
  });
}
