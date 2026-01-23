import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../providers/items_provider.dart';

/// Bottom sheet for filtering items.
class ItemFilters extends ConsumerWidget {
  const ItemFilters({super.key});

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final sourceTypes = ref.watch(sourceTypesProvider);
    final itemsState = ref.watch(itemsProvider);
    final currentFilter = itemsState.sourceTypeFilter;

    return Container(
      padding: const EdgeInsets.all(16),
      child: Column(
        mainAxisSize: MainAxisSize.min,
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Row(
            children: [
              Text(
                'Filter by source',
                style: Theme.of(context).textTheme.titleMedium,
              ),
              const Spacer(),
              if (currentFilter != null)
                TextButton(
                  onPressed: () {
                    ref.read(itemsProvider.notifier).setSourceTypeFilter(null);
                    Navigator.of(context).pop();
                  },
                  child: const Text('Clear'),
                ),
            ],
          ),
          const SizedBox(height: 16),
          Wrap(
            spacing: 8,
            runSpacing: 8,
            children: [
              _FilterChip(
                label: 'All',
                isSelected: currentFilter == null,
                onSelected: () {
                  ref.read(itemsProvider.notifier).setSourceTypeFilter(null);
                  Navigator.of(context).pop();
                },
              ),
              ...sourceTypes.map((sourceType) => _FilterChip(
                    label: _formatSourceType(sourceType),
                    isSelected: currentFilter == sourceType,
                    onSelected: () {
                      ref.read(itemsProvider.notifier).setSourceTypeFilter(sourceType);
                      Navigator.of(context).pop();
                    },
                  )),
            ],
          ),
          const SizedBox(height: 16),
        ],
      ),
    );
  }

  String _formatSourceType(String sourceType) {
    switch (sourceType) {
      case 'linkedin':
        return 'LinkedIn';
      case 'youtube':
        return 'YouTube';
      case 'arxiv':
        return 'arXiv';
      case 'bluesky':
        return 'Bluesky';
      case 'microblog':
        return 'Micro.blog';
      case 'github':
        return 'GitHub';
      default:
        return sourceType;
    }
  }
}

class _FilterChip extends StatelessWidget {
  final String label;
  final bool isSelected;
  final VoidCallback onSelected;

  const _FilterChip({
    required this.label,
    required this.isSelected,
    required this.onSelected,
  });

  @override
  Widget build(BuildContext context) {
    return FilterChip(
      label: Text(label),
      selected: isSelected,
      onSelected: (_) => onSelected(),
    );
  }
}
