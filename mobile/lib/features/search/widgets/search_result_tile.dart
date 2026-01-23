import 'package:flutter/material.dart';

import '../../../core/database/models/item.dart';

/// A list tile for search results with query highlighting.
class SearchResultTile extends StatelessWidget {
  final Item item;
  final String query;
  final VoidCallback? onTap;

  const SearchResultTile({
    super.key,
    required this.item,
    required this.query,
    this.onTap,
  });

  @override
  Widget build(BuildContext context) {
    return ListTile(
      title: item.title != null
          ? _buildHighlightedText(
              context,
              item.title!,
              query,
              Theme.of(context).textTheme.titleMedium!,
            )
          : Text(
              item.sourceTypeDisplay,
              style: Theme.of(context).textTheme.titleMedium,
            ),
      subtitle: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          const SizedBox(height: 4),
          _buildHighlightedText(
            context,
            item.getContentPreview(maxLength: 100),
            query,
            Theme.of(context).textTheme.bodyMedium!.copyWith(
                  color: Theme.of(context).colorScheme.onSurfaceVariant,
                ),
          ),
          const SizedBox(height: 4),
          Row(
            children: [
              Container(
                padding: const EdgeInsets.symmetric(horizontal: 6, vertical: 2),
                decoration: BoxDecoration(
                  color: Theme.of(context).colorScheme.surfaceContainerHighest,
                  borderRadius: BorderRadius.circular(4),
                ),
                child: Text(
                  item.sourceTypeDisplay,
                  style: Theme.of(context).textTheme.labelSmall,
                ),
              ),
              if (item.author != null) ...[
                const SizedBox(width: 8),
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
            ],
          ),
        ],
      ),
      onTap: onTap,
      contentPadding: const EdgeInsets.symmetric(horizontal: 16, vertical: 8),
    );
  }

  Widget _buildHighlightedText(
    BuildContext context,
    String text,
    String query,
    TextStyle baseStyle,
  ) {
    if (query.isEmpty) {
      return Text(text, style: baseStyle, maxLines: 2, overflow: TextOverflow.ellipsis);
    }

    final matches = <TextSpan>[];
    final lowerText = text.toLowerCase();
    final lowerQuery = query.toLowerCase();
    var currentIndex = 0;

    while (true) {
      final matchIndex = lowerText.indexOf(lowerQuery, currentIndex);
      if (matchIndex == -1) {
        // Add remaining text
        if (currentIndex < text.length) {
          matches.add(TextSpan(text: text.substring(currentIndex)));
        }
        break;
      }

      // Add text before match
      if (matchIndex > currentIndex) {
        matches.add(TextSpan(text: text.substring(currentIndex, matchIndex)));
      }

      // Add highlighted match
      matches.add(TextSpan(
        text: text.substring(matchIndex, matchIndex + query.length),
        style: TextStyle(
          backgroundColor: Theme.of(context).colorScheme.primaryContainer,
          color: Theme.of(context).colorScheme.onPrimaryContainer,
        ),
      ));

      currentIndex = matchIndex + query.length;
    }

    return RichText(
      text: TextSpan(style: baseStyle, children: matches),
      maxLines: 2,
      overflow: TextOverflow.ellipsis,
    );
  }
}
