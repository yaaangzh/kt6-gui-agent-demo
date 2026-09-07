from __future__ import annotations

import json
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
                snapshot, graph, preview = self._capture()
        except BrowserHarnessError as exc:
            raise ScenarioExecutionError(exc.error_code) from exc
        return {
            "browser_session": browser_session,
            "capture_id": snapshot["capture_id"],
            "graph_id": graph["graph_id"],
            "ui_graph": graph,
            "preview_data_url": preview,
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
        ) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
            nonlocal capture_sequence
            capture_sequence += 1
            snapshot, graph, preview = self._capture()
            graph_name = f"capture-{capture_sequence:03d}-{label}-ui-graph.json"
            _write_json(out_dir / graph_name, graph)
            emit(
                {
                    "capture_id": snapshot["capture_id"],
                    "graph_id": graph["graph_id"],
                    "latest_preview": preview,
                    "capture_file": graph_name,
                }
            )
            return snapshot, graph, {"capture_file": graph_name}

        try:
            for step_index, step in enumerate(action_plan["steps"], start=1):
                started_at = self.clock()
                emit(
                    {
                        "status": "running",
                        "current_step": step["id"],
                        "current_step_index": step_index,
                    }
                )
                if step["op"] == "click":
                    result, pending = self._click(
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
                elif step["op"] == "verify":
                    result, pending = self._verify(
                        step,
                        pending=pending,
                        capture=capture,
                    )
                elif step["op"] == "wait":
                    result, pending = self._wait_for(
                        step,
                        pending=pending,
                        capture=capture,
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

    def _capture(self) -> tuple[dict[str, Any], dict[str, Any], str]:
        payload = self.client.capture_page_payload(include_canvas=True)
        preview = str(payload.pop("preview_data_url", ""))
        ingested = self.page_perception.ingest(payload)
        capture_id = ingested["capture_id"]
        graph = self.page_perception.get_ui_graph(capture_id)
        snapshot = self.page_perception.get_action_snapshot(capture_id)
        if graph is None or snapshot is None:
            raise ScenarioExecutionError("scenario_capture_incomplete")
        return snapshot, graph, preview

    def _click(
        self,
        step: Mapping[str, Any],
        *,
        pending: dict[str, Any] | None,
        capture: Callable[[str], tuple[dict[str, Any], dict[str, Any], dict[str, Any]]],
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        if pending is not None:
            raise ScenarioExecutionError("scenario_previous_outcome_unverified")
        before, graph, _ = capture(f"{step['id']}-before")
        grounded = self.grounders.resolve(step["target"], graph)
        if isinstance(grounded, BrowserTarget):
            fresh, fresh_graph, _ = capture(f"{step['id']}-fresh")
            fresh_grounded = self.grounders.resolve(step["target"], fresh_graph)
            if not isinstance(fresh_grounded, BrowserTarget):
                raise ScenarioExecutionError("scenario_grounding_modality_changed")
            if self.action_guard.target_fingerprint(
                grounded
            ) != self.action_guard.target_fingerprint(fresh_grounded):
                raise ScenarioExecutionError("scenario_grounding_changed")
            grounded = fresh_grounded
            before = fresh
            graph = fresh_graph
        action = BrowserAction("click", grounded)
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
            },
        )

    def _type(
        self,
        step: Mapping[str, Any],
        *,
        pending: dict[str, Any] | None,
        capture: Callable[[str], tuple[dict[str, Any], dict[str, Any], dict[str, Any]]],
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
            },
        )

    def _verify(
        self,
        step: Mapping[str, Any],
        *,
        pending: dict[str, Any] | None,
        capture: Callable[[str], tuple[dict[str, Any], dict[str, Any], dict[str, Any]]],
    ) -> tuple[dict[str, Any], None]:
        if pending is None:
            raise ScenarioExecutionError("scenario_verification_without_action")
        if step["expected"]["type"] in {"page_changed", "url_changed"}:
            self.client.wait_for_page_settle(
                previous_url=pending["before_graph"]["page"]["url"]
            )
        after, graph, _ = capture(f"{step['id']}-verify")
        verified, verifier_id = self.verifiers.verify_expected(
            expected=step["expected"],
            action_id="",
            before=pending["before"],
            after=after,
            before_graph=pending["before_graph"],
            after_graph=graph,
        )
        if not verified:
            raise ScenarioExecutionError("scenario_expected_outcome_missing")
        return (
            {
                "capture_id": str(graph.get("capture_id", "")),
                "graph_id": str(graph.get("graph_id", "")),
                "verifier": verifier_id,
                "outcome_verified": True,
            },
            None,
        )

    def _wait_for(
        self,
        step: Mapping[str, Any],
        *,
        pending: dict[str, Any] | None,
        capture: Callable[[str], tuple[dict[str, Any], dict[str, Any], dict[str, Any]]],
    ) -> tuple[dict[str, Any], None]:
        if pending is None:
            raise ScenarioExecutionError("scenario_wait_without_action")
        deadline = time.monotonic() + int(step["timeout_ms"]) / 1_000
        attempt = 0
        while time.monotonic() < deadline:
            attempt += 1
            after, graph, _ = capture(f"{step['id']}-wait-{attempt}")
            verified, verifier_id = self.verifiers.verify_expected(
                expected=step["expected"],
                action_id="",
                before=pending["before"],
                after=after,
                before_graph=pending["before_graph"],
                after_graph=graph,
            )
            if verified:
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
            self.wait(0.15)
        raise ScenarioExecutionError("scenario_wait_timeout")


def _write_json(path: Path, value: Any) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


__all__ = ["ScenarioExecutionError", "ScenarioRunner"]
