importScripts('lib/api.js');

// Context menu setup
chrome.runtime.onInstalled.addListener(() => {
  chrome.contextMenus.create({
    id: 'save-to-lestash',
    title: 'Save to Le Stash',
    contexts: ['selection', 'page'],
  });
});

// Context menu handler
chrome.contextMenus.onClicked.addListener(async (info, tab) => {
  if (info.menuItemId !== 'save-to-lestash') return;

  try {
    const item = await LeStashAPI.createItem({
      source_type: 'web',
      source_id: tab.url,
      url: tab.url,
      title: tab.title || 'Untitled',
      content: info.selectionText || '',
      is_own_content: false,
    });
    showBadge('OK', '#4ecca3');
  } catch (err) {
    console.error('Le Stash save failed:', err);
    showBadge('ERR', '#e94560');
  }
});

// Message handler for popup and content scripts
chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  handleMessage(msg).then(sendResponse).catch(err => {
    sendResponse({ success: false, error: err.message });
  });
  return true; // keep channel open for async response
});

async function handleMessage(msg) {
  switch (msg.action) {
    case 'checkConfig': {
      const health = await LeStashAPI.checkHealth();
      return { success: true, ...health };
    }

    case 'getCollections': {
      const collections = await LeStashAPI.getCollections();
      return { success: true, collections };
    }

    case 'saveItem': {
      const item = await LeStashAPI.createItem(msg.data);
      if (msg.tags && msg.tags.length) {
        await LeStashAPI.addTags(item.id, msg.tags);
      }
      if (msg.collectionId) {
        await LeStashAPI.addToCollection(msg.collectionId, item.id);
      }
      return { success: true, item };
    }

    case 'saveGeminiConversation': {
      const item = await LeStashAPI.createItem(msg.data);
      if (msg.tags && msg.tags.length) {
        await LeStashAPI.addTags(item.id, msg.tags);
      }
      if (msg.collectionId) {
        await LeStashAPI.addToCollection(msg.collectionId, item.id);
      }
      return { success: true, item };
    }

    case 'saveBulkGemini': {
      const results = { saved: 0, failed: 0, errors: [] };
      for (const conv of msg.items) {
        try {
          await LeStashAPI.createItem(conv);
          results.saved++;
        } catch (err) {
          results.failed++;
          results.errors.push(`${conv.title || conv.source_id}: ${err.message}`);
        }
      }
      return { success: true, ...results };
    }

    default:
      throw new Error(`Unknown action: ${msg.action}`);
  }
}

function showBadge(text, color) {
  chrome.action.setBadgeText({ text });
  chrome.action.setBadgeBackgroundColor({ color });
  setTimeout(() => chrome.action.setBadgeText({ text: '' }), 3000);
}
