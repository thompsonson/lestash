import 'package:flutter_test/flutter_test.dart';

import 'package:lestash_mobile/core/database/models/item.dart';
import 'package:lestash_mobile/core/database/models/tag.dart';
import 'package:lestash_mobile/core/database/models/peer.dart';

void main() {
  group('Item model', () {
    test('creates item from map', () {
      final map = {
        'id': 1,
        'url': 'https://example.com',
        'title': 'Example',
        'description': 'A test item',
        'source_type': 'bookmark',
        'created_at': '2024-01-01T00:00:00.000Z',
        'updated_at': '2024-01-01T00:00:00.000Z',
      };

      final item = Item.fromMap(map);

      expect(item.id, 1);
      expect(item.url, 'https://example.com');
      expect(item.title, 'Example');
      expect(item.sourceType, 'bookmark');
    });
  });

  group('Tag model', () {
    test('creates tag from map', () {
      final map = {
        'id': 1,
        'name': 'flutter',
        'created_at': '2024-01-01T00:00:00.000Z',
      };

      final tag = Tag.fromMap(map);

      expect(tag.id, 1);
      expect(tag.name, 'flutter');
    });
  });

  group('Peer model', () {
    test('creates peer from map', () {
      final map = {
        'id': 1,
        'site_id': 'abc123',
        'name': 'MacBook',
        'address': '192.168.1.100',
        'port': 8384,
        'last_sync_at': null,
      };

      final peer = Peer.fromMap(map);

      expect(peer.id, 1);
      expect(peer.siteId, 'abc123');
      expect(peer.name, 'MacBook');
      expect(peer.port, 8384);
    });
  });
}
