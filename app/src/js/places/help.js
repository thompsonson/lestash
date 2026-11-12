// Help (#277): a reminder of where each source's data comes from, how to ask for its
// export, and how LeStash takes it in. The steps follow each package's README.

import { route } from '../router.js';
import { escHtml } from '../util.js';

const link = (href, text) => `<a href="${href}" target="_blank" rel="noopener">${escHtml(text)}</a>`;

// [name, how it arrives, steps (HTML)]
const SOURCES = [
  ['Google Takeout', 'An export you ask for; synced from Drive', `
    <p>One Takeout covers YouTube history, Gemini, NotebookLM and Keep.</p>
    <ol>
      <li>Open ${link('https://takeout.google.com', 'Google Takeout')} and choose the products.</li>
      <li>For YouTube, set the history format to <b>JSON</b>; HTML isn't read.</li>
      <li>For Gemini chats, include <b>My Activity → Gemini Apps</b>, also as <b>JSON</b>.</li>
      <li>Set <b>Delivery method → Add to Drive</b>. <b>Export every 2 months</b> keeps it coming.</li>
      <li>The sync reads new <code>takeout-*.zip</code> files from Drive by itself.</li>
    </ol>
    <p class="muted">Exports sent as an email link can't be fetched; upload those in
      <a href="/settings/sources">Sources → Import</a>. The export must be in English.</p>`],
  ['LinkedIn', 'Synced; older history from an export', `
    <ol>
      <li>Your posts, comments and reactions sync through LinkedIn's data API.</li>
      <li>For older history, ask for ${link('https://www.linkedin.com/mypreferences/d/download-my-data', 'a copy of your data')}.</li>
      <li>LinkedIn emails a link when the zip is ready, often within a day.</li>
      <li>Import it on pop-mini: <code>lestash linkedin import ~/Downloads/&lt;file&gt;.zip</code></li>
    </ol>`],
  ['Claude', 'An export you ask for', `
    <ol>
      <li>In ${link('https://claude.ai/settings/data-privacy-controls', 'Claude → Settings → Privacy')}, choose <b>Export data</b>.</li>
      <li>Claude emails a download link.</li>
    </ol>
    <p class="muted">Uploading this zip isn't read yet; a parser for it is
      ${link('https://github.com/thompsonson/lestash/issues/88', 'issue #88')}.</p>`],
  ['Claude Code', 'Read from this machine', `
    <p>Sessions are read from <code>~/.claude</code> on pop-mini; other machines' sessions are copied in first.</p>`],
  ['Gemini', 'Takeout, or the Chrome extension', `
    <ol>
      <li>Chats arrive with Google Takeout, above.</li>
      <li>The LeStash Chrome extension saves a chat from <code>gemini.google.com</code> in one click.</li>
      <li>A chat saved as an HTML page can be uploaded in <a href="/settings/sources">Sources → Import</a>.</li>
    </ol>`],
  ['NotebookLM', 'Takeout', `<p>Notebooks, sources and generated audio arrive with Google Takeout, above.</p>`],
  ['Mistral Le Chat', 'An export you upload', `
    <ol>
      <li>Export your data from ${link('https://chat.mistral.ai', 'Le Chat')}'s settings.</li>
      <li>Upload the zip in <a href="/settings/sources">Sources → Import</a>.</li>
    </ol>`],
  ['Bluesky', 'Synced', `
    <p>Your posts and threads sync by themselves once signed in on pop-mini:
      <code>lestash bluesky auth --handle &lt;handle&gt;</code>. An
      ${link('https://bsky.app/settings/app-passwords', 'app password')} is safer than your own.</p>`],
  ['Micro.blog', 'Synced', `
    <p>Posts sync with a token from ${link('https://micro.blog/account/apps', 'Micro.blog → Account → App tokens')}.</p>`],
  ['Kobo', 'Plug it in', `
    <ol>
      <li>Plug the Kobo into pop-mini and tap <b>Connect</b>.</li>
      <li>LeStash copies it, unmounts it and imports highlights, notes and markups.</li>
    </ol>`],
  ['Audible', 'Synced', `
    <p>Bookmarks, notes and highlights sync once signed in on pop-mini:
      <code>lestash audible auth --locale &lt;uk|us|fr|…&gt;</code>.</p>`],
  ['Files and pages', 'You add them', `
    <p>Upload JSON, HTML pages and PDFs in <a href="/settings/sources">Sources → Import</a>,
      or share them to the app from your phone.</p>`],
];

function draw() {
  const tab = document.getElementById('tab-help');
  if (tab.dataset.built) return;
  tab.dataset.built = '1';
  tab.innerHTML = `
    <h2 class="help-title">Where your data comes from</h2>
    <p class="muted">How to get each source's data into LeStash. Sync state is under
      <a href="/settings/sources">Sources</a>.</p>
    <div class="help-list">${SOURCES.map(([name, how, body]) => `
      <details class="help-source">
        <summary><b>${escHtml(name)}</b> <span class="muted">· ${escHtml(how)}</span></summary>
        <div class="help-body">${body}</div>
      </details>`).join('')}
    </div>`;
}

export function startHelp(show) {
  route(/^\/help$/, () => { show('tab-help', 'help'); draw(); });
}
