# SQLite Distributed Sync Research

## Context: Le Stash

Le Stash currently uses a local SQLite database (`~/.config/lestash/lestash.db`) with:
- **items** table with full-text search (FTS5)
- **item_history** for change tracking
- **sources** configuration and sync logging
- Idempotent upserts using `ON CONFLICT ... DO UPDATE`

The goal is to enable distributed sync for a Flutter-based Android app to access and update data from anywhere.

---

## SQLite Sync Solutions Comparison

### 1. PowerSync (Recommended for Flutter)

**What it is:** A sync engine that keeps backend databases (Postgres/MySQL/MongoDB) in sync with on-device SQLite databases.

**Key Features:**
- First-class Flutter SDK with real-time streaming
- Direct access to local SQLite database
- Automatic schema management (schemaless sync with client-side views)
- Asynchronous background execution
- Works with Drift ORM (Flutter's most popular SQLite wrapper)

**Architecture:**
```
┌─────────────────┐     ┌─────────────────┐     ┌─────────────────┐
│  Flutter App    │ ←→  │  PowerSync      │ ←→  │  Postgres/      │
│  (SQLite)       │     │  Service        │     │  MySQL/MongoDB  │
└─────────────────┘     └─────────────────┘     └─────────────────┘
```

**Pros:**
- Native Flutter SDK (`powersync` on pub.dev)
- Open-source self-hosted option (Open Edition)
- Simon Binder (Drift creator) joined team in Jan 2025
- Supports partial sync (only sync what's needed)
- Real-time streaming of changes

**Cons:**
- Requires a backend database (Postgres/MySQL/MongoDB)
- Additional infrastructure to manage
- Would require migrating from local SQLite-only to client-server model

**Pricing:** Free tier available, self-hosted open edition, enterprise options

**Links:**
- [PowerSync Website](https://www.powersync.com)
- [Flutter SDK](https://pub.dev/packages/powersync)
- [GitHub](https://github.com/powersync-ja/powersync.dart)

---

### 2. ElectricSQL

**What it is:** A sync layer for active-active replication between Postgres (cloud) and SQLite (device), built by CRDT inventors.

**Key Features:**
- CRDT-based conflict resolution
- Bi-directional sync with causal+ consistency
- New "Durable Streams" protocol (Dec 2025)
- Postgres-native design

**Architecture:**
```
┌─────────────────┐     ┌─────────────────┐     ┌─────────────────┐
│  Client         │ ←→  │  Electric       │ ←→  │  Postgres       │
│  (SQLite)       │     │  Sync Service   │     │                 │
└─────────────────┘     └─────────────────┘     └─────────────────┘
```

**Pros:**
- CRDT-based (mathematically proven conflict resolution)
- Open source
- From the inventors of CRDTs (Marc Shapiro, Nuno Preguiça)
- Good for collaborative apps

**Cons:**
- Primarily JavaScript/TypeScript focused
- No official Flutter SDK (would need custom integration)
- Postgres-only backend

**Links:**
- [ElectricSQL Website](https://electric-sql.com)
- [GitHub](https://github.com/electric-sql/postgres-to-sqlite-sync-example)

---

### 3. cr-sqlite (Conflict-free Replicated SQLite)

**What it is:** A run-time loadable SQLite extension that adds CRDT-based multi-master replication.

**Key Features:**
- Works with any SQLite database
- No external sync service required
- Peer-to-peer or hub-and-spoke sync
- Tables become Grow-Only Sets or Observe-Remove Sets
- Columns can be counters, fractional indices, or LWW registers

**Architecture:**
```
┌─────────────────┐           ┌─────────────────┐
│  Device A       │ ←──────→  │  Device B       │
│  (SQLite +      │   merge   │  (SQLite +      │
│   cr-sqlite)    │           │   cr-sqlite)    │
└─────────────────┘           └─────────────────┘
         ↑                            ↑
         └────────────┬───────────────┘
                      ↓
              ┌───────────────┐
              │  Sync Server  │  (optional)
              │  (any transport)
              └───────────────┘
```

**Pros:**
- Pure SQLite extension (no backend service required)
- True peer-to-peer capable
- Works with existing SQLite databases
- Language agnostic (any language that can load SQLite extensions)

**Cons:**
- 2.5x slower inserts than regular SQLite
- No official Flutter SDK (need to use FFI)
- Requires Rust toolchain to build
- Less mature than other solutions

**Links:**
- [cr-sqlite GitHub](https://github.com/vlcn-io/cr-sqlite)
- [vlcn.io Documentation](https://vlcn.io/docs/cr-sqlite/intro)

---

### 4. SQLite Sync (sqlite.ai)

**What it is:** A newer local-first SQLite extension using CRDTs, similar to cr-sqlite.

**Key Features:**
- CRDT-based conflict-free sync
- Works offline with automatic merge on reconnect
- No manual conflict resolution needed

**Pros:**
- Simpler API than cr-sqlite
- Modern implementation

**Cons:**
- Newer/less established
- No official Flutter SDK

**Links:**
- [SQLite Sync Website](https://www.sqlite.ai/sqlite-sync)
- [GitHub](https://github.com/sqliteai/sqlite-sync)

---

### 5. Turso / libSQL

**What it is:** A distributed database built on libSQL (SQLite fork) optimized for edge deployment.

**Key Features:**
- Global replication across edge locations
- Embedded replicas (database copy inside your app)
- Concurrent writes (overcomes SQLite's single-writer limitation)
- Full-text search with Tantivy

**Architecture:**
```
┌─────────────────┐     ┌─────────────────┐     ┌─────────────────┐
│  Flutter App    │ ←→  │  Edge Replica   │ ←→  │  Primary DB     │
│  (embedded      │     │  (nearest PoP)  │     │  (Turso Cloud)  │
│   replica)      │     │                 │     │                 │
└─────────────────┘     └─────────────────┘     └─────────────────┘
```

**Pros:**
- SQLite-compatible
- Global edge distribution
- Embedded replicas for offline
- Open source (libSQL)

**Cons:**
- Cloud-oriented (less pure local-first)
- No official Flutter SDK (HTTP API or custom FFI)
- Pricing for cloud service

**Links:**
- [Turso Website](https://turso.tech)
- [libSQL GitHub](https://github.com/tursodatabase/libsql)

---

### 6. Couchbase Lite (NoSQL Alternative)

**What it is:** An embedded NoSQL document database with built-in sync.

**Key Features:**
- Document-oriented (JSON)
- SQL++ query language
- Full-text search built-in
- Sync with Couchbase Capella (cloud)

**Pros:**
- Official Flutter SDK (`cbl_flutter`)
- Mature sync technology
- Works offline with bi-directional sync
- Good conflict resolution

**Cons:**
- NoSQL (not SQLite-compatible)
- Would require data model migration
- Community-maintained Flutter SDK

**Links:**
- [cbl_flutter Package](https://pub.dev/packages/cbl_flutter)
- [Couchbase Lite for Dart](https://cbl-dart.dev/)

---

### 7. Realm / MongoDB Atlas Device Sync (NOT RECOMMENDED)

**Status:** ⚠️ **DEPRECATED** - End of Life: September 30, 2025

MongoDB announced deprecation in September 2024. Not recommended for new projects.

---

## Recommendation for Le Stash

Given Le Stash's requirements (Flutter Android app, existing SQLite schema, personal knowledge base with sync), here are the top options:

### Option A: PowerSync (Best for Production)

**Why:** Best Flutter support, maintained by active team, works with existing relational schema.

**Migration Path:**
1. Set up Postgres backend (can use Supabase, Neon, or self-hosted)
2. Mirror current SQLite schema to Postgres
3. Add PowerSync service (self-hosted or cloud)
4. Build Flutter app with `powersync` + `drift` packages

**Effort:** Medium-High (requires backend infrastructure)

### Option B: cr-sqlite (Best for True Local-First)

**Why:** Pure SQLite, no backend required, peer-to-peer capable.

**Migration Path:**
1. Add cr-sqlite extension to existing schema
2. Mark tables as CRRs (conflict-free replicated relations)
3. Build Flutter app with SQLite FFI + cr-sqlite
4. Implement sync transport (WebSocket, HTTP, or direct)

**Effort:** High (less ecosystem support, more DIY)

### Option C: Turso + Embedded Replicas (Best for Simplicity)

**Why:** Managed service, SQLite-compatible, good offline support.

**Migration Path:**
1. Create Turso database with existing schema
2. Use embedded replica in Flutter app
3. Automatic sync handled by Turso

**Effort:** Low-Medium (but cloud-dependent)

---

## Architecture Proposal for Le Stash

### Recommended: PowerSync with Postgres

```
┌─────────────────────────────────────────────────────────────────────┐
│                         DESKTOP (Current)                           │
│  ┌─────────────┐    ┌─────────────┐    ┌─────────────────────────┐ │
│  │  lestash    │ →  │  Local      │ ←→ │  PowerSync Client       │ │
│  │  CLI        │    │  SQLite     │    │  (background sync)      │ │
│  └─────────────┘    └─────────────┘    └───────────┬─────────────┘ │
└──────────────────────────────────────────────────────────────────────┘
                                                      │
                                                      ↓
                                         ┌────────────────────────┐
                                         │  PowerSync Service     │
                                         │  (self-hosted or cloud)│
                                         └───────────┬────────────┘
                                                      │
                                                      ↓
                                         ┌────────────────────────┐
                                         │  Postgres              │
                                         │  (source of truth)     │
                                         └────────────────────────┘
                                                      ↑
                                                      │
┌──────────────────────────────────────────────────────────────────────┐
│                         MOBILE (Flutter)                            │
│  ┌─────────────┐    ┌─────────────┐    ┌─────────────────────────┐ │
│  │  Flutter    │ ←→ │  Local      │ ←→ │  PowerSync SDK          │ │
│  │  App UI     │    │  SQLite     │    │  (powersync package)    │ │
│  └─────────────┘    └─────────────┘    └─────────────────────────┘ │
└──────────────────────────────────────────────────────────────────────┘
```

### Key Benefits:
1. **Offline-first**: Both CLI and mobile work offline
2. **Real-time sync**: Changes propagate automatically
3. **Existing schema**: Can keep relational model
4. **Flutter-native**: First-class SDK support
5. **Self-hostable**: No vendor lock-in

---

## Next Steps

1. **Prototype PowerSync**: Set up a minimal proof-of-concept
   - Create Postgres schema matching current SQLite
   - Deploy PowerSync (Docker or cloud)
   - Build minimal Flutter app with sync

2. **Evaluate cr-sqlite**: If avoiding cloud infrastructure
   - Test cr-sqlite extension with current schema
   - Build custom sync layer

3. **Consider hybrid**: Desktop stays local-only, mobile syncs to cloud
   - Less complex migration
   - Mobile-only cloud sync

---

## Resources

- [PowerSync Flutter Quick Start](https://docs.powersync.com/client-sdks/reference/flutter)
- [cr-sqlite Documentation](https://vlcn.io/docs/cr-sqlite/intro)
- [ElectricSQL Blog](https://electric-sql.com/blog)
- [Local-First Software](https://www.inkandswitch.com/local-first/) - Foundational paper
- [Turso Docs](https://docs.turso.tech/)

---

*Research compiled: January 2026*
