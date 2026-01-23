import 'package:dio/dio.dart';

import '../database/database.dart';
import '../database/models/peer.dart';
import 'sync_protocol.dart';

/// Service for syncing with remote Le Stash instances.
class SyncService {
  final Database _database;
  final Dio _dio;

  SyncService(this._database) : _dio = Dio(BaseOptions(
    connectTimeout: const Duration(seconds: 10),
    receiveTimeout: const Duration(seconds: 30),
  ));

  /// Sync with a specific peer.
  ///
  /// Returns a [SyncResult] with details about what was synced.
  Future<SyncResult> syncWithPeer(Peer peer) async {
    final startTime = DateTime.now();

    try {
      // 1. Get peer status to check compatibility
      final status = await _getPeerStatus(peer);

      // 2. Check protocol compatibility
      final compatibility = SyncProtocol.checkCompatibility(
        localSchemaVersion: 2, // TODO: Get from database
        localCrsqliteVersion: '0.16.3',
        remoteProtocol: status.protocol,
      );

      if (!compatibility.isCompatible) {
        return SyncResult.failed(
          peer: peer,
          error: 'Incompatible: ${compatibility.reason}',
          duration: DateTime.now().difference(startTime),
        );
      }

      // 3. Pull changes from peer
      final pullResult = await _pullChanges(peer, status);

      // 4. Rebuild FTS if we received changes
      if (pullResult.changesReceived > 0) {
        _database.rebuildFtsIndex();
      }

      // 5. Update peer sync status
      _database.updatePeerSyncStatus(peer.id, status.dbVersion);

      return SyncResult.success(
        peer: peer,
        changesReceived: pullResult.changesReceived,
        changesApplied: pullResult.changesApplied,
        changesSent: 0, // TODO: Implement push
        duration: DateTime.now().difference(startTime),
        warnings: compatibility.warnings,
      );
    } on DioException catch (e) {
      return SyncResult.failed(
        peer: peer,
        error: _formatDioError(e),
        duration: DateTime.now().difference(startTime),
      );
    } catch (e) {
      return SyncResult.failed(
        peer: peer,
        error: e.toString(),
        duration: DateTime.now().difference(startTime),
      );
    }
  }

  /// Get status from a peer.
  Future<PeerStatus> _getPeerStatus(Peer peer) async {
    final response = await _dio.get('${peer.url}/sync/status');
    return PeerStatus.fromJson(response.data as Map<String, dynamic>);
  }

  /// Pull changes from a peer.
  Future<_PullResult> _pullChanges(Peer peer, PeerStatus status) async {
    final response = await _dio.get(
      '${peer.url}/sync/changes',
      queryParameters: {'since': peer.lastSyncVersion},
    );

    final data = response.data as Map<String, dynamic>;
    final changes = (data['changes'] as List)
        .map((c) => c as Map<String, dynamic>)
        .toList();

    if (changes.isEmpty) {
      return _PullResult(changesReceived: 0, changesApplied: 0);
    }

    final applied = _database.applyChanges(changes);

    return _PullResult(
      changesReceived: changes.length,
      changesApplied: applied,
    );
  }

  String _formatDioError(DioException e) {
    switch (e.type) {
      case DioExceptionType.connectionTimeout:
        return 'Connection timed out';
      case DioExceptionType.connectionError:
        return 'Could not connect to peer';
      case DioExceptionType.badResponse:
        return 'Bad response: ${e.response?.statusCode}';
      default:
        return e.message ?? 'Network error';
    }
  }

  void dispose() {
    _dio.close();
  }
}

class _PullResult {
  final int changesReceived;
  final int changesApplied;

  _PullResult({required this.changesReceived, required this.changesApplied});
}

/// Result of a sync operation.
class SyncResult {
  final Peer peer;
  final bool success;
  final String? error;
  final int changesReceived;
  final int changesApplied;
  final int changesSent;
  final Duration duration;
  final List<String> warnings;

  const SyncResult({
    required this.peer,
    required this.success,
    this.error,
    this.changesReceived = 0,
    this.changesApplied = 0,
    this.changesSent = 0,
    required this.duration,
    this.warnings = const [],
  });

  factory SyncResult.success({
    required Peer peer,
    required int changesReceived,
    required int changesApplied,
    required int changesSent,
    required Duration duration,
    List<String> warnings = const [],
  }) {
    return SyncResult(
      peer: peer,
      success: true,
      changesReceived: changesReceived,
      changesApplied: changesApplied,
      changesSent: changesSent,
      duration: duration,
      warnings: warnings,
    );
  }

  factory SyncResult.failed({
    required Peer peer,
    required String error,
    required Duration duration,
  }) {
    return SyncResult(
      peer: peer,
      success: false,
      error: error,
      duration: duration,
    );
  }
}

/// Status information from a peer.
class PeerStatus {
  final String siteId;
  final int dbVersion;
  final ProtocolInfo protocol;

  const PeerStatus({
    required this.siteId,
    required this.dbVersion,
    required this.protocol,
  });

  factory PeerStatus.fromJson(Map<String, dynamic> json) {
    return PeerStatus(
      siteId: json['site_id'] as String,
      dbVersion: json['db_version'] as int,
      protocol: ProtocolInfo.fromJson(json['protocol'] as Map<String, dynamic>),
    );
  }
}

/// Protocol information from a peer.
class ProtocolInfo {
  final int version;
  final String format;
  final int schemaVersion;
  final String crsqliteVersion;
  final List<String> syncTables;

  const ProtocolInfo({
    required this.version,
    required this.format,
    required this.schemaVersion,
    required this.crsqliteVersion,
    required this.syncTables,
  });

  factory ProtocolInfo.fromJson(Map<String, dynamic> json) {
    return ProtocolInfo(
      version: json['version'] as int,
      format: json['format'] as String,
      schemaVersion: json['schema_version'] as int,
      crsqliteVersion: json['crsqlite_version'] as String,
      syncTables: (json['sync_tables'] as List).cast<String>(),
    );
  }
}
