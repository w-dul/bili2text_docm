from __future__ import annotations

MIN_SEGMENT_CHARS = 120
MAX_SEGMENT_CHARS = 300
MIN_TOTAL_CHARS = 20

_SENTENCE_ENDERS = set("。！？；…!?;")
_CLOSING_CHARS = set("”’』」）)]】》\"'")


def visible_char_count(text: str) -> int:
    return sum(1 for char in text if not char.isspace())


def practice_text(text: str) -> str:
    """Return the text used for typing practice: no whitespace, no control chars."""
    return "".join(char for char in text if not char.isspace() and char.isprintable())


def segment_text(
    text: str,
    *,
    min_chars: int = MIN_SEGMENT_CHARS,
    max_chars: int = MAX_SEGMENT_CHARS,
    min_total: int = MIN_TOTAL_CHARS,
) -> list[str]:
    """Split text into ordered segments without changing any character.

    The segments concatenated in order always reproduce the input text exactly.
    """
    if visible_char_count(text) < min_total:
        return []

    pieces: list[str] = []
    buffer = ""

    def flush() -> None:
        nonlocal buffer
        if buffer:
            pieces.append(buffer)
            buffer = ""

    for start, end in _sentence_spans(text):
        sentence = text[start:end]
        sentence_length = visible_char_count(sentence)

        if sentence_length > max_chars:
            flush()
            parts = _split_long(sentence, max_chars)
            pieces.extend(parts[:-1])
            buffer = parts[-1]
            continue

        buffer += sentence
        if visible_char_count(buffer) >= min_chars:
            flush()

    flush()
    return _rebalance_tail(pieces, min_chars=min_chars, max_chars=max_chars)


def _rebalance_tail(pieces: list[str], *, min_chars: int, max_chars: int) -> list[str]:
    """Merge a too-short final segment backward so the tail is still usable."""
    while len(pieces) >= 2 and visible_char_count(pieces[-1]) < min_chars:
        tail = pieces.pop()
        head = pieces.pop()
        combined = head + tail
        total = visible_char_count(combined)
        if total <= max_chars:
            pieces.append(combined)
            continue
        lower = max(min_chars, total - max_chars)
        upper = total - min_chars
        if lower > upper:
            pieces.extend([head, tail])
            break
        split_at = _boundary_near(combined, total // 2, lower, upper)
        pieces.append(combined[:split_at])
        pieces.append(combined[split_at:])
        break
    return pieces


def _boundary_near(text: str, target: int, lower: int, upper: int) -> int:
    boundary_chars = set("，。！？；…、,.!?;\n")
    best: int | None = None
    best_distance = -1
    visible = 0
    for index, char in enumerate(text):
        if not char.isspace():
            visible += 1
        if lower <= visible <= upper and char in boundary_chars:
            distance = abs(visible - target)
            if best is None or distance < best_distance:
                best = index + 1
                best_distance = distance
    if best is not None:
        return best

    visible = 0
    for index, char in enumerate(text):
        if not char.isspace():
            visible += 1
        if visible >= target:
            return index + 1
    return len(text)


def _sentence_spans(text: str) -> list[tuple[int, int]]:
    spans: list[tuple[int, int]] = []
    start = 0
    index = 0
    length = len(text)
    while index < length:
        char = text[index]
        if char == "\n":
            end = index + 1
            while end < length and text[end] == "\n":
                end += 1
            spans.append((start, end))
            start = end
            index = end
            continue
        if char in _SENTENCE_ENDERS:
            end = index + 1
            while end < length and (text[end] in _SENTENCE_ENDERS or text[end] in _CLOSING_CHARS):
                end += 1
            spans.append((start, end))
            start = end
            index = end
            continue
        index += 1
    if start < length:
        spans.append((start, length))
    return spans


def _split_long(sentence: str, max_chars: int) -> list[str]:
    parts: list[str] = []
    remaining = sentence
    while visible_char_count(remaining) > max_chars:
        cut = _visible_cut_index(remaining, max_chars)
        minimum = int(max_chars * 0.6)
        split_at = None
        for index in range(cut - 1, 0, -1):
            if remaining[index] in "，,、；;：:":
                if visible_char_count(remaining[: index + 1]) >= minimum:
                    split_at = index + 1
                break
        if split_at is None:
            split_at = cut
        parts.append(remaining[:split_at])
        remaining = remaining[split_at:]
    if remaining:
        parts.append(remaining)
    return parts or [sentence]


def _visible_cut_index(text: str, max_chars: int) -> int:
    count = 0
    for index, char in enumerate(text):
        if not char.isspace():
            count += 1
        if count > max_chars:
            return index
    return len(text)
