// An item's related items (#219): suggestions from other sources, by their vectors,
// each with "Related" and "Not related"; then "Also similar", the nearest items from
// any source, with the same two buttons. A confirmed pair becomes one of the item's
// links; a rejected one is never suggested again, nor listed as similar.

import { api } from '../../api.js';
import { renderCard } from './card.js';

const SHOWN = 5;

export function relatedSectionHtml(item) {
  // A stub or a part has no vector of its own to compare
  if (item.parent_id || item.held === false) return '';
  return `<div class="item-view-children" id="related-section" data-item-id="${item.id}" style="margin-top:1rem">
    <div class="item-view-children-title">Related</div>
    <div class="item-list" id="related-list"><span class="muted" style="font-size:0.8rem">Loading...</span></div>
    <div class="item-view-children-title" id="similar-title" style="margin-top:0.75rem;display:none">Also similar</div>
    <div class="item-list" id="similar-list"></div>
  </div>`;
}

function row(item, other, { score = null, onDecided, toast }) {
  const li = document.createElement('div');
  li.className = 'collection-item related-item';
  li.appendChild(renderCard(other));
  const actions = document.createElement('div');
  actions.className = 'collection-moves';
  const match = score === null ? '' : ` (match ${Math.round(score * 100)}%)`;
  actions.innerHTML = `
    <button type="button" data-status="confirmed" title="Related${match}" aria-label="Related: keep as a link">✓</button>
    <button type="button" data-status="rejected" title="Not related" aria-label="Not related: don't suggest again">✕</button>`;
  actions.addEventListener('click', async (e) => {
    const btn = e.target.closest('button[data-status]');
    if (!btn) return;
    actions.querySelectorAll('button').forEach(b => { b.disabled = true; });
    try {
      await api(`/api/items/${item.id}/related/${other.id}`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ status: btn.dataset.status }),
      });
      li.remove();
      toast(btn.dataset.status === 'confirmed' ? 'Kept as related' : "Won't be suggested again", 'ok');
      onDecided(btn.dataset.status);
    } catch (err) {
      actions.querySelectorAll('button').forEach(b => { b.disabled = false; });
      toast('Failed: ' + err.message, 'error');
    }
  });
  li.appendChild(actions);
  return li;
}

// onLinked: reload the item's links, where a confirmed pair now shows
export async function loadRelated(item, toast, onLinked) {
  const section = document.getElementById('related-section');
  if (!section) return;
  const current = () => section.isConnected && section.dataset.itemId === String(item.id);
  let suggestions = [], similar = [], links = [];
  try {
    [suggestions, similar, links] = await Promise.all([
      api(`/api/items/${item.id}/related?limit=${SHOWN}`).then(d => d.suggestions).catch(() => []),
      api(`/api/embeddings/similar/${item.id}?limit=${SHOWN * 2}`),
      api(`/api/items/${item.id}/links`).then(d => d.links).catch(() => []),
    ]);
  } catch {
    if (current()) document.getElementById('related-list').innerHTML = '<span class="muted" style="font-size:0.8rem">Unavailable</span>';
    return;
  }
  if (!current()) return;
  const onDecided = (status) => { if (status === 'confirmed') onLinked(); };
  const list = document.getElementById('related-list');
  list.innerHTML = '';
  if (!suggestions.length) {
    list.innerHTML = '<span class="muted" style="font-size:0.8rem">No suggestions from other sources</span>';
  }
  suggestions.forEach(s => list.appendChild(row(item, s.item, { score: s.score, onDecided, toast })));

  // Similar items not suggested above, and not linked already
  const shown = new Set([...suggestions.map(s => s.item.id), ...links.map(l => l.item.id)]);
  const rest = similar.filter(s => !shown.has(s.id)).slice(0, SHOWN);
  const simList = document.getElementById('similar-list');
  simList.innerHTML = '';
  if (!rest.length) return;
  document.getElementById('similar-title').style.display = '';
  rest.forEach(s => simList.appendChild(row(item, s, { onDecided, toast })));
}
