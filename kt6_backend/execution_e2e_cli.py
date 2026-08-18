from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from .app import create_services


ROOT = Path(__file__).resolve().parents[1]


def run_scenario_e2e(
    *,
    start_url: str,
    user_request: str,
) -> dict[str, Any]:
    services = create_services(ROOT)
    generated = services.execution_scenarios.generate_plan(
        start_url=start_url,
        user_request=user_request,
    )
    status = services.execution_scenarios.run_sync(generated["plan"])
    result = dict(status["result"])
    result["readable_steps"] = generated["readable_steps"]
    result["planner"] = generated["planner"]
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run one generic KT6 model-planned browser scenario."
    )
    parser.add_argument("--url", required=True, help="approved target page URL")
    parser.add_argument("--task", required=True, help="natural-language task")
    args = parser.parse_args(argv)
    try:
        result = run_scenario_e2e(
            start_url=args.url,
            user_request=args.task,
        )
    except (RuntimeError, OSError, ValueError) as exc:
        print(
            json.dumps(
                {
                    "status": "failed",
                    "error_code": getattr(exc, "error_code", "execution_failed"),
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
