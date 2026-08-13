"""CLI for one DeepSeek-planned UI-TARS API evaluation repetition."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Sequence

from .evaluation_executor import (
    EvaluationExecutionError,
    load_execution_task,
    optional_env,
    required_env,
)
from .evaluation_report import EvaluationDataError, load_json_object, validate_suite
from .ui_tars_evaluation import (
    ModelEndpointConfig,
    UITarsEvaluationConfig,
    run_ui_tars_evaluation,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run one isolated DeepSeek + UI-TARS API evaluation."
    )
    parser.add_argument("--suite", required=True, type=Path)
    parser.add_argument("--task", required=True, type=Path)
    parser.add_argument("--runs", required=True, type=Path)
    parser.add_argument("--workspace", required=True, type=Path)
    parser.add_argument("--repetition", required=True, type=int)
    parser.add_argument("--planner-api-base-url", default=None)
    parser.add_argument("--planner-model", default=None)
    parser.add_argument("--planner-api-allowed-host", action="append", default=None)
    parser.add_argument("--vision-api-base-url", default=None)
    parser.add_argument("--vision-model", default=None)
    parser.add_argument("--vision-api-allowed-host", action="append", default=None)
    parser.add_argument("--allow-remote-planner", action="store_true")
    parser.add_argument("--allow-remote-vision", action="store_true")
    parser.add_argument("--execute-actions", action="store_true")
    parser.add_argument(
        "--coordinate-mode",
        choices=("scale_1000", "unit", "pixel"),
        default="scale_1000",
    )
    parser.add_argument("--cdp-url", default=None)
    parser.add_argument("--headless", action="store_true")
    parser.add_argument("--max-steps", type=int, default=None)
    parser.add_argument("--viewport-width", type=int, default=1920)
    parser.add_argument("--viewport-height", type=int, default=1080)
    parser.add_argument("--implementation-version", default="ui-tars-api-v1")
    parser.add_argument("--implementation-revision", required=True)
    parser.add_argument("--implementation-branch", default="eval-ui-tars-api")
    parser.add_argument("--environment-id", required=True)
    parser.add_argument("--browser-label", default="Playwright Chromium 1.49.1")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    stage = "configuration"
    try:
        task = load_execution_task(args.task)
        suite = validate_suite(load_json_object(args.suite))
        if suite["suite_id"] != task.suite_id:
            raise EvaluationExecutionError("execution task does not belong to suite")
        planner_hosts = args.planner_api_allowed_host or _hosts_env(
            "KT6_DEEPSEEK_API_ALLOWED_HOSTS"
        )
        vision_hosts = args.vision_api_allowed_host or _hosts_env(
            "KT6_UI_TARS_API_ALLOWED_HOSTS"
        )
        planner = ModelEndpointConfig(
            base_url=args.planner_api_base_url
            or required_env("KT6_DEEPSEEK_API_BASE_URL"),
            api_key=required_env("KT6_DEEPSEEK_API_KEY"),
            model=args.planner_model or required_env("KT6_DEEPSEEK_MODEL"),
            allowed_hosts=frozenset(planner_hosts),
            allow_remote=args.allow_remote_planner,
        )
        vision = ModelEndpointConfig(
            base_url=args.vision_api_base_url
            or required_env("KT6_UI_TARS_API_BASE_URL"),
            api_key=required_env("KT6_UI_TARS_API_KEY"),
            model=args.vision_model or required_env("KT6_UI_TARS_MODEL"),
            allowed_hosts=frozenset(vision_hosts),
            allow_remote=args.allow_remote_vision,
        )
        config = UITarsEvaluationConfig(
            planner=planner,
            vision=vision,
            coordinate_mode=args.coordinate_mode,
            execute_actions=args.execute_actions,
            cdp_url=args.cdp_url or optional_env("KT6_UI_TARS_CDP_URL") or None,
            headless=args.headless,
            max_steps=args.max_steps if args.max_steps is not None else task.step_limit,
            viewport_width=args.viewport_width,
            viewport_height=args.viewport_height,
        )
        stage = "execution"
        recorded = asyncio.run(
            run_ui_tars_evaluation(
                suite=suite,
                runs_path=args.runs,
                workspace_root=args.workspace,
                task=task,
                repetition=args.repetition,
                config=config,
                implementation={
                    "name": "KT6 DeepSeek + UI-TARS API",
                    "version": args.implementation_version,
                    "revision": args.implementation_revision,
                    "branch": args.implementation_branch,
                },
                environment={
                    "environment_id": args.environment_id,
                    "browser": args.browser_label,
                    "viewport": f"{args.viewport_width}x{args.viewport_height}",
                },
            )
        )
        print(
            json.dumps(
                {
                    "status": "recorded",
                    "run_id": recorded["run_id"],
                    "outcome": recorded["outcome"],
                    "evidence_status": recorded["evidence"]["status"],
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 0
    except (EvaluationExecutionError, EvaluationDataError, OSError, ValueError):
        print(
            json.dumps(
                {"error_code": "ui_tars_evaluation_failed", "stage": stage},
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 3


def _hosts_env(name: str) -> list[str]:
    return [item.strip() for item in optional_env(name).split(",") if item.strip()]


if __name__ == "__main__":
    raise SystemExit(main())
