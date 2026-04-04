/**
 * Gemini conversation DOM extractor.
 *
 * Selectors are isolated here for easy updating when Google changes the DOM.
 * Targets data-test-id attributes and custom element names (more stable than
 * obfuscated class names).
 */

const SELECTORS = {
  // Sidebar
  conversationLink: 'a[data-test-id="conversation"]',
  conversationTitle: '.conversation-title',
  activeConversation: 'a[data-test-id="conversation"][aria-current="true"]',
  pinnedIcon: 'mat-icon[data-mat-icon-name="push_pin"]',

  // Turn structure
  turnContainer: 'div.conversation-container',
  userQuery: 'user-query',
  userQueryText: '.query-text p.query-text-line',
  modelResponse: 'model-response',
  modelResponseMarkdown: '.markdown.markdown-main-panel',
  thinkingBlock: 'model-thoughts',
};

/**
 * Extract the currently visible conversation from the page.
 * Called from popup.js via chrome.scripting.executeScript.
 */
function extractGeminiConversation() {
  const url = window.location.href;
  const match = url.match(/\/app\/([a-f0-9]+)/);
  if (!match) return null;

  const conversationId = match[1];

  // Title: try active sidebar entry first, fall back to page title / first prompt
  let title = 'Untitled';
  const activeLink = document.querySelector(SELECTORS.activeConversation);
  if (activeLink) {
    const titleEl = activeLink.querySelector(SELECTORS.conversationTitle);
    if (titleEl) title = titleEl.textContent.trim();
  }

  // Pinned state
  const isPinned = activeLink
    ? !!activeLink.querySelector(SELECTORS.pinnedIcon)
    : false;

  // Extract turns
  const turnEls = document.querySelectorAll(SELECTORS.turnContainer);
  const turns = [];
  let firstPrompt = null;

  for (const turn of turnEls) {
    const turnId = turn.id || '';

    // User prompt
    const queryEl = turn.querySelector(SELECTORS.userQuery);
    if (queryEl) {
      const lines = queryEl.querySelectorAll(SELECTORS.userQueryText);
      if (lines.length) {
        const text = [...lines].map(p => p.textContent.trim()).join('\n');
        if (text) {
          turns.push({ turn_id: turnId, role: 'user', text });
          if (!firstPrompt) firstPrompt = text;
        }
      }
    }

    // Model response
    const responseEl = turn.querySelector(SELECTORS.modelResponse);
    if (responseEl) {
      const mdEl = responseEl.querySelector(SELECTORS.modelResponseMarkdown);
      if (mdEl) {
        const text = mdEl.innerText.trim();
        if (text) {
          const hasThinking = !!turn.querySelector(SELECTORS.thinkingBlock);
          turns.push({
            turn_id: turnId,
            role: 'model',
            text,
            ...(hasThinking ? { has_thinking: true } : {}),
          });
        }
      }
    }
  }

  // Fall back title to first prompt
  if (title === 'Untitled' && firstPrompt) {
    title = firstPrompt.slice(0, 80) + (firstPrompt.length > 80 ? '...' : '');
  }

  return {
    conversation_id: `c_${conversationId}`,
    title,
    is_pinned: isPinned,
    url,
    turns,
  };
}

/**
 * Extract the list of conversations from the sidebar.
 * Returns an array of { id, title, is_pinned, url }.
 */
function extractGeminiSidebar() {
  const links = document.querySelectorAll(SELECTORS.conversationLink);
  return [...links].map(link => {
    const href = link.getAttribute('href') || '';
    const match = href.match(/\/app\/([a-f0-9]+)/);
    const id = match ? `c_${match[1]}` : href;
    const titleEl = link.querySelector(SELECTORS.conversationTitle);
    const title = titleEl ? titleEl.textContent.trim() : 'Untitled';
    const isPinned = !!link.querySelector(SELECTORS.pinnedIcon);
    return {
      id,
      title,
      is_pinned: isPinned,
      url: `https://gemini.google.com${href}`,
    };
  });
}

// Listen for messages from popup/background
chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  if (msg.action === 'extractGeminiConversation') {
    sendResponse(extractGeminiConversation());
  } else if (msg.action === 'extractGeminiSidebar') {
    sendResponse(extractGeminiSidebar());
  }
});

// Track SPA navigation — Gemini is an Angular SPA that changes conversations
// without full page reloads. Notify the extension when the URL changes.
let lastUrl = window.location.href;
const urlObserver = new MutationObserver(() => {
  if (window.location.href !== lastUrl) {
    lastUrl = window.location.href;
    chrome.runtime.sendMessage({ action: 'geminiNavigated', url: lastUrl });
  }
});
urlObserver.observe(document.body, { childList: true, subtree: true });
