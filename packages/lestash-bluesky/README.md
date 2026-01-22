# lestash-bluesky

Bluesky (AT Protocol) source plugin for Le Stash.

## Features

- Authenticate with your Bluesky account
- Fetch all your posts and threads
- Store posts with rich metadata (facets, embeds, replies)
- Content-addressed storage using CIDs
- Support for AT Protocol repository exports

## Installation

This package is part of the Le Stash workspace and is installed automatically when you install `lestash`.

## Usage

### Authentication

```bash
lestash bluesky auth --handle your.handle.bsky.social
```

You'll be prompted for your password. Your credentials are stored securely in `~/.config/lestash/bluesky_credentials.json`.

### Syncing Posts

```bash
# Sync all your posts
lestash bluesky sync

# Or use the generic sync command
lestash sources sync bluesky
```

### Status Check

```bash
lestash bluesky status
```

## AT Protocol

This plugin uses the official [atproto](https://github.com/MarshalX/atproto) Python SDK to interact with Bluesky's AT Protocol APIs.

### Data Stored

For each post, the following data is stored:

- Post URI (unique identifier)
- Post text content
- Author information
- Creation timestamp
- Content hash (CID)
- Rich text features (mentions, links, hashtags)
- Embeds (images, videos, quotes)
- Reply relationships (threads)

## Configuration

Add to your `~/.config/lestash/config.toml`:

```toml
[sources.bluesky]
enabled = true
handle = "your.handle.bsky.social"
```
