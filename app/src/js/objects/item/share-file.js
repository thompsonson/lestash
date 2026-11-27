// A recording, PDF, text or Markdown file shared from another app (#114). It is saved
// as a new item, as before, or put under an existing item picked here. A recording
// is transcribed when it is saved.
import { api } from '../../api.js';
import { showToast } from '../../toast.js';
import { errorReason } from './add-files.js';
import { attachPickerGetId, attachPickerReset } from './picker.js';

const PREFIX = 'share-file';

let pending = null;  // { file, audio, saveNew, saved }
let saving = false;
let wired = false;

const $ = id => document.getElementById(id);

const size = n => n < 1048576 ? `${Math.max(1, Math.round(n / 1024))} KB` : `${(n / 1048576).toFixed(1)} MB`;

// `saveNew(file)` saves it as a new item, the way a share did before; `saved()` runs
// once it is saved under an item
export function openShareFile(file, { audio = false, saveNew, saved }) {
  wire();
  // One at a time: a file shared while another is open is refused, not swapped in
  if (pending) {
    showToast(`Finish with ${pending.file.name} first, then share ${file.name} again`, 'error');
    return;
  }
  pending = { file, audio, saveNew, saved };
  $('share-file-heading').textContent = audio ? 'Save Shared Recording' : 'Save Shared File';
  $('share-file-name').textContent = `${file.name} · ${size(file.size)}`;
  $('share-file-hint').textContent = audio ? 'It is transcribed when you save it.' : '';
  document.querySelector(`input[name="${PREFIX}-mode"][value="new"]`).checked = true;
  $(`${PREFIX}-attach-section`).style.display = 'none';
  attachPickerReset(PREFIX);
  draw();
  $('share-file-modal').classList.add('open');
}

function draw() {
  const btn = $('share-file-save');
  btn.disabled = saving;
  btn.textContent = !saving ? 'Save' : pending && pending.audio ? 'Transcribing…' : 'Saving…';
  $('share-file-cancel').disabled = saving;
}

function close() {
  if (saving) return;  // the file is on its way
  $('share-file-modal').classList.remove('open');
  attachPickerReset(PREFIX);
  pending = null;
}

async function save() {
  if (saving || !pending) return;
  const mode = document.querySelector(`input[name="${PREFIX}-mode"]:checked`).value;
  const { file, audio, saveNew, saved } = pending;
  if (mode === 'new') {
    close();
    saveNew(file);
    return;
  }
  const parentId = attachPickerGetId(PREFIX);
  if (!parentId) { showToast('Select an item to attach to', 'error'); return; }

  const form = new FormData();
  form.append('file', file);
  form.append('parent_id', String(parentId));
  if (audio) form.append('title', file.name.replace(/\.[^.]+$/, ''));
  saving = true;
  draw();
  let done = null;
  try {
    if (audio) {
      const res = await api('/api/voice/transcribe', { method: 'POST', body: form });
      done = res.parent_id === parentId
        ? `Transcribed and attached: ${res.title}`
        : `The item was deleted meanwhile: saved ${res.title} as a new note`;
    } else {
      const res = await api('/api/import', { method: 'POST', body: form });
      // A file the server could read nothing from comes back 200 with no item
      if (!res.item_id) throw new Error((res.errors || []).join('; ') || 'nothing found in it');
      done = res.items_held ? 'Already in LeStash, left where it is' : `Attached ${file.name}`;
    }
  } catch (e) {
    showToast(`${audio ? 'Transcription' : 'Save'} failed: ${errorReason(e)}`, 'error');
  } finally {
    saving = false;
    draw();
  }
  if (done === null) return;  // it stays open, to try again or pick another item
  close();
  showToast(done, 'ok');
  if (saved) saved();
}

function wire() {
  if (wired) return;
  wired = true;
  $('share-file-cancel').addEventListener('click', close);
  $('share-file-save').addEventListener('click', save);
}
