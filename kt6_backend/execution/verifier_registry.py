from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from .verifier import OutcomeVerifier, UIGraphOutcomeVerifier


class OutcomeVerifierRegistryError(ValueError):
    def __init__(self, error_code: str):
        super().__init__(error_code)
        self.error_code = error_code


class OutcomeVerifierRegistry:
    def __init__(
        self,
        verifiers: Sequence[OutcomeVerifier],
        *,
        ui_graph_verifier: UIGraphOutcomeVerifier | None = None,
    ):
        self._by_action: dict[str, OutcomeVerifier] = {}
        self._by_expected: dict[str, OutcomeVerifier] = {}
        for verifier in verifiers:
            action_id = str(verifier.supported_action_id).strip()
            expected_type = str(verifier.expected_type).strip()
            if (
                not action_id
                or not expected_type
                or action_id in self._by_action
                or expected_type in self._by_expected
            ):
                raise ValueError("duplicate or invalid outcome verifier")
            self._by_action[action_id] = verifier
            self._by_expected[expected_type] = verifier
        self.ui_graph_verifier = ui_graph_verifier

    def supports_action(self, action_id: str) -> bool:
        return str(action_id).strip() in self._by_action

    def verify_action(
        self,
        *,
        action_id: str,
        asset_id: str,
        before: Mapping[str, Any],
        after: Mapping[str, Any],
    ) -> tuple[bool, str]:
        verifier = self._by_action.get(str(action_id).strip())
        if verifier is None:
            raise OutcomeVerifierRegistryError("outcome_verifier_unavailable")
        return (
            verifier.verify(
                action_id=action_id,
                asset_id=asset_id,
                before=before,
                after=after,
            ),
            verifier.verifier_id,
        )

    def verify_expected(
        self,
        *,
        expected: Mapping[str, Any],
        action_id: str,
        before: Mapping[str, Any],
        after: Mapping[str, Any],
        before_graph: Mapping[str, Any] | None = None,
        after_graph: Mapping[str, Any] | None = None,
    ) -> tuple[bool, str]:
        expected_type = str(expected.get("type", "")).strip()
        if (
            self.ui_graph_verifier is not None
            and expected_type in self.ui_graph_verifier.expected_types
        ):
            if before_graph is None or after_graph is None:
                raise OutcomeVerifierRegistryError("outcome_ui_graph_missing")
            return (
                self.ui_graph_verifier.verify(
                    expected=expected,
                    before=before_graph,
                    after=after_graph,
                ),
                self.ui_graph_verifier.verifier_id,
            )
        verifier = self._by_expected.get(expected_type)
        if verifier is None or verifier.supported_action_id != action_id:
            raise OutcomeVerifierRegistryError("outcome_verifier_mismatch")
        return (
            verifier.verify(
                action_id=action_id,
                asset_id=str(expected.get("asset_id", "")),
                before=before,
                after=after,
            ),
            verifier.verifier_id,
        )

    def health(self) -> dict[str, Any]:
        return {
            "configured": bool(self._by_action),
            "actions": sorted(self._by_action),
            "expected_types": sorted(self._by_expected),
            "ui_graph_expected_types": sorted(
                self.ui_graph_verifier.expected_types
                if self.ui_graph_verifier is not None
                else ()
            ),
        }


__all__ = ["OutcomeVerifierRegistry", "OutcomeVerifierRegistryError"]
