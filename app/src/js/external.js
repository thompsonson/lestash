// Links that leave the app. In the Tauri app (Android, desktop) a webview can't open
// a new tab, so a link with target="_blank" did nothing; there it opens in the
// system browser through the shell plugin. The web app keeps the browser's own way.

import { IS_TAURI, tauriInvoke } from './api.js';

function onClick(e) {
  if (e.defaultPrevented || e.button !== 0) return;
  const a = e.target.closest('a[href]');
  if (!a) return;
  const url = new URL(a.href, location.href);
  if (!/^(https?|mailto):$/.test(url.protocol)) return;
  // Pages of the app itself stay with the router
  if (url.origin === location.origin && a.target !== '_blank') return;
  e.preventDefault();
  tauriInvoke('plugin:shell|open', { path: url.href }).catch(err => {
    console.error('Could not open link', url.href, err);
  });
}

if (IS_TAURI) document.addEventListener('click', onClick);
