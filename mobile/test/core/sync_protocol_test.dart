import 'package:flutter_test/flutter_test.dart';

import 'package:lestash_mobile/core/sync/sync_protocol.dart';

void main() {
  group('SyncProtocol', () {
    test('has correct protocol version', () {
      expect(SyncProtocol.protocolVersion, '1.0.0');
    });

    test('returns correct format name', () {
      expect(SyncProtocol.formatName, 'lestash-crsqlite-v1');
    });

    test('generates version info', () {
      final versionInfo = SyncProtocol.getVersionInfo();

      expect(versionInfo, isA<Map<String, dynamic>>());
      expect(versionInfo['protocol_version'], SyncProtocol.protocolVersion);
      expect(versionInfo['format'], SyncProtocol.formatName);
    });
  });

  group('VersionCompatibility', () {
    test('detects compatible versions', () {
      final result = SyncProtocol.checkCompatibility({
        'protocol_version': '1.0.0',
        'format': 'lestash-crsqlite-v1',
      });

      expect(result.isCompatible, true);
    });

    test('detects incompatible protocol version', () {
      final result = SyncProtocol.checkCompatibility({
        'protocol_version': '2.0.0',
        'format': 'lestash-crsqlite-v1',
      });

      expect(result.isCompatible, false);
      expect(result.reason, contains('protocol'));
    });

    test('detects incompatible format', () {
      final result = SyncProtocol.checkCompatibility({
        'protocol_version': '1.0.0',
        'format': 'unknown-format',
      });

      expect(result.isCompatible, false);
      expect(result.reason, contains('format'));
    });
  });
}
