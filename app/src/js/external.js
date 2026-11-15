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
  openExternal(url.href);
}

if (IS_TAURI) document.addEventListener('click', onClick);

// Trace for #328: links do nothing on Android, and no tap reached openExternal. Log
// that this file loaded, and every tap on a link as it first arrives (capture phase),
// before any other handler can act on it. To be removed once links work.
addDebugLog('LINK', 'external.js loaded', 'INIT', null, `IS_TAURI=${IS_TAURI}`);
for (const type of ['pointerup', 'click']) {
  document.addEventListener(type, e => {
    const a = e.target && e.target.closest ? e.target.closest('a[href]') : null;
    if (!a) return;
    addDebugLog('LINK', a.getAttribute('href'), type.toUpperCase(), null,
      `target=${a.target || '-'} button=${e.button} prevented=${e.defaultPrevented} on=${e.target.tagName}`);
  }, true);
}
