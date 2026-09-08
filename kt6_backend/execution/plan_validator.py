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
    MAX_INPUT_TEXT = 1_000
    MAX_OPTION_TEXT = 300
    _ACTION_OPERATIONS = frozenset(
        {
            "click",
            "double_click",
            "hover",
            "type",
            "press_key",
            "scroll",
            "select_option",
        }
    )
    _ALLOWED_KEYS = frozenset({"Enter", "Escape"})
    _SCROLL_DIRECTIONS = frozenset({"up", "down"})
    _SCROLL_AMOUNTS = frozenset({"small", "page"})
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
            "input_value",
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
        raw_request = value.get("user_request")
        if not isinstance(raw_request, str) or len(raw_request.strip()) > 2_000:
            raise ActionPlanValidationError("action_plan_request_invalid")
        # Preserve intentional line breaks/spaces for the exact planner context check.
        user_request = raw_request.strip()
        if not scenario_id or not user_request:
            raise ActionPlanValidationError("action_plan_identity_missing")
        parsed = urlsplit(start_url)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.netloc
            or parsed.username
            or parsed.password
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
        if op in {"click", "double_click", "hover"}:
            if set(value) != {"id", "op", "target"}:
                raise ActionPlanValidationError(
                    f"action_plan_{op}_fields_invalid"
                )
            return {"id": step_id, "op": op, "target": self._target(value["target"])}
        if op == "type":
            if set(value) != {"id", "op", "target", "text"}:
                raise ActionPlanValidationError("action_plan_type_fields_invalid")
            text = value.get("text")
            if (
                not isinstance(text, str)
                or not text
                or len(text) > self.MAX_INPUT_TEXT
                or any(ord(character) < 32 or ord(character) == 127 for character in text)
            ):
                raise ActionPlanValidationError("action_plan_type_text_invalid")
            return {
                "id": step_id,
                "op": op,
                "target": self._target(value["target"]),
                "text": text,
            }
        if op == "press_key":
            if set(value) != {"id", "op", "target", "key"}:
                raise ActionPlanValidationError("action_plan_press_key_fields_invalid")
            key = compact_text(value.get("key"), 20)
            if key not in self._ALLOWED_KEYS:
                raise ActionPlanValidationError("action_plan_press_key_invalid")
            return {
                "id": step_id,
                "op": op,
                "target": self._target(value["target"]),
                "key": key,
            }
        if op == "scroll":
            if set(value) != {"id", "op", "direction", "amount"}:
                raise ActionPlanValidationError("action_plan_scroll_fields_invalid")
            direction = compact_text(value.get("direction"), 20).casefold()
            amount = compact_text(value.get("amount"), 20).casefold()
            if (
                direction not in self._SCROLL_DIRECTIONS
                or amount not in self._SCROLL_AMOUNTS
            ):
                raise ActionPlanValidationError("action_plan_scroll_invalid")
            return {
                "id": step_id,
                "op": op,
                "direction": direction,
                "amount": amount,
            }
        if op == "select_option":
            if set(value) != {"id", "op", "target", "option"}:
                raise ActionPlanValidationError(
                    "action_plan_select_option_fields_invalid"
                )
            option = value.get("option")
            if (
                not isinstance(option, str)
                or not option.strip()
                or len(option.strip()) > self.MAX_OPTION_TEXT
                or any(ord(character) < 32 or ord(character) == 127 for character in option)
            ):
                raise ActionPlanValidationError("action_plan_select_option_invalid")
            return {
                "id": step_id,
                "op": op,
                "target": self._target(value["target"]),
                "option": option.strip(),
            }
        if op not in {"verify", "wait"}:
            raise ActionPlanValidationError("action_plan_operation_unsupported")
        allowed = {"id", "op", "expected"}
        if set(value) != allowed:
            raise ActionPlanValidationError("action_plan_condition_fields_invalid")
        expected = self._expected(value["expected"])
        result: dict[str, Any] = {"id": step_id, "op": op, "expected": expected}
        if op == "wait":
            if expected["type"] in {"page_changed", "url_changed"}:
                raise ActionPlanValidationError("action_plan_wait_expected_invalid")
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
        if expected_type == "input_value":
            if set(value) != {"type", "target", "value"}:
                raise ActionPlanValidationError("action_plan_expected_invalid")
            expected_value = value.get("value")
            if (
                not isinstance(expected_value, str)
                or not expected_value
                or len(expected_value) > self.MAX_INPUT_TEXT
                or any(
                    ord(character) < 32 or ord(character) == 127
                    for character in expected_value
                )
            ):
                raise ActionPlanValidationError("action_plan_expected_invalid")
            return {
                "type": expected_type,
                "target": self._target(value["target"]),
                "value": expected_value,
            }
        if set(value) != {"type", "target"}:
            raise ActionPlanValidationError("action_plan_expected_invalid")
        return {"type": expected_type, "target": self._target(value["target"])}

    @staticmethod
    def _sequence(steps: list[dict[str, Any]]) -> None:
        if len(steps) % 2:
            raise ActionPlanValidationError("action_plan_sequence_invalid")
        for index in range(0, len(steps), 2):
            action = steps[index]
            outcome = steps[index + 1]
            if (
                action["op"] not in ActionPlanValidator._ACTION_OPERATIONS
                or outcome["op"] not in {"verify", "wait"}
            ):
                raise ActionPlanValidationError("action_plan_sequence_invalid")
            expected = outcome["expected"]
            if action["op"] == "type":
                if (
                    outcome["op"] != "verify"
                    or expected["type"] != "input_value"
                    or expected["target"] != action["target"]
                    or expected["value"] != action["text"]
                ):
                    raise ActionPlanValidationError("action_plan_type_outcome_invalid")
            elif action["op"] == "select_option":
                if (
                    expected["type"] not in {"element_selected", "selected"}
                    or compact_text(expected["target"].get("query"), 300).casefold()
                    != action["option"].casefold()
                ):
                    raise ActionPlanValidationError(
                        "action_plan_select_option_outcome_invalid"
                    )
            elif action["op"] == "scroll":
                if expected["type"] in {
                    "input_value",
                    "page_changed",
                    "url_changed",
                }:
                    raise ActionPlanValidationError("action_plan_scroll_outcome_invalid")
            elif expected["type"] == "input_value":
                raise ActionPlanValidationError("action_plan_action_outcome_invalid")


__all__ = [
    "ACTION_PLAN_SCHEMA_VERSION",
    "ActionPlanValidationError",
    "ActionPlanValidator",
]
