from __future__ import annotations

import argparse
import copy
import json
import sys
from pathlib import Path
from typing import Any

from .codeagent_canvas_vision import (
    CodeAgentVisionError,
    CodeAgentVisionResponseError,
)
from .local_cv_canvas_vision import (
    LocalVisionDependencyError,
    LocalVisionRecognitionError,
)
from .topology_artifact_common import TopologyArtifactCLIError, write_json
from .topology_cv_cli import generate_cv_artifact
from .topology_fusion import TopologyFusionError, fuse_topology_payloads
from .topology_fusion_cli import load_json
from .topology_model_cli import (
    DEFAULT_MODEL_IDLE_TIMEOUT_SECONDS,
    DEFAULT_MODEL_MAX_ATTEMPTS,
    MAX_MODEL_ATTEMPTS,
    generate_model_artifact,
)

_DEGRADABLE_MODEL_ERROR_CODES = frozenset(
    {
        "ambiguous_model_response",
        "final_step_incomplete",
        "invalid_model_response",
        "missing_final_text",
        "step_incomplete",
    }
)


def _can_degrade_to_local_cv(exc: CodeAgentVisionError) -> bool:
    return (
        isinstance(exc, CodeAgentVisionResponseError)
        and exc.category == "model_response"
        and exc.retryable
        and exc.error_code in _DEGRADABLE_MODEL_ERROR_CODES
    )


def _model_error_payload(exc: CodeAgentVisionError) -> dict[str, Any]:
    return {
        "error": str(exc),
        "error_type": type(exc).__name__,
        "error_code": exc.error_code,
        "category": exc.category,
        "retryable": exc.retryable,
    }


def _local_cv_fallback(
    cv_result: dict[str, Any],
    *,
    model_error: dict[str, Any],
) -> dict[str, Any]:
    fused = fuse_topology_payloads(
        cv_result,
        {"topology": {"nodes": [], "edges": []}},
    )
    summary = fused.get("summary")
    if not isinstance(summary, dict):
        raise TopologyFusionError("fusion summary must be an object")

    result = fused.get("result")
    result_objects = result.get("objects", []) if isinstance(result, dict) else []
    if (
        not isinstance(result_objects, list)
        or not result_objects
        or summary.get("cv_object_count", 0) < 1
        or summary.get("grounded_object_count", 0) < 1
    ):
        raise TopologyFusionError(
            "cannot degrade to local CV without grounded topology objects"
        )
    result_links = result.get("links", []) if isinstance(result, dict) else []
    if not isinstance(result_links, list):
        raise TopologyFusionError("fusion result links must be a list")
    for graph_name in (
        "result",
        "grounded_graph",
        "display_graph",
        "semantic_graph",
    ):
        graph = fused.get(graph_name)
        links = graph.get("links", []) if isinstance(graph, dict) else []
        if not isinstance(links, list):
            raise TopologyFusionError(f"{graph_name} links must be a list")
        for link in links:
            if not isinstance(link, dict):
                raise TopologyFusionError(f"{graph_name} link must be an object")
            attributes = link.setdefault("attributes", {})
            if not isinstance(attributes, dict):
                raise TopologyFusionError(
                    f"{graph_name} link attributes must be an object"
                )
            attributes["relation_state"] = "disputed"
            attributes["degradation_reason"] = "model_stage_failed"
            attributes["interaction_eligible"] = False
    fused["disputed_links"] = copy.deepcopy(result_links)
    summary["accepted_link_count"] = 0
    summary["disputed_link_count"] = len(result_links)
    summary["degraded_to"] = "local_cv"
    summary["model_error_code"] = model_error.get("error_code")
    summary["model_error_category"] = model_error.get("category")
    fused["degradation"] = {
        "degraded_to": "local_cv",
        "reason": "model_stage_failed",
        "model_error": dict(model_error),
    }
    return fused


def run_pipeline(
    image_path: Path,
    *,
    source_id: str,
    output_dir: Path,
    executable: str = "codeagent",
    agent: str | None = None,
    permission_mode: str = "dontAsk",
    timeout_seconds: float = 600.0,
    idle_timeout_seconds: float | None = DEFAULT_MODEL_IDLE_TIMEOUT_SECONDS,
    max_attempts: int = DEFAULT_MODEL_MAX_ATTEMPTS,
    workdir: Path | None = None,
    reuse_cv: bool = False,
) -> dict[str, Path | None]:
    output_dir.mkdir(parents=True, exist_ok=True)
    cv_path = output_dir / "cv-result.json"
    model_path = output_dir / "model-result.json"
    events_path = output_dir / "codeagent-events.jsonl"
    stderr_path = output_dir / "codeagent-stderr.log"
    fused_path = output_dir / "fused-result.json"
    model_error_path = output_dir / "model-error.json"

    stale_paths = (
        (model_path, events_path, stderr_path, fused_path, model_error_path)
        if reuse_cv
        else (
            cv_path,
            model_path,
            events_path,
            stderr_path,
            fused_path,
            model_error_path,
        )
    )
    for stale_path in stale_paths:
        try:
            stale_path.unlink(missing_ok=True)
        except OSError as exc:
            raise TopologyArtifactCLIError(
                f"cannot replace stale artifact: {stale_path}"
            ) from exc

    if reuse_cv:
        if not cv_path.is_file():
            raise TopologyArtifactCLIError(
                f"cannot reuse missing CV artifact: {cv_path}"
            )
        cv_result = load_json(cv_path)
    else:
        cv_result = generate_cv_artifact(
            image_path,
            source_id=source_id,
            output_path=cv_path,
        )
    try:
        model_result = generate_model_artifact(
            image_path,
            source_id=source_id,
            output_path=model_path,
            events_path=events_path,
            stderr_path=stderr_path,
            cv_path=cv_path,
            executable=executable,
            agent=agent,
            permission_mode=permission_mode,
            timeout_seconds=timeout_seconds,
            idle_timeout_seconds=idle_timeout_seconds,
            max_attempts=max_attempts,
            workdir=workdir,
        )
    except CodeAgentVisionError as exc:
        if not _can_degrade_to_local_cv(exc):
            raise
        model_error = _model_error_payload(exc)
        write_json(model_error_path, model_error)
        write_json(
            fused_path,
            _local_cv_fallback(cv_result, model_error=model_error),
        )
        return {
            "cv": cv_path,
            "events": events_path if events_path.is_file() else None,
            "stderr": stderr_path if stderr_path.is_file() else None,
            "fused": fused_path,
            "model_error": model_error_path,
        }
    write_json(fused_path, fuse_topology_payloads(cv_result, model_result))
    return {
        "cv": cv_path,
        "model": model_path,
        "events": events_path,
        "stderr": stderr_path,
        "fused": fused_path,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Run standalone local CV, standalone CodeAgent recognition, then "
            "offline topology fusion. Intermediate artifacts are retained."
        )
    )
    parser.add_argument("image", type=Path)
    parser.add_argument("--source-id", required=True)
    parser.add_argument("--out-dir", type=Path, required=True)
    parser.add_argument(
        "--timeout",
        type=float,
        default=600.0,
        help="total wall-clock budget shared by all model attempts",
    )
    parser.add_argument(
        "--idle-timeout",
        type=float,
        default=DEFAULT_MODEL_IDLE_TIMEOUT_SECONDS,
        help=(
            "abort and retry after this many post-Read seconds without stdout; "
            "use 0 to disable"
        ),
    )
    parser.add_argument(
        "--max-attempts",
        type=int,
        choices=range(1, MAX_MODEL_ATTEMPTS + 1),
        default=DEFAULT_MODEL_MAX_ATTEMPTS,
        help="maximum CodeAgent sessions within --timeout",
    )
    parser.add_argument("--executable", default="codeagent")
    parser.add_argument("--agent")
    parser.add_argument(
        "--permission-mode",
        choices=("dontAsk", "bypassPermissions"),
        default="dontAsk",
    )
    parser.add_argument("--workdir", type=Path, default=Path.cwd())
    parser.add_argument(
        "--reuse-cv",
        action="store_true",
        help="keep and reuse an existing cv-result.json; retry only model/fusion",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        paths = run_pipeline(
            args.image,
            source_id=args.source_id,
            output_dir=args.out_dir,
            executable=args.executable,
            agent=args.agent,
            permission_mode=args.permission_mode,
            timeout_seconds=args.timeout,
            idle_timeout_seconds=args.idle_timeout,
            max_attempts=args.max_attempts,
            workdir=args.workdir,
            reuse_cv=args.reuse_cv,
        )
        degraded = "model_error" in paths
        path_payload = {
            name: str(path.resolve()) if path is not None else None
            for name, path in paths.items()
        }
        if degraded:
            path_payload["model"] = None
        print(
            json.dumps(
                {
                    "status": "degraded" if degraded else "ok",
                    **(
                        {"degraded_to": "local_cv"}
                        if degraded
                        else {}
                    ),
                    **path_payload,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    except KeyboardInterrupt:
        events_path = args.out_dir / "codeagent-events.jsonl"
        stderr_path = args.out_dir / "codeagent-stderr.log"
        print(
            json.dumps(
                {
                    "error": "interrupted; CodeAgent process tree was terminated",
                    "error_type": "KeyboardInterrupt",
                    "events": (
                        str(events_path.resolve()) if events_path.exists() else None
                    ),
                    "stderr": (
                        str(stderr_path.resolve()) if stderr_path.exists() else None
                    ),
                },
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
        return 130
    except (
        CodeAgentVisionError,
        LocalVisionDependencyError,
        LocalVisionRecognitionError,
        TopologyArtifactCLIError,
        TopologyFusionError,
        OSError,
        ValueError,
    ) as exc:
        events_path = args.out_dir / "codeagent-events.jsonl"
        stderr_path = args.out_dir / "codeagent-stderr.log"
        print(
            json.dumps(
                {
                    "error": str(exc),
                    "error_type": type(exc).__name__,
                    "error_code": getattr(exc, "error_code", None),
                    "category": getattr(exc, "category", None),
                    "retryable": getattr(exc, "retryable", False),
                    "events": (
                        str(events_path.resolve()) if events_path.exists() else None
                    ),
                    "stderr": (
                        str(stderr_path.resolve()) if stderr_path.exists() else None
                    ),
                },
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
