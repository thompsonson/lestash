import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../core/database/database.dart';
import '../../../core/database/models/item.dart';
import '../../../core/database/models/tag.dart';
import '../../../main.dart';

/// Provider for the list of items with filtering.
final itemsProvider = StateNotifierProvider<ItemsNotifier, ItemsState>((ref) {
  final database = ref.watch(databaseProvider);
  return ItemsNotifier(database);
});

/// Provider for a single item by ID.
final itemProvider = FutureProvider.family<Item?, int>((ref, id) async {
  final database = ref.watch(databaseProvider);
  return database.getItem(id);
});

/// Provider for tags of a specific item.
final itemTagsProvider = FutureProvider.family<List<Tag>, int>((ref, itemId) async {
  final database = ref.watch(databaseProvider);
  return database.getTagsForItem(itemId);
});

/// Provider for available source types.
final sourceTypesProvider = Provider<List<String>>((ref) {
  final database = ref.watch(databaseProvider);
  return database.getSourceTypes();
});

/// State for the items list.
class ItemsState {
  final List<Item> items;
  final bool isLoading;
  final String? error;
  final String? sourceTypeFilter;
  final bool hasMore;
  final int currentPage;

  const ItemsState({
    this.items = const [],
    this.isLoading = false,
    this.error,
    this.sourceTypeFilter,
    this.hasMore = true,
    this.currentPage = 0,
  });

  ItemsState copyWith({
    List<Item>? items,
    bool? isLoading,
    String? error,
    String? sourceTypeFilter,
    bool? hasMore,
    int? currentPage,
  }) {
    return ItemsState(
      items: items ?? this.items,
      isLoading: isLoading ?? this.isLoading,
      error: error,
      sourceTypeFilter: sourceTypeFilter ?? this.sourceTypeFilter,
      hasMore: hasMore ?? this.hasMore,
      currentPage: currentPage ?? this.currentPage,
    );
  }
}

/// Notifier for items list state.
class ItemsNotifier extends StateNotifier<ItemsState> {
  final Database _database;
  static const int _pageSize = 20;

  ItemsNotifier(this._database) : super(const ItemsState()) {
    loadItems();
  }

  /// Load initial items.
  Future<void> loadItems() async {
    state = state.copyWith(isLoading: true, error: null);

    try {
      final items = _database.getAllItems(
        sourceType: state.sourceTypeFilter,
        limit: _pageSize,
        offset: 0,
      );

      state = state.copyWith(
        items: items,
        isLoading: false,
        hasMore: items.length >= _pageSize,
        currentPage: 0,
      );
    } catch (e) {
      state = state.copyWith(
        isLoading: false,
        error: e.toString(),
      );
    }
  }

  /// Load more items (pagination).
  Future<void> loadMore() async {
    if (state.isLoading || !state.hasMore) return;

    state = state.copyWith(isLoading: true);

    try {
      final nextPage = state.currentPage + 1;
      final newItems = _database.getAllItems(
        sourceType: state.sourceTypeFilter,
        limit: _pageSize,
        offset: nextPage * _pageSize,
      );

      state = state.copyWith(
        items: [...state.items, ...newItems],
        isLoading: false,
        hasMore: newItems.length >= _pageSize,
        currentPage: nextPage,
      );
    } catch (e) {
      state = state.copyWith(
        isLoading: false,
        error: e.toString(),
      );
    }
  }

  /// Set source type filter.
  void setSourceTypeFilter(String? sourceType) {
    if (state.sourceTypeFilter == sourceType) return;

    state = state.copyWith(
      sourceTypeFilter: sourceType,
      items: [],
      currentPage: 0,
      hasMore: true,
    );
    loadItems();
  }

  /// Refresh the items list.
  Future<void> refresh() async {
    state = state.copyWith(
      items: [],
      currentPage: 0,
      hasMore: true,
    );
    await loadItems();
  }
}
