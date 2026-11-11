// What each item kind looks like, and words for what the sources store as codes.
// Kinds come from the server (core/kinds.py): item.kind, item.kind_label, item.kind_detail.

const ICONS = {
  post: '▭', comment: '↳', reaction: '♥', message: '✉', video: '▶', search: '⌕',
  transcript: '≡', paper: '▤', book: '▥', highlight: '❝', chat: '✦', turn: '·',
  notebook: '▣', artifact: '◇', project: '▣', document: '▤', note: '✎', email: '✉',
  image: '▨', stub: '◌', other: '·',
};

const ARTIFACT_ICONS = {
  'Audio overview': '▶', 'Explainer video': '▶', Slides: '▦', Infographic: '▦', App: '◇',
};

export function kindIcon(item) {
  if (item.kind === 'artifact' && ARTIFACT_ICONS[item.kind_detail]) return ARTIFACT_ICONS[item.kind_detail];
  return ICONS[item.kind] || '·';
}

// "Audio overview" for a generated artifact, else the kind's label
export function kindText(item) {
  return item.kind_detail || item.kind_label || item.kind || '';
}

// A person's name as shown: an account LeStash can't name reads as words, not a URN
export function personName(display, source) {
  if (!display || display === '-') return '';
  if (display.startsWith('urn:li:')) return 'A LinkedIn member';
  if (display.startsWith('did:')) return 'A Bluesky account';
  if (display.startsWith('urn:')) return `An account on ${source || 'another site'}`;
  return display;
}

const LANGUAGE = (() => {
  try { return new Intl.DisplayNames(undefined, { type: 'language' }); } catch { return null; }
})();

const OPTION_LABELS = {
  episodeFocus: 'Prompt', freeTextSteeringPrompt: 'Prompt', episodeLength: 'Length',
  targetAudience: 'Audience', showFormat: 'Format', languageCode: 'Language', appType: 'Type',
};
const PROMPT_KEYS = new Set(['episodeFocus', 'freeTextSteeringPrompt']);

// "EPISODE_LENGTH_SHORT" under episodeLength reads "Short"; "SHOW_FORMAT_THE_BRIEF" "The brief"
function optionValue(key, value) {
  if (key === 'languageCode') return (LANGUAGE && LANGUAGE.of(value)) || value;
  const prefix = key.replace(/([a-z])([A-Z])/g, '$1_$2').toUpperCase() + '_';
  let words = value.startsWith(prefix) ? value.slice(prefix.length) : value;
  if (/^[A-Z0-9_]+$/.test(words)) {
    words = words.replace(/^APP_TYPE_/, '').toLowerCase().replace(/_/g, ' ');
    words = words.charAt(0).toUpperCase() + words.slice(1);
  }
  return words;
}

// A NotebookLM artifact's generation settings in words, unspecified ones left out.
// Returns { prompt, settings: [[label, value], ...] }
export function generationSettings(item) {
  const options = item.metadata && item.metadata.options;
  const generation = options && typeof options === 'object' ? options.generationOptions : null;
  const out = { prompt: '', settings: [] };
  if (!generation || typeof generation !== 'object') return out;
  for (const [key, value] of Object.entries(generation)) {
    if (typeof value !== 'string' || !value.trim() || value.endsWith('_UNSPECIFIED')) continue;
    if (PROMPT_KEYS.has(key)) { out.prompt = value.trim(); continue; }
    if (!OPTION_LABELS[key]) continue;
    out.settings.push([OPTION_LABELS[key], optionValue(key, value)]);
  }
  return out;
}
