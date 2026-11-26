// Suggested themes (#266): groups of related items that cut across sources, shown
// after your collections. Keep makes a collection of a theme's items; Dismiss means
// it, or one mostly like it, isn't suggested again.

import { api } from '../../api.js';
import { navigate } from '../../router.js';
import { escHtml } from '../../util.js';
import { forgetThemeCount } from '../../places/library.js';

const SOURCES_SHOWN = 4;
const KINDS_SHOWN = 3;

function listed(names, shown) {
  return names.slice(0, shown).join(', ')
    + (names.length > shown ? ` and ${names.length - shown} more` : '');
}

// Kinds in the theme, most items first, by the server's plural labels ("replies")
function kindsOf(items) {
  const counts = {};
  items.forEach(i => {
    const label = (i.kind_plural || i.kind_label || i.kind || '').toLowerCase();
    if (label) counts[label] = (counts[label] || 0) + 1;
  });
  return Object.keys(counts).sort((a, b) => counts[b] - counts[a] || a.localeCompare(b));
}

// An item with no title shows its text's start, so the list says what it is
function itemHtml(i) {
  const label = (i.title || '').trim() || i.excerpt || `Item ${i.id}`;
  const kind = i.kind_label || i.kind || '';
  const what = [kind, i.source_type].filter(Boolean).join(' · ');
  return `<li><a href="/items/${i.id}">${escHtml(label)}</a> <span class="muted">${escHtml(what)}</span></li>`;
}

// The name is three words its items' titles share: a hint, not a title
function themeHtml(t) {
  const kinds = kindsOf(t.items);
  const n = t.items.length;
  const summary = [`${n} item${n === 1 ? '' : 's'}`, kinds.length ? listed(kinds, KINDS_SHOWN) : '',
    t.sources.length ? `from ${listed(t.sources, SOURCES_SHOWN)}` : ''].filter(Boolean).join(' · ');
  return `
    <div class="item-card-header"><span class="item-title"><span class="muted">About</span> ${escHtml(t.name)}</span></div>
    <div class="item-footer"><span>${escHtml(summary)}</span></div>
    <details class="theme-items"><summary>Items</summary><ul>${t.items.map(itemHtml).join('')}</ul></details>
    <div class="theme-actions">
      <button type="button" class="sync-btn" data-act="keep">Keep as a collection</button>
      <button type="button" class="sync-btn" data-act="dismiss">Dismiss</button>
    </div>
    <form class="theme-keep" hidden>
      <label>Collection name <input type="text" name="name" required maxlength="200"></label>
      <button type="submit" class="sync-btn">Keep</button>
      <button type="button" class="sync-btn" data-act="cancel">Cancel</button>
    </form>`;
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
  let left = themes.length;
  const showCount = () => {
    const jump = document.getElementById('themes-jump');
    if (jump) {
      jump.textContent = `Suggested themes (${left})`;
      jump.hidden = !left;
    }
    if (!left) {
      list.innerHTML = '<div class="empty-state muted" style="padding:1rem;text-align:center">No suggested themes now.</div>';
    }
  };
  showCount();
  if (!left) return;
  list.innerHTML = '';
  themes.forEach(t => {
    const card = document.createElement('div');
    card.className = 'item-card theme-card';
    card.innerHTML = themeHtml(t);
    const actions = card.querySelector('.theme-actions');
    const form = card.querySelector('.theme-keep');
    const showForm = (on) => {
      form.hidden = !on;
      actions.hidden = on;
      if (on) {
        form.elements.name.value = t.name;
        form.elements.name.select();
      }
    };
    form.querySelector('[data-act="cancel"]').addEventListener('click', () => showForm(false));
    form.addEventListener('submit', (e) => {
      e.preventDefault();
      decide('keep', form.elements.name.value.trim() || t.name);
    });
    actions.addEventListener('click', (e) => {
      const btn = e.target.closest('button[data-act]');
      if (!btn) return;
      if (btn.dataset.act === 'keep') showForm(true);
      else decide('dismiss');
    });
    const decide = async (act, name) => {
      card.querySelectorAll('button').forEach(b => { b.disabled = true; });
      try {
        if (act === 'keep') {
          const kept = await api(`/api/themes/${t.id}/keep`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ name }),
          });
          forgetThemeCount();
          toast('Kept as a collection', 'ok');
          navigate(`/collections/${kept.collection_id}`);
        } else {
          await api(`/api/themes/${t.id}/dismiss`, { method: 'POST' });
          forgetThemeCount();
          card.remove();
          left -= 1;
          showCount();
          toast("Dismissed; it won't be suggested again", 'ok');
        }
      } catch (err) {
        if (String(err.message).startsWith('404')) {
          // A newer run replaced this theme: show the themes as they are now
          toast('Themes have changed since this list loaded; here they are now', 'error');
          loadThemes(toast);
          return;
        }
        card.querySelectorAll('button').forEach(b => { b.disabled = false; });
        toast('Failed: ' + err.message, 'error');
      }
    };
    list.appendChild(card);
  });
}
