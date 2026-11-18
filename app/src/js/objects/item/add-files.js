// + Add → File: one or more PDFs, web pages, text or Markdown files, each its own item
// under the item it was added from. They are saved one at a time, in the order shown,
// so they appear under the item in that order.
import { api } from '../../api.js';
import { showToast } from '../../toast.js';
import { escHtml } from '../../util.js';

// MIME types as well as suffixes: a phone's picker greys out what it can't match
export const FILE_TYPES = 'application/pdf,text/html,text/plain,text/markdown,.pdf,.html,.htm,.txt,.md,.markdown';
const TAKEN = /\.(pdf|html?|txt|md|markdown)$/i;
const MAX_BYTES = 50 * 1024 * 1024;  // the server's limit

let files = [];
let parent = null;
let saving = false;
let onSaved = null;
let wired = false;

const $ = id => document.getElementById(id);

const size = n => n < 1024 ? `${n} B` : n < 1048576 ? `${Math.round(n / 1024)} KB` : `${(n / 1048576).toFixed(1)} MB`;

// Opens the dialog with the picked files; `saved(parentId)` runs once any are saved
export function openAddFiles(picked, to, saved) {
  wire();
  parent = to;
  onSaved = saved;
  files = [];
  $('add-files-adding-to').textContent = `Adding to: ${to.title || 'this item'}`;
  addFiles(picked);
  $('add-files-modal').classList.add('open');
}

function addFiles(picked) {
  const skipped = [];
  for (const f of picked) {
    if (!TAKEN.test(f.name)) skipped.push(`${f.name} (not a PDF, web page, text or Markdown file)`);
    else if (f.size > MAX_BYTES) skipped.push(`${f.name} (over 50 MB)`);
    else files.push(f);
  }
  if (skipped.length) showToast(`Skipped ${skipped.join(', ')}`, 'error');
  draw();
}

function draw() {
  $('add-files-list').innerHTML = files.map((f, i) =>
    `<li><span class="add-files-name">${escHtml(f.name)}</span><small>${size(f.size)}</small>`
    + `<button type="button" data-remove="${i}" aria-label="Remove ${escHtml(f.name)}"${saving ? ' disabled' : ''}>×</button></li>`
  ).join('');
  const n = files.length;
  $('add-files-count').textContent = `${n} file${n === 1 ? '' : 's'} · each saved as its own item`;
  const btn = $('add-files-save');
  if (!saving) {
    btn.textContent = n ? `Save ${n}` : 'Save';
    btn.disabled = !n;
  }
}

function close() {
  if (saving) return;  // the files still to save would be lost
  $('add-files-modal').classList.remove('open');
  files = [];
  parent = null;
  onSaved = null;
}

// One file's error, in words: the server's detail when it gives one
function reason(e) {
  const m = /^\d+: (.*)$/s.exec(e.message || '');
  if (!m) return e.message;
  try { return JSON.parse(m[1]).detail || m[1]; } catch { return m[1]; }
}

async function save() {
  if (saving || !files.length || !parent) return;
  saving = true;
  const btn = $('add-files-save');
  btn.disabled = true;
  $('add-files-cancel').disabled = true;
  $('add-files-more').hidden = true;
  draw();
  const total = files.length;
  const parentId = parent.id;
  const failed = [];
  const errors = [];
  let added = 0;
  let held = 0;
  for (const [i, f] of files.entries()) {
    btn.textContent = `Saving ${i + 1}/${total}…`;
    const form = new FormData();
    form.append('file', f);
    form.append('parent_id', String(parentId));
    try {
      const res = await api('/api/import', { method: 'POST', body: form });
      // A page the server could read nothing from comes back 200 with no item
      if (!res.item_id) throw new Error((res.errors || []).join('; ') || 'nothing found in it');
      if (res.items_held) held++; else added++;
    } catch (e) {
      failed.push(f);
      errors.push(`${f.name}: ${reason(e)}`);
    }
  }
  saving = false;
  $('add-files-cancel').disabled = false;
  $('add-files-more').hidden = false;
  const done = [
    added ? `Saved ${added} file${added === 1 ? '' : 's'}` : '',
    held ? `${held} already in LeStash, left where ${held === 1 ? 'it is' : 'they are'}` : '',
  ].filter(Boolean).join('; ');
  if (failed.length) {
    // Only the files that failed stay, to try again
    files = failed;
    draw();
    showToast(`${done ? done + '. ' : ''}Not saved: ${errors.join('; ')}`, 'error');
  } else {
    const saved = onSaved;
    close();
    showToast(done, 'ok');
    if (saved) saved(parentId);
    return;
  }
  if (added && onSaved) onSaved(parentId);
}

function wire() {
  if (wired) return;
  wired = true;
  $('add-files-list').addEventListener('click', e => {
    const i = e.target.closest('[data-remove]')?.dataset.remove;
    if (i === undefined || saving) return;
    files.splice(Number(i), 1);
    draw();
  });
  $('add-files-more').addEventListener('click', e => {
    e.preventDefault();
    if (!saving) $('add-files-more-input').click();
  });
  $('add-files-more-input').addEventListener('change', e => {
    const more = [...e.target.files];
    e.target.value = '';
    if (more.length) addFiles(more);
  });
  $('add-files-cancel').addEventListener('click', close);
  $('add-files-save').addEventListener('click', save);
}
