// One address, one page (#260).
//
// Every page change goes through navigate() or the browser's own Back and Forward,
// and every page is drawn by render() from the address alone. The web app and the
// Tauri app share this: Android's back button moves through the same history.
//
// Back (the browser's or Android's) with a dialog open closes the dialog and puts
// the page's address back, so the page stays.

const routes = [];
const items = new Map();
const ITEM_CACHE_SIZE = 500;
let rendering = null;
let drawnPath = null;
// The entry on screen, to put back when Back only closes a dialog
let current = null;
let ready;
// Settles once the first page is drawn
export const routerReady = new Promise((resolve) => { ready = resolve; });

// Register a page: `pattern` matches the path; `page(match, url)` draws it and may
// return a promise that settles once its main content is on the page.
export function route(pattern, page) {
  routes.push({ pattern, page });
}

function routeFor(pathname) {
  return routes.find(({ pattern }) => pattern.test(pathname));
}

// Keep an item already loaded, so opening its page needs no second fetch
export function cacheItem(item) {
  if (item && item.id != null) {
    const key = String(item.id);
    items.delete(key);
    items.set(key, item);
    if (items.size > ITEM_CACHE_SIZE) items.delete(items.keys().next().value);
  }
  return item;
}

export function cachedItem(id) {
  return items.get(String(id));
}

function main() {
  return document.querySelector('main');
}

function depth() {
  return (history.state && history.state.depth) || 0;
}

function openDialogs() {
  return document.querySelectorAll('.modal-overlay.open');
}

// Remember where the page was scrolled, so Back returns to the same place
function saveScroll() {
  const state = { ...(history.state || {}), scrollY: main()?.scrollTop || 0 };
  history.replaceState(state, '', location.href);
}

function remember() {
  current = { href: location.href, state: history.state };
}

export function navigate(href, { replace = false } = {}) {
  const url = new URL(href, location.href);
  const target = url.pathname + url.search + url.hash;
  const here = location.pathname + location.search + location.hash;
  if (replace) {
    history.replaceState({ ...(history.state || {}), depth: depth(), scrollY: 0 }, '', target);
  } else if (target !== here) {
    saveScroll();
    history.pushState({ depth: depth() + 1, scrollY: 0 }, '', target);
  } else {
    saveScroll();  // same page again: keep its place
  }
  remember();
  return render();
}

// The page's own ← Back: step back in history when there is a page to return to in
// this app, else go to `fallback` (a link opened directly has no page behind it)
export function back(fallback = '/') {
  if (depth() > 0) history.back();
  else navigate(fallback, { replace: true });
}

// Addresses from before the router (#feed, #collection/11) become real paths
function upgrade(url) {
  const hash = url.hash.replace(/^#/, '');
  if (!hash || url.pathname !== '/') return null;
  const coll = hash.match(/^collection\/(\d+)$/);
  if (coll) return `/collections/${coll[1]}`;
  const legacy = {
    feed: '/', recent: '/recent', notes: '/notes', search: '/search',
    collections: '/collections', sources: '/settings/sources',
    stats: '/settings/stats', debug: '/settings/debug',
  };
  return hash in legacy ? legacy[hash] + (hash === 'feed' ? url.search : '') : null;
}

// Content often arrives after the page is drawn; keep applying the saved position
// while the page grows, until it is reached, the user scrolls, or 3 s pass
function restoreScroll(y, token) {
  const el = main();
  if (!el) return;
  el.scrollTop = y;
  if (!y || el.scrollTop >= y - 1) return;
  const events = ['wheel', 'touchstart', 'keydown', 'pointerdown'];
  const stop = () => {
    observer.disconnect();
    clearTimeout(timer);
    events.forEach((t) => el.removeEventListener(t, stop));
  };
  const observer = new MutationObserver(() => {
    if (rendering !== token) return stop();
    el.scrollTop = y;
    if (el.scrollTop >= y - 1) stop();
  });
  observer.observe(el, { childList: true, subtree: true });
  const timer = setTimeout(stop, 3000);
  events.forEach((t) => el.addEventListener(t, stop, { passive: true }));
}

export async function render() {
  const url = new URL(location.href);
  const upgraded = upgrade(url);
  if (upgraded) {
    history.replaceState({ depth: depth(), scrollY: 0 }, '', upgraded);
    return render();
  }
  // Leaving a page closes its dialogs; drawing the same page again keeps them
  if (drawnPath !== null && drawnPath !== url.pathname) {
    openDialogs().forEach((m) => m.classList.remove('open'));
  }
  drawnPath = url.pathname;
  const scrollY = (history.state && history.state.scrollY) || 0;
  const found = routeFor(url.pathname);
  if (!found) return navigate('/', { replace: true });
  const token = {};
  rendering = token;
  try {
    await found.page(url.pathname.match(found.pattern), url);
  } finally {
    // A later navigation owns the scroll position
    if (rendering === token) restoreScroll(scrollY, token);
  }
}

// Links to app pages change the page without a reload. Other links (files under
// /api, downloads, new-tab clicks, data-external) keep the browser's behaviour.
function onClick(e) {
  if (e.defaultPrevented || e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) {
    return;
  }
  const a = e.target.closest('a[href]');
  if (!a || a.target || a.hasAttribute('download') || a.dataset.external !== undefined) return;
  const url = new URL(a.href, location.href);
  if (url.origin !== location.origin || !routeFor(url.pathname)) return;
  // A link within the page (#section) keeps the browser's own jump
  if (url.hash && url.pathname === location.pathname && url.search === location.search) return;
  e.preventDefault();
  navigate(url.pathname + url.search + url.hash);
}

function onPop() {
  if (openDialogs().length > 0 && current) {
    openDialogs().forEach((m) => m.classList.remove('open'));
    const state = { ...(current.state || {}), scrollY: main()?.scrollTop || 0 };
    history.pushState(state, '', current.href);
    return;
  }
  const was = current && new URL(current.href);
  if (!history.state) {
    // A #section link made this entry: one step on from the page it belongs to
    const below = (current && current.state && current.state.depth) || 0;
    history.replaceState({ depth: below + 1, scrollY: main()?.scrollTop || 0 }, '', location.href);
  }
  remember();
  // Only the #section changed: the browser already jumped, the page stays
  if (was && was.pathname === location.pathname && was.search === location.search) return;
  render();
}

// Listen from the start, so a dialog opened before the first page is drawn
// (the server slow to answer) is still closed by Back
document.addEventListener('click', onClick);
window.addEventListener('popstate', onPop);

export function startRouter() {
  if (!history.state || history.state.depth === undefined) {
    history.replaceState({ depth: 0, scrollY: 0 }, '', location.href);
  }
  remember();
  return render().finally(ready);
}
