# lestash-linkedin

LinkedIn source plugin for Le Stash using the DMA Portability API.

## Requirements

- **EU/EEA Location**: The LinkedIn DMA Portability API is only available to members located in the European Economic Area and Switzerland.
- **LinkedIn Developer App**: You need to create an app on the [LinkedIn Developer Portal](https://www.linkedin.com/developers/).

## Setup

### 1. Create LinkedIn App

1. Go to [LinkedIn Developer Portal](https://www.linkedin.com/developers/)
2. Create a new app (requires a Company Page)
3. Request access to **Member Data Portability API (Member)** product
4. Note your Client ID and Client Secret

### 2. Authenticate

```bash
# First time - provide credentials
lestash linkedin auth --mode self-serve --client-id YOUR_ID --client-secret YOUR_SECRET

# Subsequent times - credentials are stored
lestash linkedin auth
```

## Commands

### `lestash linkedin doctor`

Check API configuration and discover available data.

```bash
lestash linkedin doctor
```

This will show:

- Credential and token status
- Available Snapshot API domains for your account
- Changelog API status and recent activity types

### `lestash linkedin fetch`

Fetch your LinkedIn data.

```bash
# Fetch from Changelog API (posts, comments, likes after consent)
lestash linkedin fetch --changelog

# Fetch specific Snapshot domain
lestash linkedin fetch --domain PROFILE
lestash linkedin fetch --domain ARTICLES
lestash linkedin fetch --domain CONNECTIONS

# Fetch common Snapshot domains (PROFILE, ARTICLES, CONNECTIONS, INBOX, POSITIONS, EDUCATION)
lestash linkedin fetch --all
```

### `lestash linkedin import`

Import from a LinkedIn data export ZIP file.

```bash
lestash linkedin import ~/Downloads/linkedin-export.zip
```

## Understanding the APIs

LinkedIn provides two APIs under the DMA Portability program:

### Snapshot API

Returns historical data at the time of the API call. Available domains vary by user - use `doctor` to see what's available for your account.

Common domains include:

- `PROFILE` - Your profile information
- `ARTICLES` - Long-form articles you've written
- `CONNECTIONS` - Your connections
- `INBOX` - Messages
- `POSITIONS` - Work experience
- `EDUCATION` - Education history

**Note**: Posts, comments, and likes are **not available** via the Snapshot API. Use the Changelog API (`--changelog`) to fetch this activity.

### Changelog API

Tracks activity **after you consent** to the API (past 28 days only). This is where your recent posts, comments, and likes are captured.

Activity types include:

- `ugcPosts` - Posts you create
- `socialActions/comments` - Comments you make
- `socialActions/likes` - Reactions you give
- `invitations` - Connection requests

Use `--changelog` to fetch this data:

```bash
lestash linkedin fetch --changelog
```

## Rate Limits

LinkedIn applies **per-user daily quotas** that reset at midnight UTC:

| Endpoint | App Quota/day | User Quota/day |
|----------|---------------|----------------|
| memberSnapshotData | 200,000 | **200** |
| memberChangeLogs | 500,000 | **50** |

**Important**: The per-user limits are very low. With only 200 Snapshot API calls per day, be selective about which domains to fetch. The CLI will retry up to 3 times on 429 errors, then fail with a quota warning.

To check your usage:

1. Go to [LinkedIn Developer Portal](https://www.linkedin.com/developers/apps)
2. Select your application → **Analytics** tab
3. View "24 hour quotas" to see usage and limits

**Note**: The `Retry-After` header in 429 responses is a generic value (60s). If you've hit your daily quota, you must wait until midnight UTC - retrying sooner won't help.

See: [LinkedIn API Rate Limiting](https://learn.microsoft.com/en-us/linkedin/shared/api-guide/concepts/rate-limits)

## API Modes

### Self-Serve Mode (default)

For personal data mining - you access your own data.

- Use LinkedIn's default company page during app creation
- Request "Member Data Portability API (Member)" product
- Uses scope: `r_dma_portability_self_serve`

### 3rd-Party Mode

For building apps that access other users' data (with their consent).

- Requires your own verified company page
- Request "Member Data Portability API (3rd Party)" product
- Uses scope: `r_dma_portability_3rd_party`
- Requires manual review

## References

- [Member Data Portability API Overview](https://learn.microsoft.com/en-us/linkedin/dma/member-data-portability/)
- [Member Snapshot API](https://learn.microsoft.com/en-us/linkedin/dma/member-data-portability/shared/member-snapshot-api)
- [Member Changelog API](https://learn.microsoft.com/en-us/linkedin/dma/member-data-portability/shared/member-changelog-api)
- [Snapshot Domains Reference](https://learn.microsoft.com/en-us/linkedin/dma/member-data-portability/shared/snapshot-domain)
