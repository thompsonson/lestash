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

  // Deep Research widgets
  deepResearchWidget: 'deep-research-confirmation-widget',
  deepResearchTitle: '[data-test-id="title"]',
  deepResearchSteps: '.research-step-description',
};

/**
 * Extract the currently visible conversation from the page.
 * Called from popup.js via chrome.scripting.executeScript.
 */
function extractGeminiConversation() {
  const url = window.location.href;
  const appMatch = url.match(/\/app\/([a-f0-9]+)/);
  const shareMatch = url.match(/\/share\/([a-f0-9]+)/);
  const match = appMatch || shareMatch;
  if (!match) return null;

  const conversationId = match[1];
  const isShared = !!shareMatch;

  // Title: try active sidebar entry (not available on shared pages)
  let title = 'Untitled';
  let isPinned = false;

  if (!isShared) {
    const activeLink = document.querySelector(SELECTORS.activeConversation);
    if (activeLink) {
      const titleEl = activeLink.querySelector(SELECTORS.conversationTitle);
      if (titleEl) title = titleEl.textContent.trim();
      isPinned = !!activeLink.querySelector(SELECTORS.pinnedIcon);
    }
  }

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
      let text = '';
      let isDeepResearch = false;

      const mdEl = responseEl.querySelector(SELECTORS.modelResponseMarkdown);
      if (mdEl) {
        // Check for Deep Research widget inside the markdown
        const drWidget = mdEl.querySelector(SELECTORS.deepResearchWidget);
        if (drWidget) {
          isDeepResearch = true;
          const drTitle = drWidget.querySelector(SELECTORS.deepResearchTitle);
          const drSteps = drWidget.querySelectorAll(SELECTORS.deepResearchSteps);
          const parts = [];
          if (drTitle) parts.push(`[Deep Research] ${drTitle.innerText.trim()}`);
          // Get any text before the widget
          const preText = mdEl.innerText.split(drTitle?.innerText || '')[0]?.trim();
          if (preText) parts.unshift(preText);
          if (drSteps.length) {
            parts.push('Research plan:');
            drSteps.forEach((s, i) => parts.push(`${i + 1}. ${s.innerText.trim()}`));
          }
          text = parts.join('\n\n');
        } else {
          text = mdEl.innerText.trim();
        }
      }

      if (text) {
        const hasThinking = !!turn.querySelector(SELECTORS.thinkingBlock);
        turns.push({
          turn_id: turnId,
          role: 'model',
          text,
          ...(hasThinking ? { has_thinking: true } : {}),
          ...(isDeepResearch ? { is_deep_research: true } : {}),
        });
      }
    }
  }

  // Fall back title to first prompt
  if (title === 'Untitled' && firstPrompt) {
    title = firstPrompt.slice(0, 80) + (firstPrompt.length > 80 ? '...' : '');
  }

  return {
    conversation_id: isShared ? `share_${conversationId}` : `c_${conversationId}`,
    title,
    is_pinned: isPinned,
    is_shared: isShared,
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
    try {
      chrome.runtime.sendMessage({ action: 'geminiNavigated', url: lastUrl });
    } catch {
      // Extension was reloaded/updated — old content script is orphaned.
      urlObserver.disconnect();
    }
  }
});
urlObserver.observe(document.body, { childList: true, subtree: true });
