from __future__ import annotations

from collections.abc import Mapping
from typing import Any
from urllib.parse import urlsplit

from ..asset_inventory import compact_text


ACTION_PLAN_SCHEMA_VERSION = "kt6.action-plan.v1"


class ActionPlanValidationError(ValueError):
    def __init__(self, error_code: str):
        super().__init__(error_code)
        self.error_code = error_code


class ActionPlanValidator:
    """Fail-closed validator for the model-to-Runner semantic contract."""

    MAX_STEPS = 12
    _PLAN_KEYS = frozenset(
        {"schema_version", "scenario_id", "start_url", "user_request", "steps"}
    )
    _TARGET_KEYS = frozenset({"query", "asset_id", "action", "role"})
    _EXPECTED_TYPES = frozenset(
        {
            "element_visible",
            "element_disappeared",
            "element_selected",
            "selected",
            "text_present",
            "url_changed",
            "page_changed",
        }
    )

    def validate(self, value: Mapping[str, Any]) -> dict[str, Any]:
        if not isinstance(value, Mapping):
            raise ActionPlanValidationError("action_plan_must_be_object")
        if set(value) != self._PLAN_KEYS:
            raise ActionPlanValidationError("action_plan_fields_invalid")
        if value.get("schema_version") != ACTION_PLAN_SCHEMA_VERSION:
            raise ActionPlanValidationError("action_plan_schema_mismatch")
        scenario_id = compact_text(value.get("scenario_id"), 200)
        start_url = compact_text(value.get("start_url"), 2048)
        user_request = compact_text(value.get("user_request"), 2_000)
        if not scenario_id or not user_request:
            raise ActionPlanValidationError("action_plan_identity_missing")
        parsed = urlsplit(start_url)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.netloc
            or parsed.username
            or parsed.password
            or parsed.fragment
        ):
            raise ActionPlanValidationError("action_plan_start_url_invalid")
        raw_steps = value.get("steps")
        if (
            not isinstance(raw_steps, list)
            or not raw_steps
            or len(raw_steps) > self.MAX_STEPS
        ):
            raise ActionPlanValidationError("action_plan_steps_invalid")
        steps = [self._step(item, index) for index, item in enumerate(raw_steps, 1)]
        self._sequence(steps)
        return {
            "schema_version": ACTION_PLAN_SCHEMA_VERSION,
            "scenario_id": scenario_id,
            "start_url": start_url,
            "user_request": user_request,
            "steps": steps,
        }

    def _step(self, value: Any, index: int) -> dict[str, Any]:
        if not isinstance(value, Mapping):
            raise ActionPlanValidationError("action_plan_step_fields_invalid")
        step_id = compact_text(value.get("id"), 100)
        if step_id != f"step-{index}":
            raise ActionPlanValidationError("action_plan_step_sequence_invalid")
        op = compact_text(value.get("op"), 50)
        if op == "click":
            if set(value) != {"id", "op", "target"}:
                raise ActionPlanValidationError("action_plan_click_fields_invalid")
            return {"id": step_id, "op": op, "target": self._target(value["target"])}
        if op not in {"verify", "wait"}:
            raise ActionPlanValidationError("action_plan_operation_unsupported")
        allowed = {"id", "op", "expected"}
        if op == "wait":
            allowed.add("timeout_ms")
        if set(value) != allowed:
            raise ActionPlanValidationError("action_plan_condition_fields_invalid")
        expected = self._expected(value["expected"])
        result: dict[str, Any] = {"id": step_id, "op": op, "expected": expected}
        if op == "wait":
            timeout = value.get("timeout_ms")
            if (
                isinstance(timeout, bool)
                or not isinstance(timeout, int)
                or not 100 <= timeout <= 30_000
            ):
                raise ActionPlanValidationError("action_plan_wait_timeout_invalid")
            if expected["type"] in {"page_changed", "url_changed"}:
                raise ActionPlanValidationError("action_plan_wait_expected_invalid")
            result["timeout_ms"] = timeout
        return result

    def _target(self, value: Any) -> dict[str, str]:
        if (
            not isinstance(value, Mapping)
            or not set(value).issubset(self._TARGET_KEYS)
            or "query" not in value
        ):
            raise ActionPlanValidationError("action_plan_target_invalid")
        target = {key: compact_text(item, 300) for key, item in value.items()}
        if any(not item for item in target.values()):
            raise ActionPlanValidationError("action_plan_target_invalid")
        return target

    def _expected(self, value: Any) -> dict[str, Any]:
        if not isinstance(value, Mapping):
            raise ActionPlanValidationError("action_plan_expected_invalid")
        expected_type = compact_text(value.get("type"), 100)
        if expected_type not in self._EXPECTED_TYPES:
            raise ActionPlanValidationError("action_plan_expected_invalid")
        if expected_type in {"page_changed", "url_changed"}:
            if set(value) != {"type"}:
                raise ActionPlanValidationError("action_plan_expected_invalid")
            return {"type": expected_type}
        if set(value) != {"type", "target"}:
            raise ActionPlanValidationError("action_plan_expected_invalid")
        return {"type": expected_type, "target": self._target(value["target"])}

    @staticmethod
    def _sequence(steps: list[dict[str, Any]]) -> None:
        if len(steps) % 2:
            raise ActionPlanValidationError("action_plan_sequence_invalid")
        for index in range(0, len(steps), 2):
            if steps[index]["op"] != "click" or steps[index + 1]["op"] not in {
                "verify",
                "wait",
            }:
                raise ActionPlanValidationError("action_plan_sequence_invalid")


__all__ = [
    "ACTION_PLAN_SCHEMA_VERSION",
    "ActionPlanValidationError",
    "ActionPlanValidator",
]
