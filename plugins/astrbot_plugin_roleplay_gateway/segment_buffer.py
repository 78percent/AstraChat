"""Synchronous text segmentation only; no timers, I/O, sending or native state.

The coordinator must call poll(next_due) when the model stops producing text.
One instance represents one stream: finish is idempotent and ends admission.
Timeouts use the arrival time of the first still-buffered non-whitespace text,
not the most recent feed/poll. A paragraph is a blank line or U+2029; a single
line break is preserved without forcing an otherwise incomplete line out.

Length counts non-whitespace Unicode grapheme clusters (regex \\X), including
punctuation. All currently buffered clusters are kept intact. Eager length,
sentence or time emission cannot anticipate a later combining mark, ZWJ,
variation selector, or closing quote; it cannot reattach these to text already
returned. Closing quotes received with the sentence are included when within
the length bound. ASCII terminal dots wait for right context, timeout or finish
to avoid splitting decimals/domains across chunks; this is not a full parser.

Text and layout inside non-blank segments are returned verbatim. The only
discarded input is isolated whitespace remaining at finish (or a wholly blank
stream): it cannot be sent alone or appended to a segment already returned.
No semantic completeness, speaker attribution, 1.5-second sending interval or
QQ/model performance claim follows from these pure buffer operations.
"""

from __future__ import annotations

from collections import deque
import math

import regex


_GRAPHEME = regex.compile(r"\X")
_URL = regex.compile(r'''(?i)(?:[a-z][a-z0-9+.-]*://|www\.)[^\s<>"'。！？「」『』“”]+|[\w.+-]+@[\w.-]+''')
_END = frozenset("。！？!?…")
_CLOSE = frozenset("\"'”’」』）》】〕〉»›)]}")


class TextSegmenter:
    def __init__(self, max_characters: int = 120, max_wait_seconds: float = 0.8):
        if isinstance(max_characters, bool) or not isinstance(max_characters, int):
            raise TypeError("max_characters must be an integer")
        if max_characters <= 0:
            raise ValueError("max_characters must be positive")
        if isinstance(max_wait_seconds, bool) or not isinstance(max_wait_seconds, (int, float)):
            raise TypeError("max_wait_seconds must be a number")
        try:
            wait = float(max_wait_seconds)
        except OverflowError:
            raise ValueError("max_wait_seconds must be finite") from None
        if not math.isfinite(wait) or wait <= 0:
            raise ValueError("max_wait_seconds must be finite and positive")
        self.max_characters = max_characters
        self.max_wait_seconds = wait
        self._text = ""
        self._arrivals: deque[tuple[str, float]] = deque()
        self._last_now: float | None = None
        self._finished = False

    @property
    def next_due(self) -> float | None:
        for text, arrived in self._arrivals:
            if not text.isspace():
                return arrived + self.max_wait_seconds
        return None

    def _time(self, now: float) -> float:
        if isinstance(now, bool) or not isinstance(now, (int, float)):
            raise TypeError("now must be a number")
        try:
            value = float(now)
        except OverflowError:
            raise ValueError("now must be finite") from None
        if (not math.isfinite(value) or value < 0
                or not math.isfinite(value + self.max_wait_seconds)):
            raise ValueError("now must be finite and nonnegative")
        if self._last_now is not None and value < self._last_now:
            raise ValueError("now must not move backwards")
        return value

    def feed(self, text: str, now: float) -> list[str]:
        if not isinstance(text, str):
            raise TypeError("text must be a string")
        if self._finished:
            raise ValueError("segmenter is finished")
        value = self._time(now)
        self._last_now = value
        if text:
            self._text += text
            self._arrivals.append((text, value))
        return self._drain(value)

    def poll(self, now: float) -> list[str]:
        value = self._time(now)
        self._last_now = value
        return self._drain(value)

    def finish(self) -> list[str]:
        if self._finished:
            return []
        self._finished = True
        text = self._take(len(self._text))
        return [text] if text and not text.isspace() else []

    def _take(self, end: int) -> str:
        result, self._text = self._text[:end], self._text[end:]
        remaining = end
        while remaining and self._arrivals:
            text, arrived = self._arrivals.popleft()
            if len(text) > remaining:
                self._arrivals.appendleft((text[remaining:], arrived))
                break
            remaining -= len(text)
        return result

    def _cut(self) -> int | None:
        clusters = list(_GRAPHEME.finditer(self._text))
        protected = [match.span() for match in _URL.finditer(self._text)]
        cuts: list[int] = []

        def whitespace_after(index: int) -> int:
            while index + 1 < len(clusters) and clusters[index + 1].group().isspace():
                index += 1
            return clusters[index].end()

        def newline(index: int) -> bool:
            text = clusters[index].group()
            # A trailing CR can still become CRLF in the next feed.
            return text in {"\n", "\r\n"} or (text == "\r" and index + 1 < len(clusters))

        count = 0
        for index, match in enumerate(clusters):
            cluster = match.group()
            count += not cluster.isspace()
            if count == self.max_characters:
                cuts.append(whitespace_after(index))
                break

        for index, match in enumerate(clusters):
            cluster = match.group()
            char = cluster[0]
            sentence = char in _END
            in_url = any(start <= match.start() < end for start, end in protected)
            if char in {"!", "?"} and in_url:
                sentence = False
            if char == "." and not in_url and index + 1 < len(clusters):
                following = clusters[index + 1].group()
                sentence = following.isspace() or following[0] in _CLOSE | _END
            if sentence:
                stop = index
                while stop + 1 < len(clusters) and clusters[stop + 1].group()[0] in _CLOSE | _END | {"."}:
                    stop += 1
                cuts.append(whitespace_after(stop))
                break
            paragraph = cluster == "\u2029"
            if newline(index):
                cursor = index + 1
                while cursor < len(clusters) and clusters[cursor].group().isspace() and not newline(cursor):
                    # Do not mistake an unconfirmed trailing CR for a blank line.
                    if clusters[cursor].group() == "\r":
                        break
                    cursor += 1
                paragraph = cursor < len(clusters) and newline(cursor)
                if paragraph:
                    index = cursor
            if paragraph and self._text[:match.start()].strip():
                cuts.append(whitespace_after(index))
                break
        return min(cuts) if cuts else None

    def _drain(self, now: float) -> list[str]:
        output = []
        while self._text:
            cut = self._cut()
            if cut is None:
                due = self.next_due
                if due is None or now < due:
                    break
                cut = len(self._text)
            text = self._take(cut)
            if text and not text.isspace():
                output.append(text)
        return output
