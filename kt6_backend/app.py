from __future__ import annotations

import json
from dataclasses import dataclass
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Type
from urllib.parse import parse_qs, urlparse

from .memory import SQLiteMemoryStore
from .page_perception import PagePerceptionService, SQLitePageCaptureStore
from .perception import HybridPerception
from .perception_runtime import PerceptionRuntime
from .playbook_loader import PlaybookLoader
from .runtime import KT6Runtime
from .scene_store import SQLiteSceneStore
from .tools import MockBusinessTools


ROOT = Path(__file__).resolve().parent.parent
DEMO_DIR = ROOT / "demo"


@dataclass(frozen=True)
class AppServices:
    memory: SQLiteMemoryStore
    scene_store: SQLiteSceneStore
    perception_runtime: PerceptionRuntime
    page_capture_store: SQLitePageCaptureStore
    page_perception: PagePerceptionService
    tools: MockBusinessTools
    runtime: KT6Runtime


def create_services(root: Path = ROOT) -> AppServices:
    root = root.resolve()
    runtime_dir = root / "runtime_data"
    memory = SQLiteMemoryStore(runtime_dir / "kt6_memory.sqlite3")
    scene_store = SQLiteSceneStore(runtime_dir / "kt6_scene.sqlite3")
    perception_runtime = PerceptionRuntime(HybridPerception(), scene_store)
    page_capture_store = SQLitePageCaptureStore(
        runtime_dir / "kt6_page_captures.sqlite3",
        runtime_dir / "page_captures",
    )
    page_perception = PagePerceptionService(page_capture_store, perception_runtime)
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
        length = int(self.headers.get("Content-Length", "0"))
        if not length:
            return {}
        return json.loads(self.rfile.read(length).decode("utf-8"))

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        services = self.app
        runtime = services.runtime
        if path == "/api/health":
            self._json(200, {"status": "ok"})
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
        if path == "/api/perception/captures":
            try:
                capture = services.page_perception.ingest(self._body())
            except (TypeError, ValueError) as exc:
                self._json(400, {"error": str(exc)})
                return
            self._json(201, capture)
            return
        if path == "/api/tasks":
            payload = self._body()
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
            payload = self._body()
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
        self._json(404, {"error": "not found"})


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
