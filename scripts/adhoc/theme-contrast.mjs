#!/usr/bin/env node
// Checks every theme in app/src/css/themes.css for readable contrast (WCAG 2.2):
// text, muted text, accent text and links need 4.5:1 on the page and on cards;
// kind colours, used for icons, need 3:1 on cards.
//   node scripts/adhoc/theme-contrast.mjs [path/to/themes.css]
import { readFileSync } from 'node:fs';

const file = process.argv[2] || new URL('../../app/src/css/themes.css', import.meta.url);
const css = readFileSync(file, 'utf8');

const themes = {};
for (const m of css.matchAll(/\[data-theme="([\w-]+)"\][^{]*\{([^}]*)\}/g)) {
  const vars = {};
  for (const v of m[2].matchAll(/--([\w-]+):\s*(#[0-9a-fA-F]{6})/g)) vars[v[1]] = v[2];
  themes[m[1]] = vars;
}

const lum = hex => {
  const c = [1, 3, 5].map(i => parseInt(hex.slice(i, i + 2), 16) / 255)
    .map(x => (x <= 0.03928 ? x / 12.92 : ((x + 0.055) / 1.055) ** 2.4));
  return 0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2];
};
const ratio = (a, b) => {
  const [x, y] = [lum(a), lum(b)].sort((p, q) => q - p);
  return (x + 0.05) / (y + 0.05);
};

const TEXT = ['text', 'muted', 'accent-text', 'link'];
const ICONS = ['c-red', 'c-orange', 'c-yellow', 'c-green', 'c-teal', 'c-blue', 'c-purple', 'c-pink'];
let failed = 0;
for (const [name, v] of Object.entries(themes)) {
  const rows = [];
  for (const fg of TEXT) for (const bg of ['bg', 'surface']) {
    const r = ratio(v[fg], v[bg]);
    if (r < 4.5) rows.push(`${fg} on ${bg} ${r.toFixed(2)}`);
  }
  const onAccent = ratio(v['on-accent'], v.accent);
  if (onAccent < 4.5) rows.push(`on-accent on accent ${onAccent.toFixed(2)}`);
  for (const fg of ICONS) {
    const r = ratio(v[fg], v.surface);
    if (r < 3) rows.push(`${fg} on surface ${r.toFixed(2)} (icons need 3)`);
  }
  const worst = Math.min(...TEXT.flatMap(fg => ['bg', 'surface'].map(bg => ratio(v[fg], v[bg]))));
  console.log(`${rows.length ? '✗' : '✓'} ${name.padEnd(16)} lowest text contrast ${worst.toFixed(2)}`);
  rows.forEach(r => console.log(`    ${r}`));
  failed += rows.length;
}
console.log(`${Object.keys(themes).length} themes, ${failed} failures`);
process.exit(failed ? 1 : 0);
