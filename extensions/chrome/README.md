# Le Stash Chrome Extension

Save web pages and Gemini conversations to Le Stash with one click.

## Features

- **Quick capture** — click the extension icon on any page to save URL, title, selected text, and your notes
- **Context menu** — right-click "Save to LeStash" on any page or selection
- **Tags** — add tags before saving
- **Collections** — add items to collections directly from the popup
- **Gemini importer** — extract conversations from `gemini.google.com` via DOM scraping (no API needed)
- **Bulk Gemini import** — save all conversations from the sidebar in one click

## Install

This is a personal-use extension loaded as an unpacked extension (no Chrome Web Store).

1. Open `chrome://extensions/`
2. Enable **Developer mode** (top right toggle)
3. Click **Load unpacked**
4. Select the `extensions/chrome/` directory

## Setup

1. Click the extension icon → the popup will show "API endpoint not configured"
2. Click **Open settings** (or right-click the extension icon → Options)
3. Enter your Le Stash server URL, e.g. `https://pop-mini.monkey-ladon.ts.net:8444`
4. Click **Save** — you should see a green "connected" message with your item count

## Usage

### Universal page capture

1. Navigate to any web page
2. Click the extension icon
3. URL and title are auto-filled; selected text appears in the content field
4. Choose a source type (web, article, reference, note)
5. Add tags (type and press Enter)
6. Optionally select a collection
7. Click **Save**

Alternatively, right-click anywhere on a page → **Save to Le Stash** for a quick save without the popup.

### Gemini conversations

1. Navigate to a conversation at `gemini.google.com/app/*`
2. Click the extension icon
3. The **Gemini Conversation** section appears below the capture form
4. Click **Save This Conversation** to save the current conversation
5. Click **Save All** to iterate through the sidebar and save every conversation

Conversations are saved with `source_type: "gemini"`, matching the format used by the Google Takeout import. Re-saving the same conversation updates it (upsert on `source_id`).

## Architecture

```
popup/content script  →  background.js (service worker)  →  Le Stash API
                              ↑
                         lib/api.js (shared client)
```

- **No build tools** — vanilla JS, no npm, no bundler
- **MV3** (Manifest V3) with service worker
- All API communication goes through the background service worker
- Gemini DOM selectors are isolated in a config object at the top of `content-scripts/gemini.js` for easy updating

## File structure

```
extensions/chrome/
├── manifest.json              # MV3 manifest
├── background.js              # Service worker: API calls, context menu
├── lib/
│   └── api.js                 # Shared Le Stash API client
├── popup/
│   ├── popup.html             # Capture form + Gemini section
│   ├── popup.js               # Auto-fill, tags, collections, save logic
│   └── popup.css              # Dark theme matching Le Stash app
├── content-scripts/
│   └── gemini.js              # Gemini DOM extraction
├── options/
│   ├── options.html           # API endpoint configuration
│   └── options.js             # Settings persistence
└── icons/                     # Extension icons
```

## API endpoints used

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/api/health` | Connection check |
| `POST` | `/api/items` | Create/upsert item |
| `POST` | `/api/items/{id}/tags` | Add tag to item |
| `GET` | `/api/collections` | List collections for picker |
| `POST` | `/api/collections/{id}/items` | Add item to collection |

## Known limitations

- **Gemini timestamps** — not available in the DOM; `created_at` is null, `updated_at` is capture time
- **Gemini thinking blocks** — only extracted if already expanded by the user
- **Gemini sidebar** — virtualized; "Save All" only captures conversations visible in the sidebar (scroll down to load more before clicking)
- **DOM stability** — Gemini selectors target `data-test-id` attributes and custom element names, which are more stable than class names but may break on major UI updates. Update the `SELECTORS` object in `content-scripts/gemini.js` if extraction stops working.
