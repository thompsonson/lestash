import {
  IS_TAURI, tauriInvoke, tauriListen, DEFAULT_SETTINGS, settings, saveSettings, getApiBase,
  debugLog, addDebugLog, api,
} from './api.js';
import { escHtml, relTime, sourceClass, sourceName } from './util.js';
import { openExternal } from './external.js';
import { renderCard } from './objects/item/card.js';
import { startTimeline, redrawTimeline } from './places/timeline.js';
import { startPeople, redrawPeople } from './places/people.js';
import { startLibrary, redrawLibrary } from './places/library.js';
import { startHelp } from './places/help.js';
import {
  itemHeader, cleanContent, itemPlayer, wireTranscript, reactionsLine, wireReactions,
  renderParts, PARTS_QUERY, titleOnce,
} from './objects/item/page.js';
import { selfName, selfReady } from './objects/person/self.js';
import { setSheet, onSheetClose } from './sheet.js';
import {
  route, navigate, back, render, setAddress, startRouter, routerReady, cacheItem, cachedItem,
} from './router.js';

/* ── Error visibility (Android WebView debugging) ── */
window.onerror = function(msg, url, line) {
  document.title = 'ERR: ' + msg;
  console.error('JS Error:', msg, 'at', url, ':', line);
};

// Theme, density and type apply as they change; js/appearance.js keeps them
function wireAppearance() {
  const A = window.LeStashAppearance;
  if (!A) return;
  const now = A.load();
  for (const [id, key, options] of [['s-theme', 'theme', A.THEMES], ['s-density', 'density', A.DENSITIES], ['s-font', 'font', A.FONTS]]) {
    const select = document.getElementById(id);
    if (!select.options.length) {
      select.innerHTML = options.map(([v, l]) => `<option value="${v}">${escHtml(l)}</option>`).join('');
      select.addEventListener('change', () => A.save({ [key]: select.value }));
    }
    select.value = now[key];
  }
}

function openSettings() {
  wireAppearance();
  document.getElementById('s-host').value = settings.host;
  document.getElementById('s-port').value = settings.port;
  document.getElementById('s-llm-url').value = settings.llmUrl || '';
  document.getElementById('s-llm-model').value = settings.llmModel || '';
  document.getElementById('s-llm-prompt').value = settings.llmPrompt || '';
  document.getElementById('settings-modal').classList.add('open');
}

function closeSettings() {
  document.getElementById('settings-modal').classList.remove('open');
}

function saveSettingsForm() {
  saveSettings({
    host: document.getElementById('s-host').value || DEFAULT_SETTINGS.host,
    port: parseInt(document.getElementById('s-port').value) || DEFAULT_SETTINGS.port,
    llmUrl: document.getElementById('s-llm-url').value,
    llmModel: document.getElementById('s-llm-model').value,
    llmPrompt: document.getElementById('s-llm-prompt').value,
  });
  closeSettings();
  showToast('Settings saved — reloading...', 'ok');
  setTimeout(() => location.reload(), 500);
}

window.addEventListener('lestash:debuglog', () => renderDebugLog());

function renderDebugLog() {
  const el = document.getElementById('debug-log');
  if (!el) return;
  el.innerHTML = debugLog.map(e => {
    const color = e.error ? 'var(--error)' : (e.status === 200 ? 'var(--ok)' : 'var(--warn)');
    const ts = e.ts.split('T')[1].split('.')[0];
    return `<div style="padding:0.3rem 0;border-bottom:1px solid var(--border)">
      <span style="color:var(--muted)">${ts}</span>
      <span style="color:${color}">${e.status}</span>
      <span>${e.method}</span>
      <span style="color:var(--muted);word-break:break-all">${e.url}</span>
      ${e.time ? `<span style="color:var(--muted)">${e.time}ms</span>` : ''}
      ${e.error ? `<div style="color:var(--error);margin-top:0.2rem">${e.error}</div>` : ''}
    </div>`;
  }).join('');
}

let appVersion = 'unknown';
async function loadAppVersion() {
  if (IS_TAURI && tauriInvoke) {
    try { appVersion = await tauriInvoke('get_app_version'); } catch {}
  } else {
    appVersion = '0.1.0 (web)';
  }
  const el = document.getElementById('app-version');
  if (el) el.textContent = `v${appVersion}`;
}

function renderDebugInfo() {
  const el = document.getElementById('debug-info');
  if (!el) return;
  el.innerHTML = `
    <div><strong>Version:</strong> ${appVersion}</div>
    <div><strong>API Base:</strong> ${getApiBase() || '(relative)'}</div>
    <div><strong>Host:</strong> ${settings.host}</div>
    <div><strong>Port:</strong> ${settings.port}</div>
    <div><strong>IS_TAURI:</strong> ${IS_TAURI}</div>
    <div><strong>tauriInvoke:</strong> ${tauriInvoke ? 'available' : 'null'}</div>
    <div><strong>User Agent:</strong> ${navigator.userAgent.substring(0, 80)}...</div>
  `;
}

async function debugTestConnection() {
  const base = getApiBase();
  addDebugLog('INFO', 'Starting connection test', '---', null, `Base: ${base}`);

  // Test 1: fetch health
  try {
    const start = performance.now();
    const res = await fetch(base + '/api/health');
    const time = Math.round(performance.now() - start);
    const data = await res.json();
    addDebugLog('GET', base + '/api/health', res.status, time, JSON.stringify(data));
  } catch (e) {
    addDebugLog('GET', base + '/api/health', 'ERR', null, `${e.name}: ${e.message}`);
  }

  // Test 2: fetch items
  try {
    const start = performance.now();
    const res = await fetch(base + '/api/items?limit=1');
    const time = Math.round(performance.now() - start);
    const data = await res.json();
    addDebugLog('GET', base + '/api/items?limit=1', res.status, time, `${data.total} total items`);
  } catch (e) {
    addDebugLog('GET', base + '/api/items?limit=1', 'ERR', null, `${e.name}: ${e.message}`);
  }

  // Test 3: XMLHttpRequest (sometimes gives different error info)
  await new Promise(resolve => {
    const xhr = new XMLHttpRequest();
    xhr.open('GET', base + '/api/health');
    xhr.timeout = 10000;
    xhr.onload = () => addDebugLog('XHR', base + '/api/health', xhr.status, null, xhr.responseText.substring(0, 200)) || resolve();
    xhr.onerror = () => addDebugLog('XHR', base + '/api/health', 'ERR', null, `readyState=${xhr.readyState} status=${xhr.status} statusText="${xhr.statusText}"`) || resolve();
    xhr.ontimeout = () => addDebugLog('XHR', base + '/api/health', 'TIMEOUT', null, '10s timeout exceeded') || resolve();
    xhr.send();
  });

  // Test 4: check if we can reach any HTTPS at all (Google)
  try {
    const start = performance.now();
    const res = await fetch('https://www.google.com/generate_204', { mode: 'no-cors' });
    const time = Math.round(performance.now() - start);
    addDebugLog('NET', 'https://www.google.com/generate_204', res.type === 'opaque' ? 'OK(opaque)' : res.status, time, 'Internet connectivity check');
  } catch (e) {
    addDebugLog('NET', 'https://www.google.com/generate_204', 'ERR', null, `No internet: ${e.message}`);
  }
}

/* ── Toasts ── */
function showToast(message, level) {
  const el = document.createElement('div');
  el.className = `toast ${level || ''}`;
  el.textContent = message;
  const container = document.getElementById('toasts');
  while (container.children.length >= 5) container.removeChild(container.firstChild);
  container.appendChild(el);
  el.addEventListener('animationend', () => el.remove());
}

/* ── Pages (#260): the router draws exactly one; see js/router.js ── */
const TAB_PATHS = {
  timeline: '/', recent: '/recent', notes: '/notes', search: '/search', people: '/people',
  library: '/library', more: '/more',
  collections: '/collections', sources: '/settings/sources',
  stats: '/settings/stats', debug: '/settings/debug',
};

// The list place last shown: an item opened from it slides over it as a sheet
let placeShown = null;
// The history depth of the list under the sheet, for ✕ Close
let sheetFrom = null;

// Show one page and hide every other; `tab` is the tab to mark, if any. An item or
// person page opened from a list place opens over it (js/sheet.js); opened directly,
// it is the whole page.
function showOnly(id, tab) {
  if (id !== 'tab-search') clearTimeout(searchTimer);
  if (tab && id === `tab-${tab}`) tabAddress[tab] = location.pathname + location.search;
  const sheet = id === 'item-view' && placeShown !== null;
  if (id !== 'item-view') placeShown = id;
  document.querySelectorAll('.tab-content').forEach(t => {
    t.classList.toggle('active', t.id === id || (sheet && t.id === placeShown));
    t.style.display = '';
  });
  document.getElementById('item-view').style.display = id === 'item-view' ? 'block' : 'none';
  if (sheet && !document.body.classList.contains('sheet-open')) {
    sheetFrom = Math.max(0, ((history.state && history.state.depth) || 1) - 1);
  }
  if (!sheet) sheetFrom = null;
  setSheet(sheet);
  // Under a sheet, the place it opened over stays marked
  if (sheet) tab = placeShown.replace(/^tab-/, '');
  // A place without its own button (Collections, Notes, Settings...) lives under More
  const mark = !tab || document.querySelector(`#tabs [data-tab="${tab}"]`) ? tab : 'more';
  document.querySelectorAll('#tabs button').forEach(b => {
    const on = b.dataset.tab === mark;
    b.classList.toggle('active', on);
    if (on) b.setAttribute('aria-current', 'page'); else b.removeAttribute('aria-current');
  });
}

// Each tab's last address, so a tab returns to its filters or query
const tabAddress = {};

let focusSearch = false;

function switchTab(tab) {
  if (tab === 'search') focusSearch = true;
  navigate(tabAddress[tab] || TAB_PATHS[tab] || '/');
}

function showTab(tab) {
  showOnly(`tab-${tab}`, tab);
  const loads = {
    recent: () => [loadRecent()],
    notes: () => [loadSavedNotes(), checkSttModel()],
    sources: () => [loadSources(), loadGoogleAuth(), loadAudibleAuth(), loadLinkedInAuth()],
    stats: () => [loadStats()],
    collections: () => [loadCollections()],
    debug: () => [renderDebugInfo(), renderDebugLog()],
  };
  return Promise.allSettled((loads[tab] || (() => []))());
}

document.getElementById('tabs').addEventListener('click', e => {
  if (e.target.tagName !== 'BUTTON') return;
  switchTab(e.target.dataset.tab);
});

/* ── Relative time ── */

/* ── Source badge ── */


const CHAT_ROLES = new Set(['user', 'model', 'assistant', 'system', 'human']);
function getChatRole(item) {
  return item.author || (item.metadata && item.metadata.sender) || null;
}
function renderChatBubble(item) {
  const role = getChatRole(item);
  const isUser = role === 'user' || role === 'human';
  const roleClass = isUser ? 'user' : 'model';
  const roleLabel = isUser ? (item.by_self && item.by_name) || selfName() : (item.source_type === 'claude' || item.source_type === 'claude-code' ? 'Claude' : item.source_type === 'mistral' ? 'Mistral' : item.source_type === 'gemini' ? 'Gemini' : 'Assistant');
  const wrapper = document.createElement('div');
  wrapper.innerHTML = `
    <div class="chat-role ${roleClass}">${escHtml(roleLabel)}</div>
    <div class="chat-bubble ${roleClass}">${renderMarkdown(item.content)}</div>
  `;
  wrapper.querySelector('.chat-bubble').addEventListener('click', e => {
    if (e.target.closest('a')) return;
    openItemView(item);
  });
  return wrapper;
}

/* ── Item and collection pages ── */
// The item or collection address that could not load, so Reconnect redraws it
let failedPath = null;
function openItemView(item) {
  navigate(`/items/${cacheItem(item).id}`);
}

// ← Back returns to the page before; a link opened directly falls back to its list.
// ✕ Close on the sheet returns to the list under it, however many items were opened
// in the sheet since.
function closeItemView() {
  const depth = (history.state && history.state.depth) || 0;
  if (document.body.classList.contains('sheet-open') && sheetFrom !== null && depth > sheetFrom) {
    history.go(sheetFrom - depth);
    return;
  }
  const path = location.pathname;
  back(path.startsWith('/collections/') ? '/collections' : path.startsWith('/people/') ? '/people' : '/');
}

route(/^\/items\/(\d+)$/, async ([, id]) => {
  showOnly('item-view', null);
  failedPath = null;
  let item = cachedItem(id);
  if (!item) {
    const container = document.getElementById('item-view-content');
    container.innerHTML = '<div class="empty-state"><div class="spinner"></div></div>';
    try {
      item = cacheItem(await api(`/api/items/${id}?include_children=true`));
    } catch (e) {
      if (location.pathname === `/items/${id}`) {
        container.innerHTML = `<div class="empty-state" style="padding:2rem">Item not found: ${escHtml(e.message)}</div>`;
        failedPath = `/items/${id}`;
      }
      return;
    }
  }
  if (location.pathname !== `/items/${id}`) return;  // the user moved on meanwhile
  renderItemView(item);
});

async function loadItemHistory(itemId) {
  const list = document.getElementById('item-history-list');
  list.textContent = 'Loading...';
  try {
    const data = await api(`/api/items/${itemId}/history`);
    if (!data.versions.length) {
      list.innerHTML = '<span style="color:var(--muted)">No versions yet — edits create history rows.</span>';
      return;
    }
    const reasonColor = {
      'user-edit': '#3b82f6',
      'sync': '#6b7280',
      'enricher': '#10b981',
      'restore': '#f59e0b',
      'api-update': '#9ca3af',
    };
    list.innerHTML = data.versions.map(v => {
      const reason = v.change_reason || 'unknown';
      const color = reasonColor[reason] || '#9ca3af';
      const dateFmt = new Date(v.changed_at + 'Z').toLocaleString();
      const titlePart = v.title_old !== null && v.title_old !== undefined
        ? `<span style="margin-left:0.5rem">${escHtml(v.title_old)}</span>`
        : '';
      const previewPart = v.content_preview
        ? `<div style="color:var(--muted);margin-top:0.15rem">${escHtml(v.content_preview)}</div>`
        : '';
      return `<div style="padding:0.4rem 0;border-top:1px solid var(--border)">
        <div style="display:flex;align-items:center;gap:0.4rem;flex-wrap:wrap">
          <span style="font-size:0.7rem;padding:0.1rem 0.35rem;border-radius:3px;background:${color};color:white">${escHtml(reason)}</span>
          <span style="color:var(--muted);font-size:0.75rem">${dateFmt}</span>
          ${titlePart}
          <span style="margin-left:auto;display:flex;gap:0.3rem">
            <button class="sync-btn" data-action="expand-version" data-id="${itemId}" data-version-id="${v.id}" style="font-size:0.7rem;padding:0.1rem 0.4rem">View</button>
            <button class="sync-btn" data-action="restore-version" data-id="${itemId}" data-version-id="${v.id}" data-parent-null="${v.parent_id_old === null || v.parent_id_old === undefined ? '1' : '0'}" style="font-size:0.7rem;padding:0.1rem 0.4rem">Restore</button>
          </span>
        </div>
        ${previewPart}
        <div id="version-detail-${v.id}" style="display:none;margin-top:0.35rem;padding:0.35rem;background:var(--bg);border:1px solid var(--border);border-radius:3px"></div>
      </div>`;
    }).join('');
  } catch (err) {
    list.innerHTML = '<span style="color:var(--muted)">Failed to load history</span>';
  }
}

// "Replies (9)" on a post, "Parts (10)" elsewhere; reactions are counted apart
function partsTitle(item) {
  const n = (item.child_count || 0) - (item.reaction_count || 0);
  if (n <= 0) return 'Notes';
  return `${item.kind === 'post' ? 'Replies' : 'Parts'} (${n})`;
}

function renderItemView(item) {
  const container = document.getElementById('item-view-content');
  const emoji = item.metadata && item.metadata.emoji ? item.metadata.emoji + ' ' : '';

  const once = titleOnce(item, item.title || item.preview);
  const title = `${emoji}${once.title || (item.held === false && item.url) || '(untitled)'}`;
  let html = itemHeader(item, title);
  const player = itemPlayer(item);
  html += player.html;
  html += reactionsLine(item);
  if (item.held === false) {
    // A stub: known by its address only, so it has no content to show
    html += `<div class="item-view-content" id="item-content-display" style="color:var(--muted)">LeStash doesn't hold this; it knows it only by its address. Its links and activity are below.</div>`;
  } else {
    html += `<div class="item-view-content" id="item-content-display">${renderMarkdown(cleanContent(item, title, once))}</div>`;
  }

  // People: who made, voiced, published or received it. Under the content, as a
  // reader asks who made a thing before what it links. Hidden until some load.
  html += `<div class="item-view-children" id="people-section" data-item-id="${item.id}" style="margin-top:1rem;display:none">
    <div class="item-view-children-title" id="people-title">People</div>
    <div id="people-list" style="font-size:0.8rem"></div>
  </div>`;

  html += `<div id="item-edit-form" style="display:none">
    <input type="text" id="item-edit-title" placeholder="Title" style="width:100%;font-size:1.05rem;font-weight:bold;padding:0.4rem;background:var(--bg);color:var(--text);border:1px solid var(--border);border-radius:4px;margin-bottom:0.4rem">
    <textarea id="item-edit-content" rows="10" style="width:100%;resize:vertical;font-family:inherit;font-size:0.9rem;padding:0.5rem;background:var(--bg);color:var(--text);border:1px solid var(--border);border-radius:4px"></textarea>
    <div style="display:flex;gap:0.5rem;margin-top:0.4rem;align-items:center;flex-wrap:wrap">
      <button class="sync-btn" data-action="save-edit" data-id="${item.id}">Save</button>
      <button class="sync-btn" data-action="cancel-edit">Cancel</button>
      <button class="sync-btn" data-action="attach-image" type="button">Attach image</button>
      <input type="file" id="item-edit-image-input" accept="image/*" multiple style="display:none">
    </div>
  </div>`;

  // Media gallery
  const shownMedia = (item.media || []).filter(m => m.available || m.media_type === 'link' || m.media_type === 'pdf');
  // Older versions of a file are kept; they are listed under the current ones
  const visibleMedia = shownMedia.filter(m => !m.superseded_at && !player.ids.has(m.id));
  // Newest first, by when each was replaced: a version found later can be older
  const olderMedia = shownMedia.filter(m => m.superseded_at && m.available)
    .sort((a, b) => String(b.superseded_at).localeCompare(String(a.superseded_at)));
  if (visibleMedia.length > 0) {
    html += '<div class="item-media-gallery">';
    for (const m of visibleMedia) {
      if ((m.media_type === 'image' || m.media_type === 'thumbnail') && m.available) {
        html += `<img src="${getApiBase()}${escHtml(m.serve_url)}" alt="${escHtml(m.alt_text || '')}">`;
      } else if (m.media_type === 'pdf') {
        // A stored PDF is served here; an unstored one is at its source
        const href = m.available ? getApiBase() + m.serve_url : m.url;
        html += `<a href="${escHtml(href)}" target="_blank" rel="noopener">📄 PDF</a>`;
      } else if (m.media_type === 'audio' && m.available) {
        html += `<audio controls preload="none" src="${getApiBase()}${escHtml(m.serve_url)}"></audio>`;
      } else if (m.media_type === 'video' && m.available) {
        html += `<video controls preload="none" style="max-width:100%" src="${getApiBase()}${escHtml(m.serve_url)}"></video>`;
      } else if (m.media_type === 'file' && m.available) {
        html += `<a href="${getApiBase()}${escHtml(m.serve_url)}">📎 ${escHtml(m.alt_text || 'File')}</a>`;
      } else if (m.media_type === 'link') {
        html += `<a href="${escHtml(m.url)}" target="_blank" rel="noopener">🔗 ${escHtml(m.alt_text || m.url)}</a>`;
      }
    }
    html += '</div>';
  }
  if (olderMedia.length > 0) {
    html += '<div class="item-detail-field"><label>Earlier versions</label><ul class="media-versions">';
    for (const m of olderMedia) {
      const when = String(m.superseded_at).slice(0, 10);
      html += `<li><a href="${getApiBase()}${escHtml(m.serve_url)}" target="_blank" rel="noopener">${escHtml(m.alt_text || m.media_type)}</a>, version ${escHtml(String(m.version))}, replaced ${escHtml(when)}</li>`;
    }
    html += '</ul></div>';
  }

  if (item.metadata && item.metadata.processed_text) {
    html += `<div class="item-detail-field" style="margin-top:0.75rem"><label>Processed Note</label><div class="item-view-content">${escHtml(item.metadata.processed_text)}</div></div>`;
  }
  if (item.source_type === 'voice') {
    html += `<div style="margin-top:0.5rem"><button class="sync-btn" onclick="openProcessDialog(${item.id})">Process</button></div>`;
  }

  // Context section for reactions/comments
  const hasReactionContext = item.metadata && (item.metadata.reacted_to || item.metadata.commented_on);
  if (hasReactionContext) {
    const isUrn = (s) => s && s.startsWith('urn:');
    const actorUrn = item.metadata.raw && item.metadata.raw.actor;
    const ownerUrn = item.metadata.raw && item.metadata.raw.owner;
    // For "you reacted to someone else's content", author is the post author
    // For "someone else reacted to your content", actor is the reactor
    const authorUrn = item.metadata.reacted_to || item.metadata.commented_on;

    html += '<hr style="border-color:var(--border);margin:0.75rem 0">';

    // Actor (who performed the action)
    if (actorUrn) {
      html += `<div class="item-detail-field"><label>Actor</label>`;
      if (isUrn(item.actor_display)) {
        html += `<span id="actor-display">${escHtml(item.actor_display)}</span>
          <button class="sync-btn" data-action="edit-profile" data-urn="${escHtml(actorUrn)}" data-role="actor" style="font-size:0.7rem;padding:0.15rem 0.4rem;margin-left:0.3rem">Set name</button>
          <div id="actor-edit" style="display:none" class="tag-input-row">
            <input type="text" placeholder="Name" data-profile-name="actor" style="flex:2">
            <input type="text" placeholder="Profile URL (optional)" data-profile-url="actor" style="flex:3">
            <button class="sync-btn" data-action="save-profile" data-urn="${escHtml(actorUrn)}" data-role="actor" style="font-size:0.7rem;padding:0.15rem 0.4rem">Save</button>
          </div>`;
      } else {
        html += `<span>${escHtml(item.actor_display)}</span>`;
      }
      html += `</div>`;
    }

    // Author (of the target post) — only show when someone reacted to another's content
    if (ownerUrn && actorUrn && ownerUrn === actorUrn && item.author_display) {
      html += `<div class="item-detail-field"><label>Post author</label>`;
      if (item.author_display === '-') {
        html += `<span style="color:var(--muted)">Unknown</span>`;
      } else {
        html += `<span>${escHtml(item.author_display)}</span>`;
      }
      html += `</div>`;
    }

    // Parent post link
    if (item.parent_id) {
      html += `<div class="item-detail-field"><label>Parent post</label>
        <div class="item-list" id="parent-post"><span style="color:var(--muted);font-size:0.8rem">Loading...</span></div>
      </div>`;
    }
  }

  // A stub can't be edited, tagged, noted or collected: when its post is imported,
  // the stub is merged into it. Its links and activity still show.
  html += `<div${item.held === false ? ' style="display:none"' : ''}>`;
  // Tags
  html += '<hr style="border-color:var(--border);margin:0.75rem 0">';
  const currentTags = (item.tags || []).map(t =>
    `<span class="tag-chip">${escHtml(t)}<span class="tag-remove" data-action="remove-tag" data-tag="${escHtml(t)}" data-id="${item.id}">&times;</span></span>`
  ).join('');
  html += `<div class="item-detail-field">
    <div class="item-tags">${currentTags || '<span style="color:var(--muted);font-size:0.75rem">No tags</span>'}</div>
    <div class="tag-input-row">
      <input type="text" placeholder="Add tag..." data-tag-input="${item.id}">
      <button class="sync-btn" data-action="add-tag" data-id="${item.id}" style="font-size:0.75rem;padding:0.25rem 0.5rem">Add</button>
    </div>
  </div>`;

  // Add note
  html += `<div class="item-detail-field">
    <div id="note-form" style="display:none">
      <textarea id="note-text" placeholder="Add a note..." rows="3" style="width:100%;resize:vertical;font-family:inherit;font-size:0.85rem;padding:0.5rem;background:var(--bg);color:var(--text);border:1px solid var(--border);border-radius:4px"></textarea>
      <audio id="note-audio-playback" controls style="display:none;width:100%;margin-top:0.25rem"></audio>
      <span id="note-record-status" style="font-size:0.75rem;color:var(--muted)"></span>
      <div style="display:flex;gap:0.5rem;margin-top:0.25rem;align-items:center">
        <button class="sync-btn" data-action="record-note" data-id="${item.id}" style="font-size:0.75rem;padding:0.25rem 0.5rem" title="Record voice note">Mic</button>
        <button class="sync-btn" data-action="save-note" data-id="${item.id}" style="font-size:0.75rem;padding:0.25rem 0.5rem">Save</button>
        <button class="sync-btn" data-action="cancel-note" style="font-size:0.75rem;padding:0.25rem 0.5rem">Cancel</button>
      </div>
    </div>
  </div>`;

  // Actions
  html += `<div class="item-detail-actions" id="item-detail-actions">
    <button class="sync-btn" data-action="edit-item" data-id="${item.id}">Edit</button>
    <button class="sync-btn" data-action="toggle-history" data-id="${item.id}">History</button>
    <button class="sync-btn" data-action="show-note-form">+ Note</button>
    <button class="sync-btn" data-action="link-item" data-id="${item.id}">+ Link</button>
    <button class="sync-btn" data-action="copy" data-id="${item.id}">Copy</button>
    <button class="sync-btn" data-action="copy-link" data-id="${item.id}">Copy Link</button>
    <button class="sync-btn" onclick="addToCollection(${item.id})">+ Collection</button>
    <button class="sync-btn" onclick="openMicroblogCompose(${item.id})">Share to micro.blog</button>
    <select id="add-to-coll-select" style="display:none;font-size:0.75rem;padding:0.25rem;background:var(--bg);color:var(--text);border:1px solid var(--border);border-radius:4px" onchange="confirmAddToCollection(this)"></select>
  </div>`;
  html += `</div>`;
  html += `<div id="item-history-panel" style="display:none;margin-top:0.5rem;padding:0.5rem;border:1px solid var(--border);border-radius:4px">
    <div style="font-size:0.85rem;font-weight:bold;margin-bottom:0.4rem">Version history</div>
    <div id="item-history-list" style="font-size:0.8rem"></div>
  </div>`;

  // Children section (always shown so new notes appear immediately)
  html += `<div class="item-view-children">
    <div class="item-view-children-title" id="parts-title">${partsTitle(item)}</div>
    <div class="item-list" id="children-list" data-item-id="${item.id}">${item.child_count - (item.reaction_count || 0) > 0
      ? '<span class="parts-placeholder" style="color:var(--muted);font-size:0.8rem">Loading...</span>'
      : '<span class="parts-placeholder" style="color:var(--muted);font-size:0.8rem">No notes yet</span>'
    }</div>
  </div>`;

  // Similar items (held parent items only: a stub has no text to compare)
  if (!item.parent_id && item.held !== false) {
    html += `<div class="item-view-children" style="margin-top:1rem">
      <div class="item-view-children-title">Similar items</div>
      <div class="item-list" id="similar-list"><span style="color:var(--muted);font-size:0.8rem">Loading...</span></div>
    </div>`;
  }

  // Links to and from other items (hidden until some load)
  html += `<div class="item-view-children" id="links-section" data-item-id="${item.id}" style="margin-top:1rem;display:none">
    <div class="item-view-children-title" id="links-title">Links</div>
    <div id="links-list" style="font-size:0.8rem"></div>
  </div>`;

  // Activity history: watches, searches, prompts (hidden until some load)
  html += `<div class="item-view-children" id="activity-section" data-item-id="${item.id}" style="margin-top:1rem;display:none">
    <div class="item-view-children-title" id="activity-title">Activity</div>
    <div id="activity-list" style="font-size:0.8rem"></div>
  </div>`;

  // Debug section
  html += `<div class="debug-only" style="margin-top:0.75rem">
    <hr style="border-color:var(--border);margin-bottom:0.75rem">
    <div class="item-detail-field"><label>ID</label>${item.id}</div>
    <div class="item-detail-field"><label>Source ID</label>${escHtml(item.source_id || '-')}</div>
    <div class="item-detail-field"><label>Type</label>${escHtml(item.subtype)}</div>
    <div class="item-detail-field"><label>Author</label>${escHtml(item.author_display)}</div>
    <div class="item-detail-field"><label>Actor</label>${escHtml(item.actor_display)}</div>
    <div class="item-detail-field"><label>Fetched</label>${item.fetched_at ? new Date(item.fetched_at).toLocaleString() : '-'}</div>
    <div class="item-detail-field"><label>Parent ID</label>${item.parent_id || '-'}</div>
    ${item.metadata ? `<div class="item-detail-field"><label>Metadata</label><div class="item-detail-meta">${escHtml(JSON.stringify(item.metadata, null, 2))}</div></div>` : ''}
  </div>`;

  container.innerHTML = html;
  wireTranscript(container);
  wireReactions(container, item);

  // Wire up image paste + file-input upload on the edit textarea
  const editTextarea = container.querySelector('#item-edit-content');
  if (editTextarea) {
    let uploadSeq = 0;
    const uploadAndInsert = async (blob, filename) => {
      const placeholder = `![Uploading…${++uploadSeq}]()`;
      const start = editTextarea.selectionStart;
      const end = editTextarea.selectionEnd;
      editTextarea.value = editTextarea.value.slice(0, start) + placeholder + '\n' + editTextarea.value.slice(end);
      editTextarea.selectionStart = editTextarea.selectionEnd = start + placeholder.length + 1;
      try {
        const fd = new FormData();
        fd.append('file', blob, filename);
        const res = await api(`/api/items/${item.id}/media`, { method: 'POST', body: fd });
        editTextarea.value = editTextarea.value.replace(placeholder, `![](${res.serve_url})`);
      } catch (err) {
        editTextarea.value = editTextarea.value.replace(placeholder, '');
        showToast('Image upload failed', 'error');
      }
    };

    editTextarea.addEventListener('paste', async (e) => {
      const items = e.clipboardData?.items || [];
      const images = [];
      for (const it of items) {
        if (it.kind === 'file' && it.type.startsWith('image/')) {
          const f = it.getAsFile();
          if (f) images.push(f);
        }
      }
      if (images.length === 0) return;
      e.preventDefault();
      for (const f of images) {
        const ext = (f.type.split('/')[1] || 'png').split('+')[0];
        const name = f.name || `pasted-${Date.now()}.${ext}`;
        await uploadAndInsert(f, name);
      }
    });

    const fileInput = container.querySelector('#item-edit-image-input');
    if (fileInput) {
      fileInput.addEventListener('change', async () => {
        for (const f of fileInput.files) {
          await uploadAndInsert(f, f.name || `upload-${Date.now()}.png`);
        }
        fileInput.value = '';
      });
    }
  }

  // Event delegation for actions — remove previous listener to prevent stacking
  if (container._actionHandler) container.removeEventListener('click', container._actionHandler);
  container._actionHandler = async (e) => {
    const btn = e.target.closest('[data-action]');
    if (!btn) return;
    if (btn.dataset.action === 'copy') copyToClipboard(item.content, 'Copied to clipboard');
    if (btn.dataset.action === 'copy-link') copyToClipboard(`${getApiBase() || location.origin}/items/${item.id}`, 'Link copied');
    if (btn.dataset.action === 'edit-item') {
      document.getElementById('item-title-display').style.display = 'none';
      document.getElementById('item-content-display').style.display = 'none';
      document.getElementById('item-detail-actions').style.display = 'none';
      document.getElementById('item-edit-title').value = item.title || '';
      document.getElementById('item-edit-content').value = item.content || '';
      document.getElementById('item-edit-form').style.display = 'block';
      document.getElementById('item-edit-title').focus();
    }
    if (btn.dataset.action === 'cancel-edit') {
      document.getElementById('item-edit-form').style.display = 'none';
      document.getElementById('item-title-display').style.display = '';
      document.getElementById('item-content-display').style.display = '';
      document.getElementById('item-detail-actions').style.display = '';
    }
    if (btn.dataset.action === 'attach-image') {
      document.getElementById('item-edit-image-input')?.click();
    }
    if (btn.dataset.action === 'save-edit') {
      const newTitle = document.getElementById('item-edit-title').value;
      const newContent = document.getElementById('item-edit-content').value;
      btn.disabled = true;
      btn.textContent = 'Saving...';
      try {
        const updated = await api(`/api/items/${btn.dataset.id}`, {
          method: 'PATCH',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ title: newTitle, content: newContent }),
        });
        Object.assign(item, updated);
        renderItemView(item);
        showToast('Saved');
        // No finally block — renderItemView() detached btn from the DOM,
        // so resetting its state would mutate a stale element.
      } catch (err) {
        showToast('Failed to save', 'error');
        btn.disabled = false;
        btn.textContent = 'Save';
      }
    }
    if (btn.dataset.action === 'toggle-history') {
      const panel = document.getElementById('item-history-panel');
      if (panel.style.display === 'none') {
        panel.style.display = 'block';
        await loadItemHistory(btn.dataset.id);
      } else {
        panel.style.display = 'none';
      }
    }
    if (btn.dataset.action === 'expand-version') {
      const detailDiv = container.querySelector(`#version-detail-${btn.dataset.versionId}`);
      if (detailDiv.style.display === 'none') {
        if (!detailDiv.dataset.loaded) {
          detailDiv.textContent = 'Loading...';
          try {
            const detail = await api(`/api/items/${btn.dataset.id}/history/${btn.dataset.versionId}`);
            detailDiv.textContent = '';
            const titleLine = document.createElement('div');
            titleLine.style.fontWeight = 'bold';
            titleLine.style.marginBottom = '0.25rem';
            titleLine.textContent = detail.title_old || '(no title)';
            const contentBlock = document.createElement('pre');
            contentBlock.style.whiteSpace = 'pre-wrap';
            contentBlock.style.wordBreak = 'break-word';
            contentBlock.style.margin = '0';
            contentBlock.style.fontSize = '0.75rem';
            contentBlock.textContent = detail.content_old || '(no content)';
            detailDiv.appendChild(titleLine);
            detailDiv.appendChild(contentBlock);
            detailDiv.dataset.loaded = '1';
          } catch (err) {
            detailDiv.textContent = 'Failed to load version';
          }
        }
        detailDiv.style.display = 'block';
        btn.textContent = 'Hide';
      } else {
        detailDiv.style.display = 'none';
        btn.textContent = 'View';
      }
    }
    if (btn.dataset.action === 'restore-version') {
      // Pre-Migration-9 history rows have parent_id_old=NULL by default. If
      // the current item still has a parent, restoring would silently detach
      // it. Detect and pass restore_parent=false to keep the current parent.
      const versionParentNull = btn.dataset.parentNull === '1';
      const currentHasParent = item.parent_id !== null && item.parent_id !== undefined;
      let restoreParent = true;
      let prompt = 'Restore this version? The current state will be saved as a new version.';
      if (versionParentNull && currentHasParent) {
        restoreParent = false;
        prompt = 'This version was captured before parent tracking. Restore content but keep the current parent attachment?';
      }
      if (!confirm(prompt)) return;
      btn.disabled = true;
      btn.textContent = 'Restoring...';
      try {
        const url = `/api/items/${btn.dataset.id}/history/${btn.dataset.versionId}/restore?restore_parent=${restoreParent}`;
        const updated = await api(url, { method: 'POST' });
        Object.assign(item, updated);
        renderItemView(item);
        showToast('Restored');
        // No finally — renderItemView detached btn from the DOM.
      } catch (err) {
        showToast('Failed to restore', 'error');
        btn.disabled = false;
        btn.textContent = 'Restore';
      }
    }
    if (btn.dataset.action === 'open-linked') return;
    if (btn.dataset.action === 'remove-link') {
      const q = new URLSearchParams({ other_id: btn.dataset.other, kind: btn.dataset.kind, direction: btn.dataset.direction });
      try {
        await api(`/api/items/${btn.dataset.id}/links?${q}`, { method: 'DELETE' });
      } catch {
        showToast('Failed to remove the link', 'error');
        return;
      }
      loadLinks(Number(btn.dataset.id));
      return;
    }
    if (btn.dataset.action === 'remove-tag') {
      await api(`/api/items/${btn.dataset.id}/tags/${encodeURIComponent(btn.dataset.tag)}`, { method: 'DELETE' });
      const updated = await api(`/api/items/${btn.dataset.id}`);
      item.tags = updated.tags;
      renderItemView(item);
    }
    if (btn.dataset.action === 'add-tag') {
      const input = container.querySelector(`[data-tag-input="${btn.dataset.id}"]`);
      const name = input.value.trim();
      if (!name) return;
      await api(`/api/items/${btn.dataset.id}/tags`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ name }),
      });
      const updated = await api(`/api/items/${btn.dataset.id}`);
      item.tags = updated.tags;
      renderItemView(item);
    }
    if (btn.dataset.action === 'show-note-form') {
      document.getElementById('note-form').style.display = 'block';
      document.getElementById('note-text').focus();
      btn.style.display = 'none';
    }
    if (btn.dataset.action === 'link-item') {
      openLinkDialog(parseInt(btn.dataset.id));
    }
    if (btn.dataset.action === 'record-note') {
      // Toggle recording for item detail note form
      const noteStatus = document.getElementById('note-record-status');
      const notePlayback = document.getElementById('note-audio-playback');
      if (isRecording) {
        // Stop recording
        isRecording = false;
        btn.textContent = 'Mic';
        btn.classList.remove('recording');
        clearTimeout(recordingTimer);
        if (mediaRecorder && mediaRecorder.state !== 'inactive') {
          mediaRecorder.onstop = async () => {
            mediaRecorder.stream.getTracks().forEach(t => t.stop());
            recordedBlob = new Blob(audioChunks, { type: mediaRecorder.mimeType });
            // Show playback
            notePlayback.src = URL.createObjectURL(recordedBlob);
            notePlayback.style.display = 'block';
            noteStatus.textContent = 'Transcribing...';
            // Auto-transcribe
            const ext = recordedBlob.type.includes('webm') ? 'webm' : 'ogg';
            const file = new File([recordedBlob], `recording.${ext}`, { type: recordedBlob.type });
            try {
              const data = await transcribeAudioFile(file);
              if (data) {
                document.getElementById('note-text').value = data.text;
                noteStatus.textContent = `${data.text.split(' ').length} words — edit and save`;
                // Store the server-created item ID for media attachment
                btn.dataset.transcribedItemId = data.item_id;
              }
            } catch (e) {
              noteStatus.textContent = 'Transcription failed — type your note instead';
            }
          };
          mediaRecorder.stop();
        }
      } else {
        // Start recording
        try {
          const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
          const mimeType = MediaRecorder.isTypeSupported('audio/webm;codecs=opus')
            ? 'audio/webm;codecs=opus'
            : MediaRecorder.isTypeSupported('audio/webm') ? 'audio/webm' : '';
          mediaRecorder = new MediaRecorder(stream, mimeType ? { mimeType } : undefined);
          audioChunks = [];
          recordedBlob = null;
          mediaRecorder.ondataavailable = (e) => {
            if (e.data.size > 0) audioChunks.push(e.data);
          };
          mediaRecorder.start(1000);
          isRecording = true;
          btn.textContent = 'Stop';
          btn.classList.add('recording');
          recordingStartTime = Date.now();
          const updateTimer = () => {
            if (!isRecording) return;
            const elapsed = Math.floor((Date.now() - recordingStartTime) / 1000);
            const m = Math.floor(elapsed / 60), s = elapsed % 60;
            noteStatus.textContent = `Recording... ${m}:${String(s).padStart(2, '0')}`;
            recordingTimer = setTimeout(updateTimer, 1000);
          };
          updateTimer();
        } catch (e) {
          showToast('Microphone error (' + e.name + '): ' + e.message, 'error');
        }
      }
    }
    if (btn.dataset.action === 'cancel-note') {
      document.getElementById('note-form').style.display = 'none';
      document.getElementById('note-text').value = '';
      const notePlayback = document.getElementById('note-audio-playback');
      if (notePlayback) { notePlayback.src = ''; notePlayback.style.display = 'none'; }
      const noteStatus = document.getElementById('note-record-status');
      if (noteStatus) noteStatus.textContent = '';
      recordedBlob = null;
      if (isRecording) {
        isRecording = false;
        clearTimeout(recordingTimer);
        if (mediaRecorder && mediaRecorder.state !== 'inactive') {
          mediaRecorder.stream.getTracks().forEach(t => t.stop());
          mediaRecorder.stop();
        }
      }
      const addBtn = container.querySelector('[data-action="show-note-form"]');
      if (addBtn) addBtn.style.display = '';
    }
    if (btn.dataset.action === 'save-note') {
      const textarea = document.getElementById('note-text');
      const content = textarea.value.trim();
      if (!content && !recordedBlob) return;
      btn.disabled = true;
      btn.textContent = 'Saving...';
      try {
        const isVoice = !!recordedBlob;
        const micBtn = container.querySelector('[data-action="record-note"]');
        const transcribedItemId = micBtn ? micBtn.dataset.transcribedItemId : null;
        let note;

        if (transcribedItemId) {
          // Server created item during transcription — PATCH with edits, reparent under this item
          await api(`/api/items/${transcribedItemId}`, {
            method: 'PATCH',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
              content: content || '(audio recording)',
              parent_id: item.id,
            }),
          });
          // Attach raw audio as media
          if (recordedBlob) {
            const ext = recordedBlob.type.includes('webm') ? 'webm' : 'ogg';
            const audioFile = new File([recordedBlob], `recording.${ext}`, { type: recordedBlob.type });
            const formData = new FormData();
            formData.append('file', audioFile);
            await api(`/api/items/${transcribedItemId}/media`, { method: 'POST', body: formData });
          }
          note = await api(`/api/items/${transcribedItemId}`);
        } else {
          // Text-only note or audio without transcription
          note = await api('/api/items', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
              source_type: 'note',
              source_id: 'note-' + Date.now(),
              content: content || '(audio recording)',
              author: 'user',
              is_own_content: true,
              parent_id: item.id,
              metadata: { input_type: isVoice ? 'voice' : 'text' },
            }),
          });
          // Attach audio if present
          if (recordedBlob && note.id) {
            const ext = recordedBlob.type.includes('webm') ? 'webm' : 'ogg';
            const audioFile = new File([recordedBlob], `recording.${ext}`, { type: recordedBlob.type });
            const formData = new FormData();
            formData.append('file', audioFile);
            await api(`/api/items/${note.id}/media`, { method: 'POST', body: formData });
          }
        }

        textarea.value = '';
        document.getElementById('note-form').style.display = 'none';
        const notePlayback = document.getElementById('note-audio-playback');
        if (notePlayback) { notePlayback.src = ''; notePlayback.style.display = 'none'; }
        const noteStatus = document.getElementById('note-record-status');
        if (noteStatus) noteStatus.textContent = '';
        recordedBlob = null;
        if (micBtn) delete micBtn.dataset.transcribedItemId;
        const addBtn = container.querySelector('[data-action="show-note-form"]');
        if (addBtn) addBtn.style.display = '';
        // Append the new note to the children list
        const list = document.getElementById('children-list');
        if (list) {
          const placeholder = list.querySelector(':scope > .parts-placeholder');
          if (placeholder) list.innerHTML = '';
          list.appendChild(renderCard(note));
        }
        item.child_count = (item.child_count || 0) + 1;
        const title = container.querySelector('#parts-title');
        if (title) title.textContent = partsTitle(item);
        showToast('Note added');
      } catch (err) {
        showToast('Failed to save note', 'error');
      } finally {
        btn.disabled = false;
        btn.textContent = 'Save';
      }
    }
    if (btn.dataset.action === 'edit-profile') {
      const role = btn.dataset.role;
      const editRow = container.querySelector(`#${role}-edit`);
      if (editRow) editRow.style.display = 'flex';
      btn.style.display = 'none';
    }
    if (btn.dataset.action === 'save-profile') {
      const role = btn.dataset.role;
      const urn = btn.dataset.urn;
      const nameInput = container.querySelector(`[data-profile-name="${role}"]`);
      const urlInput = container.querySelector(`[data-profile-url="${role}"]`);
      const displayName = nameInput.value.trim();
      if (!displayName) return;
      const body = { display_name: displayName };
      const profileUrl = urlInput.value.trim();
      if (profileUrl) body.profile_url = profileUrl;
      await api(`/api/profiles/${encodeURIComponent(urn)}`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      });
      // Refresh item to get updated display names
      const updated = await api(`/api/items/${item.id}`);
      Object.assign(item, updated);
      renderItemView(item);
    }
  };
  container.addEventListener('click', container._actionHandler);

  // Enter key in tag input
  container.querySelector(`[data-tag-input="${item.id}"]`)?.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') container.querySelector(`[data-action="add-tag"][data-id="${item.id}"]`).click();
  });

  // Load parent post card
  if (item.parent_id && document.getElementById('parent-post')) {
    api(`/api/items/${item.parent_id}`).then(parent => {
      const list = document.getElementById('parent-post');
      if (!list || location.pathname !== `/items/${item.id}`) return;
      list.innerHTML = '';
      list.appendChild(renderCard(parent));
    }).catch(() => {
      const list = document.getElementById('parent-post');
      if (list && location.pathname === `/items/${item.id}`) list.innerHTML = '<span style="color:var(--muted);font-size:0.8rem">Could not load parent</span>';
    });
  }

  // Load children
  if (item.child_count - (item.reaction_count || 0) > 0) {
    loadAllChildren(item.id, PARTS_QUERY).then(children => {
      const list = document.getElementById('children-list');
      // The user may have opened another item while the pages loaded
      if (!list || list.dataset.itemId !== String(item.id)) return;
      list.innerHTML = '';
      const isChat = children.length > 0 && CHAT_ROLES.has(getChatRole(children[0]));
      if (isChat) {
        const thread = document.createElement('div');
        thread.className = 'chat-thread';
        children.slice().reverse().forEach(child => thread.appendChild(renderChatBubble(child)));
        list.appendChild(thread);
      } else {
        renderParts(item, children, list);
      }
    }).catch(() => {
      const list = document.getElementById('children-list');
      if (list && list.dataset.itemId === String(item.id)) {
        list.innerHTML = '<span style="color:var(--muted);font-size:0.8rem">Could not load these items</span>';
      }
    });
  }

  loadActivities(item.id);
  loadPeople(item.id, item.source_type);
  loadLinks(item.id);

  // Load similar items (held parent items only)
  if (!item.parent_id && item.held !== false) {
    api(`/api/embeddings/similar/${item.id}?limit=5`).then(data => {
      const list = document.getElementById('similar-list');
      if (!list || location.pathname !== `/items/${item.id}`) return;
      if (!data.length) {
        list.innerHTML = '<span style="color:var(--muted);font-size:0.8rem">No embeddings yet — run rebuild</span>';
        return;
      }
      list.innerHTML = '';
      data.forEach(sim => {
        const card = renderCard(sim);
        list.appendChild(card);
      });
    }).catch(() => {
      const list = document.getElementById('similar-list');
      if (list && location.pathname === `/items/${item.id}`) list.innerHTML = '<span style="color:var(--muted);font-size:0.8rem">Unavailable</span>';
    });
  }
}

const ACTIVITY_SHOWN = 20;

async function loadActivities(itemId) {
  let data;
  try {
    data = await api(`/api/items/${itemId}/activities?limit=${ACTIVITY_SHOWN}`);
  } catch {
    return;  // older server, or offline: the section stays hidden
  }
  const section = document.getElementById('activity-section');
  // The view may have moved on to another item while this loaded
  if (!section || section.dataset.itemId !== String(itemId) || !data.total) return;
  const verb = v => v.charAt(0).toUpperCase() + v.slice(1);
  const rows = data.activities.map(a => {
    const query = a.details && a.details.query ? ` “${escHtml(String(a.details.query))}”` : '';
    // Someone else's: "Ann Lee liked"; yours: "Liked"
    const what = a.actor_id ? `${escHtml(a.actor || 'Someone')} ${escHtml(a.verb)}` : escHtml(verb(a.verb));
    return `<div>${what}${query} · ${new Date(a.occurred_at).toLocaleString()}</div>`;
  }).join('');
  const more = data.total > data.activities.length
    ? `<div style="color:var(--muted)">and ${data.total - data.activities.length} earlier</div>` : '';
  document.getElementById('activity-title').textContent = `Activity (${data.total})`;
  document.getElementById('activity-list').innerHTML = rows + more;
  section.style.display = '';
}

const ROLE_LABELS = {
  author: 'Author', creator: 'Creator', narrator: 'Narrator',
  publisher: 'Publisher', recipient: 'To', mentioned: 'Mentions',
};

// A source's items name people by accounts on this platform
const ACCOUNT_PLATFORM = { gmail: 'email' };

async function loadPeople(itemId, sourceType) {
  let data;
  try {
    data = await api(`/api/items/${itemId}/agents`);
  } catch {
    return;  // older server, or offline: the section stays hidden
  }
  const section = document.getElementById('people-section');
  if (!section || section.dataset.itemId !== String(itemId)) return;
  if (!data.agents.length) {
    section.style.display = 'none';
    return;
  }
  const rows = data.agents.map(a => {
    const name = a.person.is_self ? selfName() : a.person.name;
    // The account on a web page: the one on this item's platform first, so "Matt"
    // on a LinkedIn post links your LinkedIn profile. Only web and mail addresses:
    // a URL from imported data must not run script.
    const safe = a.accounts.filter(x => x.url && /^(https?:|mailto:)/i.test(x.url));
    const platform = ACCOUNT_PLATFORM[sourceType] || sourceType;
    const account = safe.find(x => x.platform === platform) || safe[0];
    // The name opens their page here; their account elsewhere is the ↗ beside it
    const shown = `<a href="/people/${a.person.id}">${escHtml(name)}</a>`
      + (account ? ` <a href="${escHtml(account.url)}" target="_blank" rel="noopener" class="muted" aria-label="${escHtml(name)} on ${escHtml(account.platform)}">↗</a>` : '');
    const kind = a.person.kind === 'person' ? '' : a.person.kind;
    return `<div style="display:flex;gap:0.4rem;align-items:baseline;padding:0.1rem 0">
      <span style="color:var(--muted);white-space:nowrap">${escHtml(ROLE_LABELS[a.role] || a.role)}</span>
      ${shown}
      <span style="color:var(--muted)">${escHtml(kind)}</span>
    </div>`;
  }).join('');
  document.getElementById('people-title').textContent = `People (${data.agents.length})`;
  document.getElementById('people-list').innerHTML = rows;
  section.style.display = '';
}

// kind -> [label when this item is the link's from end, label when it is the to end]
const LINK_LABELS = {
  references: ['Links to', 'Linked from'],
  syndication: ['Cross-posted as', 'Cross-post of'],
  in_reply_to: ['Reply to', 'Replies'],
};

// Children come newest first, a page at a time: load every page, so a long chat
// shows every turn
const CHILDREN_PAGE = 100;
async function loadAllChildren(parentId, query = '') {
  const children = [];
  const seen = new Set();
  for (let offset = 0; ; offset += CHILDREN_PAGE) {
    const data = await api(`/api/items?parent_id=${parentId}&include_children=true`
      + `&limit=${CHILDREN_PAGE}&offset=${offset}${query}`);
    // A child added while the pages load shifts the next page by one: skip repeats
    data.items.forEach(child => { if (!seen.has(child.id)) { seen.add(child.id); children.push(child); } });
    if (data.items.length < CHILDREN_PAGE || offset + CHILDREN_PAGE >= data.total) return children;
  }
}

async function loadLinks(itemId) {
  let data;
  try {
    data = await api(`/api/items/${itemId}/links`);
  } catch {
    return;  // older server, or offline: the section stays hidden
  }
  const section = document.getElementById('links-section');
  // The view may have moved on to another item while this loaded
  if (!section || section.dataset.itemId !== String(itemId)) return;
  if (!data.links.length) {
    section.style.display = 'none';
    return;
  }
  const rows = data.links.map(l => {
    const labels = LINK_LABELS[l.kind] || [l.kind, `${l.kind} (from)`];
    const label = labels[l.direction === 'outgoing' ? 0 : 1];
    // A comment has no title, and its address is its post's: its text tells it apart
    const title = l.item.title || l.item.preview || l.item.url || `Item ${l.item.id}`;
    return `<div style="display:flex;gap:0.4rem;align-items:baseline;padding:0.1rem 0">
      <span style="color:var(--muted);white-space:nowrap">${escHtml(label)}</span>
      <a href="/items/${l.item.id}" data-action="open-linked" data-id="${l.item.id}"
        style="color:var(--ok);text-decoration:none;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;min-width:0">${escHtml(title)}</a>
      <span style="color:var(--muted)">${escHtml(l.item.source_type)}</span>
      <span class="tag-remove" title="Remove this link" data-action="remove-link" data-id="${itemId}"
        data-other="${l.item.id}" data-kind="${escHtml(l.kind)}" data-direction="${l.direction}">&times;</span>
    </div>`;
  }).join('');
  document.getElementById('links-title').textContent = `Links (${data.links.length})`;
  document.getElementById('links-list').innerHTML = rows;
  section.style.display = '';
}


marked.use({
  renderer: {
    link(token) {
      // Links to this app's own pages (/items/1, #heading) open here, judged by where
      // the address resolves (so "/\\host" is not ours); files and others in a new tab
      let href = token.href || '';
      let own = false;
      try {
        const u = new URL(href, location.href);
        own = u.origin === location.origin && !u.pathname.startsWith('/api/');
      } catch { /* not an address: leave as written */ }
      if (href.startsWith('/api/')) href = getApiBase() + href;
      return own ? `<a href="${escHtml(href)}">${token.text}</a>`
        : `<a href="${escHtml(href)}" target="_blank" rel="noopener">${token.text}</a>`;
    },
    image(token) {
      let href = token.href || '';
      if (href.startsWith('/api/')) href = getApiBase() + href;
      return `<img src="${escHtml(href)}" alt="${escHtml(token.text || '')}" style="max-width:100%;height:auto">`;
    }
  }
});

function renderMarkdown(s) {
  if (!s) return '';
  const html = marked.parse(s);
  return DOMPurify.sanitize(html, { ADD_ATTR: ['target'] });
}

// After saving something new, the Timeline shows it if it is open
function refreshTimeline() {
  redrawTimeline();
  redrawLibrary();
  const path = location.pathname;
  if (path === '/' || path.startsWith('/day/') || path === '/library') render();
}

/* ── Recent Activity ── */
async function loadRecent() {
  const list = document.getElementById('recent-list');
  const summary = document.getElementById('recent-summary');
  const hours = parseInt(document.getElementById('recent-period').value);

  list.innerHTML = renderSkeletons(5);
  summary.textContent = '';

  try {
    const since = new Date(Date.now() - hours * 3600000).toISOString();
    const data = await api(`/api/items?since=${encodeURIComponent(since)}&limit=100`);

    list.innerHTML = '';

    if (data.items.length === 0) {
      list.innerHTML = '<div class="empty-state">No new items in this period</div>';
      summary.textContent = '';
      return;
    }

    // Count by source
    const counts = {};
    data.items.forEach(i => {
      const s = i.source_type;
      counts[s] = (counts[s] || 0) + 1;
    });
    const parts = Object.entries(counts).map(([k, v]) => `${k}: ${v}`);
    summary.innerHTML = `<strong>${data.items.length}</strong> items synced — ${parts.join(', ')}`;

    data.items.forEach(item => list.appendChild(renderCard(item)));
  } catch (e) {
    list.innerHTML = '';
    showToast(`Failed to load recent items: ${e.message}`, 'error');
  }
}

document.getElementById('recent-period').addEventListener('change', loadRecent);

/* ── Search ── */
let searchTimer = null;
// Each search gets a number; only the newest may update the page, so a slow earlier
// request can't overwrite later results
let searchSeq = 0;

function searchAddress() {
  const q = document.getElementById('search-input').value.trim();
  const mode = document.getElementById('search-mode').value;
  const params = new URLSearchParams();
  if (q) params.set('q', q);
  if (mode !== 'hybrid') params.set('mode', mode);
  const qs = params.toString();
  return qs ? '/search?' + qs : '/search';
}

document.getElementById('search-input').addEventListener('input', () => {
  // The address follows every keystroke, so nothing typed is lost; the search waits
  setAddress(searchAddress());
  tabAddress.search = location.pathname + location.search;
  clearTimeout(searchTimer);
  searchTimer = setTimeout(() => {
    if (location.pathname === '/search') navigate(searchAddress(), { replace: true });
  }, 300);
});
document.getElementById('search-mode').addEventListener('change', () => {
  clearTimeout(searchTimer);
  navigate(searchAddress(), { replace: true });
});

let searchShown = null;
route(/^\/search$/, (m, url) => {
  showOnly('tab-search', 'search');
  const input = document.getElementById('search-input');
  const q = (url.searchParams.get('q') || '').trim();
  const modes = ['hybrid', 'keyword', 'semantic'];
  const mode = url.searchParams.get('mode');
  document.getElementById('search-mode').value = modes.includes(mode) ? mode : 'hybrid';
  if (input.value.trim() !== q) input.value = q;
  const key = q + '|' + document.getElementById('search-mode').value;
  if (focusSearch) { focusSearch = false; input.focus(); }
  if (key === searchShown) return;
  searchShown = key;
  if (!q) {
    searchSeq++;
    document.getElementById('search-list').innerHTML = '';
    document.getElementById('search-status').textContent = 'Tip: use "quotes" for exact phrases, -"quotes" to exclude one, word* for prefix, AND/OR/NOT for boolean';
    return;
  }
  return doSearch(q);
});

// A result found by one of its parts (a chat's round, a post's reply, a book's
// highlight): the card opens at that part, which links up to the whole, and says so.
// Its preview is the part's text: the matched words, or its opening (#221).
function foundInPart(card, item) {
  card.href = `/items/${item.match_id}`;
  const body = card.querySelector('.item-body');
  if (!body) return;
  let preview = card.querySelector('.item-preview');
  if (!preview) {
    preview = document.createElement('div');
    preview.className = 'item-preview';
    body.querySelector('.item-card-line')?.after(preview);
  }
  if (item.preview && !item.preview.includes('<<')) preview.textContent = item.preview;
  const where = document.createElement('div');
  where.className = 'item-card-counts';
  where.textContent = `Found in a ${(item.match_label || 'part').toLowerCase()} · opens there`;
  preview.after(where);
}

async function doSearch(q) {
  const list = document.getElementById('search-list');
  const status = document.getElementById('search-status');
  const mode = document.getElementById('search-mode').value;
  const seq = ++searchSeq;
  list.innerHTML = '<div class="empty-state"><div class="spinner"></div></div>';
  status.textContent = `Searching (${mode})...`;
  try {
    const data = await api(`/api/items/search?q=${encodeURIComponent(q)}&limit=50&mode=${mode}`);
    if (seq !== searchSeq) return;
    list.innerHTML = '';
    if (data.items.length === 0) {
      status.textContent = `No results for "${q}"`;
      list.innerHTML = `<div class="empty-state">Try different keywords or use prefix matching (e.g. "learn*")</div>`;
      return;
    }
    status.textContent = `${data.total} result${data.total !== 1 ? 's' : ''}`;
    data.items.forEach(item => {
      const card = renderCard(item);
      if (item.match_id) foundInPart(card, item);
      // Replace preview with highlighted snippet if it contains markers
      const preview = card.querySelector('.item-preview');
      if (preview && item.preview && item.preview.includes('<<')) {
        preview.innerHTML = item.preview
          .replace(/</g, '&lt;').replace(/>/g, '&gt;')
          .replace(/&lt;&lt;/g, '<mark style="background:var(--accent);color:var(--on-accent);padding:0 2px;border-radius:2px">')
          .replace(/&gt;&gt;/g, '</mark>');
      }
      list.appendChild(card);
    });
  } catch (e) {
    if (seq !== searchSeq) return;
    searchShown = null;  // coming back to this search tries again
    // Keep the failure on the page, not only in a toast that fades.
    // api() throws "<status>: <body>"; show the server's detail when there is one.
    let reason = e.message;
    const m = /^(\d{3}): (.*)$/s.exec(e.message);
    if (m) {
      try {
        const detail = JSON.parse(m[2]).detail;
        if (detail !== undefined) {
          reason = `${m[1]}: ${typeof detail === 'string' ? detail : JSON.stringify(detail)}`;
        }
      } catch { /* not JSON: keep the raw message */ }
    }
    status.textContent = `Search failed (${mode})`;
    list.innerHTML = `<div class="empty-state">Search failed: ${escHtml(reason)}</div>`;
    showToast(`Search failed: ${reason}`, 'error');
  }
}

/* ── Sources ── */
async function loadSources() {
  try {
    const [sources, status] = await Promise.all([
      api('/api/sources'),
      api('/api/sources/status'),
    ]);

    const tbody = document.querySelector('#sources-table tbody');
    tbody.innerHTML = '';
    sources.forEach(s => {
      const tr = document.createElement('tr');
      tr.innerHTML = `
        <td><span class="source-badge ${sourceClass(s.name)}">${escHtml(s.name)}</span></td>
        <td>${escHtml(s.description)}</td>
        <td style="font-size:0.8rem;color:var(--muted)">${s.last_sync ? relTime(s.last_sync) : 'never'}</td>
        <td><button class="sync-btn" onclick="triggerSync('${escHtml(s.name)}', this)">Sync</button></td>
      `;
      tbody.appendChild(tr);
    });

    const logTbody = document.querySelector('#sync-log-table tbody');
    logTbody.innerHTML = '';
    if (status.length === 0) {
      logTbody.innerHTML = '<tr><td colspan="5" style="color:var(--muted)">No sync history</td></tr>';
    } else {
      status.forEach(s => {
        const tr = document.createElement('tr');
        tr.innerHTML = `
          <td>${escHtml(s.source_type)}</td>
          <td style="font-size:0.8rem">${s.started_at ? relTime(s.started_at) : '-'}</td>
          <td><span class="sync-status ${s.status}">${s.status}</span></td>
          <td title="new / changed">${[s.items_added ? `+${s.items_added}` : '', s.items_updated ? `~${s.items_updated}` : ''].filter(Boolean).join(' ') || '-'}</td>
          <td style="font-size:0.75rem;color:var(--muted)">${s.error_message ? escHtml(s.error_message.substring(0,60)) : '-'}</td>
        `;
        logTbody.appendChild(tr);
      });
    }
  } catch (e) {
    showToast(`Failed to load sources: ${e.message}`, 'error');
  }
}

async function triggerSync(name, btn) {
  btn.disabled = true;
  btn.textContent = 'Syncing...';
  try {
    await api(`/api/sources/${encodeURIComponent(name)}/sync`, { method: 'POST' });
    showToast(`Sync started for ${name}`, 'ok');
    // Poll for completion
    setTimeout(() => { loadSources(); btn.disabled = false; btn.textContent = 'Sync'; }, 3000);
  } catch (e) {
    showToast(`Sync failed: ${e.message}`, 'error');
    btn.disabled = false;
    btn.textContent = 'Sync';
  }
}

/* ── Audible Auth ── */
let _audibleStateToken = null;

async function loadAudibleAuth() {
  const section = document.getElementById('audible-auth-section');
  const statusEl = document.getElementById('audible-auth-status');
  const flowEl = document.getElementById('audible-auth-flow');
  try {
    const data = await api('/api/audible/auth-status');
    section.style.display = 'block';
    if (data.authenticated) {
      statusEl.innerHTML = `<span style="color:var(--ok)">Connected</span> (${escHtml(data.locale || 'unknown')})`;
      flowEl.style.display = 'none';
    } else {
      statusEl.innerHTML = `<select id="audible-locale" style="font-size:0.8rem;padding:0.25rem;background:var(--bg);color:var(--text);border:1px solid var(--border);border-radius:4px;margin-right:0.3rem">
        <option value="fr">France</option><option value="uk">UK</option><option value="us">US</option>
        <option value="de">Germany</option><option value="au">Australia</option><option value="ca">Canada</option>
        <option value="it">Italy</option><option value="es">Spain</option><option value="jp">Japan</option><option value="in">India</option>
      </select><button class="sync-btn" onclick="startAudibleAuth()">Connect</button>`;
      flowEl.style.display = 'none';
    }
  } catch {
    section.style.display = 'none';
  }
}

async function startAudibleAuth() {
  const statusEl = document.getElementById('audible-auth-status');
  const flowEl = document.getElementById('audible-auth-flow');
  try {
    const locale = document.getElementById('audible-locale')?.value || 'uk';
    const data = await api(`/api/audible/auth-url?locale=${locale}`);
    _audibleStateToken = data.state_token;
    openExternal(data.url);
    statusEl.innerHTML = '<span style="color:var(--muted)">Waiting for login...</span>';
    flowEl.style.display = 'block';
    document.getElementById('audible-redirect-url').focus();
  } catch (e) {
    showToast(`Failed to start auth: ${e.message}`, 'error');
  }
}

async function completeAudibleAuth() {
  const input = document.getElementById('audible-redirect-url');
  const url = input.value.trim();
  if (!url) return;
  try {
    await api('/api/audible/auth-complete', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ redirect_url: url, state_token: _audibleStateToken }),
    });
    showToast('Audible connected!', 'ok');
    input.value = '';
    loadAudibleAuth();
  } catch (e) {
    showToast(`Auth failed: ${e.message}`, 'error');
  }
}

/* ── Google Auth ── */
let _googleAuthState = null;

async function loadGoogleAuth() {
  const section = document.getElementById('google-auth-section');
  const statusEl = document.getElementById('google-auth-status');
  const flowEl = document.getElementById('google-auth-flow');
  const driveSection = document.getElementById('drive-import-section');
  try {
    const data = await api('/api/google/auth-status');
    section.style.display = 'block';
    const hasYoutube = data.scopes.some(s => s.includes('youtube'));
    const hasDrive = data.scopes.some(s => s.includes('drive'));
    if (data.authenticated && hasYoutube && hasDrive) {
      statusEl.innerHTML = `<span style="color:var(--ok)">Connected</span> <span style="color:var(--muted);font-size:0.75rem">(YouTube + Drive)</span> <button class="sync-btn" onclick="startGoogleAuth()" style="margin-left:0.5rem;font-size:0.75rem;padding:0.15rem 0.5rem">Reconnect</button>`;
      flowEl.style.display = 'none';
      driveSection.style.display = '';
    } else {
      const missing = [!hasYoutube && 'YouTube', !hasDrive && 'Drive'].filter(Boolean);
      const scopeNote = data.authenticated && missing.length ? ` (missing: ${missing.join(', ')})` : '';
      statusEl.innerHTML = `<button class="sync-btn" onclick="startGoogleAuth()">Connect Google</button><span style="color:var(--muted);font-size:0.75rem;margin-left:0.3rem">${scopeNote}</span>`;
      flowEl.style.display = 'none';
      driveSection.style.display = hasDrive && data.authenticated ? '' : 'none';
    }
  } catch {
    section.style.display = 'none';
    driveSection.style.display = 'none';
  }
}

function _isAndroid() {
  return IS_TAURI && /Android/i.test(navigator.userAgent);
}

async function startGoogleAuth() {
  if (_isAndroid() && tauriInvoke) {
    return startGoogleAuthAndroid();
  }
  const statusEl = document.getElementById('google-auth-status');
  const flowEl = document.getElementById('google-auth-flow');
  try {
    const data = await api('/api/google/auth-url');
    _googleAuthState = data.state;
    openExternal(data.url);
    statusEl.innerHTML = '<span style="color:var(--muted)">Waiting for authorization...</span>';
    flowEl.style.display = 'block';
    document.getElementById('google-auth-code').focus();
  } catch (e) {
    showToast(`Failed to start auth: ${e.message}`, 'error');
  }
}

async function startGoogleAuthAndroid() {
  const statusEl = document.getElementById('google-auth-status');
  const scopes = [
    'https://www.googleapis.com/auth/youtube.readonly',
    'https://www.googleapis.com/auth/drive.readonly',
  ];
  try {
    statusEl.innerHTML = '<span style="color:var(--muted)">Opening Google sign-in...</span>';
    const cfg = await api('/api/google/android-config');
    const result = await tauriInvoke('plugin:google-auth|start_google_auth', {
      payload: { scopes, webClientId: cfg.web_client_id },
    });
    statusEl.innerHTML = '<span style="color:var(--muted)">Exchanging credentials...</span>';
    await api('/api/google/android-auth-complete', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        code: result.serverAuthCode,
        granted_scopes: result.grantedScopes || scopes,
      }),
    });
    showToast('Google connected!', 'ok');
    loadGoogleAuth();
  } catch (e) {
    const msg = typeof e === 'string' ? e : (e && e.message) || String(e);
    showToast(`Auth failed: ${msg}`, 'error');
    loadGoogleAuth();
  }
}

async function completeGoogleAuth() {
  const input = document.getElementById('google-auth-code');
  const code = input.value.trim();
  if (!code) return;
  try {
    await api('/api/google/auth-complete', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ code, state: _googleAuthState }),
    });
    showToast('Google connected!', 'ok');
    input.value = '';
    loadGoogleAuth();
  } catch (e) {
    showToast(`Auth failed: ${e.message}`, 'error');
  }
}

function _isDriveOrDocsUrl(url) {
  return /^https?:\/\/(drive\.google\.com|docs\.google\.com)\//i.test(url);
}

async function importDriveUrl() {
  const input = document.getElementById('drive-url-input');
  const url = input.value.trim();
  if (!url) return;
  const btn = document.getElementById('drive-import-btn');
  const progress = document.getElementById('drive-import-progress');
  const result = document.getElementById('drive-import-result');
  btn.disabled = true;
  progress.style.display = '';
  result.style.display = 'none';
  try {
    const data = await api('/api/import/drive/sync', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ urls: [url] }),
    });
    result.style.display = '';
    let html = `Imported <strong>${data.items_added}</strong> items`;
    if (data.items_skipped) html += `, ${data.items_skipped} skipped`;
    if (data.errors.length) {
      html += `<br><span style="color:var(--error)">${data.errors.length} errors</span>`;
    }
    result.innerHTML = html;
    showToast(`Imported ${data.items_added} items from Drive`, 'ok');
    input.value = '';
  } catch (e) {
    result.style.display = '';
    result.innerHTML = `<span style="color:var(--error)">Import failed: ${escHtml(e.message)}</span>`;
    showToast(`Drive import failed: ${e.message}`, 'error');
  } finally {
    btn.disabled = false;
    progress.style.display = 'none';
  }
}

/* ── Voice Notes ── */
let isRecording = false;
let mediaRecorder = null;
let audioChunks = [];
let recordedBlob = null;
let recordingStartTime = null;
let recordingTimer = null;
let currentTranscript = '';
let refineResult = null;
let lastTranscribedItemId = null;

function toggleRecording() {
  if (isRecording) stopRecording();
  else startRecording();
}

async function startRecording() {
  const btn = document.getElementById('record-btn');
  const status = document.getElementById('record-status');

  try {
    const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    const mimeType = MediaRecorder.isTypeSupported('audio/webm;codecs=opus')
      ? 'audio/webm;codecs=opus'
      : MediaRecorder.isTypeSupported('audio/webm') ? 'audio/webm' : '';
    mediaRecorder = new MediaRecorder(stream, mimeType ? { mimeType } : undefined);
    audioChunks = [];
    recordedBlob = null;
    lastTranscribedItemId = null;

    mediaRecorder.ondataavailable = (e) => {
      if (e.data.size > 0) audioChunks.push(e.data);
    };

    mediaRecorder.onstop = () => {
      stream.getTracks().forEach(t => t.stop());
      recordedBlob = new Blob(audioChunks, { type: mediaRecorder.mimeType });
      showPostRecordingUI();
    };

    mediaRecorder.start(1000); // collect chunks every second
    isRecording = true;
    btn.classList.add('recording');
    btn.textContent = 'Stop';
    recordingStartTime = Date.now();
    updateRecordingTimer();
  } catch (e) {
    showToast('Microphone error (' + e.name + '): ' + e.message, 'error');
  }
}

function updateRecordingTimer() {
  if (!isRecording) return;
  const elapsed = Math.floor((Date.now() - recordingStartTime) / 1000);
  const mins = Math.floor(elapsed / 60);
  const secs = elapsed % 60;
  document.getElementById('record-status').textContent =
    `Recording... ${mins}:${String(secs).padStart(2, '0')}`;
  recordingTimer = setTimeout(updateRecordingTimer, 1000);
}

function stopRecording() {
  const btn = document.getElementById('record-btn');
  isRecording = false;
  btn.classList.remove('recording');
  btn.textContent = 'Record';
  clearTimeout(recordingTimer);

  if (mediaRecorder && mediaRecorder.state !== 'inactive') {
    mediaRecorder.stop();
  }
}

function showPostRecordingUI() {
  const status = document.getElementById('record-status');
  const editor = document.getElementById('note-editor');
  const elapsed = Math.floor((Date.now() - recordingStartTime) / 1000);
  const mins = Math.floor(elapsed / 60);
  const secs = elapsed % 60;
  status.textContent = `Recorded ${mins}:${String(secs).padStart(2, '0')}`;

  // Show audio playback
  const playback = document.getElementById('audio-playback');
  if (playback) {
    const url = URL.createObjectURL(recordedBlob);
    playback.src = url;
    playback.style.display = 'block';
  }

  // Show transcribe button, hide transcript fields until transcribed
  const transcribeBtn = document.getElementById('transcribe-btn');
  if (transcribeBtn) transcribeBtn.style.display = '';
  document.getElementById('note-title').value = '';
  document.getElementById('original-transcript').textContent = '';
  document.getElementById('edited-note').value = '';
  editor.style.display = '';
  currentTranscript = '';
  refineResult = null;
}

// Listen for Tauri STT events (Phase 3)
if (IS_TAURI && typeof tauriListen === 'function') {
  tauriListen('stt-transcription', (event) => {
    currentTranscript = event.payload;
    const status = document.getElementById('record-status');
    status.textContent = `${currentTranscript.split(' ').length} words`;
  });
}

function checkSttModel() {
  if (IS_TAURI && tauriInvoke) {
    tauriInvoke('get_stt_status').then(status => {
      if (!status.model_exists) {
        const el = document.getElementById('record-status');
        el.innerHTML = '<button class="sync-btn" id="download-model-btn" style="font-size:0.75rem">Download Model (~142MB)</button>';
        document.getElementById('download-model-btn').addEventListener('click', startModelDownload);
      }
    }).catch(() => {});
  }
}

async function startModelDownload() {
  const btn = document.getElementById('download-model-btn');
  const status = document.getElementById('record-status');
  if (!btn) return;
  btn.disabled = true;
  btn.textContent = 'Downloading...';

  // Listen for progress
  if (tauriListen) {
    tauriListen('model-download-progress', (event) => {
      const { downloaded, total } = event.payload;
      if (total > 0) {
        const pct = Math.round((downloaded / total) * 100);
        const mb = (downloaded / 1024 / 1024).toFixed(1);
        const totalMb = (total / 1024 / 1024).toFixed(0);
        status.innerHTML = `<span style="font-size:0.75rem">Downloading: ${mb}/${totalMb} MB (${pct}%)</span>`;
      }
    });
  }

  try {
    await tauriInvoke('download_model');
    status.innerHTML = '<span style="color:var(--ok);font-size:0.8rem">Model ready — hit Record</span>';
    showToast('Whisper model downloaded', 'ok');
  } catch (e) {
    status.innerHTML = '<span style="color:var(--error);font-size:0.8rem">Download failed: ' + escHtml(e) + '</span>';
    showToast('Model download failed: ' + e, 'error');
  }
}



async function transcribeRecordedAudio() {
  if (!recordedBlob) { showToast('No recording to transcribe', 'error'); return; }
  const btn = document.getElementById('transcribe-btn');
  const status = document.getElementById('record-status');
  btn.disabled = true;
  btn.textContent = 'Transcribing...';
  status.textContent = 'Uploading audio...';

  const ext = recordedBlob.type.includes('webm') ? 'webm' : 'ogg';
  const file = new File([recordedBlob], `recording.${ext}`, { type: recordedBlob.type });
  try {
    const data = await transcribeAudioFile(file);
    if (data) {
      lastTranscribedItemId = data.item_id;
      currentTranscript = data.text;
      document.getElementById('note-title').value = data.title || '';
      document.getElementById('original-transcript').textContent = data.text;
      document.getElementById('edited-note').value = data.text;
      status.textContent = `${data.text.split(' ').length} words transcribed`;
      btn.style.display = 'none';
    }
  } catch (e) {
    showToast('Transcription failed: ' + e.message, 'error');
    status.textContent = 'Transcription failed — you can still save the audio';
  } finally {
    btn.disabled = false;
    btn.textContent = 'Transcribe';
  }
}

async function saveVoiceNote() {
  const original = document.getElementById('original-transcript').textContent;
  const edited = document.getElementById('edited-note').value.trim();
  const title = document.getElementById('note-title').value.trim() || null;

  // Allow saving audio-only (no transcript) if we have a recording
  if (!original && !edited && !recordedBlob) {
    showToast('Nothing to save', 'error'); return;
  }

  const content = edited || original || '(audio recording)';
  const metadata = {};
  if (original) metadata.original_transcript = original;
  if (edited && edited !== original) metadata.edited_text = edited;
  if (refineResult) {
    metadata.edit_prompt = refineResult.prompt_used;
    metadata.edit_model = refineResult.model_used;
  }
  metadata.input_type = recordedBlob ? 'recording' : 'file';

  try {
    let itemId = lastTranscribedItemId;

    if (itemId) {
      // Server already created the item via transcribe — PATCH if edited
      if ((edited && edited !== original) || title) {
        await api(`/api/items/${itemId}`, {
          method: 'PATCH',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            ...(edited && edited !== original ? { content: edited } : {}),
            ...(title ? { title } : {}),
          }),
        });
      }
    } else {
      // No transcription — create new item
      const item = await api('/api/items', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          source_type: 'voice',
          source_id: 'voice-' + Date.now(),
          title,
          content,
          is_own_content: true,
          metadata,
        }),
      });
      itemId = item.id;
    }

    // Attach raw audio as media
    if (recordedBlob && itemId) {
      const ext = recordedBlob.type.includes('webm') ? 'webm' : 'ogg';
      const audioFile = new File([recordedBlob], `recording.${ext}`, { type: recordedBlob.type });
      const formData = new FormData();
      formData.append('file', audioFile);
      await api(`/api/items/${itemId}/media`, { method: 'POST', body: formData });
    }

    showToast('Voice note saved', 'ok');
    resetVoiceEditor();
    loadSavedNotes();
  } catch (e) {
    showToast('Save failed: ' + e.message, 'error');
  }
}

function resetVoiceEditor() {
  document.getElementById('note-editor').style.display = 'none';
  document.getElementById('note-title').value = '';
  document.getElementById('original-transcript').textContent = '';
  document.getElementById('edited-note').value = '';
  document.getElementById('record-status').textContent = '';
  const playback = document.getElementById('audio-playback');
  if (playback) { playback.src = ''; playback.style.display = 'none'; }
  const transcribeBtn = document.getElementById('transcribe-btn');
  if (transcribeBtn) transcribeBtn.style.display = 'none';
  currentTranscript = '';
  refineResult = null;
  recordedBlob = null;
  lastTranscribedItemId = null;
}

async function loadSavedNotes() {
  const list = document.getElementById('notes-list');
  list.innerHTML = renderSkeletons(3);
  try {
    const data = await api('/api/items?source=voice&limit=20');
    list.innerHTML = '';
    if (data.items.length === 0) {
      list.innerHTML = '<div class="empty-state">No voice notes yet. Hit Record to start.</div>';
      return;
    }
    data.items.forEach(item => list.appendChild(renderCard(item)));
  } catch (e) {
    list.innerHTML = '';
    showToast('Failed to load notes: ' + e.message, 'error');
  }
}

/* ── Process Dialog ── */
let processItemId = null;
const DEFAULT_PROCESS_PROMPT = 'Clean up this voice note transcript. Fix grammar, remove filler words (um, uh, like, you know), improve punctuation, and organize into clear paragraphs. Maintain the original meaning and tone. Return only the cleaned text, no commentary.';

function openProcessDialog(itemId) {
  processItemId = itemId;
  // Show preview text from editor (null) or from the card's content
  let previewText = '';
  if (itemId === null) {
    previewText = document.getElementById('original-transcript').textContent ||
                  document.getElementById('edited-note').value;
  } else {
    const card = document.querySelector(`.item-detail-content`);
    previewText = card ? card.textContent : '(loading...)';
  }
  document.getElementById('process-text-preview').textContent = previewText;
  document.getElementById('process-system-prompt').value = settings.llmPrompt || DEFAULT_PROCESS_PROMPT;
  document.getElementById('process-user-prompt').value = '';
  if (settings.llmModel) {
    document.getElementById('process-model').value = settings.llmModel;
  }
  document.getElementById('process-modal').classList.add('open');
}

function closeProcessDialog() {
  document.getElementById('process-modal').classList.remove('open');
  processItemId = null;
}

async function runProcessDialog() {
  const text = document.getElementById('process-text-preview').textContent;
  const systemPrompt = document.getElementById('process-system-prompt').value.trim();
  const userPrompt = document.getElementById('process-user-prompt').value.trim();
  const model = document.getElementById('process-model').value;
  if (!text) { showToast('No text to process', 'error'); return; }

  const btn = document.getElementById('process-run-btn');
  btn.disabled = true;
  btn.textContent = 'Processing...';

  try {
    const prompt = userPrompt
      ? systemPrompt + '\n\nAdditional instructions: ' + userPrompt
      : systemPrompt;
    const data = await api('/api/voice/refine', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ text, prompt, model }),
    });
    refineResult = data;
    // Update the editor with refined text
    document.getElementById('edited-note').value = data.refined_text;
    showToast('Text refined', 'ok');
    closeProcessDialog();
  } catch (e) {
    showToast('Processing failed: ' + e.message, 'error');
  } finally {
    btn.disabled = false;
    btn.textContent = 'Process';
  }
}

/* ── Shared attach-to-existing item picker ──
   Resolves elements by convention: {prefix}-search, {prefix}-results,
   {prefix}-selected, {prefix}-mode, {prefix}-attach-section,
   optional {prefix}-new-section. Used by capture-modal,
   image-capture-modal, multi-image-capture-modal, and #113. */
const _attachPickerState = {};

function _pickerState(prefix) {
  return _attachPickerState[prefix] || (_attachPickerState[prefix] = {});
}

function attachPickerReset(prefix) {
  const state = _pickerState(prefix);
  if (state.timer) { clearTimeout(state.timer); state.timer = null; }
  state.selectedId = null;
  const search = document.getElementById(`${prefix}-search`);
  if (search) search.value = '';
  const results = document.getElementById(`${prefix}-results`);
  if (results) { results.style.display = 'none'; results.innerHTML = ''; }
  const selected = document.getElementById(`${prefix}-selected`);
  if (selected) selected.style.display = 'none';
}

function attachPickerSearch(prefix, query) {
  const state = _pickerState(prefix);
  if (state.timer) clearTimeout(state.timer);
  const results = document.getElementById(`${prefix}-results`);
  if (!query || query.length < 2) { results.style.display = 'none'; return; }
  state.timer = setTimeout(async () => {
    try {
      const data = await api(`/api/items/search?q=${encodeURIComponent(query)}&limit=10`);
      const items = data.items || data;
      if (!items.length) {
        results.innerHTML = '<div style="padding:0.4rem 0.6rem;font-size:0.8rem;color:var(--muted)">No items found</div>';
      } else {
        results.innerHTML = items.map(it => {
          const date = it.created_at ? new Date(it.created_at).toLocaleDateString() : '';
          const title = it.title || it.content?.substring(0, 60) || 'Untitled';
          return `<div class="img-attach-result" data-id="${it.id}" data-title="${escHtml(title)}" onclick="attachPickerSelect('${prefix}', ${it.id})">
            <div>${escHtml(title)}</div>
            <div class="result-meta">${escHtml(it.source_type)}${date ? ' · ' + date : ''}</div>
          </div>`;
        }).join('');
      }
      results.style.display = 'block';
    } catch (e) {
      results.innerHTML = '<div style="padding:0.4rem 0.6rem;font-size:0.8rem;color:var(--muted)">Search failed</div>';
      results.style.display = 'block';
    }
  }, 300);
}

function attachPickerSelect(prefix, itemId) {
  _pickerState(prefix).selectedId = itemId;
  const results = document.getElementById(`${prefix}-results`);
  results.querySelectorAll('.img-attach-result').forEach(r => r.classList.remove('selected'));
  const row = results.querySelector(`[data-id="${itemId}"]`);
  if (row) row.classList.add('selected');
  const selectedLabel = document.getElementById(`${prefix}-selected`);
  selectedLabel.textContent = 'Attaching to: ' + (row?.dataset.title || 'Item #' + itemId);
  selectedLabel.style.display = 'block';
}

function attachPickerGetId(prefix) {
  return _pickerState(prefix).selectedId ?? null;
}

function toggleAttachMode(prefix, hasNewSection) {
  const mode = document.querySelector(`input[name="${prefix}-mode"]:checked`).value;
  const attachSection = document.getElementById(`${prefix}-attach-section`);
  if (attachSection) attachSection.style.display = mode === 'attach' ? 'block' : 'none';
  if (hasNewSection) {
    const newSection = document.getElementById(`${prefix}-new-section`);
    if (newSection) newSection.style.display = mode === 'new' ? 'block' : 'none';
  }
}

/* ── Capture Dialog (text/URL share) ── */
function _isYouTubeUrl(url) {
  return /youtube\.com\/watch|youtu\.be\//.test(url || '');
}

function openCaptureDialog(share) {
  const text = share.text || '';
  const urlMatch = text.match(/https?:\/\/[^\s]+/);
  const url = urlMatch ? urlMatch[0] : '';
  document.getElementById('capture-url').value = url;
  document.getElementById('capture-title').value = share.subject || '';
  document.getElementById('capture-notes').value = '';
  document.getElementById('capture-content').value = text;
  document.getElementById('capture-transcript-option').style.display = _isYouTubeUrl(url) ? 'block' : 'none';
  document.getElementById('capture-fetch-transcript').checked = true;
  document.getElementById('capture-drive-option').style.display = _isDriveOrDocsUrl(url) ? 'block' : 'none';
  document.getElementById('capture-drive-import').checked = true;
  document.querySelector('input[name="capture-mode"][value="new"]').checked = true;
  toggleAttachMode('capture');
  attachPickerReset('capture');
  document.getElementById('capture-modal').classList.add('open');
}

function closeCaptureDialog() {
  document.getElementById('capture-modal').classList.remove('open');
  attachPickerReset('capture');
}

async function saveCaptureItem() {
  const url = document.getElementById('capture-url').value.trim();
  const title = document.getElementById('capture-title').value.trim();
  const notes = document.getElementById('capture-notes').value.trim();
  const content = document.getElementById('capture-content').value.trim();
  if (!url && !content) { showToast('Nothing to save', 'error'); return; }
  const mode = document.querySelector('input[name="capture-mode"]:checked').value;
  const parentId = mode === 'attach' ? attachPickerGetId('capture') : null;
  if (mode === 'attach' && !parentId) {
    showToast('Select an item to attach to', 'error');
    return;
  }
  const btn = document.getElementById('capture-save-btn');
  btn.disabled = true;
  btn.textContent = 'Saving...';
  try {
    // A shared YouTube link is a YouTube video, not a generic capture:
    // mint a canonical youtube item so it enriches + dedupes and any
    // transcript nests under it. Attach mode keeps the generic share so it
    // can be a child of another item.
    const mintYouTube = mode === 'new' && _isYouTubeUrl(url);
    let savedYouTube = false;
    if (mintYouTube) {
      btn.textContent = 'Importing video...';
      try {
        await api('/api/youtube/import-video', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ url, note: notes || null }),
        });
        savedYouTube = true;
      } catch (e) {
        // Fall back to a generic capture so the share is never lost.
      }
    }
    if (!savedYouTube) {
      await api('/api/items', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          source_type: 'share',
          source_id: 'share-' + Date.now(),
          url: url || null,
          title: title || null,
          content: notes ? notes + '\n\n---\n\n' + content : content,
          is_own_content: false,
          parent_id: parentId,
          metadata: notes ? { notes, shared_text: content } : { shared_text: content },
        }),
      });
    }
    const importDrive = _isDriveOrDocsUrl(url)
      && document.getElementById('capture-drive-import').checked;
    const fetchTranscript = _isYouTubeUrl(url)
      && document.getElementById('capture-fetch-transcript').checked;
    if (importDrive) {
      btn.textContent = 'Importing from Drive...';
      try {
        const result = await api('/api/import/drive/sync', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ urls: [url] }),
        });
        showToast(`Saved + imported ${result.items_added} items`, 'ok');
      } catch (e) {
        showToast('Saved (Drive import failed)', 'ok');
      }
    } else if (fetchTranscript) {
      btn.textContent = 'Fetching transcript...';
      try {
        const result = await api('/api/youtube/fetch-transcript', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ url }),
        });
        showToast(`Saved + transcript (${result.word_count} words)`, 'ok');
      } catch (e) {
        showToast('Saved (transcript unavailable)', 'ok');
      }
    } else {
      showToast(savedYouTube ? 'Saved video' : 'Saved', 'ok');
    }
    closeCaptureDialog();
    refreshTimeline();
    loadRecent();
  } catch (e) {
    showToast('Save failed: ' + e.message, 'error');
  } finally {
    btn.disabled = false;
    btn.textContent = 'Save';
  }
}

/* ── Link Existing Item Dialog ── */
let _linkParentId = null;

function openLinkDialog(parentItemId) {
  _linkParentId = parentItemId;
  attachPickerReset('link');
  const btn = document.getElementById('link-save-btn');
  btn.disabled = false;
  btn.textContent = 'Link';
  document.getElementById('link-modal').classList.add('open');
  setTimeout(() => document.getElementById('link-search').focus(), 50);
}

function closeLinkDialog() {
  document.getElementById('link-modal').classList.remove('open');
  attachPickerReset('link');
  _linkParentId = null;
}

async function saveLink() {
  const childId = attachPickerGetId('link');
  if (!childId) { showToast('Select an item to link', 'error'); return; }
  if (!_linkParentId) { showToast('No parent item', 'error'); return; }
  if (childId === _linkParentId) { showToast('Cannot link item to itself', 'error'); return; }
  const parentId = _linkParentId;  // closeLinkDialog clears it
  const btn = document.getElementById('link-save-btn');
  btn.disabled = true;
  btn.textContent = 'Linking...';
  try {
    await api(`/api/items/${childId}`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ parent_id: parentId }),
    });
    const linked = await api(`/api/items/${childId}`);
    showToast('Item linked', 'ok');
    closeLinkDialog();
    // Incrementally update the current detail view (matches save-note pattern)
    const container = document.getElementById('item-view-content');
    const list = container?.querySelector('#children-list');
    if (list) {
      const placeholder = list.querySelector(':scope > .parts-placeholder');
      if (placeholder) list.innerHTML = '';
      list.appendChild(renderCard(linked));
      const shown = cachedItem(parentId);
      const titleEl = container.querySelector('#parts-title');
      if (shown && titleEl) {
        shown.child_count = (shown.child_count || 0) + 1;
        titleEl.textContent = partsTitle(shown);
      }
    }
  } catch (e) {
    showToast('Link failed: ' + e.message, 'error');
    btn.disabled = false;
    btn.textContent = 'Link';
  }
}

/* ── HTML Import Dialog ─��� */
let _pendingHtmlFile = null; // File object for web upload, or {base64, fileName} for Android

function _detectHtmlType(html) {
  if (html.includes('user-query') && html.includes('model-response')) return 'gemini';
  if (html.includes('data-message-author-role')) return 'chatgpt';
  if (html.includes('<article')) return 'article';
  return 'unknown';
}

function _htmlTypeLabel(type) {
  return { gemini: 'Gemini conversation', chatgpt: 'ChatGPT conversation', article: 'Article', unknown: 'Unknown' }[type] || type;
}

function openHtmlImportDialog(fileOrData, htmlText) {
  _pendingHtmlFile = fileOrData;
  const detected = _detectHtmlType(htmlText);
  const preview = document.getElementById('html-import-preview');
  const snippet = htmlText.replace(/<[^>]+>/g, ' ').replace(/\s+/g, ' ').trim().slice(0, 200);
  preview.textContent = `Detected: ${_htmlTypeLabel(detected)} — ${snippet}...`;
  document.getElementById('html-import-type').value = detected === 'unknown' ? 'auto' : detected;
  document.getElementById('html-import-url').value = '';
  document.getElementById('html-import-notes').value = '';
  document.getElementById('html-import-modal').classList.add('open');
}

function closeHtmlImportDialog() {
  document.getElementById('html-import-modal').classList.remove('open');
  _pendingHtmlFile = null;
}

async function saveHtmlImport() {
  const btn = document.getElementById('html-import-save-btn');
  btn.disabled = true;
  btn.textContent = 'Importing...';
  try {
    const formData = new FormData();
    if (_pendingHtmlFile instanceof File) {
      formData.append('file', _pendingHtmlFile);
    } else if (_pendingHtmlFile && _pendingHtmlFile.base64) {
      const byteStr = atob(_pendingHtmlFile.base64);
      const bytes = new Uint8Array(byteStr.length);
      for (let i = 0; i < byteStr.length; i++) bytes[i] = byteStr.charCodeAt(i);
      const file = new File([bytes], _pendingHtmlFile.fileName || 'shared_page.html', { type: 'text/html' });
      formData.append('file', file);
    }
    formData.append('page_type', document.getElementById('html-import-type').value);
    const sourceUrl = document.getElementById('html-import-url').value.trim();
    if (sourceUrl) formData.append('source_url', sourceUrl);
    const notes = document.getElementById('html-import-notes').value.trim();
    if (notes) formData.append('notes', notes);

    const res = await fetch(`${getApiBase()}/api/import`, { method: 'POST', body: formData });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || res.status);
    showToast(`Imported ${data.items_added} items from ${data.source_type}`, 'ok');
    closeHtmlImportDialog();
    refreshTimeline();
    loadRecent();
  } catch (e) {
    showToast('Import failed: ' + e.message, 'error');
  } finally {
    btn.disabled = false;
    btn.textContent = 'Import';
  }
}

/* ── Image Capture Dialog ── */
let _pendingImageFile = null;
let _pendingImageUrl = null;

function openImageCaptureDialog(file) {
  _pendingImageFile = file;
  if (_pendingImageUrl) URL.revokeObjectURL(_pendingImageUrl);
  _pendingImageUrl = URL.createObjectURL(file);
  document.getElementById('image-capture-img').src = _pendingImageUrl;
  document.getElementById('image-capture-title').value = file.name.replace(/\.\w+$/, '').replace(/[_-]/g, ' ');
  document.getElementById('image-capture-notes').value = '';
  document.querySelector('input[name="image-capture-mode"][value="new"]').checked = true;
  toggleAttachMode('image-capture');
  attachPickerReset('image-capture');
  document.getElementById('image-capture-save-btn').disabled = false;
  document.getElementById('image-capture-save-btn').textContent = 'Save';
  document.getElementById('image-capture-modal').classList.add('open');
}

function closeImageCaptureDialog() {
  document.getElementById('image-capture-modal').classList.remove('open');
  attachPickerReset('image-capture');
  if (_pendingImageUrl) { URL.revokeObjectURL(_pendingImageUrl); _pendingImageUrl = null; }
  _pendingImageFile = null;
}

async function saveImageCapture() {
  if (!_pendingImageFile) { showToast('No image to save', 'error'); return; }
  const mode = document.querySelector('input[name="image-capture-mode"]:checked').value;
  const attachId = mode === 'attach' ? attachPickerGetId('image-capture') : null;
  if (mode === 'attach' && !attachId) { showToast('Select an item to attach to', 'error'); return; }
  const title = document.getElementById('image-capture-title').value.trim();
  const notes = document.getElementById('image-capture-notes').value.trim();
  const btn = document.getElementById('image-capture-save-btn');
  btn.disabled = true;
  btn.textContent = 'Saving...';

  try {
    let targetItemId;
    if (mode === 'new') {
      const item = await api('/api/items', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          source_type: 'image',
          source_id: 'image-' + Date.now(),
          title: title || _pendingImageFile.name,
          content: notes || '[Image]',
          is_own_content: false,
          metadata: { original_filename: _pendingImageFile.name, mime_type: _pendingImageFile.type },
        }),
      });
      targetItemId = item.id;
    } else {
      targetItemId = attachId;
    }

    btn.textContent = 'Uploading...';
    const formData = new FormData();
    formData.append('file', _pendingImageFile);
    const mediaRes = await fetch(`${getApiBase()}/api/items/${targetItemId}/media`, {
      method: 'POST',
      body: formData,
    });
    if (!mediaRes.ok) {
      const err = await mediaRes.json().catch(() => ({}));
      throw new Error(err.detail || mediaRes.status);
    }

    showToast(mode === 'new' ? 'Image saved' : 'Image attached', 'ok');
    closeImageCaptureDialog();
    refreshTimeline();
    loadRecent();
  } catch (e) {
    showToast('Save failed: ' + e.message, 'error');
  } finally {
    btn.disabled = false;
    btn.textContent = 'Save';
  }
}

/* ── Multi-Image Capture Dialog ── */
let _pendingMultiImageFiles = null;
let _pendingMultiImageUrls = [];

function openMultiImageCaptureDialog(files) {
  _pendingMultiImageFiles = files;
  _pendingMultiImageUrls.forEach(u => URL.revokeObjectURL(u));
  _pendingMultiImageUrls = files.map(f => URL.createObjectURL(f));

  const grid = document.getElementById('multi-image-grid');
  grid.innerHTML = _pendingMultiImageUrls.map((url, i) =>
    `<img src="${url}" alt="Image ${i + 1}" style="width:100%;aspect-ratio:1;object-fit:cover;border-radius:4px;border:1px solid var(--border)">`
  ).join('');
  document.getElementById('multi-image-count').textContent = `${files.length} image${files.length === 1 ? '' : 's'} selected`;
  document.getElementById('multi-image-title').value = '';
  document.getElementById('multi-image-notes').value = '';
  document.querySelector('input[name="multi-image-mode"][value="new"]').checked = true;
  toggleAttachMode('multi-image', true);
  attachPickerReset('multi-image');
  document.getElementById('multi-image-save-btn').disabled = false;
  document.getElementById('multi-image-save-btn').textContent = 'Save';
  document.getElementById('multi-image-capture-modal').classList.add('open');
}

function closeMultiImageCaptureDialog() {
  document.getElementById('multi-image-capture-modal').classList.remove('open');
  attachPickerReset('multi-image');
  _pendingMultiImageUrls.forEach(u => URL.revokeObjectURL(u));
  _pendingMultiImageUrls = [];
  _pendingMultiImageFiles = null;
}

async function saveMultiImageCapture() {
  if (!_pendingMultiImageFiles || !_pendingMultiImageFiles.length) { showToast('No images to save', 'error'); return; }
  const mode = document.querySelector('input[name="multi-image-mode"]:checked').value;
  const attachId = mode === 'attach' ? attachPickerGetId('multi-image') : null;
  if (mode === 'attach' && !attachId) { showToast('Select an item to attach to', 'error'); return; }
  const title = mode === 'new' ? document.getElementById('multi-image-title').value.trim() : '';
  const notes = mode === 'new' ? document.getElementById('multi-image-notes').value.trim() : '';
  if (mode === 'new' && !title && !notes) { showToast('Add a title or note', 'error'); return; }

  const btn = document.getElementById('multi-image-save-btn');
  btn.disabled = true;
  btn.textContent = 'Saving...';

  try {
    let parentId;
    if (mode === 'new') {
      const parent = await api('/api/items', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          source_type: 'note',
          source_id: 'note-' + Date.now(),
          title: title || null,
          content: notes || title || '',
          is_own_content: true,
          metadata: { image_count: _pendingMultiImageFiles.length },
        }),
      });
      parentId = parent.id;
    } else {
      parentId = attachId;
    }

    const total = _pendingMultiImageFiles.length;
    let done = 0;
    btn.textContent = `Uploading 0/${total}...`;
    const batchId = Date.now();
    await Promise.all(_pendingMultiImageFiles.map(async (file, i) => {
      const child = await api('/api/items', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          source_type: 'image',
          source_id: `image-${batchId}-${i}`,
          title: file.name,
          content: '[Image]',
          is_own_content: false,
          parent_id: parentId,
          metadata: { original_filename: file.name, mime_type: file.type },
        }),
      });
      const formData = new FormData();
      formData.append('file', file);
      const mediaRes = await fetch(`${getApiBase()}/api/items/${child.id}/media`, {
        method: 'POST',
        body: formData,
      });
      if (!mediaRes.ok) {
        const err = await mediaRes.json().catch(() => ({}));
        throw new Error(`Image ${i + 1}: ${err.detail || mediaRes.status}`);
      }
      done++;
      btn.textContent = `Uploading ${done}/${total}...`;
    }));

    showToast(`Saved ${total} images`, 'ok');
    closeMultiImageCaptureDialog();
    refreshTimeline();
    loadRecent();
  } catch (e) {
    showToast('Save failed: ' + e.message, 'error');
  } finally {
    btn.disabled = false;
    btn.textContent = 'Save';
  }
}

/* ── Share Intent Handling (Android) ── */
async function handlePendingShare() {
  if (!IS_TAURI || !tauriInvoke) return;
  try {
    const result = await tauriInvoke('check_pending_share');
    addDebugLog('SHARE', 'check_pending_share', result.share ? 'FOUND' : 'NONE', null,
      `checked: ${result.checkedPaths.join(', ')}` + (result.foundAt ? ` | found at: ${result.foundAt}` : ''));

    const share = result.share;
    if (!share) return;

    if (share.mimeType && share.mimeType.startsWith('audio/') && share.filePath) {
      // Read the shared file via Rust, then upload via fetch (which uses
      // Android's native network stack and works through Tailscale VPN)
      const fileName = share.fileName || 'shared_audio.m4a';
      addDebugLog('SHARE', 'read_shared_file', 'START', null, `file: ${share.filePath}`);
      const fileData = await tauriInvoke('read_shared_file', {
        filePath: share.filePath,
        fileName: fileName,
      });
      const byteStr = atob(fileData.base64);
      const bytes = new Uint8Array(byteStr.length);
      for (let i = 0; i < byteStr.length; i++) bytes[i] = byteStr.charCodeAt(i);
      const file = new File([bytes], fileData.fileName, { type: fileData.mimeType });
      addDebugLog('SHARE', 'read_shared_file', 'OK', null, `${file.name} (${(file.size / 1024).toFixed(0)} KB)`);

      // Switch to Notes tab and show transcription UI
      navigate('/notes');
      const uploadBtn = document.getElementById('upload-btn');
      const recordStatus = document.getElementById('record-status');
      if (uploadBtn) { uploadBtn.style.pointerEvents = 'none'; uploadBtn.style.opacity = '0.5'; uploadBtn.innerText = 'Transcribing...'; }
      if (recordStatus) recordStatus.textContent = `Uploading ${file.name}...`;
      try {
        const data = await transcribeAudioFile(file);
        if (data) {
          if (recordStatus) recordStatus.textContent = `${data.text.split(' ').length} words transcribed`;
          showToast(`Saved: ${data.title}`, 'ok');
          loadSavedNotes();
          refreshTimeline();
          loadRecent();
        }
      } finally {
        if (uploadBtn) { uploadBtn.style.pointerEvents = ''; uploadBtn.style.opacity = ''; uploadBtn.innerText = 'Upload File'; }
      }
    } else if (share.mimeType && share.mimeType.startsWith('image/') && share.filePaths && share.filePaths.length) {
      addDebugLog('SHARE', 'read_shared_files', 'START', null, `count: ${share.filePaths.length}`);
      const settled = await Promise.allSettled(share.filePaths.map(async (filePath, i) => {
        const fileName = (share.fileNames && share.fileNames[i]) || `shared_image_${i}.jpg`;
        const fileData = await tauriInvoke('read_shared_file', { filePath, fileName });
        const byteStr = atob(fileData.base64);
        const bytes = new Uint8Array(byteStr.length);
        for (let j = 0; j < byteStr.length; j++) bytes[j] = byteStr.charCodeAt(j);
        return new File([bytes], fileData.fileName, { type: fileData.mimeType });
      }));
      const files = [];
      settled.forEach((r, i) => {
        if (r.status === 'fulfilled') files.push(r.value);
        else addDebugLog('SHARE', 'read_shared_file', 'ERR', null, `idx ${i}: ${r.reason}`);
      });
      if (!files.length) { showToast('Failed to read shared images', 'error'); return; }
      addDebugLog('SHARE', 'read_shared_files', 'OK', null, `loaded ${files.length}/${share.filePaths.length}`);
      openMultiImageCaptureDialog(files);
    } else if (share.mimeType && share.mimeType.startsWith('image/') && share.filePath) {
      // Single image shared — read and open image capture dialog
      const fileName = share.fileName || 'shared_image.jpg';
      addDebugLog('SHARE', 'read_shared_file', 'START', null, `image: ${share.filePath}`);
      const fileData = await tauriInvoke('read_shared_file', {
        filePath: share.filePath,
        fileName: fileName,
      });
      const byteStr = atob(fileData.base64);
      const bytes = new Uint8Array(byteStr.length);
      for (let i = 0; i < byteStr.length; i++) bytes[i] = byteStr.charCodeAt(i);
      const file = new File([bytes], fileData.fileName, { type: fileData.mimeType });
      addDebugLog('SHARE', 'read_shared_file', 'OK', null, `${file.name} (${(file.size / 1024).toFixed(0)} KB)`);
      openImageCaptureDialog(file);
    } else if (share.mimeType === 'text/html' && share.filePath) {
      // HTML file shared — read and open import dialog
      const fileName = share.fileName || 'shared_page.html';
      addDebugLog('SHARE', 'read_shared_file', 'START', null, `html: ${share.filePath}`);
      const fileData = await tauriInvoke('read_shared_file', {
        filePath: share.filePath,
        fileName: fileName,
      });
      addDebugLog('SHARE', 'read_shared_file', 'OK', null, `${fileName} (${(fileData.base64.length * 0.75 / 1024).toFixed(0)} KB)`);
      const htmlText = atob(fileData.base64);
      openHtmlImportDialog(fileData, htmlText);
    } else if (share.mimeType === 'application/pdf' && share.filePath) {
      const fileName = share.fileName || 'shared.pdf';
      addDebugLog('SHARE', 'read_shared_file', 'START', null, `pdf: ${share.filePath}`);
      const fileData = await tauriInvoke('read_shared_file', {
        filePath: share.filePath,
        fileName: fileName,
      });
      const byteStr = atob(fileData.base64);
      const bytes = new Uint8Array(byteStr.length);
      for (let i = 0; i < byteStr.length; i++) bytes[i] = byteStr.charCodeAt(i);
      const file = new File([bytes], fileData.fileName, { type: 'application/pdf' });
      addDebugLog('SHARE', 'read_shared_file', 'OK', null, `${file.name} (${(file.size / 1024).toFixed(0)} KB)`);
      navigate('/settings/sources');
      await uploadImport(file);
    } else if (share.text) {
      openCaptureDialog(share);
    }
  } catch (e) {
    addDebugLog('SHARE', 'handlePendingShare', 'ERR', null, String(e));
    showToast('Share failed: ' + e, 'error');
    const btn = document.getElementById('upload-btn');
    if (btn) { btn.style.pointerEvents = ''; btn.style.opacity = ''; btn.innerText = 'Upload File'; }
  }
}

if (IS_TAURI && tauriInvoke) {
  routerReady.then(() => setTimeout(handlePendingShare, 500));
  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'visible') handlePendingShare();
  });
}

/* ── File Transcription ── */
const SUPPORTED_AUDIO = new Set(['.m4a', '.mp3', '.wav', '.ogg', '.flac', '.webm']);
const audioFileInput = document.getElementById('audio-file-input');

audioFileInput.addEventListener('change', async () => {
  if (audioFileInput.files.length) {
    const btn = document.getElementById('upload-btn');
    const status = document.getElementById('record-status');
    btn.style.pointerEvents = 'none';
    btn.style.opacity = '0.5';
    btn.innerText = 'Transcribing...';
    status.textContent = `Uploading ${audioFileInput.files[0].name}...`;
    try {
      const data = await transcribeAudioFile(audioFileInput.files[0]);
      if (data) {
        status.textContent = `${data.text.split(' ').length} words transcribed`;
        showToast(`Saved: ${data.title}`, 'ok');
        loadSavedNotes();
      }
    } catch (e) {
      showToast('Transcription failed: ' + e.message, 'error');
      status.textContent = '';
    } finally {
      btn.style.pointerEvents = '';
      btn.style.opacity = '';
      btn.innerText = 'Upload File';
    }
  }
  audioFileInput.value = '';
});

async function transcribeAudioFile(file) {
  const ext = '.' + file.name.split('.').pop().toLowerCase();
  if (!SUPPORTED_AUDIO.has(ext)) {
    showToast(`Unsupported format: ${ext}. Use m4a, mp3, wav, ogg, flac, or webm.`, 'error');
    return null;
  }
  if (file.size > 50 * 1024 * 1024) {
    showToast('File too large (max 50MB)', 'error');
    return null;
  }

  const formData = new FormData();
  formData.append('file', file);
  formData.append('title', file.name.replace(/\.[^.]+$/, ''));
  return await api('/api/voice/transcribe', {
    method: 'POST',
    body: formData,
  });
}

/* ── Import ── */
const dropzone = document.getElementById('import-dropzone');
const importFile = document.getElementById('import-file');

dropzone.addEventListener('dragover', e => {
  e.preventDefault();
  dropzone.classList.add('dragover');
});
dropzone.addEventListener('dragleave', () => {
  dropzone.classList.remove('dragover');
});
dropzone.addEventListener('drop', e => {
  e.preventDefault();
  dropzone.classList.remove('dragover');
  if (e.dataTransfer.files.length) uploadImport(e.dataTransfer.files[0]);
});
importFile.addEventListener('change', () => {
  if (importFile.files.length) uploadImport(importFile.files[0]);
  importFile.value = '';
});

async function uploadImport(file) {
  // HTML files get the type picker dialog
  if (file.name.match(/\.html?$/i)) {
    const htmlText = await file.text();
    openHtmlImportDialog(file, htmlText);
    return;
  }

  const progress = document.getElementById('import-progress');
  const result = document.getElementById('import-result');
  progress.style.display = '';
  result.style.display = 'none';

  try {
    const formData = new FormData();
    formData.append('file', file);
    const res = await fetch(`${getApiBase()}/api/import`, {
      method: 'POST',
      body: formData,
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || res.status);

    result.style.display = '';
    let html = `Imported <strong>${data.items_added}</strong> items`;
    html += ` from <strong>${data.source_type}</strong>`;
    if (data.errors.length) {
      html += `<br><span style="color:var(--error)">${data.errors.length} errors</span>`;
    }
    result.innerHTML = html;
    showToast(`Imported ${data.items_added} items`, 'ok');
  } catch (e) {
    result.style.display = '';
    result.innerHTML = `<span style="color:var(--error)">Import failed: ${escHtml(e.message)}</span>`;
    showToast(`Import failed: ${e.message}`, 'error');
  } finally {
    progress.style.display = 'none';
  }
}

/* ── Stats ── */
const SOURCE_COLORS = {
  linkedin: 'var(--src-linkedin)',
  bluesky: 'var(--src-bluesky)',
  youtube: 'var(--src-youtube)',
  arxiv: 'var(--src-arxiv)',
  microblog: 'var(--src-microblog)',
};

async function loadStats() {
  try {
    const data = await api('/api/stats');
    const cards = document.getElementById('stats-cards');
    cards.innerHTML = `
      <div class="stat-card">
        <div class="stat-value">${data.total_items.toLocaleString()}</div>
        <div class="stat-label">Total items</div>
      </div>
      <div class="stat-card">
        <div class="stat-value">${data.own_content.toLocaleString()}</div>
        <div class="stat-label">Own content</div>
      </div>
      <div class="stat-card">
        <div class="stat-value">${Object.keys(data.sources).length}</div>
        <div class="stat-label">Sources active</div>
      </div>
      <div class="stat-card">
        <div class="stat-value">${data.date_range.earliest ? new Date(data.date_range.earliest).toLocaleDateString() : '-'}</div>
        <div class="stat-label">Earliest item</div>
      </div>
    `;

    const chart = document.getElementById('stats-chart');
    const entries = Object.entries(data.sources);
    if (entries.length === 0) {
      chart.innerHTML = '<div class="empty-state">No items to chart</div>';
      return;
    }
    const max = Math.max(...entries.map(([,v]) => v));
    chart.innerHTML = entries.map(([name, count]) => {
      const pct = max > 0 ? (count / max * 100) : 0;
      const color = SOURCE_COLORS[name] || 'var(--muted)';
      return `
        <div class="bar-row">
          <span class="bar-label">${escHtml(name)}</span>
          <div class="bar-track">
            <div class="bar-fill" style="width:${pct}%;background:${color}">${count}</div>
          </div>
        </div>
      `;
    }).join('');
  } catch (e) {
    showToast(`Failed to load stats: ${e.message}`, 'error');
  }
}

/* ── Populate source filter ── */


/* ── Skeleton loading ── */
function renderSkeletons(count) {
  let html = '';
  for (let i = 0; i < count; i++) {
    html += `<div class="skeleton-card">
      <div class="skeleton-line short"></div>
      <div class="skeleton-line medium"></div>
      <div class="skeleton-line short"></div>
    </div>`;
  }
  return html;
}

/* ── Health check ── */
let wasDisconnected = false;


async function checkHealth() {
  const dot = document.getElementById('dot');
  const banner = document.getElementById('disconnect-banner');
  try {
    await api('/api/health');
    dot.className = 'dot ok';
    dot.title = 'Connected';
    banner.classList.remove('visible');
    if (wasDisconnected) {
      showToast('Reconnected to server', 'ok');
      wasDisconnected = false;
    }
  } catch {
    dot.className = 'dot error';
    dot.title = 'Disconnected';
    banner.classList.add('visible');
    wasDisconnected = true;
  }
}

/* ── Keyboard shortcuts ── */
document.addEventListener('keydown', e => {
  const el = document.activeElement;
  const typing = el && (['INPUT', 'TEXTAREA', 'SELECT'].includes(el.tagName) || el.isContentEditable);
  const inDialog = document.querySelector('.modal-overlay.open');
  if (e.key === '/' && !e.ctrlKey && !e.metaKey && !typing && !inDialog) {
    e.preventDefault();
    switchTab('search');
  }
  if (e.key === 'Escape') {
    closeSettings();
  }
});

/* ── Collections ── */
async function loadCollections() {
  const list = document.getElementById('collections-list');
  if (!list) return;
  try {
    const colls = await api('/api/collections');
    if (!colls.length) {
      list.innerHTML = '<div class="empty-state" style="padding:1rem;text-align:center;color:var(--muted)">No collections yet. Create one to group related items across sources.</div>';
      return;
    }
    list.innerHTML = '';
    colls.forEach(c => {
      const card = document.createElement('a');
      card.className = 'item-card';
      card.href = `/collections/${c.id}`;
      card.innerHTML = `
        <div class="item-card-header">
          <span class="item-title">${escHtml(c.name)}</span>
          <span class="item-date">${relTime(c.created_at)}</span>
        </div>
        ${c.description ? `<div class="item-preview">${escHtml(c.description)}</div>` : ''}
        <div class="item-footer">
          <span>${c.item_count} item${c.item_count !== 1 ? 's' : ''}</span>
        </div>
      `;
      list.appendChild(card);
    });
  } catch (e) {
    list.innerHTML = `<div class="empty-state" style="color:var(--error)">${escHtml(e.message)}</div>`;
  }
}

route(/^\/collections$/, () => showTab('collections'));
route(/^\/collections\/(\d+)$/, async ([, id]) => {
  showOnly('item-view', 'collections');
  failedPath = null;
  const container = document.getElementById('item-view-content');
  container.innerHTML = '<div class="empty-state"><div class="spinner"></div></div>';
  try {
    const detail = await api(`/api/collections/${id}`);
    if (location.pathname !== `/collections/${id}`) return;  // the user moved on meanwhile
    let html = `<div class="item-view-title">${escHtml(detail.name)}</div>`;
    if (detail.description) html += `<div class="item-view-meta">${escHtml(detail.description)}</div>`;
    html += `<div class="item-view-children-title">${detail.items.length} items</div>`;
    html += '<div class="item-list" id="collection-items-list"></div>';
    container.innerHTML = html;

    const itemList = document.getElementById('collection-items-list');
    detail.items.forEach(item => {
      const card = renderCard(item);
      itemList.appendChild(card);
    });
  } catch (e) {
    if (location.pathname !== `/collections/${id}`) return;
    container.innerHTML = `<div class="empty-state" style="padding:2rem">Collection not found: ${escHtml(e.message)}</div>`;
    failedPath = `/collections/${id}`;
  }
});

route(/^\/(recent|notes)$/, ([, tab]) => showTab(tab));
route(/^\/settings\/(sources|stats|debug)$/, ([, tab]) => showTab(tab));

function openNewCollectionDialog() {
  document.getElementById('coll-name').value = '';
  document.getElementById('coll-desc').value = '';
  document.getElementById('new-collection-modal').classList.add('open');
  document.getElementById('coll-name').focus();
}

async function saveNewCollection() {
  const name = document.getElementById('coll-name').value.trim();
  if (!name) { showToast('Name required', 'error'); return; }
  try {
    await api('/api/collections', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        name,
        description: document.getElementById('coll-desc').value.trim() || null,
      }),
    });
    document.getElementById('new-collection-modal').classList.remove('open');
    showToast('Collection created', 'ok');
    loadCollections();
  } catch (e) {
    showToast('Failed: ' + e.message, 'error');
  }
}

/* ── LinkedIn Auth + Compose ── */

async function loadLinkedInAuth() {
  const section = document.getElementById('linkedin-post-section');
  const statusEl = document.getElementById('linkedin-auth-status');
  try {
    const data = await api('/api/linkedin/auth-status');
    section.style.display = 'block';
    if (data.authenticated) {
      statusEl.innerHTML = `<span style="color:var(--ok)">Connected</span>` +
        (data.person_urn ? ` <span style="color:var(--muted);font-size:0.75rem">(${escHtml(data.person_urn)})</span>` : '') +
        ` <button class="sync-btn" style="margin-left:0.5rem" onclick="openLinkedInCompose()">New Post</button>` +
        ` <button class="sync-btn" style="margin-left:0.3rem;opacity:0.7" onclick="startLinkedInAuth()">Reconnect</button>`;
    } else {
      statusEl.innerHTML = `<button class="sync-btn" onclick="startLinkedInAuth()">Connect for Posting</button>` +
        ` <span style="color:var(--muted);font-size:0.75rem">Requires auth-post credentials configured via CLI</span>`;
    }
  } catch {
    section.style.display = 'none';
  }
}

async function startLinkedInAuth() {
  try {
    const callbackUrl = `${window.location.origin}/api/linkedin/auth-callback`;
    const data = await api(`/api/linkedin/auth-url?redirect_uri=${encodeURIComponent(callbackUrl)}`);
    openExternal(data.url);
    showToast('Complete authorization in the new tab, then refresh', 'ok');
  } catch (e) {
    showToast(`Failed to start auth: ${e.message}`, 'error');
  }
}

function openLinkedInCompose() {
  document.getElementById('li-post-text').value = '';
  document.getElementById('li-char-count').textContent = '0 / 3,000';
  document.getElementById('li-post-image').value = '';
  document.getElementById('li-image-preview').innerHTML = '';
  document.getElementById('li-article-url').value = '';
  document.getElementById('li-article-title').value = '';
  document.getElementById('li-article-desc').value = '';
  document.getElementById('li-visibility').value = 'PUBLIC';
  document.getElementById('li-post-btn').disabled = false;
  document.getElementById('linkedin-compose-modal').classList.add('open');
  document.getElementById('li-post-text').focus();
}

function closeLinkedInCompose() {
  document.getElementById('linkedin-compose-modal').classList.remove('open');
}

// Character counter
const ta = document.getElementById('li-post-text');
if (ta) ta.addEventListener('input', () => {
  const len = ta.value.length;
  const counter = document.getElementById('li-char-count');
  counter.textContent = len.toLocaleString() + ' / 3,000';
  counter.style.color = len > 2900 ? 'var(--accent)' : 'var(--muted)';
});
const imgInput = document.getElementById('li-post-image');
if (imgInput) imgInput.addEventListener('change', () => {
  const preview = document.getElementById('li-image-preview');
  const file = imgInput.files[0];
  if (file) {
    const img = document.createElement('img');
    img.src = URL.createObjectURL(file);
    img.style.cssText = 'max-width:100%;max-height:150px;border-radius:4px';
    preview.innerHTML = '';
    preview.appendChild(img);
  } else {
    preview.innerHTML = '';
  }
});

async function submitLinkedInPost() {
  const text = document.getElementById('li-post-text').value.trim();
  if (!text) { showToast('Post text is required', 'error'); return; }
  if (text.length > 3000) { showToast('Text too long (max 3,000)', 'error'); return; }

  const visibility = document.getElementById('li-visibility').value;
  const imageFile = document.getElementById('li-post-image').files[0];
  const articleUrl = document.getElementById('li-article-url').value.trim();
  const articleTitle = document.getElementById('li-article-title').value.trim();
  const articleDesc = document.getElementById('li-article-desc').value.trim();

  if (articleUrl && !articleTitle) { showToast('Article title required with URL', 'error'); return; }

  const btn = document.getElementById('li-post-btn');
  btn.disabled = true;
  btn.textContent = 'Posting...';

  try {
    let result;
    if (imageFile) {
      const formData = new FormData();
      formData.append('text', text);
      formData.append('visibility', visibility);
      formData.append('image', imageFile);
      result = await api('/api/linkedin/post-with-image', { method: 'POST', body: formData });
    } else {
      const body = { text, visibility };
      if (articleUrl) {
        body.article_url = articleUrl;
        body.article_title = articleTitle;
        if (articleDesc) body.article_description = articleDesc;
      }
      result = await api('/api/linkedin/post', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      });
    }
    closeLinkedInCompose();
    showToast('Posted to LinkedIn!', 'ok');
  } catch (e) {
    showToast('Post failed: ' + e.message, 'error');
  } finally {
    btn.disabled = false;
    btn.textContent = 'Post';
  }
}

/* ── micro.blog Compose (Wave 2b) ── */

// Stopgap until Wave 4 ships ?kind=category autocomplete from /api/items/tags.
// Matches docs/blog-categories-consolidation.md and the rows seeded into
// the tags table with kind='category' (DB migration 10).
const MICROBLOG_CATEGORIES = [
  'being-human', 'intelligent-agents', 'agentic-ai', 'learning',
  'ml-research', 'responsible-ai', 'vibe-engineering', 'software-craft',
  'reading', 'life', 'commentary',
];

function extractYouTubeId(url) {
  if (!url) return null;
  // youtu.be/<id>, watch?v=<id>, embed/<id>, m.youtube.com/...,
  // youtube-nocookie.com/embed/<id>. Strip query/fragment after the ID.
  const patterns = [
    /youtu\.be\/([A-Za-z0-9_-]{11})/,
    /[?&]v=([A-Za-z0-9_-]{11})/,
    /\/embed\/([A-Za-z0-9_-]{11})/,
    /\/shorts\/([A-Za-z0-9_-]{11})/,
  ];
  for (const re of patterns) {
    const m = url.match(re);
    if (m) return m[1];
  }
  return null;
}

function prefillMicroblogCompose(item) {
  // Source-aware prefill per design §3.1.
  const sub = item.subtype || item.source_type || '';
  let title = '';
  let body = '';

  if (sub.startsWith('youtube') || /youtube\.com|youtu\.be/.test(item.url || '')) {
    const vid = extractYouTubeId(item.url);
    title = item.title || '';
    if (vid) {
      // flschr/mbplugin-youtube-nocookie shortcode form
      const titleArg = (item.title || '').replace(/"/g, '\\"');
      body = `\n\n{{< yt "${vid}" "" "${titleArg}" >}}\n`;
    } else {
      body = `\n\n${item.url || ''}\n`;
    }
  } else if (sub.startsWith('linkedin') || sub.startsWith('bluesky') ||
             sub.startsWith('arxiv') || sub.startsWith('microblog')) {
    // Quote-share shape: blockquoted preview + source link.
    const preview = (item.content || '').slice(0, 280).replace(/\n+/g, ' ');
    title = (item.title || preview).slice(0, 80);
    body = `> ${preview}\n\n${item.url || ''}\n`;
  } else {
    // Own note or unknown source — first line as title, rest as body.
    const lines = (item.content || '').split('\n');
    title = item.title || lines[0].slice(0, 80);
    body = item.title ? (item.content || '') : lines.slice(1).join('\n').trim();
  }

  document.getElementById('mb-title').value = title;
  document.getElementById('mb-body').value = body.trimStart();
}

async function openMicroblogCompose(itemId) {
  // Populate categories dropdown each open (cheap, keeps stopgap in sync).
  const sel = document.getElementById('mb-categories');
  sel.innerHTML = MICROBLOG_CATEGORIES.map(
    c => `<option value="${escHtml(c)}">${escHtml(c)}</option>`
  ).join('');

  document.getElementById('mb-item-id').value = itemId;
  document.getElementById('mb-visibility').value = 'public';
  document.getElementById('mb-tag-source').checked = true;
  document.getElementById('mb-error').style.display = 'none';
  document.getElementById('mb-publish-btn').disabled = false;
  document.getElementById('mb-publish-btn').textContent = 'Publish';

  // Fetch the item fresh so prefill sees the canonical title/url/content,
  // not whatever the calling card had cached.
  try {
    const item = await api(`/api/items/${itemId}`);
    prefillMicroblogCompose(item);
  } catch (e) {
    showToast(`Could not load item: ${e.message}`, 'error');
    return;
  }

  document.getElementById('microblog-compose-modal').classList.add('open');
  document.getElementById('mb-body').focus();
}

function closeMicroblogCompose() {
  document.getElementById('microblog-compose-modal').classList.remove('open');
}

async function submitMicroblogPublish() {
  const itemId = parseInt(document.getElementById('mb-item-id').value, 10);
  const body = document.getElementById('mb-body').value.trim();
  if (!body) {
    showToast('Body is required', 'error');
    return;
  }
  const title = document.getElementById('mb-title').value.trim() || null;
  const visibility = document.getElementById('mb-visibility').value;
  const categories = Array.from(
    document.getElementById('mb-categories').selectedOptions
  ).map(o => o.value);
  const tagSource = document.getElementById('mb-tag-source').checked;

  const errEl = document.getElementById('mb-error');
  errEl.style.display = 'none';
  const btn = document.getElementById('mb-publish-btn');
  btn.disabled = true;
  btn.textContent = 'Publishing…';

  try {
    const result = await api('/api/microblog/publish', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        item_id: itemId,
        title,
        body,
        image_url: null,
        categories,
        visibility,
        if_not_already_published: false,
      }),
    });

    if (tagSource) {
      // Best-effort — don't fail the publish if the tag write fails.
      try {
        await api(`/api/items/${itemId}/tags`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ name: 'published-to-microblog' }),
        });
      } catch (tagErr) {
        console.warn('Tag-source failed:', tagErr);
      }
    }

    closeMicroblogCompose();
    showToast(`Published! ${result.url}`, 'ok');
  } catch (e) {
    // Map API errors to a friendly inline message.
    let msg = e.message || 'Publish failed';
    if (/409/.test(msg) || /already published/i.test(msg)) {
      msg = 'Already published. Open this URL to re-publish: ' + msg;
    } else if (/401/.test(msg) || /not authenticated/i.test(msg)) {
      msg = 'micro.blog isn\'t authenticated. Run `lestash microblog auth --token …` on the server.';
    } else if (/422/.test(msg)) {
      msg = 'micro.blog rejected the post: ' + msg;
    } else if (/502/.test(msg)) {
      msg = 'micro.blog upstream error — try again in a moment. ' + msg;
    }
    errEl.textContent = msg;
    errEl.style.display = '';
  } finally {
    btn.disabled = false;
    btn.textContent = 'Publish';
  }
}

async function addToCollection(itemId) {
  try {
    const colls = await api('/api/collections');
    const options = colls.map(c => `<option value="${c.id}">${escHtml(c.name)} (${c.item_count})</option>`).join('');
    const select = document.getElementById('add-to-coll-select');
    if (select) {
      select.innerHTML = '<option value="">Select collection...</option>' + options;
      select.dataset.itemId = itemId;
      select.style.display = '';
      select.focus();
    }
  } catch (e) {
    showToast('Failed to load collections: ' + e.message, 'error');
  }
}

async function confirmAddToCollection(selectEl) {
  const collId = selectEl.value;
  const itemId = selectEl.dataset.itemId;
  if (!collId) return;
  try {
    await api(`/api/collections/${collId}/items`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ item_id: parseInt(itemId) }),
    });
    showToast('Added to collection', 'ok');
    selectEl.style.display = 'none';
  } catch (e) {
    showToast('Failed: ' + e.message, 'error');
  }
}

/* ── Reconnect ── */
async function reconnect() {
  showToast('Reconnecting...', '');
  await checkHealth();
  // Reload only what failed, never redraw the page: an edit in progress and the
  // scroll position stay
  const path = location.pathname;
  if (failedPath === path) { render(); return; }
  if (path === '/' || path.startsWith('/day/') || path === '/library') { refreshTimeline(); return; }
  if (path === '/people' || path.startsWith('/people/')) {
    // Nothing to lose on these pages: draw them again, a failed load included
    redrawPeople();
    render();
    return;
  }
  if (path === '/search') {
    // A failed search left searchShown empty: run it again, in place
    const q = document.getElementById('search-input').value.trim();
    if (q && searchShown === null) {
      searchShown = q + '|' + document.getElementById('search-mode').value;
      doSearch(q);
    }
    return;
  }
  const tab = Object.keys(TAB_PATHS).find(t => TAB_PATHS[t] === path);
  if (tab && tab !== 'search') showTab(tab);
}

/* ── Clipboard ── */
function copyToClipboard(text, message) {
  if (navigator.clipboard && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(text).then(
      () => showToast(message || 'Copied', 'ok'),
      () => copyFallback(text, message)
    );
  } else {
    copyFallback(text, message);
  }
}

function copyFallback(text, message) {
  const ta = document.createElement('textarea');
  ta.value = text;
  ta.style.cssText = 'position:fixed;left:-9999px';
  document.body.appendChild(ta);
  ta.select();
  document.execCommand('copy');
  document.body.removeChild(ta);
  showToast(message || 'Copied', 'ok');
}

/* ── Init ── */
// Debug mode toggle
// Debug mode: a switch on the Debug page (the header's place is the record button)
const debugToggle = document.getElementById('debug-toggle');
debugToggle.checked = localStorage.getItem('lestash-debug-mode') === 'true';
document.body.classList.toggle('debug-mode', debugToggle.checked);
debugToggle.addEventListener('change', () => {
  document.body.classList.toggle('debug-mode', debugToggle.checked);
  localStorage.setItem('lestash-debug-mode', debugToggle.checked);
});
document.getElementById('settings-btn').addEventListener('click', openSettings);
onSheetClose(closeItemView);
loadAppVersion().then(renderDebugInfo);
renderDebugInfo();

checkHealth();
startTimeline(() => showOnly('tab-timeline', 'timeline'));
startPeople(showOnly);
startLibrary(showOnly);
route(/^\/more$/, () => showOnly('tab-more', 'more'));
startHelp(showOnly);
// Draw once your name is known ("Matt" in bylines), waiting no more than 1.5 s
Promise.race([selfReady, new Promise(r => setTimeout(r, 1500))]).finally(startRouter);
setInterval(checkHealth, 30000);

// Inline handlers (onclick="…") in the markup and in rendered HTML call these by name.
// A module's functions are not global, so these are; new code uses addEventListener.
Object.assign(window, {
  _isYouTubeUrl, addToCollection, attachPickerSearch, attachPickerSelect,
  closeCaptureDialog, closeHtmlImportDialog, closeImageCaptureDialog, closeItemView,
  closeLinkDialog, closeLinkedInCompose, closeMicroblogCompose,
  closeMultiImageCaptureDialog, closeProcessDialog, closeSettings, completeAudibleAuth,
  completeGoogleAuth, confirmAddToCollection, debugTestConnection, escHtml, importDriveUrl,
  openLinkedInCompose, openMicroblogCompose, openNewCollectionDialog,
  openProcessDialog, reconnect, runProcessDialog, saveCaptureItem, saveHtmlImport,
  saveImageCapture, saveLink, saveMultiImageCapture, saveNewCollection, saveSettingsForm,
  saveVoiceNote, startAudibleAuth, startGoogleAuth, startLinkedInAuth, submitLinkedInPost,
  submitMicroblogPublish, toggleAttachMode, toggleRecording, transcribeRecordedAudio,
  triggerSync,
});
