// Links that leave the app. In the Tauri app (Android, desktop) a webview can't open
// a new tab, so a link with target="_blank" did nothing; there it opens in the
// system browser through the app's open_external command. The web app keeps the
// browser's own way.

import { IS_TAURI, addDebugLog, tauriInvoke } from './api.js';
import { showToast } from './toast.js';

// Open a URL outside the app: the system browser under Tauri, a new tab on the web
// Each step goes to the Debug page's log, as the phone's console can't be seen: the
// tap, then the app's answer, its error, or no answer at all (#328)
const NO_ANSWER_MS = 5000;
export function openExternal(href) {
  if (!IS_TAURI) { window.open(href, '_blank'); return; }
  const start = performance.now();
  addDebugLog('LINK', href, 'TAP', null, null);
  let answered = false;
  const wait = setTimeout(() => {
    if (answered) return;
    addDebugLog('LINK', href, 'NO ANSWER', NO_ANSWER_MS, 'open_external did not answer');
    showToast("Couldn't open link: the app didn't answer", 'error');
  }, NO_ANSWER_MS);
  tauriInvoke('open_external', { url: href }).then(() => {
    answered = true;
    clearTimeout(wait);
    addDebugLog('LINK', href, 'OK', Math.round(performance.now() - start), null);
  }).catch(err => {
    answered = true;
    clearTimeout(wait);
    console.error('Could not open link', href, err);
    showToast(`Couldn't open link: ${err}`, 'error');
    addDebugLog('LINK', href, 'ERR', Math.round(performance.now() - start), String(err));
  });
}

function onClick(e) {
  if (e.defaultPrevented || e.button !== 0) return;
  const a = e.target.closest('a[href]');
  if (!a) return;
  const url = new URL(a.href, location.href);
  if (!/^(https?|mailto):$/.test(url.protocol)) return;
  // Pages of the app itself stay with the router
  if (url.origin === location.origin && a.target !== '_blank') return;
  e.preventDefault();
  confirmExternal(url.href);
}

// Temporary for #328: ask before leaving the app, in the page itself (not confirm(),
// which the WebView handles natively). If this never shows on a tap, the tap never
// reached onClick; if it shows, the fault is after it.
function confirmExternal(href) {
  addDebugLog('LINK', href, 'ASK', null, null);
  document.getElementById('external-confirm')?.remove();
  const box = document.createElement('div');
  box.id = 'external-confirm';
  box.className = 'modal-overlay open';
  box.innerHTML = `<div class="modal" role="dialog" aria-label="Open link">
    <p>This will open an external link. Are you sure?</p>
    <p class="external-confirm-url"></p>
    <div class="modal-actions">
      <button type="button" class="btn-cancel" data-answer="no">Cancel</button>
      <button type="button" class="btn-save" data-answer="yes">Open</button>
    </div>
  </div>`;
  box.querySelector('.external-confirm-url').textContent = href;
  box.addEventListener('click', ev => {
    const answer = ev.target.closest('[data-answer]')?.dataset.answer;
    if (!answer && ev.target !== box) return;
    box.remove();
    addDebugLog('LINK', href, answer === 'yes' ? 'YES' : 'NO', null, null);
    if (answer === 'yes') openExternal(href);
  });
  document.body.appendChild(box);
}

if (IS_TAURI) document.addEventListener('click', onClick);

// Trace for #328: links do nothing on Android, and no tap reached openExternal. Log
// that this file loaded, and every tap on a link as it first arrives (capture phase),
// before any other handler can act on it. To be removed once links work.
addDebugLog('LINK', `external.js loaded, IS_TAURI=${IS_TAURI}`, 'INIT', null, null);
for (const type of ['pointerup', 'click']) {
  document.addEventListener(type, e => {
    const a = e.target && e.target.closest ? e.target.closest('a[href]') : null;
    if (!a) return;
    addDebugLog('LINK', `${a.getAttribute('href')} target=${a.target || '-'} button=${e.button}`
      + ` prevented=${e.defaultPrevented} on=${e.target.tagName}`, type.toUpperCase(), null, null);
  }, true);
}
