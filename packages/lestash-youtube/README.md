# lestash-youtube

YouTube Data API v3 source plugin for Le Stash.

## Features

- OAuth 2.0 authentication with Google
- Sync liked videos with full metadata
- Attempt to sync watch history (API access may be restricted)
- Sync channel subscriptions
- Store video metadata (duration, views, likes, tags, thumbnails)

## Installation

This package is part of the Le Stash workspace and is installed automatically when you install `lestash`.

## Setup

Before using this plugin, you need to set up OAuth credentials in Google Cloud Console.

### 1. Create Google Cloud Project

1. Go to [Google Cloud Console](https://console.cloud.google.com/)
2. Create a new project or select an existing one
3. Note your project name for later

### 2. Enable YouTube Data API

1. Go to [APIs & Services > Library](https://console.cloud.google.com/apis/library)
2. Search for "YouTube Data API v3"
3. Click **Enable**

### 3. Configure OAuth Consent Screen

1. Go to [APIs & Services > OAuth consent screen](https://console.cloud.google.com/apis/credentials/consent)
2. Select **External** (or Internal if using Google Workspace)
3. Fill in the required fields:
   - App name: "Le Stash YouTube" (or your preferred name)
   - User support email: Your email
   - Developer contact: Your email
4. Click **Save and Continue**
5. Add scopes: Click **Add or Remove Scopes**
   - Add: `https://www.googleapis.com/auth/youtube.readonly`
6. Save and continue through the remaining steps

### 4. Create OAuth Credentials

1. Go to [APIs & Services > Credentials](https://console.cloud.google.com/apis/credentials)
2. Click **Create Credentials** > **OAuth client ID**
3. Application type: **Desktop app**
4. Name: "Le Stash" (or your preferred name)
5. Click **Create**
6. Click **Download JSON**
7. Save the file as:
   ```
   ~/.config/lestash/youtube_client_secrets.json
   ```

### 5. Authenticate

```bash
lestash youtube auth
```

A browser window will open for Google sign-in. Grant access to your YouTube data.

## Usage

### Check Status

```bash
lestash youtube status
```

Shows authentication status and connected channel information.

### Sync Data

```bash
# Sync liked videos and attempt watch history
lestash youtube sync

# Sync only liked videos (skip history)
lestash youtube sync --no-history

# Include subscriptions
lestash youtube sync --subscriptions

# Sync everything
lestash youtube sync --subscriptions
```

### Preview Data

```bash
# Preview liked videos without syncing
lestash youtube likes
lestash youtube likes --limit 50

# Check if watch history is accessible
lestash youtube history
```

## Data Stored

### Liked Videos

For each liked video, the following data is stored:

| Field | Description |
|-------|-------------|
| `title` | Video title |
| `content` | Video description |
| `author` | Channel name |
| `url` | YouTube watch URL |
| `created_at` | Video publish date |
| `metadata.duration_seconds` | Duration in seconds |
| `metadata.view_count` | Number of views |
| `metadata.like_count` | Number of likes |
| `metadata.channel_id` | YouTube channel ID |
| `metadata.tags` | Video tags |
| `metadata.thumbnail_url` | Best available thumbnail |
| `metadata.definition` | Video quality (hd/sd) |

### Watch History

Watch history data includes the same fields plus:

| Field | Description |
|-------|-------------|
| `metadata.watched_at` | When you watched the video |

**Note:** YouTube API access to watch history is restricted for most users. If the history command returns empty results, use [Google Takeout](https://takeout.google.com) to export your watch history.

### Subscriptions

For each subscription:

| Field | Description |
|-------|-------------|
| `title` | Channel name |
| `content` | Channel description |
| `url` | YouTube channel URL |
| `created_at` | When you subscribed |
| `metadata.channel_id` | YouTube channel ID |
| `metadata.thumbnail_url` | Channel thumbnail |

## Watch History Limitations

YouTube has deprecated direct API access to watch history for privacy reasons. When you run `lestash youtube sync`, watch history may return empty results.

**Alternative: Google Takeout**

1. Go to [takeout.google.com](https://takeout.google.com)
2. Click **Deselect all**
3. Select **YouTube and YouTube Music**
4. Click **Multiple formats**
5. Set **History** to **JSON** format
6. Click **Next step** and create export
7. Download and extract the archive

The `watch-history.json` file can be imported in a future update.

## Configuration

Add to your `~/.config/lestash/config.toml`:

```toml
[sources.youtube]
enabled = true
sync_likes = true
sync_history = true
sync_subscriptions = false
```

## API Quotas

The YouTube Data API has daily quota limits (default: 10,000 units/day). Each operation costs units:

| Operation | Cost |
|-----------|------|
| videos.list (liked videos) | 1 unit per request |
| playlistItems.list | 1 unit per request |
| subscriptions.list | 1 unit per request |
| channels.list | 1 unit per request |

A typical sync of 100 liked videos uses approximately 2-3 quota units.

## Troubleshooting

### "OAuth client secrets not found"

Ensure you've downloaded the credentials JSON and saved it to:
```
~/.config/lestash/youtube_client_secrets.json
```

### "Watch history is empty"

This is expected behavior. YouTube restricts API access to watch history. Use Google Takeout instead.

### "Access blocked: This app's request is invalid"

Your OAuth consent screen may not be properly configured. Ensure:
1. The YouTube Data API v3 is enabled
2. The `youtube.readonly` scope is added
3. Your email is added as a test user (if app is in testing mode)

### "Quota exceeded"

You've hit the daily API quota limit. Wait 24 hours or request a quota increase in Google Cloud Console.
