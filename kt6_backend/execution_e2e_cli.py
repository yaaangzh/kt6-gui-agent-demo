from __future__ import annotations

import argparse
import json
import sys
import threading
from pathlib import Path
from typing import Any

from .app import create_server
from .execution.fixture_canvas_vision import ExecutionFixtureCanvasVisionAdapter


ROOT = Path(__file__).resolve().parents[1]
FIXTURE_PAGE_URL = "http://127.0.0.1:8787/execution-test.html"
FIXTURE_REQUEST = "打开 AP_001 的详情，然后进入拓扑页面，再在拓扑中选中 AP_001"


def run_scenario_e2e(*, out_dir: Path | None = None) -> dict[str, Any]:
    server, services = create_server(
        host="127.0.0.1",
        port=8787,
        root=ROOT,
        canvas_vision_override=ExecutionFixtureCanvasVisionAdapter(),
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        generated = services.execution_scenarios.generate_plan(
            start_url=FIXTURE_PAGE_URL,
            user_request=FIXTURE_REQUEST,
        )
        if out_dir is None:
            status = services.execution_scenarios.run_sync(generated["plan"])
        else:
            runner = services.execution_scenarios.runner
            if runner is None:
                raise RuntimeError("execution_runner_not_configured")
            result = runner.run(
                generated["plan"],
                run_id="run_cli",
                out_dir=out_dir,
            )
            status = {"status": result["status"], "result": result}
        result = dict(status["result"])
        result["readable_steps"] = generated["readable_steps"]
        return result
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run the natural-language KT6 DOM + Canvas execution E2E."
    )
    parser.add_argument("--out-dir", type=Path)
    args = parser.parse_args(argv)
    try:
        result = run_scenario_e2e(out_dir=args.out_dir)
    except (RuntimeError, OSError, ValueError) as exc:
        print(
            json.dumps(
                {
                    "status": "failed",
                    "error_code": getattr(exc, "error_code", str(exc)),
                },
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
        return 2
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["status"] == "success" else 3


if __name__ == "__main__":
    raise SystemExit(main())
