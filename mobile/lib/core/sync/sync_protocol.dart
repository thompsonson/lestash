import 'sync_service.dart';

/// Sync protocol constants and compatibility checking.
class SyncProtocol {
  /// Current protocol version
  static const int version = 1;

  /// Current sync format
  static const String format = 'lestash-crsqlite-v1';

  /// Tables that are synced
  static const List<String> syncTables = [
    'items',
    'tags',
    'item_tags',
    'peers',
  ];

  /// Check compatibility with a remote peer.
  static CompatibilityResult checkCompatibility({
    required int localSchemaVersion,
    required String localCrsqliteVersion,
    required ProtocolInfo remoteProtocol,
  }) {
    final warnings = <String>[];

    // Protocol version must match exactly
    if (remoteProtocol.version != version) {
      return CompatibilityResult(
        isCompatible: false,
        reason: 'Protocol version mismatch: local=$version, remote=${remoteProtocol.version}',
      );
    }

    // Format must match exactly
    if (remoteProtocol.format != format) {
      return CompatibilityResult(
        isCompatible: false,
        reason: 'Sync format mismatch: local=$format, remote=${remoteProtocol.format}',
      );
    }

    // Schema version differences are warnings
    if (remoteProtocol.schemaVersion != localSchemaVersion) {
      warnings.add(
        'Schema version differs: local=$localSchemaVersion, remote=${remoteProtocol.schemaVersion}',
      );
    }

    // cr-sqlite version differences are warnings (compare major version)
    final localMajor = _getMajorVersion(localCrsqliteVersion);
    final remoteMajor = _getMajorVersion(remoteProtocol.crsqliteVersion);
    if (localMajor != remoteMajor) {
      warnings.add(
        'cr-sqlite major version differs: local=$localCrsqliteVersion, remote=${remoteProtocol.crsqliteVersion}',
      );
    }

    return CompatibilityResult(
      isCompatible: true,
      warnings: warnings,
    );
  }

  static int _getMajorVersion(String version) {
    final parts = version.split('.');
    if (parts.isEmpty) return 0;
    return int.tryParse(parts[0]) ?? 0;
  }
}

/// Result of a compatibility check.
class CompatibilityResult {
  final bool isCompatible;
  final String? reason;
  final List<String> warnings;

  const CompatibilityResult({
    required this.isCompatible,
    this.reason,
    this.warnings = const [],
  });
}
