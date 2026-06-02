from __future__ import annotations

import re
import unicodedata

_ZERO_WIDTH = re.compile("[\u200b\u200c\u200d\ufeff]")
_PUNCTUATION = re.compile(r"[^\w\s]|_", re.UNICODE)
_WHITESPACE = re.compile(r"\s+")


def normalize_key(value: str | None) -> str:
    if value is None:
        return ""
    normalized = unicodedata.normalize("NFKC", value)
    normalized = _ZERO_WIDTH.sub("", normalized)
    normalized = normalized.casefold().strip()
    normalized = _PUNCTUATION.sub(" ", normalized)
    return _WHITESPACE.sub(" ", normalized).strip()
