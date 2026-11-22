// Import reminders (#403). Settings › Sources lists the imports you run yourself, most
// overdue first, with how long since each and how often you mean to; and any sync that
// keeps failing. The Timeline shows the single most overdue one on today, until you
// import it or snooze it.

import { api } from '../api.js';
import { navigate } from '../router.js';
import { escHtml, relTime } from '../util.js';
import { showToast } from '../toast.js';

const AIMS = [[30, 'Monthly'], [60, 'Every 2 months'], [90, 'Quarterly'], [0, 'Never remind']];

const json = (method, body) => ({
  method, headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
});

const sinceText = r => r.days === null ? 'No import logged yet'
  : r.days === 0 ? 'Imported today'
    : `${r.days} ${r.days === 1 ? 'day' : 'days'} since last import`;

const exportLink = r => r.export_url
  ? `<a class="ir-export" href="${escHtml(r.export_url)}" target="_blank" rel="noopener">Ask for export ↗</a>` : '';

// What bringing it in takes: Import (an upload), Sync now (read from Drive: a Takeout
// is far over the upload limit), a command to run on pop-mini, or nothing yet
function howHtml(r) {
  if (r.how === 'upload') return '<button class="sync-btn ir-import" data-act="import">Import</button>';
  if (r.how === 'drive' && r.sync) {
    return `<button class="sync-btn ir-import" data-act="sync" data-sync="${escHtml(r.sync)}">Sync now</button>`;
  }
  if (r.how) return `<code class="ir-cmd" title="Run on pop-mini: ${escHtml(r.how)}">${escHtml(r.how)}</code>`;
  return '';
}

// Ask the server to read the export from Drive now, then call `done` once the sync
// has ended, so the row or card shows what it brought in
const POLL_MS = 5000, POLL_FOR_MS = 15 * 60 * 1000;
async function syncNow(button, done) {
  const name = button.dataset.sync;
  button.disabled = true;
  button.textContent = 'Syncing…';
  let runId = null;
  try {
    // The reply names the run to watch: the one it began, or one already running
    const res = await api(`/api/sources/${encodeURIComponent(name)}/sync`, { method: 'POST' });
    runId = res.run_id;
    showToast(res.status === 'already_running' ? 'A Drive sync is already running…' : 'Reading new exports from Drive…', 'ok');
  } catch (err) {
    showToast(`Could not start the sync: ${err.message}`, 'error');
    button.disabled = false;
    button.textContent = 'Sync now';
    return;
  }
  const started = Date.now();
  while (runId !== null && Date.now() - started < POLL_FOR_MS) {
    await new Promise(r => setTimeout(r, POLL_MS));
    let runs;
    try { runs = await api(`/api/sources/status?source=${encodeURIComponent(name)}`); } catch { continue; }
    const run = runs.find(r => r.id === runId);
    if (run && run.status !== 'running') {
      showToast(run.status === 'completed' ? `Drive sync done: ${run.items_added} new` : `Drive sync ${run.status}`,
        run.status === 'completed' ? 'ok' : 'error');
      break;
    }
  }
  done();
}

function aimSelect(r) {
  const options = AIMS.some(([d]) => d === r.aim_days) ? AIMS : [[r.aim_days, `Every ${r.aim_days} days`], ...AIMS];
  return `<select class="ir-aim" aria-label="How often to import ${escHtml(r.label)}">${options.map(([d, label]) =>
    `<option value="${d}"${d === r.aim_days ? ' selected' : ''}>${label}${d === r.default_aim_days ? ' (default)' : ''}</option>`).join('')}</select>`;
}

function rowHtml(r) {
  const state = r.due ? ' due' : '';
  const snoozed = r.snoozed_until
    ? ` · snoozed to ${new Date(r.snoozed_until).toLocaleDateString(undefined, { day: 'numeric', month: 'short' })}` : '';
  return `<li class="ir-row${state}" data-key="${escHtml(r.key)}">
    <div class="ir-main">
      <div class="ir-name">${escHtml(r.label)}</div>
      <div class="ir-since">${escHtml(sinceText(r))}${snoozed}</div>
      ${r.note ? `<div class="ir-note muted">${escHtml(r.note)}</div>` : ''}
      <div class="ir-links">${exportLink(r)}${r.how ? aimSelect(r) : ''}</div>
    </div>
    ${howHtml(r)}
  </li>`;
}

function failingHtml(f) {
  const ok = f.last_ok ? `last worked ${relTime(f.last_ok)}` : 'never worked';
  return `<li class="ir-row failing">
    <div class="ir-main">
      <div class="ir-name">${escHtml(f.source)}</div>
      <div class="ir-since">Last ${f.failed_runs} syncs failed · ${escHtml(ok)}</div>
      ${f.last_error ? `<div class="ir-note muted">${escHtml(f.last_error.slice(0, 120))}</div>` : ''}
    </div>
  </li>`;
}

// Open the file picker of Sources → Import, on the Sources page
function startImport() {
  if (location.pathname !== '/settings/sources') navigate('/settings/sources');
  const pick = document.getElementById('import-file');
  if (pick) pick.click();
}

function draw(box, data) {
  const failing = data.failing.length
    ? `<h3 class="ir-head">Syncs that keep failing</h3><ul class="ir-list">${data.failing.map(failingHtml).join('')}</ul>` : '';
  box.innerHTML = `<h3 class="ir-head">You import these</h3>
    <ul class="ir-list">${data.imports.map(rowHtml).join('')}</ul>${failing}`;
}

// Settings › Sources
export async function loadImportReminders() {
  const box = document.getElementById('import-reminders');
  if (!box) return;
  if (!box.dataset.wired) {
    box.dataset.wired = '1';
    box.addEventListener('click', e => {
      if (e.target.closest('[data-act="import"]')) startImport();
      const sync = e.target.closest('[data-act="sync"]');
      if (sync) syncNow(sync, loadImportReminders);
    });
    box.addEventListener('change', async e => {
      const select = e.target.closest('.ir-aim');
      if (!select) return;
      const key = select.closest('[data-key]').dataset.key;
      try {
        draw(box, await api(`/api/sources/imports/${encodeURIComponent(key)}`, json('PUT', { aim_days: Number(select.value) })));
      } catch (err) {
        showToast(`Could not save: ${err.message}`, 'error');
      }
    });
  }
  try {
    draw(box, await api('/api/sources/imports'));
  } catch (e) {
    box.innerHTML = `<div class="muted">Import reminders unavailable: ${escHtml(e.message)}</div>`;
  }
}

// The Timeline's card for today: the most overdue import, or nothing
export async function dueCardHtml() {
  let data;
  try {
    data = await api('/api/sources/imports');
  } catch {
    return '';  // a reminder is never worth an error on the Timeline
  }
  const r = data.imports.find(i => i.due);
  if (!r) return '';
  const since = r.days === null ? 'No import of it is logged yet.'
    : `${r.days} ${r.days === 1 ? 'day' : 'days'} since you last imported it.`;
  const act = howHtml(r);
  return `<div class="t-due" data-key="${escHtml(r.key)}" role="status">
    <div><b>${escHtml(r.label)} due</b> · ${escHtml(since)}</div>
    <div class="t-due-acts">${act}${exportLink(r)}
      <button class="sync-btn" data-act="snooze">Snooze a week</button></div>
  </div>`;
}

// Clicks on the Timeline's card; `list` holds it, `redraw` draws the day again
export function wireDueCard(list, redraw) {
  if (list.dataset.dueWired) return;
  list.dataset.dueWired = '1';
  list.addEventListener('click', async e => {
    const card = e.target.closest('.t-due');
    if (!card) return;
    if (e.target.closest('[data-act="import"]')) return startImport();
    const sync = e.target.closest('[data-act="sync"]');
    if (sync) return syncNow(sync, redraw);
    if (!e.target.closest('[data-act="snooze"]')) return;
    try {
      await api(`/api/sources/imports/${encodeURIComponent(card.dataset.key)}/snooze`, json('POST', { days: 7 }));
      card.remove();
      redraw();
    } catch (err) {
      showToast(`Could not snooze: ${err.message}`, 'error');
    }
  });
}
