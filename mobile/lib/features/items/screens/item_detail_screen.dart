import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:share_plus/share_plus.dart';
import 'package:url_launcher/url_launcher.dart';

import '../../../core/database/models/item.dart';
import '../providers/items_provider.dart';

/// Screen displaying the details of a single item.
class ItemDetailScreen extends ConsumerWidget {
  final int itemId;

  const ItemDetailScreen({
    super.key,
    required this.itemId,
  });

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final itemAsync = ref.watch(itemProvider(itemId));
    final tagsAsync = ref.watch(itemTagsProvider(itemId));

    return itemAsync.when(
      loading: () => const Scaffold(
        body: Center(child: CircularProgressIndicator()),
      ),
      error: (error, _) => Scaffold(
        appBar: AppBar(),
        body: Center(child: Text('Error: $error')),
      ),
      data: (item) {
        if (item == null) {
          return Scaffold(
            appBar: AppBar(),
            body: const Center(child: Text('Item not found')),
          );
        }

        return Scaffold(
          appBar: AppBar(
            title: Text(item.sourceTypeDisplay),
            actions: [
              if (item.url != null)
                IconButton(
                  icon: const Icon(Icons.open_in_new),
                  onPressed: () => _openUrl(item.url!),
                  tooltip: 'Open in browser',
                ),
              IconButton(
                icon: const Icon(Icons.share),
                onPressed: () => _shareItem(item),
                tooltip: 'Share',
              ),
            ],
          ),
          body: SingleChildScrollView(
            padding: const EdgeInsets.all(16),
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                // Title
                if (item.title != null) ...[
                  Text(
                    item.title!,
                    style: Theme.of(context).textTheme.headlineSmall,
                  ),
                  const SizedBox(height: 8),
                ],

                // Author and date
                _buildMetadata(context, item),
                const SizedBox(height: 16),

                // Tags
                tagsAsync.when(
                  loading: () => const SizedBox.shrink(),
                  error: (_, __) => const SizedBox.shrink(),
                  data: (tags) {
                    if (tags.isEmpty) return const SizedBox.shrink();
                    return Padding(
                      padding: const EdgeInsets.only(bottom: 16),
                      child: Wrap(
                        spacing: 8,
                        runSpacing: 8,
                        children: tags
                            .map((tag) => Chip(label: Text(tag.name)))
                            .toList(),
                      ),
                    );
                  },
                ),

                // Content
                const Divider(),
                const SizedBox(height: 8),
                SelectableText(
                  item.content,
                  style: Theme.of(context).textTheme.bodyLarge,
                ),
              ],
            ),
          ),
        );
      },
    );
  }

  Widget _buildMetadata(BuildContext context, Item item) {
    final parts = <String>[];

    if (item.author != null) {
      parts.add(item.author!);
    }

    if (item.createdAt != null) {
      parts.add(_formatDate(item.createdAt!));
    }

    if (parts.isEmpty) return const SizedBox.shrink();

    return Row(
      children: [
        Icon(
          Icons.person_outline,
          size: 16,
          color: Theme.of(context).colorScheme.outline,
        ),
        const SizedBox(width: 4),
        Expanded(
          child: Text(
            parts.join(' • '),
            style: Theme.of(context).textTheme.bodyMedium?.copyWith(
                  color: Theme.of(context).colorScheme.outline,
                ),
          ),
        ),
      ],
    );
  }

  String _formatDate(DateTime date) {
    final now = DateTime.now();
    final diff = now.difference(date);

    if (diff.inDays == 0) {
      return 'Today';
    } else if (diff.inDays == 1) {
      return 'Yesterday';
    } else if (diff.inDays < 7) {
      return '${diff.inDays} days ago';
    } else {
      return '${date.day}/${date.month}/${date.year}';
    }
  }

  Future<void> _openUrl(String url) async {
    final uri = Uri.parse(url);
    if (await canLaunchUrl(uri)) {
      await launchUrl(uri, mode: LaunchMode.externalApplication);
    }
  }

  Future<void> _shareItem(Item item) async {
    final text = StringBuffer();
    if (item.title != null) {
      text.writeln(item.title);
      text.writeln();
    }
    text.write(item.content);
    if (item.url != null) {
      text.writeln();
      text.write(item.url);
    }

    await Share.share(text.toString());
  }
}
