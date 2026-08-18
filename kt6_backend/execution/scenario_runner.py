from __future__ import annotations

import json
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from ..page_perception import PagePerceptionService
from ..safe_dom_actions import SafeDOMActionService
from .browser_executor import HarnessBrowserExecutor
from .browser_harness_client import BrowserHarnessError
from .grounding import GroundedDOMStep, GroundingError, TargetGrounderRegistry
from .models import BrowserAction, CanvasTarget
from .plan_validator import ActionPlanValidator
from .verifier_registry import OutcomeVerifierRegistry, OutcomeVerifierRegistryError


class ScenarioExecutionError(RuntimeError):
    def __init__(self, error_code: str):
        super().__init__(error_code)
        self.error_code = error_code


class ScenarioRunner:
    """Execute a validated plan against fresh browser evidence at every step."""

    def __init__(
        self,
        *,
        page_perception: PagePerceptionService,
        safe_dom_actions: SafeDOMActionService,
        browser_executor: HarnessBrowserExecutor,
        grounders: TargetGrounderRegistry,
        verifiers: OutcomeVerifierRegistry,
        clock: Callable[[], float] = time.time,
        wait: Callable[[float], None] = time.sleep,
    ):
        self.page_perception = page_perception
        self.safe_dom_actions = safe_dom_actions
        self.browser_executor = browser_executor
        self.client = browser_executor.client
        self.grounders = grounders
        self.verifiers = verifiers
        self.clock = clock
        self.wait = wait
        self.validator = ActionPlanValidator()

    def run(
        self,
        plan: Mapping[str, Any],
        *,
        run_id: str,
        out_dir: Path,
        update: Callable[[dict[str, Any]], None] | None = None,
    ) -> dict[str, Any]:
        action_plan = self.validator.validate(plan)
        out_dir.mkdir(parents=True, exist_ok=False)
        self.client.reset_execution_fixture(action_plan["start_url"])
        browser_session = self.client.bind_page_target(action_plan["start_url"])
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
        ) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
            nonlocal capture_sequence
            capture_sequence += 1
            payload = self.client.capture_page_payload(
                expected_page_url=action_plan["start_url"],
                include_canvas=include_canvas,
            )
            preview = str(payload.pop("preview_data_url", ""))
            ingested = self.page_perception.ingest(payload)
            capture_id = ingested["capture_id"]
            graph = self.page_perception.get_ui_graph(capture_id)
            snapshot = self.page_perception.get_action_snapshot(capture_id)
            if graph is None or snapshot is None:
                raise ScenarioExecutionError("scenario_capture_incomplete")
            graph_name = f"capture-{capture_sequence:03d}-{label}-ui-graph.json"
            _write_json(out_dir / graph_name, graph)
            emit(
                {
                    "capture_id": capture_id,
                    "graph_id": graph["graph_id"],
                    "latest_preview": preview,
                    "capture_file": graph_name,
                }
            )
            return snapshot, graph, ingested

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
                        run_id=run_id,
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
            _write_json(out_dir / "action-plan.json", action_plan)
            _write_json(out_dir / "result.json", result)
            return result
        except (BrowserHarnessError, GroundingError, OutcomeVerifierRegistryError) as exc:
            raise ScenarioExecutionError(exc.error_code) from exc

    def _click(
        self,
        step: Mapping[str, Any],
        *,
        pending: dict[str, Any] | None,
        capture: Callable[[str], tuple[dict[str, Any], dict[str, Any], dict[str, Any]]],
        run_id: str,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        if pending is not None:
            raise ScenarioExecutionError("scenario_previous_outcome_unverified")
        before, graph, _ = capture(
            f"{step['id']}-before",
            include_canvas=step["target"].get("source") == "canvas",
        )
        grounded = self.grounders.resolve(step["target"], graph)
        if isinstance(grounded, GroundedDOMStep):
            prepared = self.safe_dom_actions.prepare(
                asset_reference=grounded.asset_id,
                action=grounded.action_id,
                page_capture_id=grounded.capture_id,
                scope={"site_id": "site_1"},
                task_id=run_id,
                principal_id="execution-runner",
            )
            if prepared.get("status") != "prepared":
                raise ScenarioExecutionError(
                    str(prepared.get("reason", "scenario_prepare_failed"))
                )
            fresh, fresh_graph, _ = capture(f"{step['id']}-fresh")
            fresh_grounded = self.grounders.resolve(step["target"], fresh_graph)
            if not isinstance(fresh_grounded, GroundedDOMStep):
                raise ScenarioExecutionError("scenario_grounding_modality_changed")
            ready = self.safe_dom_actions.preflight(
                plan_id=prepared["plan_id"],
                current_capture_id=fresh_grounded.capture_id,
                confirmed=True,
                confirmed_asset_id=fresh_grounded.asset_id,
                confirmed_action=fresh_grounded.action_id,
                permissions=["assets.read"],
            )
            if ready.get("status") != "ready":
                raise ScenarioExecutionError(
                    str(ready.get("reason", "scenario_preflight_failed"))
                )
            dispatched = self.safe_dom_actions.execute(
                execution_token=ready["execution_token"],
                dry_run=False,
                graph_id=fresh_grounded.graph_id,
                target_node_id=fresh_grounded.target_node_id,
            )
            if dispatched.get("status") != "executed_pending_verification":
                raise ScenarioExecutionError(
                    str(dispatched.get("reason", "scenario_execution_failed"))
                )
            return (
                {
                    "grounder": fresh_grounded.grounder_id,
                    "capture_id": fresh_grounded.capture_id,
                    "graph_id": fresh_grounded.graph_id,
                    "target_node_id": fresh_grounded.target_node_id,
                    "execution_status": dispatched["status"],
                },
                {
                    "kind": "dom",
                    "action_id": fresh_grounded.action_id,
                    "asset_id": fresh_grounded.asset_id,
                    "plan_id": prepared["plan_id"],
                    "before": fresh,
                },
            )
        if not isinstance(grounded, CanvasTarget):
            raise ScenarioExecutionError("scenario_grounding_failed")
        execution = self.browser_executor.execute(BrowserAction("click", grounded))
        if not execution.success:
            raise ScenarioExecutionError(execution.error_code)
        return (
            {
                "grounder": "canvas_ui_graph",
                "capture_id": str(before.get("capture_id", "")),
                "graph_id": str(graph.get("graph_id", "")),
                "target_node_id": grounded.node_id,
                "canvas_backend_node_id": grounded.canvas_backend_node_id,
                "execution_status": "executed_pending_verification",
            },
            {
                "kind": "canvas",
                "action_id": "select_canvas_asset",
                "asset_id": grounded.asset_id,
                "before": before,
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
        after, graph, _ = capture(f"{step['id']}-verify")
        verified, verifier_id = self.verifiers.verify_expected(
            expected=step["expected"],
            action_id=pending["action_id"],
            before=pending["before"],
            after=after,
        )
        if not verified:
            if pending["kind"] == "dom":
                self.safe_dom_actions.verify_outcome(
                    plan_id=pending["plan_id"],
                    current_capture_id=str(after.get("capture_id", "")),
                )
            raise ScenarioExecutionError("scenario_expected_outcome_missing")
        if pending["kind"] == "dom":
            finalized = self.safe_dom_actions.verify_outcome(
                plan_id=pending["plan_id"],
                current_capture_id=str(after.get("capture_id", "")),
            )
            if finalized.get("status") != "verified":
                raise ScenarioExecutionError(
                    str(finalized.get("reason", "scenario_verification_failed"))
                )
        return (
            {
                "capture_id": str(after.get("capture_id", "")),
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
                action_id=pending["action_id"],
                before=pending["before"],
                after=after,
            )
            if verified:
                if pending["kind"] == "dom":
                    finalized = self.safe_dom_actions.verify_outcome(
                        plan_id=pending["plan_id"],
                        current_capture_id=str(after.get("capture_id", "")),
                    )
                    if finalized.get("status") != "verified":
                        raise ScenarioExecutionError(
                            str(finalized.get("reason", "scenario_verification_failed"))
                        )
                return (
                    {
                        "capture_id": str(after.get("capture_id", "")),
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
