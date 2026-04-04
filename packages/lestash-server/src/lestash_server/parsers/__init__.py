"""File parsers for import."""

from lestash_server.parsers.html_page import parse_html_page
from lestash_server.parsers.json_items import parse_json_items

__all__ = ["parse_json_items", "parse_html_page"]
