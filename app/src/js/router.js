// One address, one page (#260).
//
// Every page change goes through navigate() or the browser's own Back and Forward,
// and every page is drawn by render() from the address alone. The web app and the
// Tauri app share this: Android's back button moves through the same history.

const routes = [];
const items = new Map();
let rendering = null;

// Register a page: `pattern` matches the path; `page(match, url)` draws it.
export function route(pattern, page) {
  routes.push({ pattern, page });
}

// Keep an item already loaded, so opening its page needs no second fetch.
export function cacheItem(item) {
  if (item && item.id != null) items.set(String(item.id), item);
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

// Remember where the page was scrolled, so Back returns to the same place
function saveScroll() {
  const state = { ...(history.state || {}), scrollY: main()?.scrollTop || 0 };
  history.replaceState(state, '', location.href);
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
  }
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

export async function render() {
  const url = new URL(location.href);
  const upgraded = upgrade(url);
  if (upgraded) {
    history.replaceState({ depth: depth(), scrollY: 0 }, '', upgraded);
    return render();
  }
  // Leaving a page closes its dialogs
  document.querySelectorAll('.modal-overlay.open').forEach((m) => m.classList.remove('open'));
  const scrollY = (history.state && history.state.scrollY) || 0;
  for (const { pattern, page } of routes) {
    const match = url.pathname.match(pattern);
    if (!match) continue;
    const drawn = (rendering = Promise.resolve(page(match, url)));
    try {
      await drawn;
    } finally {
      // A later navigation owns the scroll position
      if (rendering === drawn && main()) main().scrollTop = scrollY;
    }
    return;
  }
  return navigate('/', { replace: true });
}

// Same-origin links change the page without a reload. Modified clicks (new tab,
// new window) and links marked data-external keep the browser's behaviour.
function onClick(e) {
  if (e.defaultPrevented || e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) {
    return;
  }
  const a = e.target.closest('a[href]');
  if (!a || a.target || a.hasAttribute('download') || a.dataset.external !== undefined) return;
  const url = new URL(a.href, location.href);
  if (url.origin !== location.origin) return;
  e.preventDefault();
  navigate(url.pathname + url.search + url.hash);
}

export function startRouter() {
  if (!history.state || history.state.depth === undefined) {
    history.replaceState({ depth: 0, scrollY: 0 }, '', location.href);
  }
  document.addEventListener('click', onClick);
  window.addEventListener('popstate', () => render());
  return render();
}
