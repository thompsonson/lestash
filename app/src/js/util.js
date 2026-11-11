// Small helpers every page uses: escaping, dates, source names.

export function escHtml(s) {
  if (!s) return '';
  return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}

export function relTime(dateStr) {
  if (!dateStr) return '-';
  const d = new Date(dateStr);
  const now = new Date();
  const diff = (now - d) / 1000;
  if (diff < 60) return 'just now';
  if (diff < 3600) return `${Math.floor(diff / 60)}m ago`;
  if (diff < 86400) return `${Math.floor(diff / 3600)}h ago`;
  if (diff < 604800) return `${Math.floor(diff / 86400)}d ago`;
  return d.toLocaleDateString();
}

// "11 Nov 2026"
export function dateText(dateStr) {
  if (!dateStr) return '';
  const d = new Date(dateStr);
  if (isNaN(d)) return '';
  return d.toLocaleDateString(undefined, { year: 'numeric', month: 'short', day: 'numeric' });
}

export function sourceClass(source) {
  const s = (source || '').split('/')[0];
  if (['linkedin', 'bluesky', 'youtube', 'arxiv', 'microblog'].includes(s)) return s;
  return '';
}

export function sourceName(subtype) {
  return (subtype || '').split('/')[0];
}

const SOURCE_LABELS = {
  linkedin: 'LinkedIn', bluesky: 'Bluesky', youtube: 'YouTube', arxiv: 'arXiv',
  microblog: 'Micro.blog', notebooklm: 'NotebookLM', gemini: 'Gemini', claude: 'Claude',
  'claude-code': 'Claude Code', claude_code: 'Claude Code', 'claude-project': 'Claude',
  'claude-memory': 'Claude', mistral: 'Mistral', kobo: 'Kobo', audible: 'Audible',
  jelu: 'Jelu', 'google-drive': 'Google Drive', gmail: 'Gmail', email: 'Email',
  voice: 'Voice note', note: 'Note', web: 'Web', share: 'Shared', pdf: 'PDF', image: 'Image',
  atomicguard: 'AtomicGuard',
};

// The source in words: "Claude Code", not "claude-code"
export function sourceLabel(source) {
  return SOURCE_LABELS[source] || source || '';
}
