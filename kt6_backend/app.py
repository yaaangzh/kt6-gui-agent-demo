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
from .codeagent_canvas_vision import CodeAgentCanvasVisionAdapter
from .dom_action_binding import DOMActionBindingService
from .http_canvas_vision import HTTPTopologyVisionAdapter
from .hybrid_canvas_vision import HybridCanvasVisionAdapter
from .local_cv_canvas_vision import LocalCVTopologyVisionAdapter
from .memory import SQLiteMemoryStore
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
from .vision_recognition import CanvasVisionAdapter
from .vision_cache_coordinator import VisionCacheCoordinator
from .vision_result_cache import SQLiteVisionResultCacheStore


ROOT = Path(__file__).resolve().parent.parent
DEMO_DIR = ROOT / "demo"
VISION_DRIVER_ENV = "KT6_VISION_DRIVER"
VISION_ENDPOINT_ENV = "KT6_VISION_ENDPOINT"
VISION_API_KEY_ENV = "KT6_VISION_API_KEY"
VISION_TIMEOUT_ENV = "KT6_VISION_TIMEOUT_SECONDS"
CODEAGENT_EXECUTABLE_ENV = "KT6_CODEAGENT_EXECUTABLE"
CODEAGENT_AGENT_ENV = "KT6_CODEAGENT_AGENT"
HYBRID_MODEL_DRIVER_ENV = "KT6_HYBRID_MODEL_DRIVER"
DEFAULT_VISION_TIMEOUT_SECONDS = 30.0
DEFAULT_CODEAGENT_TIMEOUT_SECONDS = 120.0
DEFAULT_CODEAGENT_EXECUTABLE = "codeagent"
MAX_VISION_TIMEOUT_SECONDS = 300.0
MAX_JSON_REQUEST_BYTES = 32 * 1024 * 1024


class RequestBodyTooLarge(ValueError):
    pass


def _optional_env(name: str) -> str | None:
    value = os.environ.get(name)
    if value is None:
        return None
    value = value.strip()
    return value or None


def _create_canvas_vision_from_env(root: Path = ROOT) -> CanvasVisionAdapter | None:
    """Build the production vision adapter without exposing secret config."""

    driver = _optional_env(VISION_DRIVER_ENV)
    endpoint = _optional_env(VISION_ENDPOINT_ENV)
    api_key = _optional_env(VISION_API_KEY_ENV)
    timeout_text = _optional_env(VISION_TIMEOUT_ENV)
    codeagent_executable = _optional_env(CODEAGENT_EXECUTABLE_ENV)
    codeagent_agent = _optional_env(CODEAGENT_AGENT_ENV)
    hybrid_model_driver = _optional_env(HYBRID_MODEL_DRIVER_ENV)

    if driver is None and (codeagent_executable is not None or codeagent_agent is not None):
        raise ValueError(
            f"{VISION_DRIVER_ENV}=codeagent_cli is required when "
            f"{CODEAGENT_EXECUTABLE_ENV} or {CODEAGENT_AGENT_ENV} is configured"
        )
    if driver is None and hybrid_model_driver is not None:
        raise ValueError(
            f"{VISION_DRIVER_ENV}=hybrid is required when "
            f"{HYBRID_MODEL_DRIVER_ENV} is configured"
        )

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
    if selected_driver not in {"http", "codeagent_cli", "local_cv_ocr", "hybrid"}:
        raise ValueError(
            f"{VISION_DRIVER_ENV} must be http, codeagent_cli, local_cv_ocr or hybrid"
        )

    if selected_driver == "local_cv_ocr":
        conflicting = [
            name
            for name, value in (
                (VISION_ENDPOINT_ENV, endpoint),
                (VISION_API_KEY_ENV, api_key),
                (VISION_TIMEOUT_ENV, timeout_text),
                (CODEAGENT_EXECUTABLE_ENV, codeagent_executable),
                (CODEAGENT_AGENT_ENV, codeagent_agent),
                (HYBRID_MODEL_DRIVER_ENV, hybrid_model_driver),
            )
            if value is not None
        ]
        if conflicting:
            raise ValueError(
                f"{', '.join(conflicting)} must not be configured for local_cv_ocr"
            )
        return LocalCVTopologyVisionAdapter()

    if selected_driver == "hybrid":
        if hybrid_model_driver is None:
            raise ValueError(
                f"{HYBRID_MODEL_DRIVER_ENV} is required for the hybrid vision driver"
            )
        effective_driver = hybrid_model_driver.strip().lower()
        if effective_driver not in {"http", "codeagent_cli"}:
            raise ValueError(
                f"{HYBRID_MODEL_DRIVER_ENV} must be http or codeagent_cli"
            )
    else:
        if hybrid_model_driver is not None:
            raise ValueError(
                f"{HYBRID_MODEL_DRIVER_ENV} requires {VISION_DRIVER_ENV}=hybrid"
            )
        effective_driver = selected_driver

    default_timeout = (
        DEFAULT_CODEAGENT_TIMEOUT_SECONDS
        if effective_driver == "codeagent_cli"
        else DEFAULT_VISION_TIMEOUT_SECONDS
    )
    timeout_seconds = default_timeout
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

    if effective_driver == "codeagent_cli":
        conflicting = [
            name
            for name, value in (
                (VISION_ENDPOINT_ENV, endpoint),
                (VISION_API_KEY_ENV, api_key),
            )
            if value is not None
        ]
        if conflicting:
            raise ValueError(
                f"{', '.join(conflicting)} must not be configured for codeagent_cli"
            )
        model_adapter = CodeAgentCanvasVisionAdapter(
            workdir=Path(root).resolve(),
            executable=codeagent_executable or DEFAULT_CODEAGENT_EXECUTABLE,
            agent=codeagent_agent,
            timeout_seconds=timeout_seconds,
        )
        if selected_driver == "hybrid":
            return HybridCanvasVisionAdapter(
                local_adapter=LocalCVTopologyVisionAdapter(),
                model_adapter=model_adapter,
            )
        return model_adapter

    if codeagent_executable is not None or codeagent_agent is not None:
        raise ValueError(
            f"{CODEAGENT_EXECUTABLE_ENV} and {CODEAGENT_AGENT_ENV} require "
            f"{VISION_DRIVER_ENV}=codeagent_cli"
        )
    if endpoint is None:
        raise ValueError(f"{VISION_ENDPOINT_ENV} is required for the http vision driver")
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
    tools: MockBusinessTools
    runtime: KT6Runtime


def create_services(root: Path = ROOT) -> AppServices:
    root = root.resolve()
    canvas_vision = _create_canvas_vision_from_env(root)
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
    safe_dom_actions = SafeDOMActionService(dom_action_binding, page_perception)
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
                },
            )
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
            )
            self._json(200 if result["status"] == "dry_run_ok" else 409, result)
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
) -> tuple[ThreadingHTTPServer, AppServices]:
    services = create_services(root)
    server = ThreadingHTTPServer((host, port), create_handler(services, root / "demo"))
    return server, services


def main() -> None:
    server, _services = create_server()
    host, port = server.server_address[:2]
    print(f"KT6 business demo running at http://{host}:{port}/")
    server.serve_forever()


if __name__ == "__main__":
    main()
