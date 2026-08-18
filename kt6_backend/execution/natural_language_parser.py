from __future__ import annotations

import re
from typing import Any

from ..asset_inventory import compact_text, strong_identity_key


_CONNECTOR = re.compile(r"\s*(?:，|,|。|；|;|然后|再|接着|之后|并(?:且)?)\s*")
_ASSET = r"(?P<asset>[A-Za-z][A-Za-z0-9_-]{0,99})"
_PATTERNS = (
    (
        "open_asset_details",
        re.compile(rf"^(?:打开|查看)\s*{_ASSET}\s*(?:的)?\s*详情(?:页面)?$", re.I),
    ),
    (
        "open_topology",
        re.compile(r"^(?:进入|打开|点击)\s*拓扑(?:页面)?$", re.I),
    ),
    (
        "select_canvas_asset",
        re.compile(
            rf"^(?:在\s*拓扑(?:中|页面中)?\s*)?(?:选中|选择)\s*{_ASSET}$",
            re.I,
        ),
    ),
)


class NaturalLanguageParseError(ValueError):
    def __init__(self, error_code: str):
        super().__init__(error_code)
        self.error_code = error_code


class NaturalLanguageIntentParser:
    """Parse the deliberately small execution-fixture command language."""

    MAX_REQUEST_CHARS = 2_000
    MAX_INTENTS = 6

    def parse(self, user_request: str) -> list[dict[str, Any]]:
        request = compact_text(user_request, self.MAX_REQUEST_CHARS)
        if not request:
            raise NaturalLanguageParseError("execution_request_required")
        clauses = [part.strip() for part in _CONNECTOR.split(request) if part.strip()]
        if not clauses or len(clauses) > self.MAX_INTENTS:
            raise NaturalLanguageParseError("execution_request_step_limit")

        intents: list[dict[str, Any]] = []
        for clause in clauses:
            matched = False
            for intent_name, pattern in _PATTERNS:
                result = pattern.fullmatch(clause)
                if result is None:
                    continue
                intent: dict[str, Any] = {"intent": intent_name}
                asset = result.groupdict().get("asset")
                if asset:
                    intent["asset_id"] = strong_identity_key("asset_id", asset)
                intents.append(intent)
                matched = True
                break
            if not matched:
                raise NaturalLanguageParseError("execution_request_unsupported")
        return intents


__all__ = ["NaturalLanguageIntentParser", "NaturalLanguageParseError"]
