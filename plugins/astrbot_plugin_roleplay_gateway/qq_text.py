"""Format QQ bubble edges without rewriting the model's internal layout."""

import re


# A paragraph separator may arrive after its preceding sentence was sent.
# Remove only complete whitespace-only edge lines; keep body indentation and
# inline/trailing spaces. The lossless stream segmenter remains unchanged.
_LEADING_BLANK_LINES = re.compile(r"\A(?:[^\S\r\n\u2028\u2029]*(?:\r\n|[\r\n\u2028\u2029]))+")
_TRAILING_BLANK_LINES = re.compile(r"(?:(?:\r\n|[\r\n\u2028\u2029])[^\S\r\n\u2028\u2029]*)+\Z")


def format_qq_bubble(text: str) -> str:
    """Trim separator lines at bubble edges, preserving the content verbatim."""
    text = _LEADING_BLANK_LINES.sub("", text)
    return _TRAILING_BLANK_LINES.sub("", text)
