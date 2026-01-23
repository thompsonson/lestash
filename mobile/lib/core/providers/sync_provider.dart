import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../database/database.dart';
import '../database/models/peer.dart';
import '../sync/sync_service.dart';
import 'database_provider.dart';

/// Provider for the sync service.
final syncServiceProvider = Provider<SyncService>((ref) {
  final database = ref.watch(databaseProvider);
  final service = SyncService(database);
  ref.onDispose(() => service.dispose());
  return service;
});

/// Provider for the list of configured peers.
final peersProvider = Provider<List<Peer>>((ref) {
  final database = ref.watch(databaseProvider);
  return database.getAllPeers();
});

/// Provider for sync state.
final syncStateProvider = StateNotifierProvider<SyncStateNotifier, SyncState>((ref) {
  final syncService = ref.watch(syncServiceProvider);
  return SyncStateNotifier(syncService);
});

/// State for sync operations.
class SyncState {
  final bool isSyncing;
  final SyncResult? lastResult;
  final String? error;

  const SyncState({
    this.isSyncing = false,
    this.lastResult,
    this.error,
  });

  SyncState copyWith({
    bool? isSyncing,
    SyncResult? lastResult,
    String? error,
  }) {
    return SyncState(
      isSyncing: isSyncing ?? this.isSyncing,
      lastResult: lastResult ?? this.lastResult,
      error: error,
    );
  }
}

/// Notifier for sync state.
class SyncStateNotifier extends StateNotifier<SyncState> {
  final SyncService _syncService;

  SyncStateNotifier(this._syncService) : super(const SyncState());

  /// Sync with a specific peer.
  Future<SyncResult> syncWithPeer(Peer peer) async {
    state = state.copyWith(isSyncing: true, error: null);

    try {
      final result = await _syncService.syncWithPeer(peer);
      state = state.copyWith(isSyncing: false, lastResult: result);
      return result;
    } catch (e) {
      state = state.copyWith(isSyncing: false, error: e.toString());
      rethrow;
    }
  }

  /// Sync with all configured peers.
  Future<List<SyncResult>> syncWithAllPeers(List<Peer> peers) async {
    final results = <SyncResult>[];

    for (final peer in peers) {
      final result = await syncWithPeer(peer);
      results.add(result);
    }

    return results;
  }
}
