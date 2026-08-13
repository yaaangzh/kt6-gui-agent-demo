"""Command-line entry point for the offline KT6 evaluation report pipeline."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .evaluation_artifacts import (
    EvaluationArtifactError,
    parse_artifact_arguments,
)
from .evaluation_report import (
    EvaluationDataError,
    append_run_record,
    build_run_template,
    build_suite_template,
    generate_report,
    load_json_object,
    load_run_records,
    validate_run_records,
    validate_suite,
    write_report_bundle,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Create, validate and render local comparison reports for the "
            "current, Browser Use and UI-TARS evaluation groups."
        )
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    init_parser = subparsers.add_parser(
        "init", help="create a draft three-scheme evaluation suite"
    )
    init_parser.add_argument("--out", type=Path, required=True)
    init_parser.add_argument("--suite-id", required=True)
    init_parser.add_argument("--title", required=True)
    init_parser.add_argument("--task-count", type=int, default=28)
    init_parser.add_argument("--repetitions", type=int, default=3)
    init_parser.add_argument("--step-limit", type=int, default=10)
    init_parser.add_argument("--planner-provider", default="deepseek")
    init_parser.add_argument(
        "--planner-model", default="fill-exact-model-name"
    )
    init_parser.add_argument(
        "--environment-id", default="fill-test-environment-id"
    )

    template_parser = subparsers.add_parser(
        "run-template", help="create one draft run-result JSON"
    )
    template_parser.add_argument("--suite", type=Path, required=True)
    template_parser.add_argument("--scheme", required=True)
    template_parser.add_argument("--task", required=True)
    template_parser.add_argument("--repetition", type=int, required=True)
    template_parser.add_argument("--out", type=Path, required=True)

    validate_parser = subparsers.add_parser(
        "validate", help="validate the ready suite and all recorded runs"
    )
    validate_parser.add_argument("--suite", type=Path, required=True)
    validate_parser.add_argument("--runs", type=Path, required=True)

    record_parser = subparsers.add_parser(
        "record", help="validate and append one final run JSON to JSONL"
    )
    record_parser.add_argument("--suite", type=Path, required=True)
    record_parser.add_argument("--runs", type=Path, required=True)
    record_parser.add_argument("--input", type=Path, required=True)
    record_parser.add_argument(
        "--artifact",
        action="append",
        required=True,
        metavar="ROLE=PATH",
        help=(
            "archive one evidence file; repeat for screenshots, perception "
            "outputs, action_trace and validation_result"
        ),
    )

    report_parser = subparsers.add_parser(
        "report", help="generate JSON, CSV, Markdown and HTML reports"
    )
    report_parser.add_argument("--suite", type=Path, required=True)
    report_parser.add_argument("--runs", type=Path, required=True)
    report_parser.add_argument("--out-dir", type=Path, required=True)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "init":
            suite = build_suite_template(
                suite_id=args.suite_id,
                title=args.title,
                task_count=args.task_count,
                repetitions=args.repetitions,
                step_limit=args.step_limit,
                planner_provider=args.planner_provider,
                planner_model=args.planner_model,
                environment_id=args.environment_id,
            )
            _write_new_json(args.out, suite)
            print(
                json.dumps(
                    {
                        "status": "draft_created",
                        "path": str(args.out.resolve()),
                        "next": (
                            "Fill tasks/model/environment, then set "
                            "suite.status to 'ready'."
                        ),
                    },
                    ensure_ascii=False,
                )
            )
            return 0

        if args.command == "run-template":
            suite = validate_suite(
                load_json_object(args.suite), require_ready=False
            )
            record = build_run_template(
                suite,
                scheme_id=args.scheme,
                task_id=args.task,
                repetition=args.repetition,
            )
            _write_new_json(args.out, record)
            print(
                json.dumps(
                    {
                        "status": "draft_created",
                        "path": str(args.out.resolve()),
                        "next": (
                            "Fill real metrics and validation evidence, then set "
                            "record_status to 'final'."
                        ),
                    },
                    ensure_ascii=False,
                )
            )
            return 0

        suite = validate_suite(load_json_object(args.suite))
        if args.command == "validate":
            records = validate_run_records(
                load_run_records(args.runs),
                suite,
                require_archived_evidence=True,
            )
            report = generate_report(
                suite,
                records,
                evidence_root=args.runs.parent,
                require_evidence=True,
            )
            print(
                json.dumps(
                    {
                        "status": report["report_status"],
                        "recorded_runs": len(records),
                        "expected_runs": report["coverage"]["expected_runs"],
                        "missing_runs": report["coverage"]["missing_runs"],
                        "fair_comparison": report["fairness"]["fair_comparison"],
                        "evidence_verified_runs": report["evidence"][
                            "verified_runs"
                        ],
                        "evidence_invalid_runs": report["evidence"][
                            "failed_runs"
                        ],
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return _report_exit_code(report)

        if args.command == "record":
            artifact_sources = parse_artifact_arguments(args.artifact)
            record = append_run_record(
                args.runs,
                load_json_object(args.input),
                suite,
                artifact_sources=artifact_sources,
            )
            print(
                json.dumps(
                    {
                        "status": "recorded",
                        "run_id": record["run_id"],
                        "runs_path": str(args.runs.resolve()),
                        "manifest_ref": record["evidence"]["manifest_ref"],
                        "manifest_sha256": record["evidence"][
                            "manifest_sha256"
                        ],
                    },
                    ensure_ascii=False,
                )
            )
            return 0

        if args.command == "report":
            records = load_run_records(args.runs)
            report = generate_report(
                suite,
                records,
                evidence_root=args.runs.parent,
                require_evidence=True,
            )
            paths = write_report_bundle(report, args.out_dir)
            print(
                json.dumps(
                    {
                        "status": report["report_status"],
                        "decision": report["conclusion"]["decision"],
                        "recommended_scheme_id": report["conclusion"].get(
                            "recommended_scheme_id"
                        ),
                        "files": [str(path) for path in paths],
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return _report_exit_code(report)
    except (EvaluationArtifactError, EvaluationDataError, OSError) as exc:
        _write_cli_error(args.command, exc)
        return 3
    return 2


def _write_cli_error(command: str, exc: BaseException) -> None:
    """Emit a stable diagnostic without copying local data from exceptions."""

    print(
        json.dumps(
            {
                "error_code": _cli_error_code(exc),
                "stage": command,
            },
            ensure_ascii=False,
        ),
        file=sys.stderr,
    )


def _cli_error_code(exc: BaseException) -> str:
    """Classify wrapped errors by type without inspecting their messages."""

    chain: list[BaseException] = []
    current: BaseException | None = exc
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        chain.append(current)
        current = current.__cause__ or current.__context__
    if any(isinstance(item, EvaluationArtifactError) for item in chain):
        return "evaluation_artifact_error"
    if any(isinstance(item, OSError) for item in chain):
        return "evaluation_io_error"
    return "evaluation_data_error"


def _write_new_json(path: Path, payload: dict[str, object]) -> None:
    destination = Path(path).expanduser().resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        rendered = json.dumps(
            payload,
            ensure_ascii=False,
            indent=2,
            allow_nan=False,
        ) + "\n"
    except (TypeError, ValueError, RecursionError) as exc:
        raise EvaluationDataError("generated JSON is not serializable") from exc
    try:
        with destination.open("x", encoding="utf-8", newline="\n") as stream:
            stream.write(rendered)
    except FileExistsError as exc:
        raise EvaluationDataError(
            f"refusing to overwrite existing file: {destination}"
        ) from exc


def _report_exit_code(report: dict[str, object]) -> int:
    """Return a stable non-zero code when a comparison is not decision-ready."""

    status = report.get("report_status")
    status_code = {
        "evidence_blocked": 4,
        "fairness_blocked": 5,
        "incomplete": 6,
        "safety_warning": 7,
    }.get(str(status), 0)
    if status_code:
        return status_code
    conclusion = report.get("conclusion")
    if isinstance(conclusion, dict) and conclusion.get("decision") not in {
        "ready_for_comparison",
    }:
        return 8
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
