"""Read-time projection of acknowledged replies into the speech protocol.

This is deliberately not a natural-language narration classifier. Ordinary
unwrapped dialogue keeps its content, including inline quotations and examples
in parentheses. Only recognizable legacy wrappers select quoted speech and
discard text outside it. Ambiguous/truncated legacy text is omitted from the
request rather than invented or written back to the memory database.
"""

from __future__ import annotations

import json

import regex


_QUOTES = {"“": "”", '"': '"'}
_STAGE_PAIRS = {"（": "）", "(": ")", "*": "*"}
_ENDS = frozenset("。！？!?…\n\r\u2028\u2029")
_CLOSERS = frozenset("”’\"'」』")
_GRAPHEME = regex.compile(r"\X")


def _wrapped_stage_end(text: str) -> int | None:
    """Return the end of one explicit leading wrapper, without guessing words."""
    if not text or text[0] not in _STAGE_PAIRS:
        return None
    closer = _STAGE_PAIRS[text[0]]
    if text[0] == "*":
        width = 2 if text.startswith("**") else 1
        end = text.find("*" * width, width)
        return end + width if end >= 0 else None
    depth = 0
    for index, char in enumerate(text):
        if char == text[0]:
            depth += 1
        elif char == closer:
            depth -= 1
            if depth == 0:
                return index + 1
    return None


def _read_quote(text: str, start: int) -> tuple[int, str] | None:
    """Read one balanced outer quote, including escaped/nested inner quotes."""
    opener = text[start]
    closer = _QUOTES[opener]
    index = start + 1
    depth = 1
    speech: list[str] = []
    while index < len(text):
        char = text[index]
        if char == "\\" and index + 1 < len(text) and text[index + 1] in {opener, closer, "\\"}:
            speech.append(text[index + 1])
            index += 2
            continue
        if char == closer:
            depth -= 1
            if depth == 0:
                return index + 1, "".join(speech)
        elif opener != closer and char == opener:
            depth += 1
        speech.append(char)
        index += 1
    return None


def _is_quoted_envelope(text: str) -> bool:
    """Require whole quoted records, not a quoted term at sentence start.

    `“勇气”就是“不怕”` is ordinary speech with two inline quotations. Neither
    the connecting words nor their relationship may be discarded. Even sentence
    punctuation inside the first quotation is not evidence of narration after
    it, so any unwrapped connecting text keeps the complete original reply.
    """
    cursor = 0
    while cursor < len(text):
        if text[cursor] not in _QUOTES:
            return False
        quoted = _read_quote(text, cursor)
        if quoted is None:
            return True  # Recognizable but truncated envelope: omit, not repair.
        cursor, _ = quoted
        while cursor < len(text) and text[cursor].isspace():
            cursor += 1
        if cursor < len(text) and text[cursor] == "$":
            return True
    return True


def _is_legacy(text: str) -> bool:
    if text.startswith("$"):
        return True
    # A dollar-delimited reply with quoted speech is the old roleplay format.
    # A dollar in ordinary text (a price, for example) alone proves nothing.
    if regex.search(r'\$\s*[“"]', text):
        return True
    if text[0] in _QUOTES:
        return _is_quoted_envelope(text)
    end = _wrapped_stage_end(text)
    if end is not None:
        following = text[end:].lstrip(" \t\r\n$")
        return not following or (following[0] in _QUOTES and _is_quoted_envelope(following))
    return False


def _quoted_speech(text: str) -> list[str]:
    parts: list[str] = []
    index = 0
    while index < len(text):
        opener = text[index]
        if opener not in _QUOTES:
            index += 1
            continue
        quoted = _read_quote(text, index)
        if quoted is None:
            # Never present a truncated quote as a completed historical answer.
            return []
        index, speech = quoted
        body = speech.replace("$", "").strip()
        if body:
            parts.append(body)
    return parts


def _short_records(text: str, limit: int = 60) -> list[str]:
    """Use short complete sentences where available, preserving grapheme units."""
    clusters = [match.group() for match in _GRAPHEME.finditer(text)]
    records: list[str] = []
    start = 0
    cursor = 0
    while cursor < len(clusters):
        char = clusters[cursor]
        cursor += 1
        terminal = any(symbol in _ENDS for symbol in char)
        if terminal:
            while cursor < len(clusters) and (
                clusters[cursor] in _CLOSERS or any(symbol in _ENDS for symbol in clusters[cursor])
            ):
                cursor += 1
        if terminal or cursor - start >= limit:
            body = "".join(clusters[start:cursor]).strip()
            if body:
                records.append(body)
            start = cursor
    tail = "".join(clusters[start:]).strip()
    if tail:
        records.append(tail)
    return records


def encode_dialogue_history(text: str) -> str:
    """Encode only projected speech; an empty result means omit this pair.

    The caller supplies joined, confirmed assistant segments from one turn.
    No speaker input, summary, persona or persistent record is changed here.
    Unmarked narration cannot reliably be distinguished from spoken language;
    this function does not claim semantic filtering of arbitrary prose.
    """
    if not isinstance(text, str):
        raise TypeError("invalid_dialogue_history")
    value = text.strip()
    if not value:
        return ""
    parts = _quoted_speech(value) if _is_legacy(value) else [value]
    records = [record for part in parts for record in _short_records(part)]
    return json.dumps({"speech": records}, ensure_ascii=False) if records else ""
