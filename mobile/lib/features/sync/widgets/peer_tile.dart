import 'package:flutter/material.dart';

import '../../../core/database/models/peer.dart';

/// A list tile displaying a peer with sync controls.
class PeerTile extends StatelessWidget {
  final Peer peer;
  final bool isSyncing;
  final VoidCallback? onSync;
  final VoidCallback? onDelete;

  const PeerTile({
    super.key,
    required this.peer,
    this.isSyncing = false,
    this.onSync,
    this.onDelete,
  });

  @override
  Widget build(BuildContext context) {
    return Card(
      margin: const EdgeInsets.symmetric(horizontal: 16, vertical: 4),
      child: ListTile(
        leading: CircleAvatar(
          child: Icon(
            Icons.computer,
            color: Theme.of(context).colorScheme.onPrimaryContainer,
          ),
        ),
        title: Text(peer.name),
        subtitle: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Text(
              '${peer.address}:${peer.port}',
              style: Theme.of(context).textTheme.bodySmall?.copyWith(
                    fontFamily: 'monospace',
                  ),
            ),
            if (peer.lastSyncAt != null)
              Text(
                'Last sync: ${_formatLastSync(peer.lastSyncAt!)}',
                style: Theme.of(context).textTheme.bodySmall?.copyWith(
                      color: Theme.of(context).colorScheme.outline,
                    ),
              ),
          ],
        ),
        trailing: Row(
          mainAxisSize: MainAxisSize.min,
          children: [
            IconButton(
              icon: isSyncing
                  ? const SizedBox(
                      width: 20,
                      height: 20,
                      child: CircularProgressIndicator(strokeWidth: 2),
                    )
                  : const Icon(Icons.sync),
              onPressed: isSyncing ? null : onSync,
              tooltip: 'Sync now',
            ),
            IconButton(
              icon: const Icon(Icons.delete_outline),
              onPressed: onDelete,
              tooltip: 'Remove peer',
            ),
          ],
        ),
        isThreeLine: peer.lastSyncAt != null,
      ),
    );
  }

  String _formatLastSync(DateTime lastSync) {
    final now = DateTime.now();
    final diff = now.difference(lastSync);

    if (diff.inMinutes < 1) {
      return 'Just now';
    } else if (diff.inMinutes < 60) {
      return '${diff.inMinutes}m ago';
    } else if (diff.inHours < 24) {
      return '${diff.inHours}h ago';
    } else if (diff.inDays < 7) {
      return '${diff.inDays}d ago';
    } else {
      return '${lastSync.day}/${lastSync.month}/${lastSync.year}';
    }
  }
}
