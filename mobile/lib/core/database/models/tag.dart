/// Represents a tag for organizing items.
class Tag {
  final int id;
  final String name;

  const Tag({
    required this.id,
    required this.name,
  });

  /// Create a Tag from a database row.
  factory Tag.fromRow(Map<String, dynamic> row) {
    return Tag(
      id: row['id'] as int,
      name: row['name'] as String,
    );
  }

  @override
  String toString() => 'Tag(id: $id, name: $name)';

  @override
  bool operator ==(Object other) {
    if (identical(this, other)) return true;
    return other is Tag && other.id == id;
  }

  @override
  int get hashCode => id.hashCode;
}
