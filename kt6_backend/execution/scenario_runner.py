from __future__ import annotations

import json
import re
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from ..page_perception import PagePerceptionService
from .action_guard import ScenarioActionGuard, ScenarioActionGuardError
from .browser_executor import HarnessBrowserExecutor
from .browser_harness_client import BrowserHarnessError
from .grounding import GroundingError, TargetGrounderRegistry
from .models import BrowserAction, BrowserTarget, VisualTarget
from .plan_validator import ActionPlanValidator
from .url_policy import ExecutionURLPolicy, ExecutionURLPolicyError
from .verifier_registry import OutcomeVerifierRegistry, OutcomeVerifierRegistryError


class ScenarioExecutionError(RuntimeError):
    def __init__(self, error_code: str):
        super().__init__(error_code)
        self.error_code = error_code


class ScenarioRunner:
    """Execute a semantic plan against fresh unified perception at every step."""

    def __init__(
        self,
        *,
        page_perception: PagePerceptionService,
        browser_executor: HarnessBrowserExecutor,
        grounders: TargetGrounderRegistry,
        verifiers: OutcomeVerifierRegistry,
        url_policy: ExecutionURLPolicy,
        action_guard: ScenarioActionGuard | None = None,
        clock: Callable[[], float] = time.time,
        wait: Callable[[float], None] = time.sleep,
    ):
        self.page_perception = page_perception
        self.browser_executor = browser_executor
        self.client = browser_executor.client
        self.grounders = grounders
        self.verifiers = verifiers
        self.url_policy = url_policy
        self.action_guard = action_guard or ScenarioActionGuard(clock=clock)
        self.clock = clock
        self.wait = wait
        self.validator = ActionPlanValidator()

    def inspect(
        self,
        start_url: str,
        *,
        browser_target_id: str = "",
        user_request: str = "",
    ) -> dict[str, Any]:
        try:
            with self.client.exclusive_session():
                target_url = self.url_policy.validate(start_url)
                if browser_target_id:
                    browser_session = self.client.open_or_bind_target(
                        target_url,
                        target_id=browser_target_id,
                    )
                else:
                    browser_session = self.client.open_or_bind_target(target_url)
                vision_profile = self._vision_profile(user_request)
                snapshot, graph, capture_summary = self._capture(
                    include_canvas=vision_profile is not None,
                    vision_profile=vision_profile or "nodes_only",
                )
        except BrowserHarnessError as exc:
            raise ScenarioExecutionError(exc.error_code) from exc
        return {
            "browser_session": browser_session,
            "capture_id": snapshot["capture_id"],
            "graph_id": graph["graph_id"],
            "ui_graph": graph,
            "capture_metrics": capture_summary.get("stage_timings_ms", {}),
        }

    def run(
        self,
        plan: Mapping[str, Any],
        *,
        run_id: str,
        out_dir: Path,
        confirmed: bool,
        browser_target_id: str = "",
        update: Callable[[dict[str, Any]], None] | None = None,
        cancelled: Callable[[], bool] | None = None,
    ) -> dict[str, Any]:
        try:
            with self.client.exclusive_session():
                return self._run(
                    plan,
                    run_id=run_id,
                    out_dir=out_dir,
                    confirmed=confirmed,
                    browser_target_id=browser_target_id,
                    update=update,
                    cancelled=cancelled or (lambda: False),
                )
        except BrowserHarnessError as exc:
            # Includes initial attach, before the per-step execution loop starts.
            raise ScenarioExecutionError(exc.error_code) from exc

    def _run(
        self,
        plan: Mapping[str, Any],
        *,
        run_id: str,
        out_dir: Path,
        confirmed: bool,
        browser_target_id: str,
        update: Callable[[dict[str, Any]], None] | None,
        cancelled: Callable[[], bool],
    ) -> dict[str, Any]:
        if not confirmed:
            raise ScenarioExecutionError("execution_confirmation_required")
        action_plan = self.validator.validate(plan)
        target_url = self.url_policy.validate(action_plan["start_url"])
        out_dir.mkdir(parents=True, exist_ok=False)
        _write_json(out_dir / "action-plan.json", action_plan)
        if browser_target_id:
            browser_session = self.client.open_or_bind_target(
                target_url,
                target_id=browser_target_id,
            )
        else:
            browser_session = self.client.open_or_bind_target(target_url)
        step_results: list[dict[str, Any]] = []
        pending: dict[str, Any] | None = None
        capture_sequence = 0

        def emit(value: dict[str, Any]) -> None:
            if update is not None:
                update(value)

        def capture(
            label: str,
            *,
            include_canvas: bool = False,
            vision_profile: str = "nodes_only",
            persist: bool = True,
        ) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
            nonlocal capture_sequence
            self._ensure_not_cancelled(cancelled)
            capture_sequence += 1
            snapshot, graph, summary = self._capture(
                include_canvas=include_canvas,
                vision_profile=vision_profile,
                persist=persist,
            )
            graph_name = f"capture-{capture_sequence:03d}-{label}-ui-graph.json"

            def retain() -> None:
                if not persist:
                    self.page_perception.persist_capture(snapshot["capture_id"])
                _write_json(out_dir / graph_name, graph)
                emit(
                    {
                        "capture_id": snapshot["capture_id"],
                        "graph_id": graph["graph_id"],
                        "capture_file": graph_name,
                        "capture_metrics": summary.get("stage_timings_ms", {}),
                    }
                )

            def discard() -> None:
                if not persist:
                    self.page_perception.discard_capture(snapshot["capture_id"])

            if persist:
                retain()
            else:
                emit(
                    {
                        "capture_metrics": summary.get("stage_timings_ms", {}),
                    }
                )
            return snapshot, graph, {
                "capture_file": graph_name,
                "retain": retain,
                "discard": discard,
            }

        try:
            for step_index, step in enumerate(action_plan["steps"], start=1):
                self._ensure_not_cancelled(cancelled)
                started_at = self.clock()
                emit(
                    {
                        "status": "running",
                        "current_step": step["id"],
                        "current_step_index": step_index,
                    }
                )
                if step["op"] in {"click", "double_click", "hover"}:
                    result, pending = self._pointer_action(
                        step,
                        pending=pending,
                        capture=capture,
                    )
                elif step["op"] == "type":
                    result, pending = self._type(
                        step,
                        pending=pending,
                        capture=capture,
                    )
                elif step["op"] == "press_key":
                    result, pending = self._press_key(
                        step,
                        pending=pending,
                        capture=capture,
                    )
                elif step["op"] == "scroll":
                    result, pending = self._scroll(
                        step,
                        pending=pending,
                        capture=capture,
                    )
                elif step["op"] == "select_option":
                    result, pending = self._select_option(
                        step,
                        pending=pending,
                        capture=capture,
                    )
                elif step["op"] == "verify":
                    result, pending = self._verify(
                        step,
                        pending=pending,
                        capture=capture,
                        cancelled=cancelled,
                    )
                elif step["op"] == "wait":
                    result, pending = self._wait_for(
                        step,
                        pending=pending,
                        capture=capture,
                        cancelled=cancelled,
                    )
                else:
                    raise ScenarioExecutionError("scenario_step_unsupported")
                result.update(
                    {
                        "id": step["id"],
                        "op": step["op"],
                        "status": "completed",
                        "duration_ms": max(
                            0, int((self.clock() - started_at) * 1_000)
                        ),
                    }
                )
                step_results.append(result)
                emit({"steps": list(step_results)})
            if pending is not None:
                raise ScenarioExecutionError("scenario_outcome_step_missing")
            result = {
                "schema_version": "kt6.execution-scenario-result.v1",
                "run_id": run_id,
                "scenario_id": action_plan["scenario_id"],
                "status": "success",
                "start_url": action_plan["start_url"],
                "browser_session": browser_session,
                "steps": step_results,
                "capture_count": capture_sequence,
                "output_dir": str(out_dir),
            }
            _write_json(out_dir / "result.json", result)
            return result
        except (
            BrowserHarnessError,
            ExecutionURLPolicyError,
            GroundingError,
            OutcomeVerifierRegistryError,
            ScenarioActionGuardError,
        ) as exc:
            raise ScenarioExecutionError(exc.error_code) from exc

    def _capture(
        self,
        *,
        include_canvas: bool,
        vision_profile: str,
        persist: bool = True,
    ) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
        payload = self.client.capture_page_payload(
            include_canvas=include_canvas,
            include_preview=False,
        )
        payload["vision_profile"] = vision_profile
        ingested = self.page_perception.ingest(
            payload,
            persist=persist,
            include_execution_views=True,
        )
        graph = ingested.get("ui_graph")
        snapshot = ingested.get("action_snapshot")
        if not isinstance(graph, dict) or not isinstance(snapshot, dict):
            raise ScenarioExecutionError("scenario_capture_incomplete")
        return snapshot, graph, dict(ingested.get("summary", {}))

    def _pointer_action(
        self,
        step: Mapping[str, Any],
        *,
        pending: dict[str, Any] | None,
        capture: Callable[..., tuple[dict[str, Any], dict[str, Any], dict[str, Any]]],
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        if pending is not None:
            raise ScenarioExecutionError("scenario_previous_outcome_unverified")
        before, graph, initial_capture = capture(
            f"{step['id']}-before-dom",
            persist=False,
        )
        try:
            grounded = self.grounders.dom.resolve(step["target"], graph)
        except GroundingError as exc:
            if exc.error_code != "dom_grounding_target_missing":
                self._capture_callback(initial_capture, "retain")
                raise
            self._capture_callback(initial_capture, "discard")
            before, graph, _ = capture(
                f"{step['id']}-before-vision",
                include_canvas=True,
                vision_profile="nodes_only",
            )
            grounded = self.grounders.resolve(step["target"], graph)
        else:
            self._capture_callback(initial_capture, "retain")
        if isinstance(grounded, BrowserTarget):
            fresh, fresh_graph, _ = capture(f"{step['id']}-fresh-dom")
            fresh_grounded = self.grounders.dom.resolve(step["target"], fresh_graph)
            if not isinstance(fresh_grounded, BrowserTarget):
                raise ScenarioExecutionError("scenario_grounding_modality_changed")
            if self.action_guard.target_fingerprint(
                grounded
            ) != self.action_guard.target_fingerprint(fresh_grounded):
                raise ScenarioExecutionError("scenario_grounding_changed")
            grounded = fresh_grounded
            before = fresh
            graph = fresh_graph
        else:
            fresh, fresh_graph, _ = capture(
                f"{step['id']}-fresh-vision",
                include_canvas=True,
                vision_profile="nodes_only",
            )
            fresh_grounded = self.grounders.vision.resolve(
                step["target"], fresh_graph
            )
            if self.action_guard.target_fingerprint(
                grounded
            ) != self.action_guard.target_fingerprint(fresh_grounded):
                raise ScenarioExecutionError("scenario_grounding_changed")
            grounded = fresh_grounded
            before = fresh
            graph = fresh_graph
        action = BrowserAction(str(step["op"]), grounded)
        token = self.action_guard.authorize(action)
        self.action_guard.consume(token, action)
        execution = self.browser_executor.execute(action)
        if not execution.success:
            raise ScenarioExecutionError(execution.error_code)
        return (
            {
                "grounder": "dom_ui_graph"
                if isinstance(grounded, BrowserTarget)
                else "vision_ui_graph",
                "capture_id": str(graph.get("capture_id", "")),
                "graph_id": str(graph.get("graph_id", "")),
                "target_node_id": grounded.node_id,
                "execution_status": "executed_pending_verification",
            },
            {
                "before": before,
                "before_graph": graph,
                "uses_vision": isinstance(grounded, VisualTarget),
            },
        )

    def _press_key(
        self,
        step: Mapping[str, Any],
        *,
        pending: dict[str, Any] | None,
        capture: Callable[..., tuple[dict[str, Any], dict[str, Any], dict[str, Any]]],
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        if pending is not None:
            raise ScenarioExecutionError("scenario_previous_outcome_unverified")
        _before, graph, _ = capture(f"{step['id']}-before")
        grounded = self.grounders.dom.resolve(step["target"], graph)
        fresh, fresh_graph, _ = capture(f"{step['id']}-fresh")
        fresh_grounded = self.grounders.dom.resolve(step["target"], fresh_graph)
        if self.action_guard.target_fingerprint(
            grounded
        ) != self.action_guard.target_fingerprint(fresh_grounded):
            raise ScenarioExecutionError("scenario_grounding_changed")
        action = BrowserAction("press_key", fresh_grounded, key=step["key"])
        token = self.action_guard.authorize(action)
        self.action_guard.consume(token, action)
        execution = self.browser_executor.execute(action)
        if not execution.success:
            raise ScenarioExecutionError(execution.error_code)
        return (
            {
                "grounder": "dom_ui_graph",
                "capture_id": str(fresh_graph.get("capture_id", "")),
                "graph_id": str(fresh_graph.get("graph_id", "")),
                "target_node_id": fresh_grounded.node_id,
                "key": step["key"],
                "execution_status": "executed_pending_verification",
            },
            {
                "before": fresh,
                "before_graph": fresh_graph,
                "uses_vision": False,
            },
        )

    def _scroll(
        self,
        step: Mapping[str, Any],
        *,
        pending: dict[str, Any] | None,
        capture: Callable[..., tuple[dict[str, Any], dict[str, Any], dict[str, Any]]],
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        if pending is not None:
            raise ScenarioExecutionError("scenario_previous_outcome_unverified")
        before, graph, _ = capture(f"{step['id']}-before")
        action = BrowserAction(
            "scroll",
            None,
            direction=step["direction"],
            amount=step["amount"],
        )
        token = self.action_guard.authorize(action)
        self.action_guard.consume(token, action)
        execution = self.browser_executor.execute(action)
        if not execution.success:
            raise ScenarioExecutionError(execution.error_code)
        return (
            {
                "grounder": "bound_viewport",
                "capture_id": str(graph.get("capture_id", "")),
                "graph_id": str(graph.get("graph_id", "")),
                "direction": step["direction"],
                "amount": step["amount"],
                "execution_status": "executed_pending_verification",
            },
            {
                "before": before,
                "before_graph": graph,
                "uses_vision": False,
            },
        )

    def _select_option(
        self,
        step: Mapping[str, Any],
        *,
        pending: dict[str, Any] | None,
        capture: Callable[..., tuple[dict[str, Any], dict[str, Any], dict[str, Any]]],
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        if pending is not None:
            raise ScenarioExecutionError("scenario_previous_outcome_unverified")
        _before, graph, _ = capture(f"{step['id']}-before")
        grounded = self.grounders.dom.resolve(step["target"], graph)
        if grounded.role not in {"combobox", "listbox"}:
            raise ScenarioExecutionError("scenario_select_requires_dom_select")
        fresh, fresh_graph, _ = capture(f"{step['id']}-fresh")
        fresh_grounded = self.grounders.dom.resolve(step["target"], fresh_graph)
        if self.action_guard.target_fingerprint(
            grounded
        ) != self.action_guard.target_fingerprint(fresh_grounded):
            raise ScenarioExecutionError("scenario_grounding_changed")
        action = BrowserAction(
            "select_option",
            fresh_grounded,
            option=step["option"],
        )
        token = self.action_guard.authorize(action)
        self.action_guard.consume(token, action)
        execution = self.browser_executor.execute(action)
        if not execution.success:
            raise ScenarioExecutionError(execution.error_code)
        return (
            {
                "grounder": "dom_ui_graph",
                "capture_id": str(fresh_graph.get("capture_id", "")),
                "graph_id": str(fresh_graph.get("graph_id", "")),
                "target_node_id": fresh_grounded.node_id,
                "option": step["option"],
                "execution_status": "executed_pending_verification",
            },
            {
                "before": fresh,
                "before_graph": fresh_graph,
                "uses_vision": False,
            },
        )

    def _type(
        self,
        step: Mapping[str, Any],
        *,
        pending: dict[str, Any] | None,
        capture: Callable[..., tuple[dict[str, Any], dict[str, Any], dict[str, Any]]],
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        if pending is not None:
            raise ScenarioExecutionError("scenario_previous_outcome_unverified")
        _before, graph, _ = capture(f"{step['id']}-before")
        grounded = self.grounders.resolve(step["target"], graph)
        if not isinstance(grounded, BrowserTarget):
            raise ScenarioExecutionError("scenario_type_requires_dom_target")
        fresh, fresh_graph, _ = capture(f"{step['id']}-fresh")
        fresh_grounded = self.grounders.resolve(step["target"], fresh_graph)
        if not isinstance(fresh_grounded, BrowserTarget):
            raise ScenarioExecutionError("scenario_grounding_modality_changed")
        if self.action_guard.target_fingerprint(
            grounded
        ) != self.action_guard.target_fingerprint(fresh_grounded):
            raise ScenarioExecutionError("scenario_grounding_changed")
        action = BrowserAction("type", fresh_grounded, text=step["text"])
        token = self.action_guard.authorize(action)
        self.action_guard.consume(token, action)
        execution = self.browser_executor.execute(action)
        if not execution.success:
            raise ScenarioExecutionError(execution.error_code)
        return (
            {
                "grounder": "dom_ui_graph",
                "capture_id": str(fresh_graph.get("capture_id", "")),
                "graph_id": str(fresh_graph.get("graph_id", "")),
                "target_node_id": fresh_grounded.node_id,
                "execution_status": "executed_pending_verification",
            },
            {
                "before": fresh,
                "before_graph": fresh_graph,
                "uses_vision": False,
            },
        )

    def _verify(
        self,
        step: Mapping[str, Any],
        *,
        pending: dict[str, Any] | None,
        capture: Callable[..., tuple[dict[str, Any], dict[str, Any], dict[str, Any]]],
        cancelled: Callable[[], bool],
    ) -> tuple[dict[str, Any], None]:
        if pending is None:
            raise ScenarioExecutionError("scenario_verification_without_action")
        return self._observe_until_verified(
            step,
            pending=pending,
            capture=capture,
            cancelled=cancelled,
            label="verify",
        )

    def _wait_for(
        self,
        step: Mapping[str, Any],
        *,
        pending: dict[str, Any] | None,
        capture: Callable[..., tuple[dict[str, Any], dict[str, Any], dict[str, Any]]],
        cancelled: Callable[[], bool],
    ) -> tuple[dict[str, Any], None]:
        if pending is None:
            raise ScenarioExecutionError("scenario_wait_without_action")
        return self._observe_until_verified(
            step,
            pending=pending,
            capture=capture,
            cancelled=cancelled,
            label="wait",
        )

    def _observe_until_verified(
        self,
        step: Mapping[str, Any],
        *,
        pending: dict[str, Any],
        capture: Callable[..., tuple[dict[str, Any], dict[str, Any], dict[str, Any]]],
        cancelled: Callable[[], bool],
        label: str,
    ) -> tuple[dict[str, Any], None]:
        attempt = 0
        while True:
            self._ensure_not_cancelled(cancelled)
            attempt += 1
            after, graph, capture_info = capture(
                f"{step['id']}-{label}-{attempt}",
                include_canvas=pending.get("uses_vision") is True,
                vision_profile="nodes_only",
                persist=False,
            )
            try:
                verified, verifier_id = self.verifiers.verify_expected(
                    expected=step["expected"],
                    action_id="",
                    before=pending["before"],
                    after=after,
                    before_graph=pending["before_graph"],
                    after_graph=graph,
                )
            except Exception:
                self._capture_callback(capture_info, "discard")
                raise
            if verified:
                self._capture_callback(capture_info, "retain")
                return (
                    {
                        "capture_id": str(graph.get("capture_id", "")),
                        "graph_id": str(graph.get("graph_id", "")),
                        "verifier": verifier_id,
                        "attempts": attempt,
                        "outcome_verified": True,
                    },
                    None,
                )
            self._capture_callback(capture_info, "discard")
            # Re-capture quickly for normal transitions, then back off so an
            # intentionally unbounded business wait cannot hammer the page,
            # local CV, or an enabled vision API.
            self.wait(min(2.0, 0.25 * (2 ** min(attempt - 1, 3))))

    @staticmethod
    def _ensure_not_cancelled(cancelled: Callable[[], bool]) -> None:
        if cancelled():
            raise ScenarioExecutionError("execution_cancelled")

    @staticmethod
    def _capture_callback(info: Mapping[str, Any], name: str) -> None:
        callback = info.get(name)
        if callable(callback):
            callback()

    @staticmethod
    def _vision_profile(user_request: str) -> str | None:
        text = re.sub(r"\s+", "", str(user_request).casefold())
        if not text:
            return None
        connectivity_hints = (
            "连接",
            "连线",
            "链路",
            "上下游",
            "拓扑关系",
            "connection",
            "connectivity",
            "linkbetween",
        )
        semantic_hints = (
            "解释图",
            "分析图",
            "设备类型",
            "厂商",
            "型号",
            "semantic",
            "explain",
        )
        visual_hints = (
            "canvas",
            "画布",
            "拓扑",
            "节点",
            "图表",
            "地图",
            "networkmap",
        )
        if any(hint in text for hint in connectivity_hints):
            return "connectivity_query"
        if any(hint in text for hint in semantic_hints):
            return "semantic_enrichment"
        if any(hint in text for hint in visual_hints):
            return "nodes_only"
        return None


def _write_json(path: Path, value: Any) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


__all__ = ["ScenarioExecutionError", "ScenarioRunner"]
