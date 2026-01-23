import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:url_launcher/url_launcher.dart';

import '../../../main.dart';

/// Settings screen.
class SettingsScreen extends ConsumerWidget {
  const SettingsScreen({super.key});

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final database = ref.watch(databaseProvider);

    return Scaffold(
      appBar: AppBar(
        title: const Text('Settings'),
      ),
      body: ListView(
        children: [
          // App info section
          _buildSectionHeader(context, 'About'),
          ListTile(
            leading: const Icon(Icons.info_outline),
            title: const Text('Le Stash Mobile'),
            subtitle: const Text('Version 1.0.0'),
          ),
          ListTile(
            leading: const Icon(Icons.code),
            title: const Text('Source Code'),
            subtitle: const Text('View on GitHub'),
            trailing: const Icon(Icons.open_in_new),
            onTap: () => _openUrl('https://github.com/thompsonson/lestash'),
          ),

          const Divider(),

          // Database section
          _buildSectionHeader(context, 'Database'),
          ListTile(
            leading: const Icon(Icons.storage),
            title: const Text('Database Path'),
            subtitle: Text(
              database.path ?? 'Unknown',
              style: const TextStyle(fontFamily: 'monospace', fontSize: 12),
            ),
          ),
          ListTile(
            leading: const Icon(Icons.numbers),
            title: const Text('Database Version'),
            subtitle: Text(database.getDbVersion().toString()),
          ),
          ListTile(
            leading: const Icon(Icons.fingerprint),
            title: const Text('Site ID'),
            subtitle: Text(
              database.getSiteId() ?? 'Not available',
              style: const TextStyle(fontFamily: 'monospace', fontSize: 12),
            ),
          ),

          const Divider(),

          // Sync section
          _buildSectionHeader(context, 'Sync'),
          ListTile(
            leading: const Icon(Icons.refresh),
            title: const Text('Rebuild Search Index'),
            subtitle: const Text('Regenerate full-text search index'),
            onTap: () => _rebuildFtsIndex(context, ref),
          ),

          const Divider(),

          // Help section
          _buildSectionHeader(context, 'Help'),
          ListTile(
            leading: const Icon(Icons.help_outline),
            title: const Text('Documentation'),
            trailing: const Icon(Icons.open_in_new),
            onTap: () => _openUrl('https://github.com/thompsonson/lestash#readme'),
          ),
          ListTile(
            leading: const Icon(Icons.bug_report_outlined),
            title: const Text('Report Issue'),
            trailing: const Icon(Icons.open_in_new),
            onTap: () => _openUrl('https://github.com/thompsonson/lestash/issues'),
          ),
        ],
      ),
    );
  }

  Widget _buildSectionHeader(BuildContext context, String title) {
    return Padding(
      padding: const EdgeInsets.fromLTRB(16, 16, 16, 8),
      child: Text(
        title,
        style: Theme.of(context).textTheme.titleSmall?.copyWith(
              color: Theme.of(context).colorScheme.primary,
            ),
      ),
    );
  }

  Future<void> _openUrl(String url) async {
    final uri = Uri.parse(url);
    if (await canLaunchUrl(uri)) {
      await launchUrl(uri, mode: LaunchMode.externalApplication);
    }
  }

  Future<void> _rebuildFtsIndex(BuildContext context, WidgetRef ref) async {
    final database = ref.read(databaseProvider);

    showDialog(
      context: context,
      barrierDismissible: false,
      builder: (context) => const AlertDialog(
        content: Row(
          children: [
            CircularProgressIndicator(),
            SizedBox(width: 16),
            Text('Rebuilding index...'),
          ],
        ),
      ),
    );

    try {
      final count = database.rebuildFtsIndex();

      if (context.mounted) {
        Navigator.of(context).pop();
        ScaffoldMessenger.of(context).showSnackBar(
          SnackBar(content: Text('Indexed $count items')),
        );
      }
    } catch (e) {
      if (context.mounted) {
        Navigator.of(context).pop();
        ScaffoldMessenger.of(context).showSnackBar(
          SnackBar(
            content: Text('Error: $e'),
            backgroundColor: Colors.red,
          ),
        );
      }
    }
  }
}
