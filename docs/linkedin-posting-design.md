# Research: Posting to LinkedIn via API

*Researched 2026-04-01*

## Context

LeStash currently uses LinkedIn's **DMA Portability API** (`r_dma_portability_self_serve` scope) for read-only data retrieval. This document covers how to add posting capability — from CLI, web UI, and the Tauri app — for all three content types: text, images, and link shares.

## Current State

- **API**: DMA Portability API — read-only, EU data export under Digital Markets Act
- **Scopes**: `r_dma_portability_self_serve` (personal) / `r_dma_portability_3rd_party` (apps)
- **Auth**: 3-legged OAuth 2.0 with local callback server (`api.py`)
- **No write capability exists** in the codebase

## What's Required

### API Product

The **Posts API** (under Community Management API) is LinkedIn's current recommended API for creating posts. It replaced the legacy UGC Posts API.

- **Endpoint**: `POST https://api.linkedin.com/rest/posts`
- **Scope**: `w_member_social` (write member social content)
- **Product**: "Share on LinkedIn" — **self-service** on the LinkedIn Developer Portal (no manual review needed)
- **Docs**: https://learn.microsoft.com/en-us/linkedin/marketing/community-management/shares/posts-api

You can add "Share on LinkedIn" to your **existing** LinkedIn developer app and request both DMA + write scopes in a single OAuth flow.

### Getting the Author URN

Posts require an `author` field with your person URN. Two ways to get it:

1. **`GET /v2/me`** — returns `id` field, construct `urn:li:person:{id}` (needs `r_liteprofile` scope)
2. **`GET /v2/userinfo`** — returns `sub` field (needs `openid` + `profile` scopes)
3. **Already available**: existing items have `metadata.raw.owner` = `urn:li:person:xu59iSkkD6`

### Required Headers (all requests)

| Header | Value |
|--------|-------|
| `Authorization` | `Bearer {access_token}` |
| `Linkedin-Version` | `YYYYMM` format (e.g. `202604`) |
| `X-Restli-Protocol-Version` | `2.0.0` |
| `Content-Type` | `application/json` |

### Response

**201 Created** — Post URN returned in `x-restli-id` response header. No JSON body.

---

## Content Type 1: Text Posts

Simplest — single API call.

```json
POST /rest/posts

{
  "author": "urn:li:person:xu59iSkkD6",
  "commentary": "Your post text here (up to 3,000 chars)",
  "visibility": "PUBLIC",
  "distribution": {
    "feedDistribution": "MAIN_FEED",
    "targetEntities": [],
    "thirdPartyDistributionChannels": []
  },
  "lifecycleState": "PUBLISHED",
  "isReshareDisabledByAuthor": false
}
```

**Visibility options**: `PUBLIC`, `CONNECTIONS` (connections only)

**Mentions**: `Hello @[Company Name](urn:li:organization:123)` in `commentary`

**Hashtags**: Just use `#hashtag` syntax in text

---

## Content Type 2: Image Posts

Three-step flow: initialize upload → upload binary → create post with image URN.

### Step 1: Initialize upload

```json
POST /rest/images?action=initializeUpload

{
  "initializeUploadRequest": {
    "owner": "urn:li:person:xu59iSkkD6"
  }
}
```

**Response:**
```json
{
  "value": {
    "uploadUrl": "https://www.linkedin.com/dms-uploads/...",
    "uploadUrlExpiresAt": 1650567510704,
    "image": "urn:li:image:C4E10AQFoyyAjHPMQuQ"
  }
}
```

### Step 2: Upload image binary

```
PUT {uploadUrl}
Content-Type: application/octet-stream
Body: <raw image bytes>
```

**Supported formats**: JPG, PNG, GIF (up to 250 frames)
**Max size**: ~36M pixels (approx 6000x6000)

### Step 3: Create post with image

```json
POST /rest/posts

{
  "author": "urn:li:person:xu59iSkkD6",
  "commentary": "Post text with image",
  "visibility": "PUBLIC",
  "distribution": {
    "feedDistribution": "MAIN_FEED",
    "targetEntities": [],
    "thirdPartyDistributionChannels": []
  },
  "content": {
    "media": {
      "id": "urn:li:image:C4E10AQFoyyAjHPMQuQ",
      "altText": "Description for accessibility"
    }
  },
  "lifecycleState": "PUBLISHED",
  "isReshareDisabledByAuthor": false
}
```

---

## Content Type 3: Article / Link Shares

**Important**: LinkedIn does NOT auto-generate link previews from URLs in text. You must provide title, description, and optionally a thumbnail.

```json
POST /rest/posts

{
  "author": "urn:li:person:xu59iSkkD6",
  "commentary": "Check out this article!",
  "visibility": "PUBLIC",
  "distribution": {
    "feedDistribution": "MAIN_FEED",
    "targetEntities": [],
    "thirdPartyDistributionChannels": []
  },
  "content": {
    "article": {
      "source": "https://example.com/article",
      "title": "Article Title",
      "description": "Article description text"
    }
  },
  "lifecycleState": "PUBLISHED",
  "isReshareDisabledByAuthor": false
}
```

To add a thumbnail: upload an image (same 3-step flow as above), then add `"thumbnail": "urn:li:image:..."` to the article object.

---

## Error Handling

| Code | Meaning | Fix |
|------|---------|-----|
| 401 | Missing/invalid token | Re-authenticate |
| 403 | Missing `w_member_social` scope | Re-auth with write scope |
| 400 `MISSING_FIELD` | Required field absent | Include `author`, `visibility`, `distribution`, `lifecycleState` |
| 400 `FIELD_LENGTH_TOO_LONG` | Text > 3,000 chars | Truncate |
| 429 | Rate limited | Exponential backoff |

## Rate Limits

LinkedIn doesn't publish hard post limits. Opaque throttling with 429 responses. A few posts/day for personal use is fine.

---

## Implementation Overview

### Auth Changes (`api.py`)

Add write scope alongside existing DMA scope:

```python
SCOPE_WRITE = "w_member_social"

# In authorize():
scope = f"{SCOPE_SELF_SERVE} {SCOPE_WRITE}"
```

Both scopes can be requested in one OAuth flow. User sees a consent screen for both.

### API Client (`api.py`)

New methods on `LinkedInAPI`:

```python
def get_member_urn(self) -> str:
    """Get authenticated member's person URN."""
    # GET /v2/userinfo or use stored URN from existing items

def create_post(self, text, visibility="PUBLIC", image_path=None, article_url=None, article_title=None) -> str:
    """Create a LinkedIn post. Returns post URN."""
    # Build body based on content type
    # Handle image upload if image_path provided
    # Handle article content if article_url provided
```

### CLI Command (`source.py`)

```
lestash linkedin post "Quick text post"
lestash linkedin post --file draft.md
lestash linkedin post --file draft.md --image photo.jpg
echo "text" | lestash linkedin post --stdin
```

### API Endpoint (server `routes/`)

```
POST /api/linkedin/post
Body: { "text": "...", "visibility": "PUBLIC", "image_path": "...", "article_url": "..." }
```

The web UI and Tauri app would call this endpoint. For image uploads from the browser, the endpoint would accept multipart form data.

### Web UI / Tauri App

A compose dialog accessible from the UI:
- Text area for post content (with char counter, max 3,000)
- Optional image attachment (file picker)
- Optional link share (URL input with title/description fields)
- Visibility selector (Public / Connections only)
- Post button

---

## Setup Steps

1. Go to https://www.linkedin.com/developers/ → your existing app
2. Under **Products**, request **"Share on LinkedIn"** (self-service approval)
3. Re-authenticate: `lestash linkedin auth` (will request the new `w_member_social` scope)
4. Verify: the new token includes write permissions

---

## References

- [Posts API docs](https://learn.microsoft.com/en-us/linkedin/marketing/community-management/shares/posts-api)
- [Images API docs](https://learn.microsoft.com/en-us/linkedin/marketing/community-management/shares/images-api)
- [Profile API docs](https://learn.microsoft.com/en-us/linkedin/shared/integrations/people/profile-api)
- [LinkedIn URNs and IDs](https://learn.microsoft.com/en-us/linkedin/shared/api-guide/concepts/urns)
- [Error handling](https://learn.microsoft.com/en-us/linkedin/shared/api-guide/concepts/error-handling)
- [Rate limits](https://learn.microsoft.com/en-us/linkedin/shared/api-guide/concepts/rate-limits)
