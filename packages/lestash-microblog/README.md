# lestash-microblog

Micro.blog source plugin for Le Stash. Syncs your Micro.blog posts to your local knowledge base.

## Installation

```bash
uv add lestash-microblog
```

## Usage

### Authentication

First, get an API token from [Micro.blog Account](https://micro.blog/account/apps).

Then authenticate:

```bash
lestash microblog auth --token YOUR_TOKEN
```

### Sync Posts

```bash
lestash microblog sync
```

### Check Status

```bash
lestash microblog status
```

## Features

- Fetches posts via Micropub API
- Supports multiple blogs (destinations)
- Full-text searchable content
- Duplicate handling with upsert
