from __future__ import annotations

import copy
import json
import math
import re
from collections.abc import Mapping
from typing import Any


SCHEMA_VERSION = "kt6.ui-operation-plan.v1"
ALLOWED_OPERATIONS = ("locate", "click", "wait", "verify")
ALLOWED_CONDITION_PREDICATES = (
    "succeeded",
    "failed",
    "found",
    "not_found",
    "verified",
    "not_verified",
)

MAX_MODEL_JSON_CHARS = 64_000
MAX_STEPS = 64
MAX_DEPENDENCIES = 64
MAX_UI_NODES = 10_000
MAX_ID_CHARS = 128
MAX_INSTRUCTION_CHARS = 8_000
MAX_QUERY_CHARS = 500
MAX_EXPECTED_CHARS = 500
MAX_TIMEOUT_MS = 30_000

_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_TRUSTED_CLICK_SOURCES = frozenset({"dom", "cdp"})
_BLOCKING_INTERACTION_STATUSES = frozenset(
    {"analysis_only", "blocked", "disabled", "not_actionable", "rejected"}
)
_ALLOWED_TOP_LEVEL_FIELDS = frozenset({"schema_version", "graph_id", "steps"})
_ALLOWED_STEP_FIELDS = frozenset(
    {"id", "op", "target_node_id", "depends_on", "condition", "args"}
)
_ALLOWED_CONDITION_FIELDS = frozenset({"step_id", "predicate"})
_ALLOWED_ARG_FIELDS = frozenset({"timeout_ms", "query", "expected"})


_MODEL_OUTPUT_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "title": "KT6 UI operation plan",
    "type": "object",
    "additionalProperties": False,
    "required": ["schema_version", "steps"],
    "properties": {
        "schema_version": {"const": SCHEMA_VERSION},
        "graph_id": {"type": "string", "minLength": 1},
        "steps": {
            "type": "array",
            "minItems": 1,
            "maxItems": MAX_STEPS,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["id", "op"],
                "properties": {
                    "id": {
                        "type": "string",
                        "pattern": _ID_PATTERN.pattern,
                        "maxLength": MAX_ID_CHARS,
                    },
                    "op": {"enum": list(ALLOWED_OPERATIONS)},
                    "target_node_id": {
                        "type": "string",
                        "pattern": _ID_PATTERN.pattern,
                        "maxLength": MAX_ID_CHARS,
                    },
                    "depends_on": {
                        "type": "array",
                        "maxItems": MAX_DEPENDENCIES,
                        "uniqueItems": True,
                        "items": {
                            "type": "string",
                            "pattern": _ID_PATTERN.pattern,
                            "maxLength": MAX_ID_CHARS,
                        },
                    },
                    "condition": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["step_id", "predicate"],
                        "properties": {
                            "step_id": {
                                "type": "string",
                                "pattern": _ID_PATTERN.pattern,
                                "maxLength": MAX_ID_CHARS,
                            },
                            "predicate": {
                                "enum": list(ALLOWED_CONDITION_PREDICATES)
                            },
                        },
                    },
                    "args": {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {
                            "timeout_ms": {
                                "type": "integer",
                                "minimum": 0,
                                "maximum": MAX_TIMEOUT_MS,
                            },
                            "query": {
                                "type": "string",
                                "maxLength": MAX_QUERY_CHARS,
                            },
                            "expected": {
                                "type": [
                                    "string",
                                    "number",
                                    "boolean",
                                    "null",
                                ]
                            },
                        },
                    },
                },
            },
        },
    },
}


class _DuplicateJSONKey(ValueError):
    pass


def operation_plan_schema() -> dict[str, Any]:
    """Return the JSON schema that an internal model response must follow."""

    return copy.deepcopy(_MODEL_OUTPUT_SCHEMA)


def validate(
    ui_graph: Mapping[str, Any],
    model_output: Mapping[str, Any] | str,
    instruction: str = "",
    *,
    require_graph_id: bool = False,
) -> dict[str, Any]:
    """Validate and normalize a model-proposed UI operation DAG.

    ``valid`` means that the payload is suitable for inspection as a dry-run
    plan.  This module never grants execution authority.  In particular, every
    click must resolve to an explicit DOM or CDP interaction candidate in the
    server-provided UI graph; model/page-api/vision/text claims cannot grant that
    capability.  A candidate makes the dry-run plan groundable, never executable.
    """

    errors: list[dict[str, Any]] = []
    payload = _decode_model_output(model_output, errors)
    normalized_steps: list[dict[str, Any]] = []
    normalized_graph_id: str | None = None

    if payload is not None:
        _validate_top_level(payload, errors)
        normalized_graph_id = _validate_graph_id(
            ui_graph,
            payload,
            errors,
            required=require_graph_id,
        )
        normalized_steps = _normalize_steps(payload.get("steps"), errors)

    nodes, ambiguous_node_ids = _index_ui_nodes(ui_graph, errors)
    _validate_dependencies(normalized_steps, errors)
    _validate_targets(
        normalized_steps,
        nodes=nodes,
        ambiguous_node_ids=ambiguous_node_ids,
        errors=errors,
    )

    has_click = any(step.get("op") == "click" for step in normalized_steps)
    valid = not errors
    safety_reasons = [
        "model_generated_plan_is_untrusted",
        "dry_run_only_no_page_side_effects",
        "live_execution_requires_separate_preflight_and_authorization",
    ]
    if has_click:
        safety_reasons.append(
            "click_requires_ui_graph_dom_or_cdp_actionable_candidate"
        )
    if valid and has_click:
        safety_reasons.extend(
            [
                "click_target_candidate_grounding_validated",
                "click_candidate_is_not_execution_authorization",
            ]
        )
    if not valid:
        safety_reasons.append("operation_plan_validation_failed")

    normalized_plan: dict[str, Any] = {"steps": normalized_steps}
    if normalized_graph_id is not None:
        normalized_plan["graph_id"] = normalized_graph_id

    return {
        "schema_version": SCHEMA_VERSION,
        "valid": valid,
        "dry_run_only": True,
        "safe_for_execution": False,
        "instruction": _bounded_instruction(instruction),
        "plan": normalized_plan,
        "errors": errors,
        "safety_reasons": safety_reasons,
    }


def _decode_model_output(
    model_output: Mapping[str, Any] | str,
    errors: list[dict[str, Any]],
) -> Mapping[str, Any] | None:
    if isinstance(model_output, str):
        if len(model_output) > MAX_MODEL_JSON_CHARS:
            _error(
                errors,
                "model_output_too_large",
                f"model JSON exceeds {MAX_MODEL_JSON_CHARS} characters",
            )
            return None
        try:
            parsed = json.loads(
                model_output,
                object_pairs_hook=_unique_object,
                parse_constant=_reject_non_finite_constant,
            )
        except _DuplicateJSONKey as exc:
            _error(errors, "duplicate_json_key", str(exc))
            return None
        except (json.JSONDecodeError, ValueError) as exc:
            _error(errors, "invalid_model_json", f"invalid model JSON: {exc}")
            return None
        if not isinstance(parsed, Mapping):
            _error(
                errors,
                "model_output_not_object",
                "model output must be a JSON object",
            )
            return None
        return parsed
    if not isinstance(model_output, Mapping):
        _error(
            errors,
            "model_output_not_object",
            "model output must be a mapping or JSON object string",
        )
        return None
    try:
        canonical_json = json.dumps(
            model_output,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError) as exc:
        _error(errors, "invalid_model_json", f"invalid model JSON: {exc}")
        return None
    if len(canonical_json.encode("utf-8")) > MAX_MODEL_JSON_CHARS:
        _error(
            errors,
            "model_output_too_large",
            f"model JSON exceeds {MAX_MODEL_JSON_CHARS} UTF-8 bytes",
        )
        return None
    try:
        return json.loads(
            canonical_json,
            object_pairs_hook=_unique_object,
            parse_constant=_reject_non_finite_constant,
        )
    except (_DuplicateJSONKey, json.JSONDecodeError, ValueError) as exc:
        _error(errors, "invalid_model_json", f"invalid model JSON: {exc}")
        return None


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateJSONKey(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _reject_non_finite_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON number is not allowed: {value}")


def _validate_top_level(
    payload: Mapping[str, Any], errors: list[dict[str, Any]]
) -> None:
    if payload.get("schema_version") != SCHEMA_VERSION:
        _error(
            errors,
            "unsupported_schema_version",
            f"schema_version must be {SCHEMA_VERSION}",
            path="schema_version",
        )
    unknown = sorted(str(key) for key in payload if key not in _ALLOWED_TOP_LEVEL_FIELDS)
    if unknown:
        _error(
            errors,
            "unknown_top_level_fields",
            f"unknown top-level fields: {', '.join(unknown[:10])}",
        )


def _validate_graph_id(
    ui_graph: Mapping[str, Any],
    payload: Mapping[str, Any],
    errors: list[dict[str, Any]],
    *,
    required: bool,
) -> str | None:
    if "graph_id" not in payload:
        if required:
            _error(
                errors,
                "missing_graph_id",
                "graph_id is required for reasoner-backed planning",
                path="graph_id",
            )
        return None
    graph_id = payload.get("graph_id")
    if not isinstance(graph_id, str) or not graph_id.strip():
        _error(
            errors,
            "invalid_graph_id",
            "graph_id must be a non-empty string when provided",
            path="graph_id",
        )
        return None
    expected = ui_graph.get("graph_id") if isinstance(ui_graph, Mapping) else None
    if graph_id != expected:
        _error(
            errors,
            "graph_id_mismatch",
            "model graph_id does not match the server-provided UI graph",
            path="graph_id",
        )
    return graph_id


def _normalize_steps(
    raw_steps: Any, errors: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    if not isinstance(raw_steps, list):
        _error(errors, "steps_not_array", "steps must be an array", path="steps")
        return []
    if not raw_steps:
        _error(errors, "steps_empty", "steps must contain at least one operation")
        return []
    if len(raw_steps) > MAX_STEPS:
        _error(
            errors,
            "too_many_steps",
            f"steps cannot contain more than {MAX_STEPS} operations",
        )

    normalized: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for index, raw_step in enumerate(raw_steps[:MAX_STEPS]):
        path = f"steps[{index}]"
        if not isinstance(raw_step, Mapping):
            _error(
                errors,
                "step_not_object",
                "each step must be an object",
                path=path,
            )
            continue
        unknown = sorted(
            str(key) for key in raw_step if key not in _ALLOWED_STEP_FIELDS
        )
        if unknown:
            _error(
                errors,
                "unknown_step_fields",
                f"unknown step fields: {', '.join(unknown[:10])}",
                path=path,
            )

        step_id = _valid_id(raw_step.get("id"))
        if step_id is None:
            _error(
                errors,
                "invalid_step_id",
                "step id must be a bounded identifier",
                path=f"{path}.id",
            )
            step_id = ""
        elif step_id in seen_ids:
            _error(
                errors,
                "duplicate_step_id",
                f"duplicate step id: {step_id}",
                step_id=step_id,
                path=f"{path}.id",
            )
        else:
            seen_ids.add(step_id)

        op_value = raw_step.get("op")
        op = op_value if isinstance(op_value, str) else ""
        if op not in ALLOWED_OPERATIONS:
            _error(
                errors,
                "unsupported_operation",
                f"op must be one of: {', '.join(ALLOWED_OPERATIONS)}",
                step_id=step_id or None,
                path=f"{path}.op",
            )

        target = _normalize_target(
            raw_step.get("target_node_id"),
            required=op in {"locate", "click", "verify"},
            step_id=step_id,
            path=path,
            errors=errors,
        )
        dependencies = _normalize_dependencies(
            raw_step.get("depends_on", []),
            step_id=step_id,
            path=path,
            errors=errors,
        )
        condition = _normalize_condition(
            raw_step.get("condition"),
            step_id=step_id,
            path=path,
            errors=errors,
        )
        args = _normalize_args(
            raw_step.get("args"),
            step_id=step_id,
            path=path,
            errors=errors,
        )

        item: dict[str, Any] = {
            "id": step_id,
            "op": op,
            "depends_on": dependencies,
        }
        if target is not None:
            item["target_node_id"] = target
        if condition is not None:
            item["condition"] = condition
        if args is not None:
            item["args"] = args
        normalized.append(item)
    return normalized


def _normalize_target(
    value: Any,
    *,
    required: bool,
    step_id: str,
    path: str,
    errors: list[dict[str, Any]],
) -> str | None:
    if value is None:
        if required:
            _error(
                errors,
                "target_node_id_required",
                "locate, click, and verify require target_node_id",
                step_id=step_id or None,
                path=f"{path}.target_node_id",
            )
        return None
    target = _valid_id(value)
    if target is None:
        _error(
            errors,
            "invalid_target_node_id",
            "target_node_id must be a bounded identifier",
            step_id=step_id or None,
            path=f"{path}.target_node_id",
        )
        return None
    return target


def _normalize_dependencies(
    value: Any,
    *,
    step_id: str,
    path: str,
    errors: list[dict[str, Any]],
) -> list[str]:
    if not isinstance(value, list):
        _error(
            errors,
            "depends_on_not_array",
            "depends_on must be an array",
            step_id=step_id or None,
            path=f"{path}.depends_on",
        )
        return []
    if len(value) > MAX_DEPENDENCIES:
        _error(
            errors,
            "too_many_dependencies",
            f"depends_on cannot contain more than {MAX_DEPENDENCIES} ids",
            step_id=step_id or None,
            path=f"{path}.depends_on",
        )
    result: list[str] = []
    seen: set[str] = set()
    for dependency in value[:MAX_DEPENDENCIES]:
        normalized = _valid_id(dependency)
        if normalized is None:
            _error(
                errors,
                "invalid_dependency_id",
                "dependency ids must be bounded identifiers",
                step_id=step_id or None,
                path=f"{path}.depends_on",
            )
            continue
        if normalized in seen:
            _error(
                errors,
                "duplicate_dependency",
                f"duplicate dependency: {normalized}",
                step_id=step_id or None,
                path=f"{path}.depends_on",
            )
            continue
        seen.add(normalized)
        result.append(normalized)
    return result


def _normalize_condition(
    value: Any,
    *,
    step_id: str,
    path: str,
    errors: list[dict[str, Any]],
) -> dict[str, str] | None:
    if value is None:
        return None
    if not isinstance(value, Mapping):
        _error(
            errors,
            "condition_not_object",
            "condition must be a declarative object, not an expression",
            step_id=step_id or None,
            path=f"{path}.condition",
        )
        return None
    unknown = sorted(str(key) for key in value if key not in _ALLOWED_CONDITION_FIELDS)
    if unknown:
        _error(
            errors,
            "unknown_condition_fields",
            f"unknown condition fields: {', '.join(unknown[:10])}",
            step_id=step_id or None,
            path=f"{path}.condition",
        )
    condition_step = _valid_id(value.get("step_id"))
    predicate_value = value.get("predicate")
    predicate = predicate_value if isinstance(predicate_value, str) else ""
    if condition_step is None:
        _error(
            errors,
            "invalid_condition_step_id",
            "condition.step_id must be a bounded identifier",
            step_id=step_id or None,
            path=f"{path}.condition.step_id",
        )
    if predicate not in ALLOWED_CONDITION_PREDICATES:
        _error(
            errors,
            "invalid_condition_predicate",
            "condition predicate is not supported",
            step_id=step_id or None,
            path=f"{path}.condition.predicate",
        )
    if condition_step is None or predicate not in ALLOWED_CONDITION_PREDICATES:
        return None
    return {"step_id": condition_step, "predicate": predicate}


def _normalize_args(
    value: Any,
    *,
    step_id: str,
    path: str,
    errors: list[dict[str, Any]],
) -> dict[str, Any] | None:
    if value is None:
        return None
    if not isinstance(value, Mapping):
        _error(
            errors,
            "args_not_object",
            "args must be an object",
            step_id=step_id or None,
            path=f"{path}.args",
        )
        return None
    result: dict[str, Any] = {}
    unknown = sorted(str(key) for key in value if key not in _ALLOWED_ARG_FIELDS)
    if unknown:
        _error(
            errors,
            "unsupported_args",
            f"unsupported args: {', '.join(unknown[:10])}",
            step_id=step_id or None,
            path=f"{path}.args",
        )
    if "timeout_ms" in value:
        timeout = value["timeout_ms"]
        if (
            isinstance(timeout, bool)
            or not isinstance(timeout, int)
            or not 0 <= timeout <= MAX_TIMEOUT_MS
        ):
            _error(
                errors,
                "invalid_timeout_ms",
                f"timeout_ms must be an integer from 0 to {MAX_TIMEOUT_MS}",
                step_id=step_id or None,
                path=f"{path}.args.timeout_ms",
            )
        else:
            result["timeout_ms"] = timeout
    if "query" in value:
        query = value["query"]
        if not isinstance(query, str) or len(query) > MAX_QUERY_CHARS:
            _error(
                errors,
                "invalid_query",
                f"query must be a string of at most {MAX_QUERY_CHARS} characters",
                step_id=step_id or None,
                path=f"{path}.args.query",
            )
        else:
            result["query"] = query
    if "expected" in value:
        expected = value["expected"]
        if not _valid_expected(expected):
            _error(
                errors,
                "invalid_expected_value",
                "expected must be a bounded finite JSON scalar",
                step_id=step_id or None,
                path=f"{path}.args.expected",
            )
        else:
            result["expected"] = expected
    return result


def _valid_expected(value: Any) -> bool:
    if value is None or isinstance(value, bool):
        return True
    if isinstance(value, str):
        return len(value) <= MAX_EXPECTED_CHARS
    if isinstance(value, int):
        return not isinstance(value, bool)
    if isinstance(value, float):
        return math.isfinite(value)
    return False


def _validate_dependencies(
    steps: list[dict[str, Any]], errors: list[dict[str, Any]]
) -> None:
    step_by_id = {
        step["id"]: step
        for step in steps
        if step.get("id") and sum(item.get("id") == step.get("id") for item in steps) == 1
    }
    dependencies: dict[str, list[str]] = {
        step_id: list(step.get("depends_on", []))
        for step_id, step in step_by_id.items()
    }
    for step_id, dependency_ids in dependencies.items():
        for dependency_id in dependency_ids:
            if dependency_id == step_id:
                _error(
                    errors,
                    "self_dependency",
                    "a step cannot depend on itself",
                    step_id=step_id,
                )
            elif dependency_id not in step_by_id:
                _error(
                    errors,
                    "unknown_dependency",
                    f"dependency does not exist: {dependency_id}",
                    step_id=step_id,
                )

    cycle_ids = _dependency_cycle_nodes(dependencies)
    if cycle_ids:
        _error(
            errors,
            "dependency_cycle",
            f"operation dependencies contain a cycle: {', '.join(cycle_ids[:10])}",
        )

    for step_id, step in step_by_id.items():
        condition = step.get("condition")
        if not isinstance(condition, Mapping):
            continue
        condition_step = str(condition.get("step_id", ""))
        if condition_step not in step_by_id:
            _error(
                errors,
                "unknown_condition_step",
                f"condition step does not exist: {condition_step}",
                step_id=step_id,
            )
            continue
        if condition_step == step_id:
            _error(
                errors,
                "condition_self_reference",
                "a condition cannot reference its own step",
                step_id=step_id,
            )
            continue
        if not _is_dependency_ancestor(
            condition_step,
            step_id,
            dependencies,
        ):
            _error(
                errors,
                "condition_step_not_dependency",
                "condition.step_id must be a dependency ancestor",
                step_id=step_id,
            )


def _dependency_cycle_nodes(dependencies: Mapping[str, list[str]]) -> list[str]:
    state: dict[str, int] = {}
    cycle_nodes: set[str] = set()

    def visit(step_id: str, path: list[str]) -> None:
        state[step_id] = 1
        path.append(step_id)
        for dependency in dependencies.get(step_id, []):
            if dependency not in dependencies:
                continue
            dependency_state = state.get(dependency, 0)
            if dependency_state == 0:
                visit(dependency, path)
            elif dependency_state == 1:
                try:
                    start = path.index(dependency)
                except ValueError:
                    start = 0
                cycle_nodes.update(path[start:])
        path.pop()
        state[step_id] = 2

    for step_id in dependencies:
        if state.get(step_id, 0) == 0:
            visit(step_id, [])
    return sorted(cycle_nodes)


def _is_dependency_ancestor(
    candidate: str,
    step_id: str,
    dependencies: Mapping[str, list[str]],
) -> bool:
    pending = list(dependencies.get(step_id, []))
    visited: set[str] = set()
    while pending:
        current = pending.pop()
        if current == candidate:
            return True
        if current in visited:
            continue
        visited.add(current)
        pending.extend(dependencies.get(current, []))
    return False


def _index_ui_nodes(
    ui_graph: Mapping[str, Any], errors: list[dict[str, Any]]
) -> tuple[dict[str, Mapping[str, Any]], set[str]]:
    if not isinstance(ui_graph, Mapping):
        _error(errors, "ui_graph_not_object", "ui_graph must be an object")
        return {}, set()
    raw_nodes = _find_ui_nodes(ui_graph)
    if not isinstance(raw_nodes, (list, Mapping)):
        _error(
            errors,
            "ui_graph_nodes_missing",
            "ui_graph must contain a nodes array or object",
        )
        return {}, set()

    entries: list[tuple[Any, Any]]
    if isinstance(raw_nodes, list):
        entries = [(None, value) for value in raw_nodes]
    else:
        entries = list(raw_nodes.items())
    if len(entries) > MAX_UI_NODES:
        _error(
            errors,
            "too_many_ui_nodes",
            f"ui_graph cannot contain more than {MAX_UI_NODES} nodes",
        )
        entries = entries[:MAX_UI_NODES]

    nodes: dict[str, Mapping[str, Any]] = {}
    ambiguous: set[str] = set()
    for index, (mapping_key, raw_node) in enumerate(entries):
        if not isinstance(raw_node, Mapping):
            _error(
                errors,
                "ui_node_not_object",
                "each UI graph node must be an object",
                path=f"ui_graph.nodes[{index}]",
            )
            continue
        explicit_id = raw_node.get("node_id", raw_node.get("id"))
        node_id = _valid_id(explicit_id)
        if node_id is None and mapping_key is not None:
            node_id = _valid_id(mapping_key)
        if node_id is None:
            _error(
                errors,
                "invalid_ui_node_id",
                "each UI graph node must have a bounded node_id",
                path=f"ui_graph.nodes[{index}]",
            )
            continue
        if node_id in nodes:
            ambiguous.add(node_id)
            continue
        nodes[node_id] = raw_node
    for node_id in sorted(ambiguous):
        _error(
            errors,
            "ambiguous_ui_node_id",
            f"UI graph contains duplicate node id: {node_id}",
            target_node_id=node_id,
        )
    return nodes, ambiguous


def _find_ui_nodes(ui_graph: Mapping[str, Any]) -> Any:
    if "nodes" in ui_graph:
        return ui_graph.get("nodes")
    for key in ("graph", "ui_graph"):
        nested = ui_graph.get(key)
        if isinstance(nested, Mapping) and "nodes" in nested:
            return nested.get("nodes")
    return None


def _validate_targets(
    steps: list[dict[str, Any]],
    *,
    nodes: Mapping[str, Mapping[str, Any]],
    ambiguous_node_ids: set[str],
    errors: list[dict[str, Any]],
) -> None:
    for step in steps:
        target = step.get("target_node_id")
        if not isinstance(target, str) or not target:
            continue
        step_id = step.get("id") or None
        if target in ambiguous_node_ids:
            _error(
                errors,
                "ambiguous_target_node",
                "target_node_id is ambiguous in the UI graph",
                step_id=step_id,
                target_node_id=target,
            )
            continue
        node = nodes.get(target)
        if node is None:
            _error(
                errors,
                "target_node_not_found",
                "target_node_id does not exist in the UI graph",
                step_id=step_id,
                target_node_id=target,
            )
            continue
        if step.get("op") == "click" and not _trusted_click_candidate(node):
            _error(
                errors,
                "click_target_not_authorized",
                (
                    "click target must have an unblocked DOM or CDP candidate "
                    "with a live-rebind reference"
                ),
                step_id=step_id,
                target_node_id=target,
            )


def _trusted_click_candidate(node: Mapping[str, Any]) -> bool:
    # A fused node's outer safety state is authoritative. In particular, a
    # nested DOM/CDP record cannot escape a disabled or blocked outer node.
    if _click_candidate_blocked(node):
        return False

    for candidate in _candidate_records(node):
        if candidate is not node and _click_candidate_blocked(candidate):
            continue
        interaction = candidate.get("interaction")
        interaction_candidate = (
            isinstance(interaction, Mapping)
            and interaction.get("candidate") is True
        )
        if not interaction_candidate:
            continue

        source_kind = _source_kind(candidate)
        if source_kind == "dom" and _has_dom_rebind_reference(candidate):
            return True
        if source_kind == "cdp" and _has_positive_backend_node_id(candidate):
            return True
    return False


def _click_candidate_blocked(candidate: Mapping[str, Any]) -> bool:
    if candidate.get("disabled") is True:
        return True
    statuses = [candidate.get("status")]
    interaction = candidate.get("interaction")
    if isinstance(interaction, Mapping):
        if interaction.get("disabled") is True:
            return True
        statuses.append(interaction.get("status"))
    return any(
        isinstance(status, str)
        and status.strip().lower() in _BLOCKING_INTERACTION_STATUSES
        for status in statuses
    )


def _has_dom_rebind_reference(candidate: Mapping[str, Any]) -> bool:
    source = candidate.get("source")
    if isinstance(source, Mapping) and _usable_rebind_value(source.get("source_ref")):
        return True

    interaction = candidate.get("interaction")
    action = candidate.get("action")
    containers = [candidate]
    if isinstance(interaction, Mapping):
        containers.append(interaction)
    if isinstance(action, Mapping):
        containers.append(action)
    for container in containers:
        for key in ("ref", "selector", "dom_ref", "source_ref"):
            if _usable_rebind_value(container.get(key)):
                return True
        binding = container.get("action_binding", container.get("binding"))
        if _usable_action_binding(binding):
            return True
    return False


def _usable_action_binding(value: Any) -> bool:
    if _usable_rebind_value(value):
        return True
    if not isinstance(value, Mapping):
        return False
    return any(
        _usable_rebind_value(value.get(key))
        for key in ("id", "ref", "selector", "dom_ref", "source_ref", "binding_id")
    )


def _usable_rebind_value(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    normalized = value.strip()
    return bool(normalized) and "@capture:" not in normalized.casefold()


def _has_positive_backend_node_id(candidate: Mapping[str, Any]) -> bool:
    values = [candidate.get("backend_node_id")]
    source = candidate.get("source")
    if isinstance(source, Mapping):
        values.append(source.get("backend_node_id"))
    return any(
        isinstance(value, int) and not isinstance(value, bool) and value > 0
        for value in values
    )


def _candidate_records(node: Mapping[str, Any]):
    yield node
    for key in ("candidates", "action_candidates", "actionable_candidates"):
        value = node.get(key)
        if isinstance(value, list):
            for candidate in value:
                if isinstance(candidate, Mapping):
                    yield candidate
        elif isinstance(value, Mapping):
            for source_key, candidate in value.items():
                if not isinstance(candidate, Mapping):
                    continue
                if _source_kind(candidate):
                    yield candidate
                else:
                    enriched = dict(candidate)
                    enriched["source"] = str(source_key)
                    yield enriched
    sources = node.get("sources")
    if isinstance(sources, list):
        for source in sources:
            if isinstance(source, Mapping):
                yield source


def _source_kind(candidate: Mapping[str, Any]) -> str:
    source = candidate.get("source")
    if isinstance(source, str):
        return source.strip().lower()
    if isinstance(source, Mapping):
        for key in ("kind", "source_kind"):
            value = source.get(key)
            if isinstance(value, str):
                return value.strip().lower()
    for key in ("source_kind", "candidate_source"):
        value = candidate.get(key)
        if isinstance(value, str):
            return value.strip().lower()
    kind = candidate.get("kind")
    if isinstance(kind, str) and kind.strip().lower() in _TRUSTED_CLICK_SOURCES:
        return kind.strip().lower()
    return ""


def _valid_id(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    if not _ID_PATTERN.fullmatch(value):
        return None
    return value


def _bounded_instruction(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    return value[:MAX_INSTRUCTION_CHARS]


def _error(
    errors: list[dict[str, Any]],
    code: str,
    message: str,
    *,
    step_id: str | None = None,
    target_node_id: str | None = None,
    path: str | None = None,
) -> None:
    item: dict[str, Any] = {"code": code, "message": message[:500]}
    if step_id:
        item["step_id"] = step_id[:MAX_ID_CHARS]
    if target_node_id:
        item["target_node_id"] = target_node_id[:MAX_ID_CHARS]
    if path:
        item["path"] = path[:300]
    errors.append(item)


__all__ = [
    "ALLOWED_CONDITION_PREDICATES",
    "ALLOWED_OPERATIONS",
    "SCHEMA_VERSION",
    "operation_plan_schema",
    "validate",
]
