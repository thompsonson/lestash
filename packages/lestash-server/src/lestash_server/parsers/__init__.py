"""File parsers for import."""

from lestash.importers.json_items import parse_json_items

from lestash_server.parsers.html_page import parse_html_page

__all__ = ["parse_html_page", "parse_json_items"]
