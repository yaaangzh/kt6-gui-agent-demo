from __future__ import annotations

import json
import math
import os
from dataclasses import dataclass
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Type
from urllib.parse import parse_qs, urlparse

from .asset_inventory import (
    AssetResolver,
    JSONAssetInventoryAdapter,
)
from .dom_action_binding import DOMActionBindingService
from .env_config import load_project_env
from .execution.browser_executor import HarnessBrowserExecutor
from .execution.browser_harness_client import BrowserHarnessClient
from .execution.error_categories import classify_error
from .execution.action_planner import ActionPlanner, OpenAIActionPlanner
from .execution.grounding import TargetGrounderRegistry
from .execution.scenario_runner import ScenarioRunner
from .execution.scenario_service import (
    ExecutionScenarioService,
    ExecutionScenarioServiceError,
)
from .execution.target_resolver import UIGraphTargetResolver
from .execution.verifier import (
    AssetDetailOutcomeVerifier,
    CanvasSelectionVerifier,
    PageReadyVerifier,
    UIGraphOutcomeVerifier,
)
from .execution.verifier_registry import OutcomeVerifierRegistry
from .execution.url_policy import ExecutionURLPolicy
from .http_canvas_vision import HTTPTopologyVisionAdapter
from .hybrid_canvas_vision import HybridCanvasVisionAdapter
from .local_cv_canvas_vision import LocalCVTopologyVisionAdapter
from .memory import SQLiteMemoryStore
from .openai_compatible_api import OpenAICompatibleChatClient
from .page_capture_jobs import PageCaptureJobCapacityError, PageCaptureJobService
from .page_perception import PagePerceptionService, SQLitePageCaptureStore
from .perception import HybridPerception
from .perception_runtime import PerceptionRuntime
from .playbook_loader import PlaybookLoader
from .runtime import KT6Runtime
from .safe_dom_actions import SafeDOMActionService
from .scene_store import SQLiteSceneStore
from .topology_text_recognizer import TopologyTextRecognizer
from .tools import MockBusinessTools
from .ui_graph_planning import (
    UIGraphNotFoundError,
    UIGraphProjectionError,
    UIGraphPlanningService,
    UIGraphReasonerNotConfiguredError,
)
from .ui_graph_reasoner import HTTPUIGraphReasoner, UIGraphReasoningError
from .vision_recognition import CanvasVisionAdapter
from .vision_cache_coordinator import VisionCacheCoordinator
from .vision_result_cache import SQLiteVisionResultCacheStore


ROOT = Path(__file__).resolve().parent.parent
DEMO_DIR = ROOT / "demo"
VISION_DRIVER_ENV = "KT6_VISION_DRIVER"
VISION_ENDPOINT_ENV = "KT6_VISION_ENDPOINT"
VISION_API_KEY_ENV = "KT6_VISION_API_KEY"
VISION_TIMEOUT_ENV = "KT6_VISION_TIMEOUT_SECONDS"
UI_GRAPH_REASONER_ENDPOINT_ENV = "KT6_UI_GRAPH_REASONER_ENDPOINT"
UI_GRAPH_REASONER_API_KEY_ENV = "KT6_UI_GRAPH_REASONER_API_KEY"
UI_GRAPH_REASONER_ALLOWED_HOSTS_ENV = "KT6_UI_GRAPH_REASONER_ALLOWED_HOSTS"
UI_GRAPH_REASONER_TIMEOUT_ENV = "KT6_UI_GRAPH_REASONER_TIMEOUT_SECONDS"
BROWSER_EXECUTION_DRIVER_ENV = "KT6_BROWSER_EXECUTION_DRIVER"
EXECUTION_ALLOW_PRIVATE_NETWORKS_ENV = "KT6_EXECUTION_ALLOW_PRIVATE_NETWORKS"
MODEL_API_PROVIDER_ENV = "KT6_MODEL_API_PROVIDER"
MODEL_API_BASE_URL_ENV = "KT6_MODEL_API_BASE_URL"
MODEL_API_KEY_ENV = "KT6_MODEL_API_KEY"
MODEL_API_MODEL_ENV = "KT6_MODEL_API_MODEL"
MODEL_API_ALLOWED_HOSTS_ENV = "KT6_MODEL_API_ALLOWED_HOSTS"
MODEL_API_MAX_TOKENS_ENV = "KT6_MODEL_API_MAX_TOKENS"
MODEL_API_TIMEOUT_ENV = "KT6_MODEL_API_TIMEOUT_SECONDS"
DEFAULT_VISION_TIMEOUT_SECONDS = 30.0
DEFAULT_UI_GRAPH_REASONER_TIMEOUT_SECONDS = 60.0
MAX_VISION_TIMEOUT_SECONDS = 300.0
MAX_JSON_REQUEST_BYTES = 32 * 1024 * 1024
MAX_UI_GRAPH_REASONER_TIMEOUT_SECONDS = 300.0


class RequestBodyTooLarge(ValueError):
    pass


def _optional_env(name: str) -> str | None:
    value = os.environ.get(name)
    if value is None:
        return None
    value = value.strip()
    return value or None


def _boolean_env(name: str, *, default: bool = False) -> bool:
    value = _optional_env(name)
    if value is None:
        return default
    normalized = value.casefold()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be a boolean")


def _create_canvas_vision_from_env() -> CanvasVisionAdapter | None:
    """Build the production vision adapter without exposing secret config."""

    driver = _optional_env(VISION_DRIVER_ENV)
    endpoint = _optional_env(VISION_ENDPOINT_ENV)
    api_key = _optional_env(VISION_API_KEY_ENV)
    timeout_text = _optional_env(VISION_TIMEOUT_ENV)
    if driver is None and endpoint is None:
        configured_companions = [
            name
            for name, value in (
                (VISION_API_KEY_ENV, api_key),
                (VISION_TIMEOUT_ENV, timeout_text),
            )
            if value is not None
        ]
        if configured_companions:
            names = ", ".join(configured_companions)
            raise ValueError(
                f"{VISION_ENDPOINT_ENV} is required when {names} is configured"
            )
        return None

    selected_driver = (driver or "http").strip().lower()
    if selected_driver not in {
        "http",
        "local_cv_ocr",
        "hybrid",
    }:
        raise ValueError(
            f"{VISION_DRIVER_ENV} must be http, local_cv_ocr, or hybrid"
        )

    if selected_driver == "local_cv_ocr":
        conflicting = [
            name
            for name, value in (
                (VISION_ENDPOINT_ENV, endpoint),
                (VISION_API_KEY_ENV, api_key),
                (VISION_TIMEOUT_ENV, timeout_text),
            )
            if value is not None
        ]
        if conflicting:
            raise ValueError(
                f"{', '.join(conflicting)} must not be configured for local_cv_ocr"
            )
        return LocalCVTopologyVisionAdapter()

    timeout_seconds = DEFAULT_VISION_TIMEOUT_SECONDS
    if timeout_text is not None:
        try:
            timeout_seconds = float(timeout_text)
        except ValueError:
            raise ValueError(
                f"{VISION_TIMEOUT_ENV} must be a finite number in (0, "
                f"{MAX_VISION_TIMEOUT_SECONDS:g}]"
            ) from None
        if not math.isfinite(timeout_seconds) or not (
            0 < timeout_seconds <= MAX_VISION_TIMEOUT_SECONDS
        ):
            raise ValueError(
                f"{VISION_TIMEOUT_ENV} must be a finite number in (0, "
                f"{MAX_VISION_TIMEOUT_SECONDS:g}]"
            )

    if endpoint is None:
        raise ValueError(
            f"{VISION_ENDPOINT_ENV} is required for the http or hybrid vision driver"
        )
    model_adapter = HTTPTopologyVisionAdapter(
        endpoint=endpoint,
        api_key=api_key,
        timeout_seconds=timeout_seconds,
    )
    if selected_driver == "hybrid":
        return HybridCanvasVisionAdapter(
            local_adapter=LocalCVTopologyVisionAdapter(),
            model_adapter=model_adapter,
        )
    return model_adapter


def _canvas_vision_health(adapter: Any | None) -> dict[str, Any]:
    if adapter is None:
        return {
            "configured": False,
            "adapter_id": None,
            "adapter_version": None,
            "routing_mode": "evidence_only",
            "timeout_seconds": None,
        }

    model_adapter = getattr(adapter, "model_adapter", None)
    timeout_value = getattr(model_adapter or adapter, "timeout_seconds", None)
    try:
        timeout_seconds = float(timeout_value) if timeout_value is not None else None
    except (TypeError, ValueError):
        timeout_seconds = None
    if timeout_seconds is not None and not math.isfinite(timeout_seconds):
        timeout_seconds = None
    hybrid = bool(
        getattr(adapter, "local_adapter", None) is not None
        and model_adapter is not None
    )
    result = {
        "configured": True,
        "adapter_id": str(getattr(adapter, "adapter_id", "unknown"))[:200],
        "adapter_version": str(
            getattr(adapter, "adapter_version", "unknown")
        )[:100],
        "routing_mode": "cv_first_adaptive" if hybrid else "single_adapter",
        "timeout_seconds": timeout_seconds,
    }
    if hybrid:
        result["local_adapter_id"] = str(
            getattr(adapter.local_adapter, "adapter_id", "unknown")
        )[:200]
        result["model_adapter_id"] = str(
            getattr(model_adapter, "adapter_id", "unknown")
        )[:200]
    return result


def _create_ui_graph_reasoner_from_env() -> HTTPUIGraphReasoner | None:
    """Build the internal GLM adapter without exposing its endpoint or token."""

    endpoint = _optional_env(UI_GRAPH_REASONER_ENDPOINT_ENV)
    api_key = _optional_env(UI_GRAPH_REASONER_API_KEY_ENV)
    timeout_text = _optional_env(UI_GRAPH_REASONER_TIMEOUT_ENV)
    allowed_hosts_text = _optional_env(UI_GRAPH_REASONER_ALLOWED_HOSTS_ENV)
    allowed_hosts = tuple(
        host.strip()
        for host in (allowed_hosts_text or "").split(",")
        if host.strip()
    )
    if endpoint is None:
        companions = [
            name
            for name, value in (
                (UI_GRAPH_REASONER_API_KEY_ENV, api_key),
                (UI_GRAPH_REASONER_TIMEOUT_ENV, timeout_text),
                (UI_GRAPH_REASONER_ALLOWED_HOSTS_ENV, allowed_hosts_text),
            )
            if value is not None
        ]
        if companions:
            raise ValueError(
                f"{UI_GRAPH_REASONER_ENDPOINT_ENV} is required when "
                f"{', '.join(companions)} is configured"
            )
        return None

    timeout_seconds = DEFAULT_UI_GRAPH_REASONER_TIMEOUT_SECONDS
    if timeout_text is not None:
        try:
            timeout_seconds = float(timeout_text)
        except ValueError:
            raise ValueError(
                f"{UI_GRAPH_REASONER_TIMEOUT_ENV} must be a finite number in (0, "
                f"{MAX_UI_GRAPH_REASONER_TIMEOUT_SECONDS:g}]"
            ) from None
        if not math.isfinite(timeout_seconds) or not (
            0 < timeout_seconds <= MAX_UI_GRAPH_REASONER_TIMEOUT_SECONDS
        ):
            raise ValueError(
                f"{UI_GRAPH_REASONER_TIMEOUT_ENV} must be a finite number in (0, "
                f"{MAX_UI_GRAPH_REASONER_TIMEOUT_SECONDS:g}]"
            )
    return HTTPUIGraphReasoner(
        endpoint=endpoint,
        api_key=api_key,
        allowed_hosts=allowed_hosts,
        timeout_seconds=timeout_seconds,
    )


def _create_execution_url_policy_from_env() -> ExecutionURLPolicy:
    return ExecutionURLPolicy(
        allow_private_networks=_boolean_env(
            EXECUTION_ALLOW_PRIVATE_NETWORKS_ENV,
        )
    )


def _create_action_planner_from_env() -> ActionPlanner | None:
    values = {
        MODEL_API_PROVIDER_ENV: _optional_env(MODEL_API_PROVIDER_ENV),
        MODEL_API_BASE_URL_ENV: _optional_env(MODEL_API_BASE_URL_ENV),
        MODEL_API_KEY_ENV: _optional_env(MODEL_API_KEY_ENV),
        MODEL_API_MODEL_ENV: _optional_env(MODEL_API_MODEL_ENV),
        MODEL_API_ALLOWED_HOSTS_ENV: _optional_env(MODEL_API_ALLOWED_HOSTS_ENV),
        MODEL_API_MAX_TOKENS_ENV: _optional_env(MODEL_API_MAX_TOKENS_ENV),
        MODEL_API_TIMEOUT_ENV: _optional_env(MODEL_API_TIMEOUT_ENV),
    }
    required = (
        MODEL_API_PROVIDER_ENV,
        MODEL_API_BASE_URL_ENV,
        MODEL_API_KEY_ENV,
        MODEL_API_MODEL_ENV,
    )
    if not any(values.values()):
        return None
    missing = [name for name in required if values[name] is None]
    if missing:
        raise ValueError(f"{', '.join(missing)} are required for the action planner")
    allowed_hosts = tuple(
        host.strip()
        for host in (values[MODEL_API_ALLOWED_HOSTS_ENV] or "").split(",")
        if host.strip()
    )
    try:
        max_tokens = int(values[MODEL_API_MAX_TOKENS_ENV] or "4096")
        timeout_seconds = float(values[MODEL_API_TIMEOUT_ENV] or "60")
    except ValueError as exc:
        raise ValueError("model API token and timeout settings are invalid") from exc
    client = OpenAICompatibleChatClient(
        base_url=values[MODEL_API_BASE_URL_ENV] or "",
        api_key=values[MODEL_API_KEY_ENV] or "",
        model=values[MODEL_API_MODEL_ENV] or "",
        timeout_seconds=timeout_seconds,
        max_tokens=max_tokens,
        allowed_hosts=allowed_hosts,
    )
    return OpenAIActionPlanner(
        client=client,
        provider=values[MODEL_API_PROVIDER_ENV] or "",
    )


def _create_browser_executor_from_env(
    url_policy: ExecutionURLPolicy,
    *,
    root: Path = ROOT,
) -> tuple[
    HarnessBrowserExecutor | None,
    UIGraphTargetResolver | None,
]:
    """Build the opt-in Browser Harness runtime for the user's daily Chrome."""

    driver = _optional_env(BROWSER_EXECUTION_DRIVER_ENV)
    if driver is None:
        return None, None
    if driver.casefold() != "browser_harness":
        raise ValueError(
            f"{BROWSER_EXECUTION_DRIVER_ENV} must be browser_harness"
        )
    client = BrowserHarnessClient(
        workspace=Path(root).resolve() / "runtime_data" / "browser_harness_workspace",
        url_policy=url_policy,
    )
    return HarnessBrowserExecutor(client), UIGraphTargetResolver()


@dataclass(frozen=True)
class AppServices:
    memory: SQLiteMemoryStore
    scene_store: SQLiteSceneStore
    perception_runtime: PerceptionRuntime
    page_capture_store: SQLitePageCaptureStore
    page_perception: PagePerceptionService
    page_capture_jobs: PageCaptureJobService
    asset_inventory: JSONAssetInventoryAdapter
    asset_resolver: AssetResolver
    dom_action_binding: DOMActionBindingService
    safe_dom_actions: SafeDOMActionService
    outcome_verifiers: OutcomeVerifierRegistry
    execution_scenarios: ExecutionScenarioService
    ui_graph_planning: UIGraphPlanningService
    tools: MockBusinessTools
    runtime: KT6Runtime


def create_services(
    root: Path = ROOT,
    *,
    canvas_vision_override: CanvasVisionAdapter | None = None,
    action_planner_override: ActionPlanner | None = None,
) -> AppServices:
    root = root.resolve()
    load_project_env(root)
    canvas_vision = canvas_vision_override or _create_canvas_vision_from_env()
    url_policy = _create_execution_url_policy_from_env()
    action_planner = action_planner_override or _create_action_planner_from_env()
    ui_graph_reasoner = _create_ui_graph_reasoner_from_env()
    browser_executor, browser_target_resolver = (
        _create_browser_executor_from_env(url_policy, root=root)
    )
    runtime_dir = root / "runtime_data"
    memory = SQLiteMemoryStore(runtime_dir / "kt6_memory.sqlite3")
    scene_store = SQLiteSceneStore(runtime_dir / "kt6_scene.sqlite3")
    perception_runtime = PerceptionRuntime(HybridPerception(), scene_store)
    page_capture_store = SQLitePageCaptureStore(
        runtime_dir / "kt6_page_captures.sqlite3",
        runtime_dir / "page_captures",
    )
    vision_cache_coordinator = VisionCacheCoordinator(
        SQLiteVisionResultCacheStore(runtime_dir / "kt6_vision_cache.sqlite3"),
        asset_root=page_capture_store.asset_dir,
    )
    page_perception = PagePerceptionService(
        page_capture_store,
        perception_runtime,
        canvas_vision=canvas_vision,
        text_recognizer=TopologyTextRecognizer(),
        vision_cache_coordinator=vision_cache_coordinator,
    )
    page_capture_jobs = PageCaptureJobService(page_perception)
    asset_inventory = JSONAssetInventoryAdapter(root / "data" / "mock_assets.json")
    asset_resolver = AssetResolver(asset_inventory)
    dom_action_binding = DOMActionBindingService(asset_resolver)
    outcome_verifiers = OutcomeVerifierRegistry(
        [
            AssetDetailOutcomeVerifier(),
            PageReadyVerifier(),
            CanvasSelectionVerifier(),
        ],
        ui_graph_verifier=UIGraphOutcomeVerifier(),
    )
    safe_dom_actions = SafeDOMActionService(
        dom_action_binding,
        page_perception,
        executor=browser_executor,
        target_resolver=browser_target_resolver,
        outcome_verifiers=outcome_verifiers,
    )
    scenario_runner = (
        ScenarioRunner(
            page_perception=page_perception,
            browser_executor=browser_executor,
            grounders=TargetGrounderRegistry(
                vision_producer_id=(
                    str(getattr(canvas_vision, "adapter_id", "")) or None
                )
            ),
            verifiers=outcome_verifiers,
            url_policy=url_policy,
        )
        if browser_executor is not None
        else None
    )
    execution_scenarios = ExecutionScenarioService(
        root=root,
        runner=scenario_runner,
        planner=action_planner,
        url_policy=url_policy,
    )
    ui_graph_planning = UIGraphPlanningService(page_perception, ui_graph_reasoner)
    tools = MockBusinessTools(
        root / "data",
        perception_runtime=perception_runtime,
        page_perception=page_perception,
    )
    runtime = KT6Runtime(tools, PlaybookLoader(root / "playbooks"), memory=memory)
    return AppServices(
        memory=memory,
        scene_store=scene_store,
        perception_runtime=perception_runtime,
        page_capture_store=page_capture_store,
        page_perception=page_perception,
        page_capture_jobs=page_capture_jobs,
        asset_inventory=asset_inventory,
        asset_resolver=asset_resolver,
        dom_action_binding=dom_action_binding,
        safe_dom_actions=safe_dom_actions,
        outcome_verifiers=outcome_verifiers,
        execution_scenarios=execution_scenarios,
        ui_graph_planning=ui_graph_planning,
        tools=tools,
        runtime=runtime,
    )


class KT6Handler(SimpleHTTPRequestHandler):
    services: AppServices | None = None
    demo_dir: Path = DEMO_DIR

    def __init__(self, *args, **kwargs):
        if self.services is None:
            raise RuntimeError("KT6Handler must be bound to AppServices")
        super().__init__(*args, directory=str(self.demo_dir), **kwargs)

    @property
    def app(self) -> AppServices:
        if self.services is None:
            raise RuntimeError("KT6Handler must be bound to AppServices")
        return self.services

    def _json(self, status: int, payload: dict | list) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self) -> dict:
        content_type = self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
        if content_type != "application/json":
            raise ValueError("Content-Type must be application/json")
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as exc:
            raise ValueError("Content-Length must be an integer") from exc
        if length < 0:
            raise ValueError("Content-Length must not be negative")
        if length > MAX_JSON_REQUEST_BYTES:
            raise RequestBodyTooLarge("JSON request body exceeds 32 MB")
        if not length:
            return {}
        raw = self.rfile.read(length)
        if len(raw) != length:
            raise ValueError("request body is incomplete")
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("request body must be valid UTF-8 JSON") from exc
        if not isinstance(payload, dict):
            raise ValueError("request body must be a JSON object")
        return payload

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        services = self.app
        runtime = services.runtime
        if path == "/api/health":
            self._json(
                200,
                {
                    "status": "ok",
                    "vision": _canvas_vision_health(
                        services.page_perception.canvas_vision
                    ),
                    "page_api": {
                        "mode": "explicit_read_only_adapter",
                        "arbitrary_network_interception": False,
                    },
                    "ui_graph_reasoning": services.ui_graph_planning.health(),
                    "browser_execution": {
                        "configured": not services.safe_dom_actions.dry_run_only,
                        "driver": (
                            services.safe_dom_actions.executor.executor_id
                            if services.safe_dom_actions.executor is not None
                            else None
                        ),
                        "supported_operations": ["type", "click"],
                        "raw_cdp_exposed": False,
                        "javascript_exposed": False,
                        "live_revalidation": [
                            "frame",
                            "dom_identity",
                            "hit_test",
                        ],
                        "outcome_verification": "fresh_kt6_capture_deterministic",
                    },
                    "execution_scenarios": services.execution_scenarios.health(),
                    "outcome_verifiers": services.outcome_verifiers.health(),
                },
            )
            return
        if path == "/api/execution/health":
            self._json(200, services.execution_scenarios.runtime_health())
            return
        if path == "/api/playbooks":
            self._json(200, runtime.playbooks.list_playbooks())
            return
        if path.startswith("/api/playbooks/"):
            scenario_id = path.split("/")[3]
            playbook = runtime.playbooks.load(scenario_id)
            self._json(
                200,
                {
                    "scenario_id": playbook.scenario_id,
                    "name": playbook.name,
                    "trigger_intents": playbook.trigger_intents,
                    "required_slots": playbook.required_slots,
                    "steps": playbook.steps,
                    "actions": playbook.actions,
                },
            )
            return
        if path == "/api/tools":
            self._json(200, runtime.tools.list_tools())
            return
        if path == "/api/memory":
            limit = int(parse_qs(parsed.query).get("limit", ["50"])[0])
            self._json(200, {"memories": services.memory.list_memories(limit=limit)})
            return
        if path == "/api/tasks":
            limit = int(parse_qs(parsed.query).get("limit", ["20"])[0])
            self._json(200, {"tasks": services.memory.list_tasks(limit=limit)})
            return
        if path == "/api/perception/cache":
            limit = int(parse_qs(parsed.query).get("limit", ["20"])[0])
            self._json(200, {"scenes": services.tools.list_perception_cache(limit=limit)})
            return
        if path == "/api/perception/captures":
            limit = int(parse_qs(parsed.query).get("limit", ["20"])[0])
            self._json(200, {"captures": services.page_perception.list_captures(limit=limit)})
            return
        graph_prefix = "/api/ui-graphs/"
        if path.startswith(graph_prefix):
            capture_id = path[len(graph_prefix) :]
            if not capture_id or "/" in capture_id:
                self._json(404, {"error": "not found"})
                return
            try:
                graph = services.ui_graph_planning.get_graph(capture_id)
            except UIGraphNotFoundError as exc:
                self._json(404, {"error": str(exc)})
                return
            self._json(200, graph)
            return
        plan_prefix = "/api/dom-actions/plans/"
        if path.startswith(plan_prefix):
            plan_id = path[len(plan_prefix) :]
            if not plan_id or "/" in plan_id:
                self._json(404, {"error": "not found"})
                return
            plan = services.safe_dom_actions.get_plan(plan_id)
            if plan.get("reason") == "plan_not_found":
                self._json(404, plan)
                return
            self._json(200, plan)
            return
        if path == "/api/dom-actions/audit":
            self._json(200, {"events": services.safe_dom_actions.audit_events()})
            return
        execution_run_prefix = "/api/execution/runs/"
        if path.startswith(execution_run_prefix):
            run_id = path[len(execution_run_prefix) :]
            if not run_id or "/" in run_id:
                self._json(404, {"error": "not found"})
                return
            try:
                run = services.execution_scenarios.get_run(run_id)
            except ExecutionScenarioServiceError as exc:
                self._json(404, {"error": exc.error_code})
                return
            self._json(200, run)
            return
        capture_job_prefix = "/api/perception/capture-jobs/"
        if path.startswith(capture_job_prefix):
            job_id = path[len(capture_job_prefix) :]
            if not job_id or "/" in job_id:
                self._json(404, {"error": "not found"})
                return
            job = services.page_capture_jobs.get(job_id)
            if job is None:
                self._json(404, {"error": "page capture job not found"})
                return
            self._json(200, job)
            return
        if path.startswith("/api/perception/captures/"):
            capture_id = path.split("/")[4]
            capture = services.page_perception.get_capture(capture_id)
            if not capture:
                self._json(404, {"error": "page capture not found"})
                return
            self._json(200, capture)
            return
        if path == "/api/topology":
            self._json(200, services.tools.query_topology(""))
            return
        if path.startswith("/api/tasks/") and path.endswith("/events"):
            task_id = path.split("/")[3]
            since = int(parse_qs(parsed.query).get("since", ["0"])[0])
            task = runtime.get_task_snapshot(task_id)
            if not task:
                self._json(404, {"error": "task not found"})
                return
            self._json(
                200,
                {
                    "task_id": task_id,
                    "state": task["state"],
                    "events": runtime.get_events(task_id, since),
                },
            )
            return
        if path.startswith("/api/tasks/"):
            task_id = path.split("/")[3]
            task = runtime.get_task_snapshot(task_id)
            if not task:
                record = services.memory.get_task_record(task_id)
                if record:
                    record["events"] = services.memory.get_task_events(task_id)
                    self._json(200, record)
                    return
                self._json(404, {"error": "task not found"})
                return
            self._json(200, task)
            return
        super().do_GET()

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        services = self.app
        runtime = services.runtime
        known_path = (
            path in {
                "/api/perception/capture-jobs",
                "/api/perception/captures",
                "/api/tasks",
                "/api/dom-actions/prepare",
                "/api/dom-actions/preflight",
                "/api/dom-actions/execute",
                "/api/dom-actions/verify",
                "/api/execution/plans",
                "/api/execution/runs",
                "/api/ui-operations/plan",
            }
            or (path.startswith("/api/tasks/") and path.endswith("/actions"))
        )
        if not known_path:
            self._json(404, {"error": "not found"})
            return
        try:
            payload = self._body()
        except RequestBodyTooLarge as exc:
            self._json(413, {"error": str(exc)})
            return
        except ValueError as exc:
            self._json(400, {"error": str(exc)})
            return
        if path == "/api/execution/plans":
            try:
                generated = services.execution_scenarios.generate_plan(
                    start_url=str(payload.get("start_url", "")),
                    user_request=str(payload.get("user_request", "")),
                    browser_target_id=str(payload.get("browser_target_id", "")),
                )
            except ValueError as exc:
                error_code = getattr(exc, "error_code", "plan_invalid")
                runtime_unavailable = str(error_code).startswith(
                    "browser_harness_"
                )
                status = 503 if runtime_unavailable else 422
                if error_code == "execution_runner_busy":
                    status = 409
                self._json(
                    status,
                    {
                        "error": error_code,
                        "error_category": classify_error(error_code),
                    },
                )
                return
            self._json(200, generated)
            return
        if path == "/api/execution/runs":
            plan = payload.get("plan")
            if not isinstance(plan, dict):
                self._json(400, {"error": "plan must be an object"})
                return
            try:
                run = services.execution_scenarios.start_run(
                    plan=plan,
                    confirmed=payload.get("confirmed") is True,
                    browser_target_id=str(payload.get("browser_target_id", "")),
                )
            except ValueError as exc:
                error_code = getattr(exc, "error_code", "execution_run_invalid")
                self._json(
                    409,
                    {
                        "error": error_code,
                        "error_category": classify_error(error_code),
                    },
                )
                return
            self._json(202, run)
            return
        if path == "/api/perception/capture-jobs":
            try:
                job = services.page_capture_jobs.submit(
                    client_request_id=payload.get("client_request_id"),
                    payload=payload.get("payload"),
                )
            except PageCaptureJobCapacityError as exc:
                self._json(503, {"error": str(exc)})
                return
            except (TypeError, ValueError) as exc:
                self._json(400, {"error": str(exc)})
                return
            self._json(202, job)
            return
        if path == "/api/perception/captures":
            try:
                capture = services.page_perception.ingest(payload)
            except (TypeError, ValueError) as exc:
                self._json(400, {"error": str(exc)})
                return
            self._json(201, capture)
            return
        if path == "/api/ui-operations/plan":
            raw_capture_id = payload.get("page_capture_id")
            raw_instruction = payload.get("instruction")
            if not isinstance(raw_capture_id, str) or not isinstance(
                raw_instruction, str
            ):
                self._json(
                    400,
                    {"error": "page_capture_id and instruction must be strings"},
                )
                return
            capture_id = raw_capture_id.strip()
            instruction = raw_instruction.strip()
            if not capture_id or not instruction:
                self._json(
                    400,
                    {"error": "page_capture_id and instruction are required"},
                )
                return
            if len(capture_id) > 200 or len(instruction) > 8_000:
                self._json(400, {"error": "page_capture_id or instruction is too long"})
                return
            try:
                proposal = services.ui_graph_planning.plan(
                    capture_id=capture_id,
                    instruction=instruction,
                )
            except UIGraphNotFoundError as exc:
                self._json(404, {"error": str(exc)})
                return
            except UIGraphReasonerNotConfiguredError as exc:
                self._json(503, {"error": str(exc)})
                return
            except UIGraphProjectionError as exc:
                self._json(422, {"error": str(exc)})
                return
            except UIGraphReasoningError as exc:
                self._json(502, {"error": str(exc)})
                return
            except ValueError as exc:
                self._json(400, {"error": str(exc)})
                return
            self._json(
                200 if proposal["status"] == "planned" else 422,
                proposal,
            )
            return
        if path == "/api/dom-actions/prepare":
            asset_reference = payload.get("asset_reference")
            action = str(payload.get("action", "")).strip()
            capture_id = str(payload.get("page_capture_id", "")).strip()
            if not asset_reference or not action or not capture_id:
                self._json(
                    400,
                    {"error": "asset_reference, action and page_capture_id are required"},
                )
                return
            result = services.safe_dom_actions.prepare(
                asset_reference=asset_reference,
                action=action,
                page_capture_id=capture_id,
                scope=payload.get("scope") if isinstance(payload.get("scope"), dict) else {},
                task_id=str(payload.get("task_id", "")),
                principal_id=str(payload.get("principal_id", "")),
            )
            self._json(201 if result["status"] == "prepared" else 409, result)
            return
        if path == "/api/dom-actions/preflight":
            permissions = payload.get("permissions", [])
            if not isinstance(permissions, list):
                self._json(400, {"error": "permissions must be an array"})
                return
            result = services.safe_dom_actions.preflight(
                plan_id=str(payload.get("plan_id", "")),
                current_capture_id=str(payload.get("page_capture_id", "")),
                confirmed=payload.get("confirmed") is True,
                confirmed_asset_id=str(payload.get("confirmed_asset_id", "")),
                confirmed_action=str(payload.get("confirmed_action", "")),
                permissions=permissions,
            )
            self._json(201 if result["status"] == "ready" else 409, result)
            return
        if path == "/api/dom-actions/execute":
            result = services.safe_dom_actions.execute(
                execution_token=str(payload.get("execution_token", "")),
                dry_run=payload.get("dry_run") is not False,
                graph_id=str(payload.get("graph_id", "")),
                target_node_id=str(payload.get("target_node_id", "")),
            )
            if result["status"] == "dry_run_ok":
                status = 200
            elif result["status"] == "executed_pending_verification":
                status = 202
            else:
                status = 409
            self._json(status, result)
            return
        if path == "/api/dom-actions/verify":
            result = services.safe_dom_actions.verify_outcome(
                plan_id=str(payload.get("plan_id", "")),
                current_capture_id=str(payload.get("page_capture_id", "")),
            )
            self._json(200 if result["status"] == "verified" else 409, result)
            return
        if path == "/api/tasks":
            query = payload.get("query", "").strip()
            if not query:
                self._json(400, {"error": "query is required"})
                return
            page_capture_id = payload.get("page_capture_id")
            if page_capture_id and not services.page_perception.get_capture(page_capture_id):
                self._json(400, {"error": "page_capture_id is invalid"})
                return
            task = runtime.create_task(query, page_capture_id=page_capture_id)
            self._json(201, {"task_id": task.task_id, "state": task.state})
            return
        if path.startswith("/api/tasks/") and path.endswith("/actions"):
            task_id = path.split("/")[3]
            page_capture_id = payload.get("page_capture_id")
            if page_capture_id and not services.page_perception.get_capture(page_capture_id):
                self._json(400, {"error": "page_capture_id is invalid"})
                return
            ok = runtime.execute_action(task_id, payload.get("action", ""), payload)
            if not ok:
                self._json(409, {"error": "action rejected"})
                return
            self._json(202, {"accepted": True})
            return


def create_handler(
    services: AppServices,
    demo_dir: Path = DEMO_DIR,
) -> Type[KT6Handler]:
    class BoundKT6Handler(KT6Handler):
        pass

    BoundKT6Handler.services = services
    BoundKT6Handler.demo_dir = demo_dir
    return BoundKT6Handler


def create_server(
    host: str = "127.0.0.1",
    port: int = 8787,
    root: Path = ROOT,
    *,
    canvas_vision_override: CanvasVisionAdapter | None = None,
    action_planner_override: ActionPlanner | None = None,
) -> tuple[ThreadingHTTPServer, AppServices]:
    services = create_services(
        root,
        canvas_vision_override=canvas_vision_override,
        action_planner_override=action_planner_override,
    )
    server = ThreadingHTTPServer((host, port), create_handler(services, root / "demo"))
    return server, services


def main() -> None:
    server, _services = create_server()
    host, port = server.server_address[:2]
    print(f"KT6 business demo running at http://{host}:{port}/")
    server.serve_forever()


if __name__ == "__main__":
    main()
