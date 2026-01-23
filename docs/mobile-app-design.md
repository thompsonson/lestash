# Le Stash Mobile App - Technical Design Document

## Overview

This document captures the architectural decisions and technical design for the Le Stash mobile application, a Flutter-based companion to the Python CLI that enables browsing and syncing content across devices.

## Goals

1. **Read-only browser** (MVP) - Browse and search items synced from desktop
2. **Bidirectional sync** - CRDT-based sync via HTTP over any network
3. **Share-to capture** (Phase 2) - Capture content from other apps
4. **Cross-platform** - Android first, iOS later

---

## Architecture Decisions

### Repository Structure

**Decision:** Monorepo - mobile code lives alongside Python CLI

```
le-stash/
├── packages/                    # Python packages (existing)
│   ├── lestash/
│   │   └── src/lestash/
│   │       ├── cli/
│   │       │   └── sync.py      # CLI sync commands
│   │       └── core/
│   │           └── crsqlite.py  # CRDT sync logic
│   └── ...
├── mobile/                      # Flutter app (new)
│   ├── lib/
│   │   ├── main.dart
│   │   ├── core/
│   │   │   ├── database/
│   │   │   └── sync/
│   │   ├── features/
│   │   │   ├── items/
│   │   │   ├── search/
│   │   │   └── settings/
│   │   └── shared/
│   ├── android/
│   ├── ios/
│   ├── native/                  # Pre-built cr-sqlite binaries
│   │   ├── android/
│   │   │   ├── arm64-v8a/
│   │   │   ├── armeabi-v7a/
│   │   │   └── x86_64/
│   │   └── ios/
│   │       └── crsqlite.xcframework/
│   ├── test/
│   └── pubspec.yaml
├── docs/
│   ├── mobile-app-design.md     # This document
│   └── sync-protocol.md         # Sync protocol spec
└── .github/
    └── workflows/
        └── build-crsqlite.yml   # CI for native binaries
```

**Rationale:** The sync protocol is tightly coupled between CLI and mobile. Having both in one repo ensures schema changes, protocol updates, and cr-sqlite versions stay in sync.

---

### Sync Transport

**Decision:** HTTP abstracted from network layer

The sync implementation uses plain HTTP requests. This works over:

- Local network (same WiFi)
- Tailscale (mesh VPN)
- Any other network that provides IP connectivity

```
┌─────────────────┐         HTTP         ┌─────────────────┐
│  Mobile App     │ ◄──────────────────► │  Desktop CLI    │
│  (Flutter)      │   /sync/status       │  (Python)       │
│                 │   /sync/changes      │                 │
└─────────────────┘                      └─────────────────┘
        │                                        │
        └──────────── Any IP Network ────────────┘
                (Local, Tailscale, VPN, etc.)
```

**Rationale:** Abstraction allows starting simple (local network) and adding Tailscale later without changing the sync logic.

---

### Peer Discovery

**Decision:** Manual IP entry (MVP), QR pairing (later)

**MVP Flow:**

1. Desktop runs `lestash sync serve` → shows IP and port
2. User manually enters IP:port in mobile app
3. Mobile stores peer in local database

**Future Enhancement:**

- QR code contains: `{"ip": "...", "port": 8384, "site_id": "...", "name": "MacBook"}`
- Mobile scans QR, auto-configures peer

---

### Sync Direction

**Decision:** Mobile → Desktop first (pull model)

**MVP:**

- Mobile initiates sync by calling desktop's HTTP API
- Desktop is passive (serves requests)

**Phase 2:**

- Desktop can push notifications via FCM
- Mobile receives push, initiates pull
- True bidirectional when both can initiate

---

### State Management

**Decision:** Riverpod

**Why Riverpod:**

- Type-safe at compile time
- Excellent testing support
- No BuildContext required for business logic
- Providers can depend on other providers
- Active maintenance and community

**Provider Architecture:**

```dart
// Core providers
final databaseProvider = Provider<Database>((ref) => Database());
final syncServiceProvider = Provider<SyncService>((ref) {
  return SyncService(ref.read(databaseProvider));
});

// Feature providers
final itemsProvider = StreamProvider<List<Item>>((ref) {
  final db = ref.read(databaseProvider);
  return db.watchAllItems();
});

final searchResultsProvider = FutureProvider.family<List<Item>, String>((ref, query) {
  final db = ref.read(databaseProvider);
  return db.search(query);
});

// Sync state
final syncStatusProvider = StateNotifierProvider<SyncNotifier, SyncState>((ref) {
  return SyncNotifier(ref.read(syncServiceProvider));
});
```

---

### Database Layer

**Decision:** `sqlite3` + `sqlite3_flutter_libs` + cr-sqlite via FFI

**Why not `sqflite`:**

- `sqflite` uses platform SQLite which cannot load extensions
- We need to load cr-sqlite extension for CRDT sync

**Architecture:**

```
┌──────────────────────────────────────────────────────────┐
│                      Flutter/Dart                         │
│  ┌────────────────────────────────────────────────────┐  │
│  │              Database Class (Dart)                  │  │
│  │  - open(), close()                                  │  │
│  │  - getAllItems(), search(), getItem()               │  │
│  │  - getChangesSince(), applyChanges()                │  │
│  └──────────────────────┬─────────────────────────────┘  │
│                         │ FFI                             │
│  ┌──────────────────────▼─────────────────────────────┐  │
│  │           sqlite3 package (dart:ffi)                │  │
│  └──────────────────────┬─────────────────────────────┘  │
│                         │                                 │
│  ┌──────────────────────▼─────────────────────────────┐  │
│  │         sqlite3_flutter_libs (bundled SQLite)       │  │
│  └──────────────────────┬─────────────────────────────┘  │
└─────────────────────────│─────────────────────────────────┘
                          │
          ┌───────────────▼───────────────┐
          │   Native SQLite + cr-sqlite    │
          │  ┌──────────┐  ┌────────────┐  │
          │  │ SQLite   │◄─│ crsqlite   │  │
          │  │ 3.44+    │  │ extension  │  │
          │  └──────────┘  └────────────┘  │
          └────────────────────────────────┘
```

---

## Protocol Versioning

Multiple components need version tracking to ensure compatibility:

### 1. Sync Protocol Version

The overall sync protocol version. Increment when breaking changes occur.

```json
{
  "protocol_version": 1
}
```

### 2. Sync Format Version

The JSON changeset format. Current: `lestash-crsqlite-v1`

```json
{
  "format": "lestash-crsqlite-v1",
  "changes": [...]
}
```

### 3. Schema Version

SQLite schema version tracked via `PRAGMA user_version`. Current: `2`

### 4. cr-sqlite Version

The cr-sqlite extension version. Current: `0.16.3`

### 5. Sync Tables

List of tables being synced:

```python
SYNC_TABLES = ["items", "tags", "item_tags", "sources", "person_profiles"]
```

### Version Negotiation

On connect, the `/sync/status` endpoint returns all version info:

```json
{
  "site_id": "a1b2c3d4e5f6",
  "db_version": 42,
  "protocol": {
    "version": 1,
    "format": "lestash-crsqlite-v1",
    "schema_version": 2,
    "crsqlite_version": "0.16.3",
    "sync_tables": ["items", "tags", "item_tags", "sources", "person_profiles"]
  }
}
```

**Compatibility Rules:**

| Component | Rule |
|-----------|------|
| `protocol.version` | Must match exactly |
| `format` | Must match exactly |
| `schema_version` | Warn if different, sync common tables |
| `crsqlite_version` | Warn if major version differs |
| `sync_tables` | Sync intersection of both peer's tables |

---

## HTTP API Specification

### Endpoints

#### `GET /sync/status`

Returns current database state and protocol info.

**Response:**

```json
{
  "site_id": "a1b2c3d4e5f6",
  "db_version": 42,
  "protocol": {
    "version": 1,
    "format": "lestash-crsqlite-v1",
    "schema_version": 2,
    "crsqlite_version": "0.16.3",
    "sync_tables": ["items", "tags", "item_tags", "sources", "person_profiles"]
  }
}
```

#### `GET /sync/changes?since={version}`

Returns all changes since the given database version.

**Parameters:**

- `since` (required): Database version to get changes after

**Response:**

```json
{
  "format": "lestash-crsqlite-v1",
  "site_id": "a1b2c3d4e5f6",
  "db_version": 42,
  "since_version": 30,
  "change_count": 15,
  "changes": [
    {
      "table": "items",
      "pk": "1",
      "cid": "content",
      "val": "Hello world",
      "col_version": 1,
      "db_version": 31,
      "site_id": "a1b2c3d4e5f6",
      "cl": 1,
      "seq": 0
    }
  ]
}
```

#### `POST /sync/changes`

Apply changes from another peer.

**Request Body:**

```json
{
  "format": "lestash-crsqlite-v1",
  "changes": [...]
}
```

**Response:**

```json
{
  "applied": 15,
  "db_version": 57
}
```

---

## cr-sqlite Binary Management

### The Challenge

cr-sqlite is a native SQLite extension written in Rust. Pre-built releases only include desktop platforms, not mobile (Android/iOS).

### Solution: Build in CI, Commit to Repo

**Approach:**

1. GitHub Actions workflow builds cr-sqlite for all mobile platforms
2. Compiled binaries are committed to `mobile/native/`
3. Developers use pre-built binaries without needing Rust toolchain

### Build Targets

| Platform | Target | Output |
|----------|--------|--------|
| Android arm64 | `aarch64-linux-android` | `libcrsqlite.so` |
| Android arm32 | `armv7-linux-androideabi` | `libcrsqlite.so` |
| Android x86_64 | `x86_64-linux-android` | `libcrsqlite.so` |
| iOS device | `aarch64-apple-ios` | `crsqlite.xcframework` |
| iOS sim (M1) | `aarch64-apple-ios-sim` | (included in xcframework) |
| iOS sim (Intel) | `x86_64-apple-ios` | (included in xcframework) |

### GitHub Actions Workflow

```yaml
# .github/workflows/build-crsqlite.yml
name: Build cr-sqlite Mobile Binaries

on:
  workflow_dispatch:
    inputs:
      crsqlite_version:
        description: 'cr-sqlite version to build'
        required: true
        default: '0.16.3'

jobs:
  build-android:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4

      - name: Install Rust
        uses: dtolnay/rust-action@stable

      - name: Install Android targets
        run: |
          rustup target add aarch64-linux-android
          rustup target add armv7-linux-androideabi
          rustup target add x86_64-linux-android

      - name: Install cargo-ndk
        run: cargo install cargo-ndk

      - name: Setup Android NDK
        uses: android-actions/setup-android@v3

      - name: Clone cr-sqlite
        run: |
          git clone --recurse-submodules https://github.com/vlcn-io/cr-sqlite.git
          cd cr-sqlite
          git checkout v${{ inputs.crsqlite_version }}

      - name: Build Android libraries
        run: |
          cd cr-sqlite/core
          cargo ndk -t arm64-v8a -t armeabi-v7a -t x86_64 \
            -o ../../mobile/native/android build --release

      - name: Commit binaries
        run: |
          git config user.name "GitHub Actions"
          git config user.email "actions@github.com"
          git add mobile/native/android/
          git commit -m "chore: update cr-sqlite Android binaries to v${{ inputs.crsqlite_version }}"
          git push

  build-ios:
    runs-on: macos-latest
    steps:
      - uses: actions/checkout@v4

      - name: Install Rust
        uses: dtolnay/rust-action@stable

      - name: Install iOS targets
        run: |
          rustup target add aarch64-apple-ios
          rustup target add aarch64-apple-ios-sim
          rustup target add x86_64-apple-ios

      - name: Clone cr-sqlite
        run: |
          git clone --recurse-submodules https://github.com/vlcn-io/cr-sqlite.git
          cd cr-sqlite
          git checkout v${{ inputs.crsqlite_version }}

      - name: Build iOS libraries
        run: |
          cd cr-sqlite/core
          cargo build --release --target aarch64-apple-ios
          cargo build --release --target aarch64-apple-ios-sim
          cargo build --release --target x86_64-apple-ios

      - name: Create xcframework
        run: |
          # Create universal sim library
          lipo -create \
            target/aarch64-apple-ios-sim/release/libcrsqlite.a \
            target/x86_64-apple-ios/release/libcrsqlite.a \
            -output target/universal-sim/libcrsqlite.a

          # Create xcframework
          xcodebuild -create-xcframework \
            -library target/aarch64-apple-ios/release/libcrsqlite.a \
            -library target/universal-sim/libcrsqlite.a \
            -output mobile/native/ios/crsqlite.xcframework

      - name: Commit binaries
        run: |
          git config user.name "GitHub Actions"
          git config user.email "actions@github.com"
          git add mobile/native/ios/
          git commit -m "chore: update cr-sqlite iOS binaries to v${{ inputs.crsqlite_version }}"
          git push
```

### Loading Extension in Flutter

```dart
// lib/core/database/database.dart
import 'dart:ffi';
import 'dart:io';
import 'package:sqlite3/sqlite3.dart';

class Database {
  late final sqlite3.Database _db;

  Future<void> open(String path) async {
    _db = sqlite3.open(path);

    // Load cr-sqlite extension
    _db.execute(
      "SELECT load_extension(?, 'sqlite3_crsqlite_init')",
      [_getExtensionPath()],
    );
  }

  String _getExtensionPath() {
    if (Platform.isAndroid) {
      // Android loads from jniLibs automatically
      return 'libcrsqlite';
    } else if (Platform.isIOS) {
      return '@rpath/crsqlite.framework/crsqlite';
    }
    throw UnsupportedError('Platform not supported');
  }

  void dispose() {
    _db.execute('SELECT crsql_finalize()');
    _db.dispose();
  }
}
```

---

## Flutter Project Structure

```
mobile/
├── lib/
│   ├── main.dart                    # App entry point
│   ├── app.dart                     # MaterialApp configuration
│   │
│   ├── core/                        # Core infrastructure
│   │   ├── database/
│   │   │   ├── database.dart        # SQLite + cr-sqlite wrapper
│   │   │   ├── migrations.dart      # Schema migrations
│   │   │   └── models/              # Database models
│   │   │       ├── item.dart
│   │   │       ├── tag.dart
│   │   │       └── peer.dart
│   │   │
│   │   ├── sync/
│   │   │   ├── sync_service.dart    # HTTP sync client
│   │   │   ├── sync_protocol.dart   # Protocol version handling
│   │   │   └── change_tracker.dart  # cr-sqlite change tracking
│   │   │
│   │   └── providers/
│   │       ├── database_provider.dart
│   │       └── sync_provider.dart
│   │
│   ├── features/                    # Feature modules
│   │   ├── items/
│   │   │   ├── providers/
│   │   │   │   └── items_provider.dart
│   │   │   ├── screens/
│   │   │   │   ├── items_list_screen.dart
│   │   │   │   └── item_detail_screen.dart
│   │   │   └── widgets/
│   │   │       ├── item_tile.dart
│   │   │       └── item_filters.dart
│   │   │
│   │   ├── search/
│   │   │   ├── providers/
│   │   │   │   └── search_provider.dart
│   │   │   ├── screens/
│   │   │   │   └── search_screen.dart
│   │   │   └── widgets/
│   │   │       └── search_result_tile.dart
│   │   │
│   │   ├── sync/
│   │   │   ├── providers/
│   │   │   │   └── sync_status_provider.dart
│   │   │   ├── screens/
│   │   │   │   └── sync_screen.dart
│   │   │   └── widgets/
│   │   │       └── peer_tile.dart
│   │   │
│   │   └── settings/
│   │       └── screens/
│   │           └── settings_screen.dart
│   │
│   └── shared/                      # Shared widgets/utilities
│       ├── widgets/
│       │   ├── loading_indicator.dart
│       │   └── error_view.dart
│       └── utils/
│           └── date_formatter.dart
│
├── android/
│   └── app/
│       └── src/main/
│           └── jniLibs/             # Symlink to native/android
│
├── ios/
│   └── Runner/
│       └── Frameworks/              # Symlink to native/ios
│
├── native/                          # Pre-built cr-sqlite binaries
│   ├── android/
│   │   ├── arm64-v8a/
│   │   │   └── libcrsqlite.so
│   │   ├── armeabi-v7a/
│   │   │   └── libcrsqlite.so
│   │   └── x86_64/
│   │       └── libcrsqlite.so
│   └── ios/
│       └── crsqlite.xcframework/
│
├── test/
│   ├── core/
│   │   ├── database_test.dart
│   │   └── sync_service_test.dart
│   └── features/
│       └── items/
│           └── items_provider_test.dart
│
├── pubspec.yaml
└── analysis_options.yaml
```

---

## Dependencies

```yaml
# pubspec.yaml
name: lestash_mobile
description: Le Stash mobile companion app
version: 1.0.0

environment:
  sdk: '>=3.0.0 <4.0.0'
  flutter: '>=3.10.0'

dependencies:
  flutter:
    sdk: flutter

  # State management
  flutter_riverpod: ^2.4.0
  riverpod_annotation: ^2.3.0

  # Database
  sqlite3: ^2.1.0
  sqlite3_flutter_libs: ^0.5.0

  # Networking
  dio: ^5.4.0

  # Routing
  go_router: ^12.0.0

  # UI
  flutter_slidable: ^3.0.0      # Swipe actions
  cached_network_image: ^3.3.0  # Image caching

  # Utilities
  intl: ^0.18.0                 # Date formatting
  share_plus: ^7.0.0            # Share functionality
  url_launcher: ^6.2.0          # Open URLs

dev_dependencies:
  flutter_test:
    sdk: flutter
  flutter_lints: ^3.0.0
  riverpod_generator: ^2.3.0
  build_runner: ^2.4.0
  mocktail: ^1.0.0
```

---

## Implementation Phases

### Phase 1: MVP Read-Only Browser (Issues #16, #17 partial)

**Goal:** Browse items synced from desktop

**Features:**

- [ ] Flutter project setup with Riverpod
- [ ] SQLite database with cr-sqlite loading
- [ ] Items list with filtering (source type, tags)
- [ ] Item detail view
- [ ] FTS5 search
- [ ] Manual peer configuration (IP entry)
- [ ] Pull sync from desktop
- [ ] Desktop `lestash sync serve` command

**Not included:**

- Push notifications
- Share-to capture
- QR pairing
- iOS support

### Phase 2: Full Sync + Capture (Issues #15, #17 complete)

**Goal:** Bidirectional sync and content capture

**Features:**

- [ ] Android share intent receiver
- [ ] Content type detection (arXiv, YouTube, etc.)
- [ ] Metadata fetching (oEmbed)
- [ ] Quick capture UI with tags
- [ ] Bidirectional sync (mobile can push changes)
- [ ] Sync conflict UI (show merge results)

### Phase 3: Push Notifications + Discovery (Issue #18)

**Goal:** Real-time sync triggers

**Features:**

- [ ] FCM integration
- [ ] QR code pairing
- [ ] Peer management UI
- [ ] Desktop push-on-save
- [ ] Background sync on push

### Phase 4: iOS Support (Issue #19)

**Goal:** Feature parity on iOS

**Features:**

- [ ] iOS share extension
- [ ] APNs / FCM relay
- [ ] App Groups for shared database
- [ ] iOS-specific UI polish

---

## Testing Strategy

### Unit Tests

- Database operations (CRUD, search)
- Sync protocol parsing
- Change tracking logic
- Provider logic

### Integration Tests

- Full sync cycle between two databases
- Schema migration across versions
- cr-sqlite extension loading

### Widget Tests

- Screen rendering with mock data
- User interactions
- Error states

### Manual Testing

- Sync between physical devices
- Various network conditions
- Large datasets

---

## Open Questions

1. **Offline queue:** Should we queue changes made offline and sync later, or is pull-only sufficient for MVP?

2. **Partial sync:** Should mobile sync all items or support selective sync (e.g., only recent, only certain sources)?

3. **Image/blob handling:** Items may have images in metadata. Cache locally or fetch on demand?

4. **Database location:** Use app documents directory or external storage (for backup)?

---

## References

- [cr-sqlite Documentation](https://vlcn.io/docs/cr-sqlite/intro)
- [Riverpod Documentation](https://riverpod.dev/)
- [sqlite3 Flutter Package](https://pub.dev/packages/sqlite3)
- [cargo-ndk](https://github.com/bbqsrc/cargo-ndk)
- [Flutter FFI](https://docs.flutter.dev/development/platform-integration/c-interop)
