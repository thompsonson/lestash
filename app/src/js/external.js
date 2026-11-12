// Links that leave the app. In the Tauri app (Android, desktop) a webview can't open
// a new tab, so a link with target="_blank" did nothing; there it opens in the
// system browser through the app's open_external command. The web app keeps the
// browser's own way.

import { IS_TAURI, tauriInvoke } from './api.js';

// Open a URL outside the app: the system browser under Tauri, a new tab on the web
export function openExternal(href) {
  if (!IS_TAURI) { window.open(href, '_blank'); return; }
  tauriInvoke('open_external', { url: href }).catch(err => {
    console.error('Could not open link', href, err);
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
