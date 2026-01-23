import 'dart:io';

import 'package:path_provider/path_provider.dart';
import 'package:sqlite3/sqlite3.dart' as sql;

import 'models/item.dart';
import 'models/tag.dart';
import 'models/peer.dart';

/// Database wrapper with cr-sqlite support for CRDT sync.
///
/// This class manages the SQLite database with cr-sqlite extension loaded
/// for conflict-free replicated data types (CRDTs).
class Database {
  sql.Database? _db;
  String? _dbPath;

  /// Whether the database is currently open
  bool get isOpen => _db != null;

  /// The database file path
  String? get path => _dbPath;

  /// Open the database, loading cr-sqlite extension.
  ///
  /// This must be called before any other database operations.
  Future<void> open() async {
    if (_db != null) return;

    final directory = await getApplicationDocumentsDirectory();
    _dbPath = '${directory.path}/lestash.db';

    _db = sql.sqlite3.open(_dbPath!);

    // Load cr-sqlite extension - must be first operation
    _loadCrSqliteExtension();

    // Initialize schema
    _initializeSchema();

    // Upgrade tables to CRRs for sync
    _upgradeTablesToCrr();
  }

  /// Load the cr-sqlite extension for CRDT support.
  void _loadCrSqliteExtension() {
    final extensionPath = _getCrSqliteExtensionPath();
    try {
      _db!.execute(
        "SELECT load_extension(?, 'sqlite3_crsqlite_init')",
        [extensionPath],
      );
    } catch (e) {
      // Extension may not be available in development
      // Log warning but continue - sync features will be disabled
      print('Warning: Could not load cr-sqlite extension: $e');
    }
  }

  /// Get the platform-specific path to cr-sqlite extension.
  String _getCrSqliteExtensionPath() {
    if (Platform.isAndroid) {
      // Android loads from jniLibs automatically
      return 'libcrsqlite';
    } else if (Platform.isIOS) {
      return '@rpath/crsqlite.framework/crsqlite';
    }
    throw UnsupportedError('Platform not supported: ${Platform.operatingSystem}');
  }

  /// Initialize the database schema.
  void _initializeSchema() {
    _db!.execute('''
      CREATE TABLE IF NOT EXISTS items (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        source_type TEXT NOT NULL,
        source_id TEXT NOT NULL,
        url TEXT,
        title TEXT,
        content TEXT NOT NULL,
        author TEXT,
        created_at DATETIME,
        fetched_at DATETIME DEFAULT CURRENT_TIMESTAMP,
        is_own_content BOOLEAN DEFAULT 0,
        metadata TEXT,
        UNIQUE(source_type, source_id)
      )
    ''');

    _db!.execute('''
      CREATE TABLE IF NOT EXISTS tags (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL UNIQUE
      )
    ''');

    _db!.execute('''
      CREATE TABLE IF NOT EXISTS item_tags (
        item_id INTEGER NOT NULL,
        tag_id INTEGER NOT NULL,
        PRIMARY KEY (item_id, tag_id),
        FOREIGN KEY (item_id) REFERENCES items(id) ON DELETE CASCADE,
        FOREIGN KEY (tag_id) REFERENCES tags(id) ON DELETE CASCADE
      )
    ''');

    _db!.execute('''
      CREATE TABLE IF NOT EXISTS peers (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        site_id TEXT NOT NULL UNIQUE,
        name TEXT NOT NULL,
        address TEXT NOT NULL,
        port INTEGER NOT NULL DEFAULT 8384,
        last_sync_version INTEGER DEFAULT 0,
        last_sync_at DATETIME,
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP
      )
    ''');

    // FTS5 virtual table for full-text search
    _db!.execute('''
      CREATE VIRTUAL TABLE IF NOT EXISTS items_fts USING fts5(
        title,
        content,
        author,
        content='items',
        content_rowid='id'
      )
    ''');

    // Triggers to keep FTS in sync
    _db!.execute('''
      CREATE TRIGGER IF NOT EXISTS items_ai AFTER INSERT ON items BEGIN
        INSERT INTO items_fts(rowid, title, content, author)
        VALUES (new.id, new.title, new.content, new.author);
      END
    ''');

    _db!.execute('''
      CREATE TRIGGER IF NOT EXISTS items_ad AFTER DELETE ON items BEGIN
        INSERT INTO items_fts(items_fts, rowid, title, content, author)
        VALUES ('delete', old.id, old.title, old.content, old.author);
      END
    ''');

    _db!.execute('''
      CREATE TRIGGER IF NOT EXISTS items_au AFTER UPDATE ON items BEGIN
        INSERT INTO items_fts(items_fts, rowid, title, content, author)
        VALUES ('delete', old.id, old.title, old.content, old.author);
        INSERT INTO items_fts(rowid, title, content, author)
        VALUES (new.id, new.title, new.content, new.author);
      END
    ''');

    // Create indexes
    _db!.execute('CREATE INDEX IF NOT EXISTS idx_items_source ON items(source_type)');
    _db!.execute('CREATE INDEX IF NOT EXISTS idx_items_created ON items(created_at)');
  }

  /// Upgrade tables to Conflict-free Replicated Relations (CRRs).
  void _upgradeTablesToCrr() {
    final tables = ['items', 'tags', 'item_tags', 'peers'];
    for (final table in tables) {
      try {
        _db!.execute("SELECT crsql_as_crr('$table')");
      } catch (e) {
        // cr-sqlite not loaded, skip CRR upgrade
      }
    }
  }

  /// Close the database connection.
  void close() {
    if (_db != null) {
      try {
        _db!.execute('SELECT crsql_finalize()');
      } catch (e) {
        // cr-sqlite not loaded
      }
      _db!.dispose();
      _db = null;
    }
  }

  // ============ Item Operations ============

  /// Get all items, optionally filtered by source type.
  List<Item> getAllItems({String? sourceType, int limit = 100, int offset = 0}) {
    _ensureOpen();

    String query = 'SELECT * FROM items';
    final params = <Object?>[];

    if (sourceType != null) {
      query += ' WHERE source_type = ?';
      params.add(sourceType);
    }

    query += ' ORDER BY created_at DESC LIMIT ? OFFSET ?';
    params.addAll([limit, offset]);

    final results = _db!.select(query, params);
    return results.map((row) => Item.fromRow(row)).toList();
  }

  /// Get a single item by ID.
  Item? getItem(int id) {
    _ensureOpen();
    final results = _db!.select('SELECT * FROM items WHERE id = ?', [id]);
    if (results.isEmpty) return null;
    return Item.fromRow(results.first);
  }

  /// Search items using full-text search.
  List<Item> search(String query, {int limit = 50}) {
    _ensureOpen();
    if (query.trim().isEmpty) return [];

    final results = _db!.select('''
      SELECT items.* FROM items
      JOIN items_fts ON items.id = items_fts.rowid
      WHERE items_fts MATCH ?
      ORDER BY rank
      LIMIT ?
    ''', [query, limit]);

    return results.map((row) => Item.fromRow(row)).toList();
  }

  /// Get distinct source types.
  List<String> getSourceTypes() {
    _ensureOpen();
    final results = _db!.select('SELECT DISTINCT source_type FROM items ORDER BY source_type');
    return results.map((row) => row['source_type'] as String).toList();
  }

  // ============ Tag Operations ============

  /// Get all tags.
  List<Tag> getAllTags() {
    _ensureOpen();
    final results = _db!.select('SELECT * FROM tags ORDER BY name');
    return results.map((row) => Tag.fromRow(row)).toList();
  }

  /// Get tags for an item.
  List<Tag> getTagsForItem(int itemId) {
    _ensureOpen();
    final results = _db!.select('''
      SELECT tags.* FROM tags
      JOIN item_tags ON tags.id = item_tags.tag_id
      WHERE item_tags.item_id = ?
      ORDER BY tags.name
    ''', [itemId]);
    return results.map((row) => Tag.fromRow(row)).toList();
  }

  // ============ Peer Operations ============

  /// Get all configured peers.
  List<Peer> getAllPeers() {
    _ensureOpen();
    final results = _db!.select('SELECT * FROM peers ORDER BY name');
    return results.map((row) => Peer.fromRow(row)).toList();
  }

  /// Add a new peer.
  int addPeer(String siteId, String name, String address, {int port = 8384}) {
    _ensureOpen();
    _db!.execute(
      'INSERT INTO peers (site_id, name, address, port) VALUES (?, ?, ?, ?)',
      [siteId, name, address, port],
    );
    return _db!.lastInsertRowId;
  }

  /// Update peer sync status.
  void updatePeerSyncStatus(int peerId, int syncVersion) {
    _ensureOpen();
    _db!.execute(
      'UPDATE peers SET last_sync_version = ?, last_sync_at = CURRENT_TIMESTAMP WHERE id = ?',
      [syncVersion, peerId],
    );
  }

  /// Delete a peer.
  void deletePeer(int peerId) {
    _ensureOpen();
    _db!.execute('DELETE FROM peers WHERE id = ?', [peerId]);
  }

  // ============ Sync Operations ============

  /// Get the site ID for this database.
  String? getSiteId() {
    _ensureOpen();
    try {
      final results = _db!.select('SELECT crsql_site_id()');
      if (results.isEmpty) return null;
      final bytes = results.first.values.first as List<int>;
      return bytes.map((b) => b.toRadixString(16).padLeft(2, '0')).join();
    } catch (e) {
      return null;
    }
  }

  /// Get the current database version (logical clock).
  int getDbVersion() {
    _ensureOpen();
    try {
      final results = _db!.select('SELECT crsql_db_version()');
      if (results.isEmpty) return 0;
      return results.first.values.first as int;
    } catch (e) {
      return 0;
    }
  }

  /// Get changes since a given version.
  List<Map<String, dynamic>> getChangesSince(int sinceVersion) {
    _ensureOpen();
    try {
      final results = _db!.select('''
        SELECT "table", pk, cid, val, col_version, db_version, site_id, cl, seq
        FROM crsql_changes
        WHERE db_version > ?
        ORDER BY db_version, seq
      ''', [sinceVersion]);

      return results.map((row) {
        final siteId = row['site_id'] as List<int>?;
        return {
          'table': row['table'],
          'pk': row['pk'],
          'cid': row['cid'],
          'val': row['val'],
          'col_version': row['col_version'],
          'db_version': row['db_version'],
          'site_id': siteId?.map((b) => b.toRadixString(16).padLeft(2, '0')).join(),
          'cl': row['cl'],
          'seq': row['seq'],
        };
      }).toList();
    } catch (e) {
      return [];
    }
  }

  /// Apply changes from another peer.
  int applyChanges(List<Map<String, dynamic>> changes) {
    _ensureOpen();
    var applied = 0;

    try {
      for (final change in changes) {
        final siteIdHex = change['site_id'] as String?;
        List<int>? siteId;
        if (siteIdHex != null) {
          siteId = [];
          for (var i = 0; i < siteIdHex.length; i += 2) {
            siteId.add(int.parse(siteIdHex.substring(i, i + 2), radix: 16));
          }
        }

        _db!.execute('''
          INSERT INTO crsql_changes
            ("table", pk, cid, val, col_version, db_version, site_id, cl, seq)
          VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', [
          change['table'],
          change['pk'],
          change['cid'],
          change['val'],
          change['col_version'],
          change['db_version'],
          siteId,
          change['cl'],
          change['seq'],
        ]);
        applied++;
      }
    } catch (e) {
      print('Error applying changes: $e');
    }

    return applied;
  }

  /// Rebuild the FTS index after sync.
  int rebuildFtsIndex() {
    _ensureOpen();
    _db!.execute('DELETE FROM items_fts');
    _db!.execute('''
      INSERT INTO items_fts(rowid, title, content, author)
      SELECT id, title, content, author FROM items
    ''');

    final results = _db!.select('SELECT COUNT(*) as count FROM items_fts');
    return results.first['count'] as int;
  }

  void _ensureOpen() {
    if (_db == null) {
      throw StateError('Database not open. Call open() first.');
    }
  }
}
