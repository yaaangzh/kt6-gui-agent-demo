from __future__ import annotations

from typing import Any, Mapping, Protocol


class OutcomeVerifier(Protocol):
    """KT6-owned business outcome verifier for a post-action fresh capture."""

    def verify(
        self,
        *,
        action_id: str,
        asset_id: str,
        before: Mapping[str, Any],
        after: Mapping[str, Any],
    ) -> bool:
        ...


__all__ = ["OutcomeVerifier"]
