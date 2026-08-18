from __future__ import annotations

from collections.abc import Mapping
from typing import Any
from urllib.parse import urlsplit

from ..asset_inventory import compact_text
from .plan_generator import ACTION_PLAN_SCHEMA_VERSION


class ActionPlanValidationError(ValueError):
    def __init__(self, error_code: str):
        super().__init__(error_code)
        self.error_code = error_code


class ActionPlanValidator:
    """Fail-closed validator for the Agent-to-Runner contract."""

    MAX_STEPS = 12
    _PLAN_KEYS = frozenset(
        {"schema_version", "scenario_id", "start_url", "user_request", "steps"}
    )
    _STEP_KEYS = frozenset({"id", "op", "target", "expected", "timeout_ms"})
    _DOM_ACTIONS = frozenset({"open_asset_details", "open_topology"})
    _EXPECTED_TYPES = frozenset(
        {"asset_detail_visible", "page_ready", "canvas_asset_selected"}
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
        normalized_steps = []
        for index, raw_step in enumerate(raw_steps, start=1):
            normalized_steps.append(self._step(raw_step, index))
        self._validate_sequence(normalized_steps)
        return {
            "schema_version": ACTION_PLAN_SCHEMA_VERSION,
            "scenario_id": scenario_id,
            "start_url": start_url,
            "user_request": user_request,
            "steps": normalized_steps,
        }

    @staticmethod
    def _validate_sequence(steps: list[dict[str, Any]]) -> None:
        expected_pairs = {
            "open_asset_details": ("verify", "asset_detail_visible"),
            "open_topology": ("wait", "page_ready"),
            "select_canvas_asset": ("verify", "canvas_asset_selected"),
        }
        if len(steps) % 2:
            raise ActionPlanValidationError("action_plan_sequence_invalid")
        for index in range(0, len(steps), 2):
            action_step = steps[index]
            outcome_step = steps[index + 1]
            if action_step["op"] != "click":
                raise ActionPlanValidationError("action_plan_sequence_invalid")
            expected_op, expected_type = expected_pairs[
                action_step["target"]["action"]
            ]
            expected = outcome_step.get("expected", {})
            if (
                outcome_step["op"] != expected_op
                or expected.get("type") != expected_type
                or expected.get("asset_id")
                != action_step["target"]["asset_id"]
            ):
                raise ActionPlanValidationError("action_plan_sequence_invalid")

    def _step(self, value: Any, index: int) -> dict[str, Any]:
        if not isinstance(value, Mapping) or not set(value).issubset(self._STEP_KEYS):
            raise ActionPlanValidationError("action_plan_step_fields_invalid")
        step_id = compact_text(value.get("id"), 100)
        if step_id != f"step-{index}":
            raise ActionPlanValidationError("action_plan_step_sequence_invalid")
        op = compact_text(value.get("op"), 50)
        if op == "click":
            if set(value) != {"id", "op", "target"}:
                raise ActionPlanValidationError("action_plan_click_fields_invalid")
            target = self._target(value.get("target"))
            return {"id": step_id, "op": op, "target": target}
        if op in {"verify", "wait"}:
            allowed = {"id", "op", "expected"}
            if op == "wait":
                allowed.add("timeout_ms")
            if set(value) != allowed:
                raise ActionPlanValidationError("action_plan_condition_fields_invalid")
            expected = self._expected(value.get("expected"))
            result = {"id": step_id, "op": op, "expected": expected}
            if op == "wait":
                timeout = value.get("timeout_ms")
                if isinstance(timeout, bool) or not isinstance(timeout, int) or not 100 <= timeout <= 30_000:
                    raise ActionPlanValidationError("action_plan_wait_timeout_invalid")
                result["timeout_ms"] = timeout
            return result
        raise ActionPlanValidationError("action_plan_operation_unsupported")

    def _target(self, value: Any) -> dict[str, Any]:
        if not isinstance(value, Mapping):
            raise ActionPlanValidationError("action_plan_target_invalid")
        source = compact_text(value.get("source"), 50)
        if source == "canvas":
            if set(value) != {"source", "asset_id", "action", "name"}:
                raise ActionPlanValidationError("action_plan_canvas_target_invalid")
            if value.get("action") != "select_canvas_asset":
                raise ActionPlanValidationError("action_plan_canvas_action_invalid")
        else:
            allowed = {"asset_id", "action"}
            if value.get("action") == "open_topology":
                allowed.add("text")
            if set(value) != allowed or value.get("action") not in self._DOM_ACTIONS:
                raise ActionPlanValidationError("action_plan_dom_target_invalid")
        normalized = {key: compact_text(item, 300) for key, item in value.items()}
        if any(not item for item in normalized.values()):
            raise ActionPlanValidationError("action_plan_target_invalid")
        return normalized

    def _expected(self, value: Any) -> dict[str, Any]:
        if not isinstance(value, Mapping):
            raise ActionPlanValidationError("action_plan_expected_invalid")
        expected_type = compact_text(value.get("type"), 100)
        allowed = {"type", "asset_id"}
        if expected_type == "page_ready":
            allowed.add("page")
        if set(value) != allowed or expected_type not in self._EXPECTED_TYPES:
            raise ActionPlanValidationError("action_plan_expected_invalid")
        normalized = {key: compact_text(item, 300) for key, item in value.items()}
        if any(not item for item in normalized.values()):
            raise ActionPlanValidationError("action_plan_expected_invalid")
        if expected_type == "page_ready" and normalized.get("page") != "topology":
            raise ActionPlanValidationError("action_plan_expected_invalid")
        return normalized


__all__ = ["ActionPlanValidationError", "ActionPlanValidator"]
