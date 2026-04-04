/**
 * LeStash API client for Chrome extension.
 * Uses importScripts-compatible global namespace (no ES modules).
 */
const LeStashAPI = {
  async getConfig() {
    return new Promise((resolve) => {
      chrome.storage.sync.get(['apiEndpoint', 'apiToken'], (data) => {
        resolve({
          endpoint: data.apiEndpoint || '',
          token: data.apiToken || '',
        });
      });
    });
  },

  async _fetch(path, options = {}) {
    const { endpoint, token } = await this.getConfig();
    if (!endpoint) throw new Error('API endpoint not configured — check extension options');

    const url = `${endpoint}/api${path}`;
    const headers = { 'Content-Type': 'application/json' };
    if (token) headers['Authorization'] = `Bearer ${token}`;

    const response = await fetch(url, { ...options, headers: { ...headers, ...options.headers } });
    if (!response.ok) {
      const body = await response.text().catch(() => '');
      throw new Error(`HTTP ${response.status}: ${body.slice(0, 200)}`);
    }
    if (response.status === 204) return null;
    return response.json();
  },

  async createItem(data) {
    return this._fetch('/items', {
      method: 'POST',
      body: JSON.stringify(data),
    });
  },

  async addTag(itemId, name) {
    return this._fetch(`/items/${itemId}/tags`, {
      method: 'POST',
      body: JSON.stringify({ name }),
    });
  },

  async addTags(itemId, tagNames) {
    const results = [];
    for (const name of tagNames) {
      if (name.trim()) {
        results.push(await this.addTag(itemId, name.trim()));
      }
    }
    return results;
  },

  async getCollections() {
    return this._fetch('/collections');
  },

  async addToCollection(collectionId, itemId, note) {
    const body = { item_id: itemId };
    if (note) body.note = note;
    return this._fetch(`/collections/${collectionId}/items`, {
      method: 'POST',
      body: JSON.stringify(body),
    });
  },

  async checkHealth() {
    return this._fetch('/health');
  },
};
