// An item's card: the same frame for every kind (icon, title, who and when, what's
// inside), with lines chosen by kind. Cards are links, so the keyboard reaches them.
// See docs/ux-object-model-design.md, "Card faces".

import { getApiBase } from '../../api.js';
import { cacheItem } from '../../router.js';
import { escHtml, relTime, dateText, sourceClass, sourceName, sourceLabel } from '../../util.js';
import { kindIcon, kindText, personName } from './kinds.js';

const plural = (n, one, many) => `${n} ${n === 1 ? one : many}`;

function firstLine(text, max = 140) {
  const line = (text || '').split('\n').map(s => s.replace(/^#+\s*/, '').trim()).find(Boolean) || '';
  return line.length > max ? line.slice(0, max - 1) + '…' : line;
}

export function cardTitle(item) {
  const by = personName(item.actor_display, item.source_type);
  switch (item.kind) {
    case 'reaction': {
      const target = item.parent_title || firstLine(item.preview);
      return `${by || 'Someone'} reacted${target ? ` to “${firstLine(target, 80)}”` : ''}`;
    }
    case 'comment':
      return item.title || firstLine(item.content) || firstLine(item.preview) || 'Reply';
    case 'stub':
      return item.title || item.url || 'Not in LeStash';
    default:
      return item.title || firstLine(item.preview) || firstLine(item.content) || '(untitled)';
  }
}

// Who and when: "Post · LinkedIn · 20 Jan 2026 · by You"
function whoWhen(item) {
  const parts = [kindText(item)];
  const meta = item.metadata || {};
  const author = personName(item.author_display, item.source_type);
  const when = dateText(item.created_at || meta.last_at);
  switch (item.kind) {
    case 'video':
      if (meta.channel_title) parts.push(meta.channel_title);
      else parts.push(sourceLabel(item.source_type));
      break;
    case 'comment':
      if (item.parent_title) parts.push(`on “${firstLine(item.parent_title, 60)}”`);
      else parts.push(sourceLabel(item.source_type));
      break;
    case 'artifact':
      if (item.parent_title) parts.push(`in ${firstLine(item.parent_title, 60)}`);
      break;
    case 'stub':
      try { parts.push(new URL(item.url).host); } catch { parts.push(sourceLabel(item.source_type)); }
      break;
    default:
      parts.push(sourceLabel(item.source_type));
  }
  if (when) parts.push(when);
  const by = item.kind === 'comment' ? personName(item.actor_display, item.source_type) || author : author;
  // A highlight or note in a book is yours; its author is the book's
  if (by && !['video', 'search', 'stub', 'highlight'].includes(item.kind)) parts.push(`by ${by}`);
  return parts.filter(Boolean).join(' · ');
}

// What's inside: counts for things with parts, else the start of the text
function inside(item, title) {
  const meta = item.metadata || {};
  const parts = item.child_count || 0;
  switch (item.kind) {
    case 'post': {
      const reactions = item.reaction_count || 0;
      const replies = parts - reactions;
      const counts = [replies && plural(replies, 'reply', 'replies'), reactions && plural(reactions, 'reaction', 'reactions')];
      return { counts: counts.filter(Boolean).join(' · ') };
    }
    case 'video': {
      const n = meta.watched_count || 0;
      return { counts: n ? `watched ${plural(n, 'time', 'times')}` : '' };
    }
    case 'book':
      return { counts: parts ? plural(parts, 'highlight or note', 'highlights and notes') : '' };
    case 'notebook':
    case 'project':
      return { counts: parts ? plural(parts, 'part', 'parts') : '' };
    case 'chat':
      return { counts: parts ? plural(parts, 'turn', 'turns') : '' };
    case 'email':
      return { counts: parts ? plural(parts, 'message', 'messages') : '' };
    case 'stub':
      return { counts: 'Not in LeStash' };
    default: {
      const preview = item.preview && firstLine(item.preview, 200) !== firstLine(title, 200) ? item.preview : '';
      return { preview };
    }
  }
}

export function renderCard(item) {
  cacheItem(item);
  const card = document.createElement('a');
  card.className = `item-card kind-${item.kind || 'other'}`;
  card.href = `/items/${item.id}`;
  const title = cardTitle(item);
  const emoji = item.metadata && item.metadata.emoji ? item.metadata.emoji + ' ' : '';
  const firstImg = (item.media || []).find(m => (m.media_type === 'image' || m.media_type === 'thumbnail') && m.available && !m.superseded_at);
  const thumb = firstImg ? `<img src="${getApiBase()}${escHtml(firstImg.serve_url)}" alt="${escHtml(firstImg.alt_text || '')}" class="item-card-thumb" loading="lazy">` : '';
  // A search result's preview holds the matched words, marked <<like this>>
  const found = inside(item, title);
  const preview = item.preview && item.preview.includes('<<') ? item.preview : found.preview;
  card.innerHTML = `
    <div class="item-card-header">
      <span class="kind-icon" aria-hidden="true">${kindIcon(item)}</span>
      <span class="item-title">${emoji}${escHtml(title)}</span>
      <span class="item-date">${relTime(item.created_at)}</span>
    </div>
    <div class="item-card-line">${escHtml(whoWhen(item))}</div>
    ${thumb}
    ${preview ? `<div class="item-preview">${escHtml(preview)}</div>` : ''}
    ${found.counts ? `<div class="item-card-counts">${escHtml(found.counts)}</div>` : ''}
    <div class="debug-only">
      <div style="margin-top:0.3rem">
        <span class="source-badge ${sourceClass(item.subtype)}">${sourceName(item.subtype)}</span>
        <span class="item-subtype">${escHtml(item.subtype)} · ${escHtml(item.kind)}</span>
      </div>
      <div class="item-meta">
        ${item.author_display !== '-' ? `<span>by ${escHtml(item.author_display)}</span>` : ''}
        ${item.actor_display !== '-' ? `<span>actor: ${escHtml(item.actor_display)}</span>` : ''}
        ${item.is_own_content ? '<span style="color:var(--ok)">own</span>' : ''}
        <span>id:${item.id}</span>
      </div>
    </div>
  `;
  return card;
}
