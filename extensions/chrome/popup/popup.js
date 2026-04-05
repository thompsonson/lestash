const $ = (sel) => document.querySelector(sel);
const dot = $('#dot');
const setupEl = $('#setup');
const formEl = $('#form');
const statusEl = $('#status');
const geminiSection = $('#gemini-section');
const geminiInfo = $('#gemini-info');
const geminiStatus = $('#gemini-status');
const geminiProgress = $('#gemini-progress');

let tags = [];
let activeTab = null;
let isGeminiPage = false;

// ── Init ──

(async () => {
  // Get active tab
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  activeTab = tab;
  isGeminiPage = tab.url && (
    tab.url.startsWith('https://gemini.google.com/app/') ||
    tab.url.startsWith('https://gemini.google.com/share/')
  );
  const isGeminiShared = tab.url && tab.url.startsWith('https://gemini.google.com/share/');

  // Check config
  const config = await LeStashAPI.getConfig();
  if (!config.endpoint) {
    setupEl.style.display = '';
    return;
  }

  // Verify connection
  try {
    await LeStashAPI.checkHealth();
    dot.classList.add('ok');
  } catch {
    dot.classList.add('err');
  }

  // Show form
  formEl.style.display = '';
  $('#url').value = tab.url || '';
  $('#title').value = tab.title || '';

  // Auto-set source type for known sites
  if (isGeminiPage) {
    $('#source-type').value = 'web'; // Gemini conversations use the dedicated section
  }

  // Try to get selected text
  try {
    const [result] = await chrome.scripting.executeScript({
      target: { tabId: tab.id },
      func: () => window.getSelection().toString(),
    });
    if (result && result.result) {
      $('#content').value = result.result;
    }
  } catch { /* no access to page, that's ok */ }

  // Load collections
  loadCollections();

  // Show Gemini section if on gemini.google.com
  if (isGeminiPage) {
    geminiSection.style.display = '';
    if (isGeminiShared) {
      $('#save-all-gemini').style.display = 'none';
    }
    loadGeminiInfo();
  }
})();

// ── Collections ──

async function loadCollections() {
  try {
    const resp = await chrome.runtime.sendMessage({ action: 'getCollections' });
    if (resp.success && resp.collections) {
      for (const sel of ['#collection', '#gemini-collection']) {
        const el = $(sel);
        if (!el) continue;
        resp.collections.forEach(c => {
          const opt = document.createElement('option');
          opt.value = c.id;
          opt.textContent = `${c.name} (${c.item_count})`;
          el.appendChild(opt);
        });
      }
    }
  } catch { /* collections optional */ }
}

// ── Tags ──

$('#tags-input').addEventListener('keydown', (e) => {
  if (e.key === 'Enter' || e.key === ',') {
    e.preventDefault();
    const val = e.target.value.trim().replace(/,/g, '');
    if (val && !tags.includes(val)) {
      tags.push(val);
      renderTags();
    }
    e.target.value = '';
  }
});

function renderTags() {
  const row = $('#tags-row');
  row.innerHTML = '';
  tags.forEach((tag, i) => {
    const pill = document.createElement('span');
    pill.className = 'tag-pill';
    pill.innerHTML = `${tag} <span class="remove" data-idx="${i}">&times;</span>`;
    row.appendChild(pill);
  });
  row.querySelectorAll('.remove').forEach(el => {
    el.addEventListener('click', () => {
      tags.splice(parseInt(el.dataset.idx), 1);
      renderTags();
    });
  });
}

// ── Save (universal capture) ──

formEl.addEventListener('submit', async (e) => {
  e.preventDefault();
  const btn = $('#save-btn');
  btn.disabled = true;
  statusEl.textContent = 'Saving...';
  statusEl.className = 'status';

  try {
    const data = {
      source_type: $('#source-type').value,
      source_id: $('#url').value,
      url: $('#url').value,
      title: $('#title').value || 'Untitled',
      content: $('#content').value,
      is_own_content: false,
    };
    const collectionId = $('#collection').value || null;

    const resp = await chrome.runtime.sendMessage({
      action: 'saveItem',
      data,
      tags,
      collectionId: collectionId ? parseInt(collectionId) : null,
    });

    if (resp.success) {
      statusEl.textContent = `Saved (item #${resp.item.id})`;
      statusEl.className = 'status ok';
    } else {
      throw new Error(resp.error);
    }
  } catch (err) {
    statusEl.textContent = err.message;
    statusEl.className = 'status err';
  } finally {
    btn.disabled = false;
  }
});

// ── Options link ──

$('#open-options').addEventListener('click', () => {
  chrome.runtime.openOptionsPage();
});

// ── Gemini ──

async function loadGeminiInfo() {
  try {
    const [result] = await chrome.scripting.executeScript({
      target: { tabId: activeTab.id },
      func: () => {
        if (typeof extractGeminiConversation === 'function') {
          const conv = extractGeminiConversation();
          return conv ? {
            title: conv.title,
            turn_count: conv.turns.length,
            conversation_id: conv.conversation_id,
          } : null;
        }
        return null;
      },
    });
    if (result && result.result) {
      const info = result.result;
      geminiInfo.textContent = `${info.title} - ${info.turn_count} turns`;
    } else {
      geminiInfo.textContent = 'Conversation detected — click Save to extract';
    }
  } catch {
    geminiInfo.textContent = 'Ready to extract conversation';
  }
}

$('#save-gemini').addEventListener('click', async () => {
  const btn = $('#save-gemini');
  btn.disabled = true;
  geminiStatus.textContent = 'Extracting...';
  geminiStatus.className = 'status';

  try {
    const [result] = await chrome.scripting.executeScript({
      target: { tabId: activeTab.id },
      func: () => {
        if (typeof extractGeminiConversation === 'function') {
          return extractGeminiConversation();
        }
        return null;
      },
    });

    if (!result || !result.result) {
      throw new Error('Could not extract conversation from page');
    }

    const conv = result.result;
    const collectionId = $('#gemini-collection').value || null;

    const data = {
      conversation_id: conv.conversation_id,
      title: conv.title,
      url: conv.url,
      turns: conv.turns,
      tags: ['gemini', 'conversation'],
      collection_id: collectionId ? parseInt(collectionId) : null,
    };

    const resp = await chrome.runtime.sendMessage({
      action: 'saveGeminiConversation',
      data,
    });

    if (resp.success) {
      const r = resp.item;
      const label = r.items_added ? `${r.items_added} items` : `item #${r.id}`;
      geminiStatus.textContent = `Saved (${label})`;
      geminiStatus.className = 'status ok';
    } else {
      throw new Error(resp.error);
    }
  } catch (err) {
    geminiStatus.textContent = err.message;
    geminiStatus.className = 'status err';
  } finally {
    btn.disabled = false;
  }
});

$('#save-all-gemini').addEventListener('click', async () => {
  const btn = $('#save-all-gemini');
  btn.disabled = true;
  geminiStatus.textContent = 'Scanning sidebar...';
  geminiStatus.className = 'status';

  try {
    // Get sidebar conversation list
    const [result] = await chrome.scripting.executeScript({
      target: { tabId: activeTab.id },
      func: () => {
        if (typeof extractGeminiSidebar === 'function') {
          return extractGeminiSidebar();
        }
        return null;
      },
    });

    if (!result || !result.result || !result.result.length) {
      throw new Error('No conversations found in sidebar');
    }

    const convList = result.result;
    geminiProgress.textContent = `Found ${convList.length} conversations`;

    // Navigate to each and extract
    const items = [];
    for (let i = 0; i < convList.length; i++) {
      geminiProgress.textContent = `Extracting ${i + 1}/${convList.length}: ${convList[i].title || 'Untitled'}`;

      // Navigate to conversation
      await chrome.tabs.update(activeTab.id, { url: convList[i].url });
      // Wait for page to load and content script to be ready
      await waitForTabLoad(activeTab.id);
      await new Promise(r => setTimeout(r, 1500));

      // Extract via content script message (persists across SPA navigations)
      try {
        const conv = await chrome.tabs.sendMessage(activeTab.id, {
          action: 'extractGeminiConversation',
        });

        if (conv) {
          items.push({
            source_type: 'gemini',
            source_id: conv.conversation_id,
            title: conv.title,
            content: conv.turns
              .map(t => `**${t.role === 'user' ? 'You' : 'Gemini'}:** ${t.text}`)
              .join('\n\n---\n\n'),
            is_own_content: false,
            metadata: {
              source: 'extension',
              conversation_id: conv.conversation_id,
              turn_count: conv.turns.length,
              is_pinned: conv.is_pinned,
              url: conv.url,
              turns: conv.turns,
            },
          });
        }
      } catch { /* skip failed extraction */ }
    }

    // Bulk save
    geminiProgress.textContent = `Saving ${items.length} conversations...`;
    const resp = await chrome.runtime.sendMessage({ action: 'saveBulkGemini', items });

    if (resp.success) {
      geminiStatus.textContent = `Done: ${resp.saved} saved, ${resp.failed} failed`;
      geminiStatus.className = resp.failed ? 'status warn' : 'status ok';
    } else {
      throw new Error(resp.error);
    }
  } catch (err) {
    geminiStatus.textContent = err.message;
    geminiStatus.className = 'status err';
  } finally {
    btn.disabled = false;
    geminiProgress.textContent = '';
  }
});

// ── Helpers ──

function waitForTabLoad(tabId) {
  return new Promise((resolve) => {
    function listener(updatedTabId, info) {
      if (updatedTabId === tabId && info.status === 'complete') {
        chrome.tabs.onUpdated.removeListener(listener);
        resolve();
      }
    }
    chrome.tabs.onUpdated.addListener(listener);
    // Safety timeout — don't wait forever
    setTimeout(() => {
      chrome.tabs.onUpdated.removeListener(listener);
      resolve();
    }, 10000);
  });
}
