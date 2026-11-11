// Talking to the LeStash server: where it is, the request log, and api().
// Shared by every page; the web app and the Tauri app use the same code.

/* ── Tauri interop ── */
export const IS_TAURI = window.__TAURI_INTERNALS__ !== undefined;
export let tauriInvoke = null;
export let tauriListen = null;

if (IS_TAURI) {
  tauriInvoke = window.__TAURI_INTERNALS__.invoke;
  tauriListen = async function(event, handler) {
    return window.__TAURI_INTERNALS__.invoke('plugin:event|listen', {
      event: event,
      target: { kind: 'Any' },
      handler: window.__TAURI_INTERNALS__.transformCallback(handler),
    });
  };
}

/* ── Settings ── */
export const SETTINGS_KEY = 'lestash-settings';
export const DEFAULT_SETTINGS = {
  host: 'pop-mini.monkey-ladon.ts.net', port: 8444,
  llmUrl: '', llmModel: '', llmPrompt: '',
};

export let settings = loadSettings();

export function loadSettings() {
  try {
    const raw = localStorage.getItem(SETTINGS_KEY);
    if (raw) return { ...DEFAULT_SETTINGS, ...JSON.parse(raw) };
  } catch {}
  return { ...DEFAULT_SETTINGS };
}

export function saveSettings(s) {
  settings = s;
  localStorage.setItem(SETTINGS_KEY, JSON.stringify(s));
}

export function getApiBase() {
  if (IS_TAURI) return `https://${settings.host}:${settings.port}`;
  return '';
}

/* ── Debug logging ── */
export const debugLog = [];
export function addDebugLog(method, url, status, time, error) {
  debugLog.unshift({ ts: new Date().toISOString(), method, url, status, time, error });
  if (debugLog.length > 50) debugLog.pop();
  window.dispatchEvent(new Event('lestash:debuglog'));
}

/* ── API helper ── */
export async function api(path, opts) {
  const fullUrl = `${getApiBase()}${path}`;
  const start = performance.now();
  try {
    const res = await fetch(fullUrl, opts);
    const time = Math.round(performance.now() - start);
    addDebugLog(opts?.method || 'GET', fullUrl, res.status, time);
    if (!res.ok) {
      const body = await res.text();
      throw new Error(`${res.status}: ${body}`);
    }
    return res.json();
  } catch (e) {
    const time = Math.round(performance.now() - start);
    let detail = `${e.name}: ${e.message}`;
    if (e.message === 'Failed to fetch' || e.message === 'NetworkError when attempting to fetch resource.') {
      detail += ' [network-level failure — possible causes: DNS resolution failed, TLS certificate not trusted by Android WebView, CORS blocked, or device not connected to Tailscale]';
    }
    addDebugLog(opts?.method || 'GET', fullUrl, 'ERR', time, detail);
    throw e;
  }
}
