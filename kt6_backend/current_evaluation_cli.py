"""CLI for one current CV/OCR + model API evaluation repetition."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Sequence

from .current_evaluation import CurrentEvaluationConfig, run_current_evaluation
from .env_config import load_project_env
from .evaluation_executor import (
    EvaluationExecutionError,
    load_execution_task,
    optional_env,
    required_env,
)
from .evaluation_report import EvaluationDataError, load_json_object, validate_suite
from .topology_cv_routing import TASK_PROFILES


PROJECT_ROOT = Path(__file__).resolve().parent.parent


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run one current local CV/OCR + model API evaluation."
    )
    parser.add_argument("--suite", required=True, type=Path)
    parser.add_argument("--task", required=True, type=Path)
    parser.add_argument("--image", required=True, type=Path)
    parser.add_argument("--runs", required=True, type=Path)
    parser.add_argument("--workspace", required=True, type=Path)
    parser.add_argument("--repetition", required=True, type=int)
    parser.add_argument("--source-id", default=None)
    parser.add_argument("--model", default=None)
    parser.add_argument("--provider", default=None)
    parser.add_argument("--api-base-url", default=None)
    parser.add_argument("--api-allowed-host", action="append", default=None)
    parser.add_argument(
        "--allow-remote-model",
        action="store_true",
        help="Explicitly allow bounded CV/OCR text to leave the test machine.",
    )
    parser.add_argument(
        "--requested-profile",
        choices=sorted(TASK_PROFILES),
        default="auto",
    )
    parser.add_argument("--implementation-version", default="current-api-v1")
    parser.add_argument("--implementation-revision", required=True)
    parser.add_argument("--implementation-branch", default="eval-current")
    parser.add_argument("--environment-id", required=True)
    parser.add_argument("--environment-label", default="offline topology image")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    stage = "configuration"
    try:
        load_project_env(PROJECT_ROOT)
        task = load_execution_task(args.task)
        suite = validate_suite(load_json_object(args.suite))
        allowed_hosts = args.api_allowed_host
        if allowed_hosts is None:
            allowed_hosts = _csv_env("KT6_MODEL_API_ALLOWED_HOSTS")
        config = CurrentEvaluationConfig(
            base_url=args.api_base_url or required_env("KT6_MODEL_API_BASE_URL"),
            api_key=required_env("KT6_MODEL_API_KEY"),
            provider=args.provider or required_env("KT6_MODEL_API_PROVIDER"),
            model=args.model or required_env("KT6_MODEL_API_MODEL"),
            api_allowed_hosts=frozenset(allowed_hosts),
            allow_remote_model=args.allow_remote_model,
            timeout_seconds=_number_env("KT6_VISION_TIMEOUT_SECONDS", 60.0),
            max_tokens=_integer_env("KT6_MODEL_API_MAX_TOKENS", 4096),
            requested_profile=args.requested_profile,
        )
        stage = "execution"
        recorded = run_current_evaluation(
            suite=suite,
            runs_path=args.runs,
            workspace_root=args.workspace,
            task=task,
            repetition=args.repetition,
            image_path=args.image,
            source_id=args.source_id or args.image.stem,
            config=config,
            implementation={
                "name": "KT6 local CV/OCR + Model API",
                "version": args.implementation_version,
                "revision": args.implementation_revision,
                "branch": args.implementation_branch,
            },
            environment={
                "environment_id": args.environment_id,
                "browser": args.environment_label,
                "viewport": "derived-from-image",
            },
        )
        result = {
            "status": "recorded",
            "run_id": recorded["run_id"],
            "outcome": recorded["outcome"],
            "evidence_status": recorded["evidence"]["status"],
        }
        process_image = (
            args.workspace / recorded["run_id"] / "processed-screenshot-001.png"
        ).resolve()
        if process_image.is_file():
            result["process_image"] = str(process_image)
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0
    except (EvaluationExecutionError, EvaluationDataError, OSError, ValueError):
        print(
            json.dumps(
                {"error_code": "current_evaluation_failed", "stage": stage},
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 3


def _csv_env(name: str) -> list[str]:
    return [item.strip() for item in optional_env(name).split(",") if item.strip()]


def _number_env(name: str, default: float) -> float:
    value = optional_env(name, str(default))
    number = float(value)
    if number <= 0:
        raise EvaluationExecutionError(f"{name} must be positive")
    return number


def _integer_env(name: str, default: int) -> int:
    value = optional_env(name, str(default))
    number = int(value)
    if number <= 0:
        raise EvaluationExecutionError(f"{name} must be positive")
    return number


if __name__ == "__main__":
    raise SystemExit(main())
