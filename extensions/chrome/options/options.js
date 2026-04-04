const endpointInput = document.getElementById('endpoint');
const tokenInput = document.getElementById('token');
const saveBtn = document.getElementById('save');
const statusEl = document.getElementById('status');

chrome.storage.sync.get(['apiEndpoint', 'apiToken'], (data) => {
  if (data.apiEndpoint) endpointInput.value = data.apiEndpoint;
  if (data.apiToken) tokenInput.value = data.apiToken;
});

saveBtn.addEventListener('click', () => {
  const endpoint = endpointInput.value.trim().replace(/\/+$/, '');
  const token = tokenInput.value.trim();

  if (endpoint && !endpoint.match(/^https?:\/\//)) {
    statusEl.textContent = 'Endpoint must start with http:// or https://';
    statusEl.className = 'status err';
    return;
  }

  chrome.storage.sync.set({ apiEndpoint: endpoint, apiToken: token }, () => {
    statusEl.textContent = 'Saved';
    statusEl.className = 'status ok';
    if (endpoint) {
      fetch(`${endpoint}/api/health`)
        .then(r => r.ok ? r.json() : Promise.reject(new Error(`HTTP ${r.status}`)))
        .then(data => {
          statusEl.textContent = `Saved — connected (${data.item_count ?? '?'} items)`;
        })
        .catch(err => {
          statusEl.textContent = `Saved — connection failed: ${err.message}`;
          statusEl.className = 'status err';
        });
    }
  });
});
