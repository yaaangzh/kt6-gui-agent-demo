from __future__ import annotations

import argparse
import json
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .app import create_server
from .execution.browser_executor import HarnessBrowserExecutor
from .execution.browser_harness_client import BrowserHarnessError
from .execution.fixture_planner import FixturePlanner, FixturePlanningError


ROOT = Path(__file__).resolve().parents[1]
FIXTURE_PAGE_URL = "http://127.0.0.1:8787/execution-test.html"
FIXTURE_INTENT = ROOT / "fixtures" / "execution" / "open_ap_details_intent.json"


def _write_json(path: Path, value: Any) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def run_dom_e2e(*, out_dir: Path | None = None) -> dict[str, Any]:
    server, services = create_server(host="127.0.0.1", port=8787, root=ROOT)
    executor = services.safe_dom_actions.executor
    if not isinstance(executor, HarnessBrowserExecutor):
        server.server_close()
        raise RuntimeError("browser_harness_executor_not_configured")
    intent = json.loads(FIXTURE_INTENT.read_text(encoding="utf-8"))
    if not isinstance(intent, dict):
        server.server_close()
        raise RuntimeError("execution_fixture_intent_invalid")

    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    planner = FixturePlanner()
    client = executor.client
    try:
        client.reset_execution_fixture(FIXTURE_PAGE_URL)

        initial = services.page_perception.ingest(
            client.capture_page_payload(expected_page_url=FIXTURE_PAGE_URL)
        )
        initial_graph = services.page_perception.get_ui_graph(initial["capture_id"])
        if initial_graph is None:
            raise RuntimeError("initial_ui_graph_missing")
        initial_decision = planner.plan(initial_graph, intent)
        prepared = services.safe_dom_actions.prepare(
            asset_reference=intent["asset_id"],
            action=initial_decision["action"],
            page_capture_id=initial["capture_id"],
            scope={"site_id": "site_1"},
            task_id="execution-e2e-open-ap-details",
            principal_id="fixture-operator",
        )
        if prepared.get("status") != "prepared":
            raise RuntimeError(str(prepared.get("reason", "prepare_failed")))

        fresh = services.page_perception.ingest(
            client.capture_page_payload(expected_page_url=FIXTURE_PAGE_URL)
        )
        fresh_graph = services.page_perception.get_ui_graph(fresh["capture_id"])
        if fresh_graph is None:
            raise RuntimeError("fresh_ui_graph_missing")
        fresh_decision = planner.plan(fresh_graph, intent)
        ready = services.safe_dom_actions.preflight(
            plan_id=prepared["plan_id"],
            current_capture_id=fresh["capture_id"],
            confirmed=True,
            confirmed_asset_id=intent["asset_id"],
            confirmed_action=fresh_decision["action"],
            permissions=["assets.read"],
        )
        if ready.get("status") != "ready":
            raise RuntimeError(str(ready.get("reason", "preflight_failed")))

        dispatched = services.safe_dom_actions.execute(
            execution_token=ready["execution_token"],
            dry_run=False,
            graph_id=fresh_decision["graph_id"],
            target_node_id=fresh_decision["target_node_id"],
        )
        if dispatched.get("status") != "executed_pending_verification":
            raise RuntimeError(str(dispatched.get("reason", "execution_failed")))

        after = services.page_perception.ingest(
            client.capture_page_payload(expected_page_url=FIXTURE_PAGE_URL)
        )
        after_graph = services.page_perception.get_ui_graph(after["capture_id"])
        if after_graph is None:
            raise RuntimeError("after_ui_graph_missing")
        verified = services.safe_dom_actions.verify_outcome(
            plan_id=prepared["plan_id"],
            current_capture_id=after["capture_id"],
        )

        target_dir = out_dir or (
            ROOT
            / "runtime_data"
            / "execution_e2e"
            / datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        )
        target_dir.mkdir(parents=True, exist_ok=False)
        _write_json(target_dir / "initial-ui-graph.json", initial_graph)
        _write_json(target_dir / "fresh-ui-graph.json", fresh_graph)
        _write_json(target_dir / "after-ui-graph.json", after_graph)
        result = {
            "schema_version": "kt6.execution-e2e.v1",
            "status": "success" if verified.get("status") == "verified" else "failed",
            "goal": intent["goal"],
            "asset_id": intent["asset_id"],
            "page_url": FIXTURE_PAGE_URL,
            "captures": {
                "initial": initial["capture_id"],
                "fresh": fresh["capture_id"],
                "after": after["capture_id"],
            },
            "graph_id": fresh_decision["graph_id"],
            "target_node_id": fresh_decision["target_node_id"],
            "execution_status": dispatched["status"],
            "verification_status": verified.get("status"),
            "outcome_verified": verified.get("outcome_verified") is True,
            "output_dir": str(target_dir),
        }
        _write_json(target_dir / "result.json", result)
        return result
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run the real-page KT6 Browser Harness DOM execution E2E."
    )
    parser.add_argument("--out-dir", type=Path)
    args = parser.parse_args(argv)
    try:
        result = run_dom_e2e(out_dir=args.out_dir)
    except (
        BrowserHarnessError,
        FixturePlanningError,
        RuntimeError,
        OSError,
        ValueError,
    ) as exc:
        print(
            json.dumps(
                {"status": "failed", "error_code": str(exc)},
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
        return 2
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["status"] == "success" else 3


if __name__ == "__main__":
    raise SystemExit(main())
