"""Bounded JSON-object candidates from mixed prose and model output.

Models often wrap a final JSON object in Markdown or explain it with examples. A
candidate finder therefore cannot require the whole answer to be JSON, but it also
must not restart a scan from every opening brace: model output is untrusted and may
contain a megabyte of brace-heavy source code.
"""

from __future__ import annotations

import re
from collections import deque
from collections.abc import Iterator


MAX_JSON_CANDIDATES = 64

_FENCE_LINE = re.compile(r"(?m)^[ \t]*```(?P<info>[^`\r\n]*)[ \t]*\r?$")
_INLINE_FENCE = re.compile(
    r"(?im)^[ \t]*```(?:json)?[ \t]+(?P<body>[^\r\n]*?)[ \t]*```[ \t]*\r?$"
)


def json_object_candidates(
    text: str, limit: int = MAX_JSON_CANDIDATES
) -> Iterator[tuple[int, str]]:
    """Yield the latest complete object spans, ordered by start position.

    One pass pairs braces while respecting JSON strings and escapes. Nested objects
    are candidates too, because a stray unmatched brace in surrounding prose must
    not hide a valid object inside it. Only the latest bounded set of completed spans
    is retained; an outer object completes after all of its children, so a large
    final answer remains present even when it contains more than ``limit`` braces.
    A small reserve of the largest top-level objects also keeps a corrected answer
    from being displaced by later brace noise when an earlier draft was longer.
    """
    if not text or limit <= 0:
        return

    spans: deque[tuple[int, int]] = deque(maxlen=limit)
    # Reserve a few slots for substantial completed top-level answers. Keeping only
    # the single largest span lets a verbose draft displace a shorter corrected final
    # answer once enough trailing ``{}`` noise evicts it from the recent deque.
    protected_limit = min(8, limit)
    protected: list[tuple[int, int]] = []
    stack: list[int] = []
    quoted = False
    escaped = False

    for index, char in enumerate(text):
        if not stack:
            if char == "{":
                stack.append(index)
                quoted = False
                escaped = False
            continue

        if quoted:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                quoted = False
            elif char in "\r\n":
                # A literal newline cannot occur inside a JSON string. The open
                # stack therefore belongs to malformed surrounding prose; dropping
                # it lets a later, independent answer object be considered.
                stack.clear()
                quoted = False
                escaped = False
            continue

        if char == '"':
            quoted = True
        elif char == "{":
            stack.append(index)
        elif char == "}":
            span = (stack.pop(), index + 1)
            spans.append(span)
            if not stack:
                protected.append(span)
                protected.sort(
                    key=lambda item: (item[1] - item[0], item[0]), reverse=True
                )
                del protected[protected_limit:]
                quoted = False
                escaped = False

    selected = list(protected)
    for span in reversed(spans):
        if span not in selected and len(selected) < limit:
            selected.append(span)
    for start, end in sorted(selected, key=lambda span: span[0], reverse=True):
        yield start, text[start:end]


def fenced_json_objects(
    text: str, limit: int = MAX_JSON_CANDIDATES
) -> Iterator[tuple[int, str]]:
    """Yield bounded JSON-looking Markdown fence bodies.

    Line fences are paired as a state machine, including non-JSON blocks, so the
    closing fence of a shell example cannot become the opening fence of the answer.
    The compact one-line form is accepted too.
    """
    if not text or limit <= 0:
        return

    candidates: deque[tuple[int, str]] = deque(maxlen=limit)

    for match in _INLINE_FENCE.finditer(text):
        body = match.group("body")
        leading = len(body) - len(body.lstrip())
        body = body.strip()
        if body.startswith("{"):
            candidates.append((match.start("body") + leading, body))

    opening: re.Match[str] | None = None
    capture = False
    for fence in _FENCE_LINE.finditer(text):
        if opening is None:
            opening = fence
            capture = fence.group("info").strip().lower() in ("", "json")
            continue
        if capture:
            raw = text[opening.end() : fence.start()]
            leading = len(raw) - len(raw.lstrip())
            chunk = raw.strip()
            if chunk.startswith("{"):
                candidates.append((opening.end() + leading, chunk))
        opening = None
        capture = False

    for candidate in sorted(candidates, key=lambda item: item[0], reverse=True):
        yield candidate
