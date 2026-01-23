/// Represents a sync peer (another Le Stash instance).
class Peer {
  final int id;
  final String siteId;
  final String name;
  final String address;
  final int port;
  final int lastSyncVersion;
  final DateTime? lastSyncAt;
  final DateTime createdAt;

  const Peer({
    required this.id,
    required this.siteId,
    required this.name,
    required this.address,
    this.port = 8384,
    this.lastSyncVersion = 0,
    this.lastSyncAt,
    required this.createdAt,
  });

  /// Create a Peer from a database row.
  factory Peer.fromRow(Map<String, dynamic> row) {
    return Peer(
      id: row['id'] as int,
      siteId: row['site_id'] as String,
      name: row['name'] as String,
      address: row['address'] as String,
      port: row['port'] as int? ?? 8384,
      lastSyncVersion: row['last_sync_version'] as int? ?? 0,
      lastSyncAt: row['last_sync_at'] != null
          ? DateTime.parse(row['last_sync_at'] as String)
          : null,
      createdAt: DateTime.parse(row['created_at'] as String),
    );
  }

  /// Get the full URL for this peer.
  String get url => 'http://$address:$port';

  /// Check if this peer has ever synced.
  bool get hasSynced => lastSyncAt != null;

  @override
  String toString() => 'Peer(id: $id, name: $name, address: $address:$port)';

  @override
  bool operator ==(Object other) {
    if (identical(this, other)) return true;
    return other is Peer && other.id == id;
  }

  @override
  int get hashCode => id.hashCode;
}
