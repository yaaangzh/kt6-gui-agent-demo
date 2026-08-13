"""CLI for one Browser Use + model API evaluation repetition."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Sequence

from .browser_use_evaluation import (
    BrowserUseEvaluationConfig,
    load_suite_and_task,
    run_browser_use_evaluation,
)
from .evaluation_executor import (
    EvaluationExecutionError,
    load_execution_task,
    optional_env,
    required_env,
)
from .evaluation_report import EvaluationDataError


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run one isolated Browser Use + model API evaluation."
    )
    parser.add_argument("--suite", required=True, type=Path)
    parser.add_argument("--task", required=True, type=Path)
    parser.add_argument("--runs", required=True, type=Path)
    parser.add_argument("--workspace", required=True, type=Path)
    parser.add_argument("--repetition", required=True, type=int)
    parser.add_argument("--model", default=None)
    parser.add_argument("--provider", default=None)
    parser.add_argument("--api-base-url", default=None)
    parser.add_argument(
        "--api-allowed-host",
        action="append",
        default=None,
        help="Exact host allowed to receive the bearer key and task context.",
    )
    parser.add_argument(
        "--allow-remote-model",
        action="store_true",
        help="Explicitly allow page/task data to leave the test machine.",
    )
    parser.add_argument("--cdp-url", default=None)
    parser.add_argument("--headless", action="store_true")
    parser.add_argument("--max-steps", type=int, default=None)
    parser.add_argument("--llm-timeout-seconds", type=int, default=90)
    parser.add_argument("--implementation-version", default="browser-use-0.13.7")
    parser.add_argument("--implementation-revision", required=True)
    parser.add_argument(
        "--implementation-branch", default="eval-browser-use"
    )
    parser.add_argument("--environment-id", required=True)
    parser.add_argument("--browser-label", default="Chromium via Browser Use 0.13.7")
    parser.add_argument("--viewport", default="1920x1080")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    stage = "configuration"
    try:
        task = load_execution_task(args.task)
        suite = load_suite_and_task(args.suite, task)
        base_url = args.api_base_url or required_env("KT6_MODEL_API_BASE_URL")
        provider = args.provider or required_env("KT6_MODEL_API_PROVIDER")
        model = args.model or required_env("KT6_MODEL_API_MODEL")
        api_key = required_env("KT6_MODEL_API_KEY")
        allowed_hosts = args.api_allowed_host
        if allowed_hosts is None:
            raw_hosts = optional_env("KT6_MODEL_API_ALLOWED_HOSTS")
            allowed_hosts = [item.strip() for item in raw_hosts.split(",") if item.strip()]
        max_steps = args.max_steps if args.max_steps is not None else task.step_limit
        config = BrowserUseEvaluationConfig(
            base_url=base_url,
            api_key=api_key,
            provider=provider,
            model=model,
            api_allowed_hosts=frozenset(allowed_hosts),
            cdp_url=args.cdp_url or optional_env("KT6_BROWSER_USE_CDP_URL") or None,
            headless=args.headless,
            max_steps=max_steps,
            llm_timeout_seconds=args.llm_timeout_seconds,
            allow_remote_model=args.allow_remote_model,
        )
        stage = "execution"
        recorded = asyncio.run(
            run_browser_use_evaluation(
                suite=suite,
                runs_path=args.runs,
                workspace_root=args.workspace,
                task=task,
                repetition=args.repetition,
                config=config,
                implementation={
                    "name": "KT6 Browser Use Model API",
                    "version": args.implementation_version,
                    "revision": args.implementation_revision,
                    "branch": args.implementation_branch,
                },
                environment={
                    "environment_id": args.environment_id,
                    "browser": args.browser_label,
                    "viewport": args.viewport,
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
        # Do not copy model/page errors, URLs, local paths, or credentials into
        # terminal/CI logs.  Detailed evidence remains in the local workspace.
        print(
            json.dumps(
                {"error_code": "browser_use_evaluation_failed", "stage": stage},
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
