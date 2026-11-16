// Suggested themes (#266): groups of related items that cut across sources, shown
// after your collections. Keep makes a collection of a theme's items; Dismiss means
// it, or one mostly like it, isn't suggested again.

import { api } from '../../api.js';
import { navigate } from '../../router.js';
import { escHtml } from '../../util.js';

const SOURCES_SHOWN = 4;

function themeHtml(t) {
  const sources = t.sources.slice(0, SOURCES_SHOWN).join(', ')
    + (t.sources.length > SOURCES_SHOWN ? ` and ${t.sources.length - SOURCES_SHOWN} more` : '');
  const items = t.items.map(i =>
    `<li><a href="/items/${i.id}">${escHtml(i.title || `Item ${i.id}`)}</a> <span class="muted">${escHtml(i.source_type)}</span></li>`
  ).join('');
  return `
    <div class="item-card-header"><span class="item-title">${escHtml(t.name)}</span></div>
    <div class="item-footer"><span>${t.items.length} items from ${escHtml(sources)}</span></div>
    <details class="theme-items"><summary>Items</summary><ul>${items}</ul></details>
    <div class="theme-actions">
      <button type="button" class="sync-btn" data-act="keep">Keep as a collection</button>
      <button type="button" class="sync-btn" data-act="dismiss">Dismiss</button>
    </div>`;
}

export async function loadThemes(toast) {
  const list = document.getElementById('themes-list');
  if (!list) return;
  let themes;
  try {
    themes = await api('/api/themes');
  } catch {
    list.innerHTML = '';  // older server, or offline: no suggestions shown
    return;
  }
  document.getElementById('themes-title').style.display = '';
  if (!themes.length) {
    list.innerHTML = '<div class="empty-state muted" style="padding:1rem;text-align:center">No suggested themes now.</div>';
    return;
  }
  list.innerHTML = '';
  themes.forEach(t => {
    const card = document.createElement('div');
    card.className = 'item-card theme-card';
    card.innerHTML = themeHtml(t);
    card.querySelector('.theme-actions').addEventListener('click', async (e) => {
      const btn = e.target.closest('button[data-act]');
      if (!btn) return;
      card.querySelectorAll('.theme-actions button').forEach(b => { b.disabled = true; });
      try {
        if (btn.dataset.act === 'keep') {
          const kept = await api(`/api/themes/${t.id}/keep`, { method: 'POST' });
          toast('Kept as a collection', 'ok');
          navigate(`/collections/${kept.collection_id}`);
        } else {
          await api(`/api/themes/${t.id}/dismiss`, { method: 'POST' });
          card.remove();
          toast("Dismissed; it won't be suggested again", 'ok');
        }
      } catch (err) {
        if (String(err.message).startsWith('404')) {
          // A newer run replaced this theme: show the themes as they are now
          toast('Themes have changed since this list loaded; here they are now', 'error');
          loadThemes(toast);
          return;
        }
        card.querySelectorAll('.theme-actions button').forEach(b => { b.disabled = false; });
        toast('Failed: ' + err.message, 'error');
      }
    });
    list.appendChild(card);
  });
}
