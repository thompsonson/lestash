import 'package:flutter/material.dart';

import '../../../core/database/models/item.dart';

/// A list tile displaying an item summary.
class ItemTile extends StatelessWidget {
  final Item item;
  final VoidCallback? onTap;

  const ItemTile({
    super.key,
    required this.item,
    this.onTap,
  });

  @override
  Widget build(BuildContext context) {
    return Card(
      margin: const EdgeInsets.symmetric(horizontal: 8, vertical: 4),
      child: InkWell(
        onTap: onTap,
        borderRadius: BorderRadius.circular(12),
        child: Padding(
          padding: const EdgeInsets.all(12),
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              // Source type badge
              Row(
                children: [
                  _buildSourceBadge(context),
                  const Spacer(),
                  if (item.createdAt != null)
                    Text(
                      _formatDate(item.createdAt!),
                      style: Theme.of(context).textTheme.bodySmall?.copyWith(
                            color: Theme.of(context).colorScheme.outline,
                          ),
                    ),
                ],
              ),
              const SizedBox(height: 8),

              // Title
              if (item.title != null) ...[
                Text(
                  item.title!,
                  style: Theme.of(context).textTheme.titleMedium,
                  maxLines: 2,
                  overflow: TextOverflow.ellipsis,
                ),
                const SizedBox(height: 4),
              ],

              // Content preview
              Text(
                item.getContentPreview(maxLength: 150),
                style: Theme.of(context).textTheme.bodyMedium?.copyWith(
                      color: Theme.of(context).colorScheme.onSurfaceVariant,
                    ),
                maxLines: 3,
                overflow: TextOverflow.ellipsis,
              ),

              // Author
              if (item.author != null) ...[
                const SizedBox(height: 8),
                Row(
                  children: [
                    Icon(
                      Icons.person_outline,
                      size: 14,
                      color: Theme.of(context).colorScheme.outline,
                    ),
                    const SizedBox(width: 4),
                    Expanded(
                      child: Text(
                        item.author!,
                        style: Theme.of(context).textTheme.bodySmall?.copyWith(
                              color: Theme.of(context).colorScheme.outline,
                            ),
                        maxLines: 1,
                        overflow: TextOverflow.ellipsis,
                      ),
                    ),
                  ],
                ),
              ],
            ],
          ),
        ),
      ),
    );
  }

  Widget _buildSourceBadge(BuildContext context) {
    final color = _getSourceColor();
    return Container(
      padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 4),
      decoration: BoxDecoration(
        color: color.withOpacity(0.1),
        borderRadius: BorderRadius.circular(12),
      ),
      child: Row(
        mainAxisSize: MainAxisSize.min,
        children: [
          Icon(
            _getSourceIcon(),
            size: 14,
            color: color,
          ),
          const SizedBox(width: 4),
          Text(
            item.sourceTypeDisplay,
            style: Theme.of(context).textTheme.labelSmall?.copyWith(
                  color: color,
                  fontWeight: FontWeight.w500,
                ),
          ),
        ],
      ),
    );
  }

  Color _getSourceColor() {
    switch (item.sourceType) {
      case 'linkedin':
        return const Color(0xFF0A66C2);
      case 'youtube':
        return const Color(0xFFFF0000);
      case 'arxiv':
        return const Color(0xFFB31B1B);
      case 'bluesky':
        return const Color(0xFF0085FF);
      case 'microblog':
        return const Color(0xFFFF8800);
      case 'github':
        return const Color(0xFF24292E);
      default:
        return Colors.grey;
    }
  }

  IconData _getSourceIcon() {
    switch (item.sourceType) {
      case 'linkedin':
        return Icons.business;
      case 'youtube':
        return Icons.play_circle_outline;
      case 'arxiv':
        return Icons.science;
      case 'bluesky':
        return Icons.cloud;
      case 'microblog':
        return Icons.rss_feed;
      case 'github':
        return Icons.code;
      default:
        return Icons.article;
    }
  }

  String _formatDate(DateTime date) {
    final now = DateTime.now();
    final diff = now.difference(date);

    if (diff.inDays == 0) {
      return 'Today';
    } else if (diff.inDays == 1) {
      return 'Yesterday';
    } else if (diff.inDays < 7) {
      return '${diff.inDays}d ago';
    } else if (diff.inDays < 30) {
      return '${(diff.inDays / 7).floor()}w ago';
    } else {
      return '${date.day}/${date.month}';
    }
  }
}
