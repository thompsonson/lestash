import 'dart:convert';

/// Represents a saved item in the knowledge base.
class Item {
  final int id;
  final String sourceType;
  final String sourceId;
  final String? url;
  final String? title;
  final String content;
  final String? author;
  final DateTime? createdAt;
  final DateTime fetchedAt;
  final bool isOwnContent;
  final Map<String, dynamic>? metadata;

  const Item({
    required this.id,
    required this.sourceType,
    required this.sourceId,
    this.url,
    this.title,
    required this.content,
    this.author,
    this.createdAt,
    required this.fetchedAt,
    this.isOwnContent = false,
    this.metadata,
  });

  /// Create an Item from a database row.
  factory Item.fromRow(Map<String, dynamic> row) {
    return Item(
      id: row['id'] as int,
      sourceType: row['source_type'] as String,
      sourceId: row['source_id'] as String,
      url: row['url'] as String?,
      title: row['title'] as String?,
      content: row['content'] as String,
      author: row['author'] as String?,
      createdAt: row['created_at'] != null
          ? DateTime.parse(row['created_at'] as String)
          : null,
      fetchedAt: DateTime.parse(row['fetched_at'] as String),
      isOwnContent: (row['is_own_content'] as int?) == 1,
      metadata: row['metadata'] != null
          ? jsonDecode(row['metadata'] as String) as Map<String, dynamic>
          : null,
    );
  }

  /// Get a display-friendly source type name.
  String get sourceTypeDisplay {
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

  /// Get a truncated preview of the content.
  String getContentPreview({int maxLength = 200}) {
    if (content.length <= maxLength) return content;
    return '${content.substring(0, maxLength)}...';
  }

  @override
  String toString() {
    return 'Item(id: $id, sourceType: $sourceType, title: $title)';
  }

  @override
  bool operator ==(Object other) {
    if (identical(this, other)) return true;
    return other is Item && other.id == id;
  }

  @override
  int get hashCode => id.hashCode;
}
