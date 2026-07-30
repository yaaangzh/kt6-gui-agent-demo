"""Shared, conservative rules for OCR topology identifier candidates."""

from __future__ import annotations

import re
from typing import Any


_STRICT_OCR_IDENTIFIER = re.compile(r"[A-Za-z][A-Za-z0-9_.:/-]{1,199}")


def is_strict_ocr_identifier(value: Any) -> bool:
    """Accept only device-like ASCII identifiers with at least one digit."""

    return bool(
        isinstance(value, str)
        and _STRICT_OCR_IDENTIFIER.fullmatch(value)
        and any(character.isdigit() for character in value)
    )


__all__ = ["is_strict_ocr_identifier"]
