from __future__ import annotations

import json
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .memory import SQLiteMemoryStore
from .perception import HybridPerception
from .perception_runtime import PerceptionRuntime
from .playbook_loader import PlaybookLoader
from .runtime import KT6Runtime
from .scene_store import SQLiteSceneStore
from .tools import MockBusinessTools


ROOT = Path(__file__).resolve().parent.parent
DEMO_DIR = ROOT / "demo"
DATA_DIR = ROOT / "data"
PLAYBOOK_DIR = ROOT / "playbooks"
MEMORY = SQLiteMemoryStore(ROOT / "runtime_data" / "kt6_memory.sqlite3")
SCENE_STORE = SQLiteSceneStore(ROOT / "runtime_data" / "kt6_scene.sqlite3")
PERCEPTION_RUNTIME = PerceptionRuntime(HybridPerception(), SCENE_STORE)
TOOLS = MockBusinessTools(DATA_DIR, perception_runtime=PERCEPTION_RUNTIME)
RUNTIME = KT6Runtime(TOOLS, PlaybookLoader(PLAYBOOK_DIR), memory=MEMORY)


class KT6Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(DEMO_DIR), **kwargs)

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
        if path == "/api/health":
            self._json(200, {"status": "ok"})
            return
        if path == "/api/playbooks":
            self._json(200, RUNTIME.playbooks.list_playbooks())
            return
        if path.startswith("/api/playbooks/"):
            scenario_id = path.split("/")[3]
            playbook = RUNTIME.playbooks.load(scenario_id)
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
            self._json(200, RUNTIME.tools.list_tools())
            return
        if path == "/api/memory":
            limit = int(parse_qs(parsed.query).get("limit", ["50"])[0])
            self._json(200, {"memories": MEMORY.list_memories(limit=limit)})
            return
        if path == "/api/tasks":
            limit = int(parse_qs(parsed.query).get("limit", ["20"])[0])
            self._json(200, {"tasks": MEMORY.list_tasks(limit=limit)})
            return
        if path == "/api/perception/cache":
            limit = int(parse_qs(parsed.query).get("limit", ["20"])[0])
            self._json(200, {"scenes": TOOLS.list_perception_cache(limit=limit)})
            return
        if path == "/api/topology":
            self._json(200, TOOLS.query_topology(""))
            return
        if path.startswith("/api/tasks/") and path.endswith("/events"):
            task_id = path.split("/")[3]
            since = int(parse_qs(parsed.query).get("since", ["0"])[0])
            task = RUNTIME.get_task(task_id)
            if not task:
                self._json(404, {"error": "task not found"})
                return
            self._json(200, {"task_id": task_id, "state": task.state, "events": RUNTIME.get_events(task_id, since)})
            return
        if path.startswith("/api/tasks/"):
            task_id = path.split("/")[3]
            task = RUNTIME.get_task(task_id)
            if not task:
                record = MEMORY.get_task_record(task_id)
                if record:
                    record["events"] = MEMORY.get_task_events(task_id)
                    self._json(200, record)
                    return
                self._json(404, {"error": "task not found"})
                return
            self._json(200, {"task_id": task_id, "state": task.state, "context": task.context, "locks": sorted(task.locks)})
            return
        super().do_GET()

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        if path == "/api/tasks":
            payload = self._body()
            query = payload.get("query", "").strip()
            if not query:
                self._json(400, {"error": "query is required"})
                return
            task = RUNTIME.create_task(query)
            self._json(201, {"task_id": task.task_id, "state": task.state})
            return
        if path.startswith("/api/tasks/") and path.endswith("/actions"):
            task_id = path.split("/")[3]
            payload = self._body()
            ok = RUNTIME.execute_action(task_id, payload.get("action", ""), payload)
            if not ok:
                self._json(409, {"error": "action rejected"})
                return
            self._json(202, {"accepted": True})
            return
        self._json(404, {"error": "not found"})


def main() -> None:
    server = ThreadingHTTPServer(("127.0.0.1", 8787), KT6Handler)
    print("KT6 business demo running at http://127.0.0.1:8787/")
    server.serve_forever()


if __name__ == "__main__":
    main()
