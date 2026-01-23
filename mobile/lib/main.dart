import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import 'app.dart';
import 'core/database/database.dart';

Future<void> main() async {
  WidgetsFlutterBinding.ensureInitialized();

  // Initialize database
  final database = Database();
  await database.open();

  runApp(
    ProviderScope(
      overrides: [
        databaseProvider.overrideWithValue(database),
      ],
      child: const LeStashApp(),
    ),
  );
}

/// Global database provider - initialized in main()
final databaseProvider = Provider<Database>((ref) {
  throw UnimplementedError('Database must be initialized in main()');
});
