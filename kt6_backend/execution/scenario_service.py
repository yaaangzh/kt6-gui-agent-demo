from __future__ import annotations

import copy
import threading
import uuid
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .action_planner import ActionPlanner, ActionPlannerError
from .error_categories import classify_error
from .plan_validator import ActionPlanValidator
from .scenario_runner import ScenarioExecutionError, ScenarioRunner, _write_json
from .url_policy import ExecutionURLPolicy, ExecutionURLPolicyError


class ExecutionScenarioServiceError(ValueError):
    def __init__(self, error_code: str):
        super().__init__(error_code)
        self.error_code = error_code


class ExecutionScenarioService:
    def __init__(
        self,
        *,
        root: Path,
        runner: ScenarioRunner | None,
        planner: ActionPlanner | None,
        url_policy: ExecutionURLPolicy,
    ):
        self.root = Path(root).resolve()
        self.runner = runner
        self.planner = planner
        self.url_policy = url_policy
        self.validator = ActionPlanValidator()
        self._runs: dict[str, dict[str, Any]] = {}
        self._lock = threading.RLock()
        # Reserve before starting a worker so queued runs also exclude planning.
        self._operation_lock = threading.Lock()

    def generate_plan(
        self,
        *,
        start_url: str,
        user_request: str,
        browser_target_id: str = "",
    ) -> dict[str, Any]:
        if self.runner is None:
            raise ExecutionScenarioServiceError("execution_runner_not_configured")
        if self.planner is None:
            raise ExecutionScenarioServiceError("execution_planner_not_configured")
        if not self._operation_lock.acquire(blocking=False):
            raise ExecutionScenarioServiceError("execution_runner_busy")
        try:
            target_url = self.url_policy.validate(start_url)
            request = str(user_request).strip()
            if not request or len(request) > 2_000:
                raise ExecutionScenarioServiceError("execution_request_invalid")
            if browser_target_id:
                inspection = self.runner.inspect(
                    target_url,
                    browser_target_id=browser_target_id,
                )
            else:
                inspection = self.runner.inspect(target_url)
            validated = self.planner.plan(
                start_url=target_url,
                user_request=request,
                ui_graph=inspection["ui_graph"],
            )
            validated = self.validator.validate(validated)
            if (
                validated["start_url"] != target_url
                or validated["user_request"] != request
            ):
                raise ExecutionScenarioServiceError(
                    "execution_planner_context_mismatch"
                )
        except (
            ActionPlannerError,
            ExecutionURLPolicyError,
            ScenarioExecutionError,
        ) as exc:
            raise ExecutionScenarioServiceError(exc.error_code) from exc
        finally:
            self._operation_lock.release()
        return {
            "plan": validated,
            "readable_steps": [
                self._readable_step(step) for step in validated["steps"]
            ],
            "requires_confirmation": True,
            "runner_configured": True,
            "planner": {
                "planner_id": self.planner.planner_id,
                "model": self.planner.planner_model,
            },
            "planning_capture_id": inspection["capture_id"],
            "planning_graph_id": inspection["graph_id"],
            "planning_preview": inspection["preview_data_url"],
            "browser_target_id": str(
                inspection.get("browser_session", {}).get("target_id", "")
            ),
        }

    def start_run(
        self,
        *,
        plan: Mapping[str, Any],
        confirmed: bool,
        browser_target_id: str = "",
    ) -> dict[str, Any]:
        if not confirmed:
            raise ExecutionScenarioServiceError("execution_confirmation_required")
        if self.runner is None:
            raise ExecutionScenarioServiceError("execution_runner_not_configured")
        validated = self.validator.validate(plan)
        try:
            self.url_policy.validate(validated["start_url"])
        except ExecutionURLPolicyError as exc:
            raise ExecutionScenarioServiceError(exc.error_code) from exc
        if not self._operation_lock.acquire(blocking=False):
            raise ExecutionScenarioServiceError("execution_runner_busy")
        with self._lock:
            run_id = f"run_{uuid.uuid4().hex[:16]}"
            record = self._new_record(run_id, validated)
            self._runs[run_id] = record
        thread = threading.Thread(
            target=self._run,
            args=(run_id, validated, browser_target_id),
            daemon=True,
        )
        try:
            thread.start()
        except Exception as exc:
            with self._lock:
                record["status"] = "failed"
                record["error_code"] = "execution_worker_start_failed"
                record["error_category"] = classify_error(record["error_code"])
            self._operation_lock.release()
            raise ExecutionScenarioServiceError("execution_worker_start_failed") from exc
        return self.get_run(run_id)

    def run_sync(
        self,
        plan: Mapping[str, Any],
        *,
        browser_target_id: str = "",
    ) -> dict[str, Any]:
        if self.runner is None:
            raise ExecutionScenarioServiceError("execution_runner_not_configured")
        validated = self.validator.validate(plan)
        try:
            self.url_policy.validate(validated["start_url"])
        except ExecutionURLPolicyError as exc:
            raise ExecutionScenarioServiceError(exc.error_code) from exc
        if not self._operation_lock.acquire(blocking=False):
            raise ExecutionScenarioServiceError("execution_runner_busy")
        run_id = f"run_{uuid.uuid4().hex[:16]}"
        with self._lock:
            self._runs[run_id] = self._new_record(run_id, validated)
        self._run(
            run_id,
            validated,
            browser_target_id,
        )
        result = self.get_run(run_id)
        if result["status"] != "success":
            raise ExecutionScenarioServiceError(result.get("error_code", "execution_failed"))
        return result

    def get_run(self, run_id: str) -> dict[str, Any]:
        with self._lock:
            record = self._runs.get(str(run_id).strip())
            if record is None:
                raise ExecutionScenarioServiceError("execution_run_not_found")
            return copy.deepcopy(record)

    def health(self) -> dict[str, Any]:
        with self._lock:
            active = sum(
                item["status"] in {"queued", "running"}
                for item in self._runs.values()
            )
        return {
            "configured": self.runner is not None,
            "planner_configured": self.planner is not None,
            "mode": "generic_llm_action_plan",
            "active_runs": active,
            "url_policy": self.url_policy.health(),
        }

    def runtime_health(self) -> dict[str, Any]:
        configured = self.health()
        if self.runner is None:
            runtime = {
                "ready": False,
                "transport": "browser_harness",
                "browser_harness": {
                    "ready": False,
                    "error": "execution_runner_not_configured",
                    "mode": "existing_chrome",
                },
            }
        else:
            runtime = self.runner.client.runtime_health()
        return {
            **configured,
            **runtime,
            "ready": (
                configured["configured"]
                and configured["planner_configured"]
                and runtime["ready"]
            ),
        }

    def _run(
        self,
        run_id: str,
        plan: dict[str, Any],
        browser_target_id: str,
    ) -> None:
        out_dir = self.root / "runtime_data" / "execution_scenarios" / run_id
        with self._lock:
            self._runs[run_id]["status"] = "running"
            self._runs[run_id]["output_dir"] = str(out_dir)

        def update(delta: dict[str, Any]) -> None:
            with self._lock:
                record = self._runs[run_id]
                for key, value in delta.items():
                    record[key] = copy.deepcopy(value)

        try:
            result = self.runner.run(
                plan,
                run_id=run_id,
                out_dir=out_dir,
                confirmed=True,
                browser_target_id=browser_target_id,
                update=update,
            )
            with self._lock:
                record = self._runs[run_id]
                record["result"] = result
                record["steps"] = copy.deepcopy(result["steps"])
                record["status"] = "success"
        except Exception as exc:
            # Worker boundary: every exception must leave a terminal, queryable
            # state. Never expose raw dependency errors or page data to the UI.
            error_code = getattr(exc, "error_code", "execution_scenario_failed")
            with self._lock:
                record = self._runs[run_id]
                record["status"] = "failed"
                record["error_code"] = str(error_code)
                record["error_category"] = classify_error(str(error_code))
            try:
                if out_dir.exists():
                    _write_json(out_dir / "failed-result.json", self.get_run(run_id))
            except OSError:
                with self._lock:
                    record["evidence_error_code"] = "execution_evidence_write_failed"
        finally:
            self._operation_lock.release()

    @staticmethod
    def _new_record(run_id: str, plan: dict[str, Any]) -> dict[str, Any]:
        return {
            "schema_version": "kt6.execution-run-status.v1",
            "run_id": run_id,
            "status": "queued",
            "scenario_id": plan["scenario_id"],
            "plan": copy.deepcopy(plan),
            "current_step": "",
            "current_step_index": 0,
            "steps": [],
            "latest_preview": "",
            "capture_id": "",
            "graph_id": "",
            "output_dir": "",
        }

    @staticmethod
    def _readable_step(step: Mapping[str, Any]) -> str:
        if step["op"] == "click":
            target = step["target"]
            return f"点击：{target['query']}"
        if step["op"] == "type":
            target = step["target"]
            return f"在“{target['query']}”中输入：{step['text']}"
        expected = step["expected"]
        if expected["type"] in {"page_changed", "url_changed"}:
            return "确认页面已经跳转"
        target = expected["target"]
        prefix = "等待" if step["op"] == "wait" else "确认"
        if expected["type"] in {"element_selected", "selected"}:
            state = "已选中"
        elif expected["type"] == "element_disappeared":
            state = "已消失"
        elif expected["type"] == "input_value":
            state = f"内容为“{expected['value']}”"
        else:
            state = "已出现"
        return f"{prefix}：{target['query']} {state}"


__all__ = ["ExecutionScenarioService", "ExecutionScenarioServiceError"]
