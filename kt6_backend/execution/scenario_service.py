from __future__ import annotations

import copy
import threading
import uuid
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .natural_language_parser import NaturalLanguageIntentParser
from .plan_generator import FixturePlanGenerator
from .plan_validator import ActionPlanValidator
from .scenario_runner import ScenarioExecutionError, ScenarioRunner, _write_json


class ExecutionScenarioServiceError(ValueError):
    def __init__(self, error_code: str):
        super().__init__(error_code)
        self.error_code = error_code


class ExecutionScenarioService:
    FIXTURE_URL = "http://127.0.0.1:8787/execution-test.html"

    def __init__(self, *, root: Path, runner: ScenarioRunner | None):
        self.root = Path(root).resolve()
        self.runner = runner
        self.parser = NaturalLanguageIntentParser()
        self.generator = FixturePlanGenerator()
        self.validator = ActionPlanValidator()
        self._runs: dict[str, dict[str, Any]] = {}
        self._lock = threading.RLock()

    def generate_plan(self, *, start_url: str, user_request: str) -> dict[str, Any]:
        if str(start_url).strip() != self.FIXTURE_URL:
            raise ExecutionScenarioServiceError("execution_start_url_not_allowed")
        intents = self.parser.parse(user_request)
        plan = self.generator.generate(
            start_url=self.FIXTURE_URL,
            user_request=str(user_request).strip(),
            intents=intents,
        )
        validated = self.validator.validate(plan)
        return {
            "plan": validated,
            "readable_steps": [
                self._readable_step(step) for step in validated["steps"]
            ],
            "requires_confirmation": True,
            "runner_configured": self.runner is not None,
        }

    def start_run(
        self,
        *,
        plan: Mapping[str, Any],
        confirmed: bool,
    ) -> dict[str, Any]:
        if not confirmed:
            raise ExecutionScenarioServiceError("execution_confirmation_required")
        if self.runner is None:
            raise ExecutionScenarioServiceError("execution_runner_not_configured")
        validated = self.validator.validate(plan)
        if validated["start_url"] != self.FIXTURE_URL:
            raise ExecutionScenarioServiceError("execution_start_url_not_allowed")
        with self._lock:
            if any(item["status"] in {"queued", "running"} for item in self._runs.values()):
                raise ExecutionScenarioServiceError("execution_runner_busy")
            run_id = f"run_{uuid.uuid4().hex[:16]}"
            record = self._new_record(run_id, validated)
            self._runs[run_id] = record
        thread = threading.Thread(
            target=self._run,
            args=(run_id, validated),
            daemon=True,
        )
        thread.start()
        return self.get_run(run_id)

    def run_sync(self, plan: Mapping[str, Any]) -> dict[str, Any]:
        if self.runner is None:
            raise ExecutionScenarioServiceError("execution_runner_not_configured")
        validated = self.validator.validate(plan)
        if validated["start_url"] != self.FIXTURE_URL:
            raise ExecutionScenarioServiceError("execution_start_url_not_allowed")
        run_id = f"run_{uuid.uuid4().hex[:16]}"
        with self._lock:
            self._runs[run_id] = self._new_record(run_id, validated)
        self._run(run_id, validated)
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
            "mode": "fixture_natural_language_plan",
            "active_runs": active,
            "supported_url": self.FIXTURE_URL,
        }

    def _run(self, run_id: str, plan: dict[str, Any]) -> None:
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
                update=update,
            )
        except (ScenarioExecutionError, OSError, ValueError) as exc:
            error_code = getattr(exc, "error_code", "execution_scenario_failed")
            with self._lock:
                record = self._runs[run_id]
                record["status"] = "failed"
                record["error_code"] = str(error_code)
            if out_dir.exists():
                _write_json(out_dir / "failed-result.json", self.get_run(run_id))
            return
        with self._lock:
            record = self._runs[run_id]
            record["status"] = "success"
            record["result"] = result
            record["steps"] = copy.deepcopy(result["steps"])

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
            if target.get("source") == "canvas":
                return f"在拓扑中选择 {target['name']}"
            if target["action"] == "open_asset_details":
                return f"打开 {target['asset_id'].upper()} 详情"
            return "打开拓扑"
        expected = step["expected"]
        if expected["type"] == "asset_detail_visible":
            return f"确认 {expected['asset_id'].upper()} 详情已经出现"
        if expected["type"] == "page_ready":
            return "等待拓扑页面加载"
        return f"确认 {expected['asset_id'].upper()} 已被选中"


__all__ = ["ExecutionScenarioService", "ExecutionScenarioServiceError"]
