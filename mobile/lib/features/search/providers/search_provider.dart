import 'dart:async';

import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../core/database/models/item.dart';
import '../../../main.dart';

/// Provider for search query.
final searchQueryProvider = StateProvider<String>((ref) => '');

/// Provider for search results with debouncing.
final searchResultsProvider = FutureProvider<List<Item>>((ref) async {
  final query = ref.watch(searchQueryProvider);

  if (query.trim().isEmpty) {
    return [];
  }

  // Debounce: wait for 300ms of no changes before searching
  await Future.delayed(const Duration(milliseconds: 300));

  // Check if query changed during debounce
  if (ref.read(searchQueryProvider) != query) {
    throw Exception('Query changed');
  }

  final database = ref.read(databaseProvider);
  return database.search(query);
});

/// Provider for search state with proper debouncing.
final searchStateProvider = StateNotifierProvider<SearchNotifier, SearchState>((ref) {
  final database = ref.read(databaseProvider);
  return SearchNotifier(database);
});

/// State for search.
class SearchState {
  final String query;
  final List<Item> results;
  final bool isSearching;
  final String? error;

  const SearchState({
    this.query = '',
    this.results = const [],
    this.isSearching = false,
    this.error,
  });

  SearchState copyWith({
    String? query,
    List<Item>? results,
    bool? isSearching,
    String? error,
  }) {
    return SearchState(
      query: query ?? this.query,
      results: results ?? this.results,
      isSearching: isSearching ?? this.isSearching,
      error: error,
    );
  }
}

/// Notifier for search with debouncing.
class SearchNotifier extends StateNotifier<SearchState> {
  final dynamic _database; // Database type
  Timer? _debounceTimer;

  SearchNotifier(this._database) : super(const SearchState());

  /// Update search query with debouncing.
  void setQuery(String query) {
    state = state.copyWith(query: query, error: null);

    _debounceTimer?.cancel();

    if (query.trim().isEmpty) {
      state = state.copyWith(results: [], isSearching: false);
      return;
    }

    state = state.copyWith(isSearching: true);

    _debounceTimer = Timer(const Duration(milliseconds: 300), () {
      _performSearch(query);
    });
  }

  Future<void> _performSearch(String query) async {
    try {
      final results = _database.search(query) as List<Item>;

      // Only update if query hasn't changed
      if (state.query == query) {
        state = state.copyWith(
          results: results,
          isSearching: false,
        );
      }
    } catch (e) {
      if (state.query == query) {
        state = state.copyWith(
          isSearching: false,
          error: e.toString(),
        );
      }
    }
  }

  /// Clear search.
  void clear() {
    _debounceTimer?.cancel();
    state = const SearchState();
  }

  @override
  void dispose() {
    _debounceTimer?.cancel();
    super.dispose();
  }
}
