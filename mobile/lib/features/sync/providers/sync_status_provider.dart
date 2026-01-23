import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../core/database/models/peer.dart';
import '../../../core/providers/sync_provider.dart';
import '../../../main.dart';

/// Provider for local database sync info.
final localSyncInfoProvider = Provider<LocalSyncInfo>((ref) {
  final database = ref.watch(databaseProvider);
  return LocalSyncInfo(
    siteId: database.getSiteId(),
    dbVersion: database.getDbVersion(),
  );
});

/// Provider for peer list with refresh.
final peerListProvider = StateNotifierProvider<PeerListNotifier, List<Peer>>((ref) {
  final database = ref.read(databaseProvider);
  return PeerListNotifier(database);
});

/// Local database sync information.
class LocalSyncInfo {
  final String? siteId;
  final int dbVersion;

  const LocalSyncInfo({
    this.siteId,
    this.dbVersion = 0,
  });
}

/// Notifier for managing peer list.
class PeerListNotifier extends StateNotifier<List<Peer>> {
  final dynamic _database;

  PeerListNotifier(this._database) : super([]) {
    refresh();
  }

  /// Refresh the peer list from database.
  void refresh() {
    state = _database.getAllPeers() as List<Peer>;
  }

  /// Add a new peer.
  Future<void> addPeer({
    required String siteId,
    required String name,
    required String address,
    int port = 8384,
  }) async {
    _database.addPeer(siteId, name, address, port: port);
    refresh();
  }

  /// Delete a peer.
  void deletePeer(int peerId) {
    _database.deletePeer(peerId);
    refresh();
  }
}
