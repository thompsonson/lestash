import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../core/database/models/peer.dart';
import '../../../core/providers/sync_provider.dart';
import '../providers/sync_status_provider.dart';
import '../widgets/peer_tile.dart';

/// Screen for managing sync and peers.
class SyncScreen extends ConsumerWidget {
  const SyncScreen({super.key});

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final localInfo = ref.watch(localSyncInfoProvider);
    final peers = ref.watch(peerListProvider);
    final syncState = ref.watch(syncStateProvider);

    return Scaffold(
      appBar: AppBar(
        title: const Text('Sync'),
        actions: [
          IconButton(
            icon: const Icon(Icons.add),
            onPressed: () => _showAddPeerDialog(context, ref),
            tooltip: 'Add peer',
          ),
        ],
      ),
      body: ListView(
        children: [
          // Local info card
          Card(
            margin: const EdgeInsets.all(16),
            child: Padding(
              padding: const EdgeInsets.all(16),
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  Text(
                    'This Device',
                    style: Theme.of(context).textTheme.titleMedium,
                  ),
                  const SizedBox(height: 8),
                  _buildInfoRow(
                    context,
                    'Site ID',
                    localInfo.siteId ?? 'Not available',
                  ),
                  _buildInfoRow(
                    context,
                    'Database Version',
                    localInfo.dbVersion.toString(),
                  ),
                ],
              ),
            ),
          ),

          // Peers section
          Padding(
            padding: const EdgeInsets.symmetric(horizontal: 16),
            child: Text(
              'Peers',
              style: Theme.of(context).textTheme.titleMedium,
            ),
          ),
          const SizedBox(height: 8),

          if (peers.isEmpty)
            Padding(
              padding: const EdgeInsets.all(32),
              child: Center(
                child: Column(
                  children: [
                    Icon(
                      Icons.devices,
                      size: 48,
                      color: Theme.of(context).colorScheme.outline,
                    ),
                    const SizedBox(height: 16),
                    Text(
                      'No peers configured',
                      style: Theme.of(context).textTheme.bodyLarge?.copyWith(
                            color: Theme.of(context).colorScheme.outline,
                          ),
                    ),
                    const SizedBox(height: 8),
                    Text(
                      'Add your desktop to start syncing',
                      style: Theme.of(context).textTheme.bodyMedium?.copyWith(
                            color: Theme.of(context).colorScheme.outline,
                          ),
                    ),
                    const SizedBox(height: 16),
                    FilledButton.icon(
                      onPressed: () => _showAddPeerDialog(context, ref),
                      icon: const Icon(Icons.add),
                      label: const Text('Add Peer'),
                    ),
                  ],
                ),
              ),
            )
          else
            ...peers.map((peer) => PeerTile(
                  peer: peer,
                  isSyncing: syncState.isSyncing,
                  onSync: () => _syncWithPeer(context, ref, peer),
                  onDelete: () => _deletePeer(context, ref, peer),
                )),

          // Last sync result
          if (syncState.lastResult != null) ...[
            const SizedBox(height: 16),
            Padding(
              padding: const EdgeInsets.symmetric(horizontal: 16),
              child: _buildLastSyncCard(context, syncState.lastResult!),
            ),
          ],
        ],
      ),
    );
  }

  Widget _buildInfoRow(BuildContext context, String label, String value) {
    return Padding(
      padding: const EdgeInsets.symmetric(vertical: 4),
      child: Row(
        mainAxisAlignment: MainAxisAlignment.spaceBetween,
        children: [
          Text(
            label,
            style: Theme.of(context).textTheme.bodyMedium?.copyWith(
                  color: Theme.of(context).colorScheme.outline,
                ),
          ),
          Text(
            value,
            style: Theme.of(context).textTheme.bodyMedium?.copyWith(
                  fontFamily: 'monospace',
                ),
          ),
        ],
      ),
    );
  }

  Widget _buildLastSyncCard(BuildContext context, SyncResult result) {
    return Card(
      color: result.success
          ? Colors.green.withOpacity(0.1)
          : Colors.red.withOpacity(0.1),
      child: Padding(
        padding: const EdgeInsets.all(16),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Row(
              children: [
                Icon(
                  result.success ? Icons.check_circle : Icons.error,
                  color: result.success ? Colors.green : Colors.red,
                ),
                const SizedBox(width: 8),
                Text(
                  result.success ? 'Sync successful' : 'Sync failed',
                  style: Theme.of(context).textTheme.titleSmall,
                ),
              ],
            ),
            const SizedBox(height: 8),
            if (result.success) ...[
              Text('Received: ${result.changesReceived} changes'),
              Text('Applied: ${result.changesApplied} changes'),
            ] else
              Text(result.error ?? 'Unknown error'),
            Text(
              'Duration: ${result.duration.inMilliseconds}ms',
              style: Theme.of(context).textTheme.bodySmall,
            ),
          ],
        ),
      ),
    );
  }

  Future<void> _showAddPeerDialog(BuildContext context, WidgetRef ref) async {
    final nameController = TextEditingController();
    final addressController = TextEditingController();
    final portController = TextEditingController(text: '8384');

    final result = await showDialog<bool>(
      context: context,
      builder: (context) => AlertDialog(
        title: const Text('Add Peer'),
        content: Column(
          mainAxisSize: MainAxisSize.min,
          children: [
            TextField(
              controller: nameController,
              decoration: const InputDecoration(
                labelText: 'Name',
                hintText: 'e.g., MacBook Pro',
              ),
            ),
            const SizedBox(height: 16),
            TextField(
              controller: addressController,
              decoration: const InputDecoration(
                labelText: 'IP Address',
                hintText: 'e.g., 192.168.1.100',
              ),
              keyboardType: TextInputType.number,
            ),
            const SizedBox(height: 16),
            TextField(
              controller: portController,
              decoration: const InputDecoration(
                labelText: 'Port',
                hintText: '8384',
              ),
              keyboardType: TextInputType.number,
            ),
          ],
        ),
        actions: [
          TextButton(
            onPressed: () => Navigator.of(context).pop(false),
            child: const Text('Cancel'),
          ),
          FilledButton(
            onPressed: () => Navigator.of(context).pop(true),
            child: const Text('Add'),
          ),
        ],
      ),
    );

    if (result == true) {
      final name = nameController.text.trim();
      final address = addressController.text.trim();
      final port = int.tryParse(portController.text) ?? 8384;

      if (name.isNotEmpty && address.isNotEmpty) {
        await ref.read(peerListProvider.notifier).addPeer(
              siteId: 'unknown', // Will be updated on first sync
              name: name,
              address: address,
              port: port,
            );
      }
    }
  }

  Future<void> _syncWithPeer(BuildContext context, WidgetRef ref, Peer peer) async {
    final result = await ref.read(syncStateProvider.notifier).syncWithPeer(peer);

    if (context.mounted) {
      ScaffoldMessenger.of(context).showSnackBar(
        SnackBar(
          content: Text(
            result.success
                ? 'Synced ${result.changesApplied} changes'
                : 'Sync failed: ${result.error}',
          ),
          backgroundColor: result.success ? Colors.green : Colors.red,
        ),
      );

      // Refresh peer list to show updated sync time
      ref.read(peerListProvider.notifier).refresh();
    }
  }

  Future<void> _deletePeer(BuildContext context, WidgetRef ref, Peer peer) async {
    final confirmed = await showDialog<bool>(
      context: context,
      builder: (context) => AlertDialog(
        title: const Text('Delete Peer'),
        content: Text('Remove "${peer.name}" from peers?'),
        actions: [
          TextButton(
            onPressed: () => Navigator.of(context).pop(false),
            child: const Text('Cancel'),
          ),
          FilledButton(
            onPressed: () => Navigator.of(context).pop(true),
            style: FilledButton.styleFrom(backgroundColor: Colors.red),
            child: const Text('Delete'),
          ),
        ],
      ),
    );

    if (confirmed == true) {
      ref.read(peerListProvider.notifier).deletePeer(peer.id);
    }
  }
}
