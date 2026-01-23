import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../database/database.dart';

/// Provider for the database instance.
///
/// This is initialized in main.dart and overridden in ProviderScope.
/// Do not use directly in widgets - use feature-specific providers instead.
final databaseProvider = Provider<Database>((ref) {
  throw UnimplementedError('Database must be initialized in main()');
});
