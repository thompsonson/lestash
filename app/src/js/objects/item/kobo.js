// An item's place on the Kobo, and its versions (#338): a switch to send it, one
// status line ("On the Kobo · 12 notes"), and every version of its document, newest
// first. Shown for PDFs, and for any item asked for, on the Kobo or with versions.

import { api } from '../../api.js';
import { escHtml, dateText } from '../../util.js';

export function koboSectionHtml(item) {
  return `<div class="item-view-children" id="kobo-section" data-item-id="${item.id}" style="margin-top:1rem;display:none"></div>`;
}

function utc(text) {
  return text && !/[zZ]|[+-]\d\d:?\d\d$/.test(text) ? text.replace(' ', 'T') + 'Z' : text;
}

function statusLine(place) {
  const parts = [place.text];
  if (place.notes) parts.push(`${place.notes} ${place.notes === 1 ? 'note' : 'notes'}`);
  if (place.collections.length) parts.push(`in ${place.collections.join(', ')}`);
  return parts.join(' · ');
}

function versionsHtml(item, versions) {
  if (versions.length < 2) return '';
  const rows = versions.map((v, i) => {
    const label = i === 0 ? 'Newest' : `Version ${versions.length - i}`;
    const when = dateText(utc(v.created_at));
    const name = escHtml(v.title || `Item ${v.id}`);
    const title = v.id === item.id ? `<b>${name}</b> (this one)` : `<a href="/items/${v.id}">${name}</a>`;
    return `<li><span class="muted">${label}${when ? `, ${escHtml(when)}` : ''}:</span> ${title}</li>`;
  }).join('');
  return `<div class="item-view-children-title">${versions.length} versions</div><ol class="item-versions">${rows}</ol>`;
}

function render(section, item, place, versions, toast) {
  // A Kobo book is on the Kobo already: no switch, only its status
  const switchHtml = item.source_type === 'kobo' ? '' : `
    <label class="debug-switch"><input type="checkbox" id="item-kobo-switch"${place.marked ? ' checked' : ''}> Send to the Kobo</label>`;
  const newest = place.newest_id !== item.id
    ? `<div class="collection-kobo-status muted">The newest version goes: <a href="/items/${place.newest_id}">item ${place.newest_id}</a>.</div>` : '';
  const error = place.error ? `<div class="collection-kobo-error">${escHtml(place.error)}</div>` : '';
  section.innerHTML = `
    <div class="item-view-children-title">Kobo</div>
    <div class="collection-kobo">
      ${switchHtml}
      <div class="collection-kobo-status muted">${escHtml(statusLine(place))}</div>
      ${newest}${error}
    </div>
    ${versionsHtml(item, versions)}`;
  const box = section.querySelector('#item-kobo-switch');
  if (!box) return;
  box.addEventListener('change', async (e) => {
    try {
      const updated = await api(`/api/items/${item.id}/kobo`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ wanted: e.target.checked }),
      });
      toast(e.target.checked ? 'Copied to the Kobo at the next plug-in' : 'Not sent any more; a copy there stays', 'ok');
      render(section, item, updated, versions, toast);
    } catch (err) {
      e.target.checked = !e.target.checked;
      toast('Failed: ' + err.message, 'error');
    }
  });
}

export async function loadKobo(item, toast) {
  let place, versions;
  try {
    [place, versions] = await Promise.all([
      api(`/api/items/${item.id}/kobo`),
      api(`/api/items/${item.id}/versions`),
    ]);
  } catch {
    return;  // older server, or offline: the section stays hidden
  }
  const section = document.getElementById('kobo-section');
  // The view may have moved on to another item while this loaded
  if (!section || section.dataset.itemId !== String(item.id)) return;
  const list = versions.versions;
  const relevant = item.source_type === 'pdf' || place.wanted || place.on || place.copied_at
    || place.notes || list.length > 1;
  if (!relevant) return;
  render(section, item, place, list, toast);
  section.style.display = '';
}
