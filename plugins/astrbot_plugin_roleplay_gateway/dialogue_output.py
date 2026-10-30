"""Bounded, incremental transport for direct-speech records.

The model emits one JSON object: ``{"speech":["first", "second"]}``. Each
complete string is released after its following comma or closing bracket is
validated, without waiting for later strings or the closing object brace.
The provider enables native JSON-object output mode. No I/O or second model
request occurs here.

This is a structural filter, not a semantic narration detector: a model can
still put an unmarked action sentence inside ``speech``. Explicit stage-style
delimiters are rejected rather than deleting potentially meaningful prose.
Consequently, even legitimate parenthetical asides must be phrased without
parentheses in this deliberately restricted roleplay output format.
"""

from __future__ import annotations

import json


class DialogueFormatError(ValueError):
    """Static error code only; never include generated or stored content."""


_OUTER_QUOTES = {"\"": "\"", "'": "'", "“": "”", "‘": "’", "「": "」", "『": "』"}
_STAGE_DELIMITERS = frozenset("()（）[]【】*")
_JSON_WHITESPACE = frozenset(" \t\r\n")
_MAX_SPEECH_CHARACTERS = 120


def _enclosed_by_quotes(text: str) -> bool:
    """Do not mistake two separate quoted references for one outer wrapper."""
    if len(text) < 2 or _OUTER_QUOTES.get(text[0]) != text[-1]:
        return False
    opening, closing = text[0], text[-1]
    depth = 1
    for index, char in enumerate(text[1:], start=1):
        if opening != closing and char == opening:
            depth += 1
        elif char == closing:
            # An apostrophe inside a word is not a speech wrapper terminator.
            if (opening == closing == "'" and index < len(text) - 1
                    and text[index - 1].isalnum() and text[index + 1].isalnum()):
                continue
            depth -= 1
            if depth == 0:
                return index == len(text) - 1
    return False


def _decode_string(record: str) -> str:
    try:
        value = json.loads(record)
    except (ValueError, TypeError):
        raise DialogueFormatError("dialogue_invalid_json") from None
    if not isinstance(value, str):
        raise DialogueFormatError("dialogue_invalid_speech")
    return value


def _speech(record: str, maximum: int = _MAX_SPEECH_CHARACTERS, *, allow_delimiters=False) -> str:
    text = _decode_string(record)
    if len(text) > maximum:
        raise DialogueFormatError("dialogue_speech_limit")
    # Dollar signs are forbidden transport separators in this roleplay mode.
    # This policy is intentionally not applied to the generic QQ formatter.
    text = text.replace("$", "").strip()
    while _enclosed_by_quotes(text):
        text = text[1:-1].strip()
    if not text:
        raise DialogueFormatError("dialogue_empty_speech")
    if not allow_delimiters and any(char in _STAGE_DELIMITERS for char in text):
        raise DialogueFormatError("dialogue_stage_delimiter")
    if any((ord(char) < 32 and char not in "\t\r\n")
           or 0xD800 <= ord(char) <= 0xDFFF for char in text):
        raise DialogueFormatError("dialogue_invalid_character")
    return text


class DialogueStream:
    """A separate instance is required for each provider attempt.

    ``feed`` returns validated speech items. ``max_record_characters`` bounds
    each raw JSON string; decoded speech is capped at 120 characters. Raw prose,
    extra fields, nesting and malformed/truncated input fail closed. Previously
    returned items remain valid; the caller owns delivery and partial-reply
    accounting. Never call ``finish`` to flush a cancelled/failed provider.
    An unfinished outer object is never reported complete.
    """

    def __init__(self, max_record_characters: int = 600,
                 max_total_characters: int = 24000,
                 max_speech_characters: int = _MAX_SPEECH_CHARACTERS,
                 allow_delimiters: bool = False):
        if any(type(value) is not int or value < 1
               for value in (max_record_characters, max_total_characters, max_speech_characters)):
            raise DialogueFormatError("invalid_dialogue_limits")
        self.max_record_characters = max_record_characters
        self.max_total_characters = max_total_characters
        self.max_speech_characters = max_speech_characters
        if type(allow_delimiters) is not bool:
            raise DialogueFormatError("invalid_dialogue_limits")
        self.allow_delimiters = allow_delimiters
        self._total = 0
        self._record: list[str] = []
        self._escaped = False
        self._state = "object_start"
        self._pending_speech: str | None = None
        self._failed = False
        self._finished = False

    def _fail(self, code: str):
        self._failed = True
        raise DialogueFormatError(code)

    def feed(self, delta: str) -> tuple[str, ...]:
        return tuple(self.iter_feed(delta))

    def iter_feed(self, delta: str):
        """Yield at validated boundaries so callers may stop before the next item."""
        if self._failed:
            raise DialogueFormatError("dialogue_stream_failed")
        if self._finished:
            raise DialogueFormatError("dialogue_stream_finished")
        if not isinstance(delta, str):
            self._fail("dialogue_invalid_delta")
        self._total += len(delta)
        if self._total > self.max_total_characters:
            self._fail("dialogue_total_limit")
        for char in delta:
            if self._state in {"key_string", "speech_string"}:
                self._record.append(char)
                limit = 64 if self._state == "key_string" else self.max_record_characters
                if len(self._record) > limit:
                    self._fail("dialogue_record_limit")
                if self._escaped:
                    self._escaped = False
                elif char == "\\":
                    self._escaped = True
                elif char == '"':
                    try:
                        raw = "".join(self._record)
                        if self._state == "key_string":
                            if _decode_string(raw) != "speech":
                                self._fail("dialogue_invalid_schema")
                            self._state = "colon"
                        else:
                            self._pending_speech = _speech(raw, self.max_speech_characters,
                                                          allow_delimiters=self.allow_delimiters)
                            self._state = "item_delimiter"
                    except DialogueFormatError:
                        self._failed = True
                        raise
                    self._record.clear()
                continue
            if char in _JSON_WHITESPACE:
                continue
            if self._state == "object_start":
                if char != "{":
                    self._fail("dialogue_expected_record")
                self._state = "key_start"
            elif self._state == "key_start":
                if char != '"':
                    self._fail("dialogue_invalid_schema")
                self._record = [char]
                self._state = "key_string"
            elif self._state == "colon":
                if char != ":":
                    self._fail("dialogue_invalid_schema")
                self._state = "array_start"
            elif self._state == "array_start":
                if char != "[":
                    self._fail("dialogue_invalid_schema")
                self._state = "item_start"
            elif self._state == "item_start":
                if char != '"':
                    self._fail("dialogue_invalid_speech")
                self._record = [char]
                self._state = "speech_string"
            elif self._state == "item_delimiter":
                if char not in ",]":
                    self._fail("dialogue_invalid_item_delimiter")
                assert self._pending_speech is not None
                speech = self._pending_speech
                self._pending_speech = None
                self._state = "item_start" if char == "," else "object_end"
                yield speech
            elif self._state == "object_end":
                if char != "}":
                    self._fail("dialogue_invalid_schema")
                self._state = "done"
            else:
                self._fail("dialogue_trailing_content")

    def finish(self) -> None:
        if self._failed:
            raise DialogueFormatError("dialogue_stream_failed")
        if self._finished:
            return
        if self._total == 0 or self._state == "object_start":
            self._fail("dialogue_empty_output")
        if self._state != "done":
            self._fail("dialogue_truncated_record")
        self._finished = True
