from __future__ import annotations

import argparse
import copy
import json
import sys
from pathlib import Path
from typing import Any, Iterable, Mapping

from .codeagent_canvas_vision import CodeAgentVisionError
from .local_cv_canvas_vision import (
    LocalVisionDependencyError,
    LocalVisionRecognitionError,
)
from .topology_artifact_common import (
    TopologyArtifactCLIError,
    ensure_distinct_paths,
    write_json,
)
from .topology_cv_cli import (
    generate_cv_artifact,
    validate_cv_artifact_metadata,
)
from .topology_cv_routing import (
    TASK_PROFILES,
    assess_cv_result,
    prepare_cv_payload_for_route,
)
from .topology_fusion import TopologyFusionError, fuse_topology_payloads
from .topology_fusion_cli import load_json
from .topology_model_cli import (
    DEFAULT_MODEL_IDLE_TIMEOUT_SECONDS,
    DEFAULT_MODEL_MAX_ATTEMPTS,
    MAX_MODEL_ATTEMPTS,
    generate_model_artifact,
)


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
    requested_profile: str = "auto",
) -> dict[str, Path | None]:
    cv_path = output_dir / "cv-result.json"
    cv_metadata_path = output_dir / "cv-metadata.json"
    model_path = output_dir / "model-result.json"
    events_path = output_dir / "codeagent-events.jsonl"
    stderr_path = output_dir / "codeagent-stderr.log"
    routing_path = output_dir / "routing-result.json"
    fused_path = output_dir / "fused-result.json"

    attempt_paths = _model_attempt_artifact_paths(events_path, stderr_path)
    ensure_distinct_paths(
        image_path,
        cv_path,
        cv_metadata_path,
        model_path,
        events_path,
        stderr_path,
        routing_path,
        fused_path,
        *attempt_paths,
    )
    output_dir.mkdir(parents=True, exist_ok=True)

    stale_paths: list[Path] = [
        model_path,
        events_path,
        stderr_path,
        routing_path,
        fused_path,
        *attempt_paths,
    ]

    if reuse_cv:
        if not cv_path.is_file():
            raise TopologyArtifactCLIError(
                f"cannot reuse missing CV artifact: {cv_path}"
            )
        if not cv_metadata_path.is_file():
            raise TopologyArtifactCLIError(
                "cannot reuse CV artifact without cv-metadata.json; "
                "rerun once without --reuse-cv"
            )
        cv_result = load_json(cv_path)
        cv_metadata = validate_cv_artifact_metadata(
            image_path,
            source_id=source_id,
            metadata=load_json(cv_metadata_path),
            cv_artifact_path=cv_path,
        )
    else:
        cv_result = generate_cv_artifact(
            image_path,
            source_id=source_id,
            output_path=cv_path,
            metadata_output_path=cv_metadata_path,
        )
        cv_metadata = validate_cv_artifact_metadata(
            image_path,
            source_id=source_id,
            metadata=load_json(cv_metadata_path),
            cv_artifact_path=cv_path,
        )

    routing = assess_cv_result(
        cv_result,
        requested_profile=requested_profile,
        trusted_provenance={
            "adapter_id": cv_metadata.get("adapter_id"),
            "adapter_version": cv_metadata.get("adapter_version"),
        },
    )
    routing["source"] = _routing_source(cv_metadata)
    routing["execution_status"] = (
        "model_pending"
        if routing["decision"] == "model_assist"
        else "completed_without_model"
    )
    _remove_stale_artifacts(stale_paths)
    write_json(routing_path, routing)

    if routing["decision"] == "model_assist":
        failure_stage = "model"
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
            failure_stage = "fusion"
            fused_result = fuse_topology_payloads(cv_result, model_result)
            failure_stage = "routing_finalization"
            routing = _finalize_model_routing(
                routing,
                model_result=model_result,
                fused_result=fused_result,
            )
        except KeyboardInterrupt as exc:
            _persist_failed_model_routing(
                routing_path,
                routing,
                stage=failure_stage,
                error=exc,
                interrupted=True,
            )
            raise
        except Exception as exc:
            _persist_failed_model_routing(
                routing_path,
                routing,
                stage=failure_stage,
                error=exc,
            )
            raise
        write_json(routing_path, routing)
        fused_result["routing"] = copy.deepcopy(routing)
        model_artifact: Path | None = model_path
        events_artifact: Path | None = events_path
        stderr_artifact: Path | None = stderr_path
    else:
        prepared_cv, disputed_links = prepare_cv_payload_for_route(
            cv_result,
            routing,
        )
        fused_result = fuse_topology_payloads(
            prepared_cv,
            {"topology": {"nodes": [], "edges": []}},
        )
        _attach_routing_audit(
            fused_result,
            routing=routing,
            disputed_links=disputed_links,
        )
        model_artifact = None
        events_artifact = None
        stderr_artifact = None

    write_json(fused_path, fused_result)
    return {
        "cv": cv_path,
        "cv_metadata": cv_metadata_path,
        "routing": routing_path,
        "model": model_artifact,
        "events": events_artifact,
        "stderr": stderr_artifact,
        "fused": fused_path,
    }


def _model_attempt_artifact_paths(
    events_path: Path,
    stderr_path: Path,
) -> tuple[Path, ...]:
    return tuple(
        path.with_name(f"{path.stem}.attempt-{attempt}{path.suffix}")
        for attempt in range(1, MAX_MODEL_ATTEMPTS)
        for path in (events_path, stderr_path)
    )


def _remove_stale_artifacts(paths: Iterable[Path]) -> None:
    for stale_path in paths:
        try:
            stale_path.unlink(missing_ok=True)
        except OSError as exc:
            raise TopologyArtifactCLIError(
                f"cannot replace stale artifact: {stale_path}"
            ) from exc


def _persist_failed_model_routing(
    routing_path: Path,
    routing: Mapping[str, Any],
    *,
    stage: str,
    error: BaseException,
    interrupted: bool = False,
) -> None:
    """Best-effort failure audit without masking the original exception."""

    failed = copy.deepcopy(dict(routing))
    reason_codes = failed.get("reason_codes", [])
    if not isinstance(reason_codes, list):
        reason_codes = []
    reason_codes = list(reason_codes)
    reason_codes.append(
        "model_pipeline_interrupted"
        if interrupted
        else "model_pipeline_failed"
    )
    failed.update(
        {
            "requirement_satisfied": False,
            "result_status": "interrupted" if interrupted else "failed",
            "execution_status": (
                "model_interrupted" if interrupted else "model_failed"
            ),
            "reason_codes": list(dict.fromkeys(reason_codes)),
            "failure": {
                "stage": stage,
                "error_type": type(error).__name__,
                "error_code": getattr(error, "error_code", None),
                "category": getattr(error, "category", None),
                "retryable": bool(getattr(error, "retryable", False)),
            },
        }
    )
    try:
        write_json(routing_path, failed)
    except OSError:
        pass


def _finalize_model_routing(
    routing: Mapping[str, Any],
    *,
    model_result: Mapping[str, Any],
    fused_result: Mapping[str, Any],
) -> dict[str, Any]:
    finalized = copy.deepcopy(dict(routing))
    raw_nodes = model_result.get("nodes", [])
    raw_links = model_result.get("links", [])
    nodes = raw_nodes if isinstance(raw_nodes, list) else []
    links = raw_links if isinstance(raw_links, list) else []
    summary = fused_result.get("summary", {})
    if not isinstance(summary, Mapping):
        raise TopologyFusionError("fused result summary must be an object")

    semantic_object_count = _summary_count(summary, "semantic_object_count")
    semantic_link_count = _summary_count(summary, "semantic_link_count")
    grounded_object_count = _summary_count(summary, "grounded_object_count")
    grounded_link_count = _summary_count(summary, "grounded_link_count")
    semantic_node_count = sum(
        1
        for node in nodes
        if isinstance(node, Mapping)
        and any(
            str(node.get(field, "")).strip()
            for field in ("role", "vendor", "model", "layer")
        )
    )
    profile = str(
        finalized.get(
            "effective_profile",
            finalized.get("requested_profile", ""),
        )
    )
    if profile == "nodes_only":
        requirement_satisfied = semantic_object_count > 0
    elif profile == "semantic_enrichment":
        requirement_satisfied = semantic_node_count > 0
    elif profile in {"visible_topology", "connectivity_query"}:
        requirement_satisfied = (
            semantic_object_count > 0 and grounded_link_count > 0
        )
    else:
        raise TopologyArtifactCLIError(
            f"unsupported requested profile in routing result: {profile}"
        )

    reason_codes = finalized.get("reason_codes", [])
    if not isinstance(reason_codes, list):
        raise TopologyArtifactCLIError("routing reason_codes must be a list")
    reason_codes = list(reason_codes)
    raw_satisfied = finalized.get("satisfied_capabilities", [])
    raw_missing = finalized.get("missing_capabilities", [])
    if not isinstance(raw_satisfied, list) or not isinstance(raw_missing, list):
        raise TopologyArtifactCLIError(
            "routing capabilities must be lists"
        )
    satisfied_capabilities = [
        str(item) for item in raw_satisfied if str(item).strip()
    ]
    missing_capabilities = [
        str(item) for item in raw_missing if str(item).strip()
    ]

    def update_capability(name: str, available: bool) -> None:
        if available:
            if name not in satisfied_capabilities:
                satisfied_capabilities.append(name)
            missing_capabilities[:] = [
                item for item in missing_capabilities if item != name
            ]
        else:
            if name not in missing_capabilities:
                missing_capabilities.append(name)
            satisfied_capabilities[:] = [
                item for item in satisfied_capabilities if item != name
            ]

    update_capability("object_identity", semantic_object_count > 0)
    update_capability(
        "analysis_only_image_geometry",
        grounded_object_count > 0,
    )
    update_capability(
        "verified_connectivity",
        grounded_link_count > 0,
    )
    if profile == "semantic_enrichment":
        update_capability(
            "semantic_enrichment",
            semantic_node_count > 0,
        )
    reason_codes.extend(
        [
            "model_stage_completed",
            (
                "requested_profile_satisfied_after_model"
                if requirement_satisfied
                else "model_result_did_not_satisfy_requested_profile"
            ),
        ]
    )
    finalized.update(
        {
            "requirement_satisfied": requirement_satisfied,
            "result_status": (
                "complete" if requirement_satisfied else "insufficient"
            ),
            "model_invoked": True,
            "execution_status": "model_completed",
            "reason_codes": list(dict.fromkeys(reason_codes)),
            "satisfied_capabilities": list(
                dict.fromkeys(satisfied_capabilities)
            ),
            "missing_capabilities": list(dict.fromkeys(missing_capabilities)),
            "post_model_metrics": {
                "model_object_count": len(nodes),
                "model_link_count": len(links),
                "semantic_object_count": semantic_object_count,
                "semantic_link_count": semantic_link_count,
                "grounded_object_count": grounded_object_count,
                "grounded_link_count": grounded_link_count,
                "semantic_enriched_node_count": semantic_node_count,
            },
        }
    )
    return finalized


def _summary_count(summary: Mapping[str, Any], name: str) -> int:
    value = summary.get(name, 0)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise TopologyFusionError(f"fused result summary {name} must be a count")
    return value


def _routing_source(metadata: Mapping[str, Any]) -> dict[str, Any]:
    frames = metadata.get("frames", [])
    if not isinstance(frames, list) or len(frames) != 1:
        raise TopologyArtifactCLIError(
            "CV artifact metadata must describe exactly one source frame"
        )
    frame = frames[0]
    if not isinstance(frame, dict):
        raise TopologyArtifactCLIError(
            "CV artifact metadata frame must be an object"
        )
    return {
        "source_id": metadata.get("source_id"),
        "sha256": frame.get("sha256"),
        "mime_type": frame.get("mime_type"),
        "width": frame.get("width"),
        "height": frame.get("height"),
        "cv_adapter_id": metadata.get("adapter_id"),
        "cv_adapter_version": metadata.get("adapter_version"),
    }


def _attach_routing_audit(
    fused_result: dict[str, Any],
    *,
    routing: Mapping[str, Any],
    disputed_links: list[dict[str, Any]],
) -> None:
    fused_result["routing"] = copy.deepcopy(dict(routing))
    if not disputed_links:
        return
    semantic_graph = fused_result.get("semantic_graph")
    summary = fused_result.get("summary")
    existing_disputed = fused_result.get("disputed_links")
    if (
        not isinstance(semantic_graph, dict)
        or not isinstance(semantic_graph.get("links"), list)
        or not isinstance(summary, dict)
        or not isinstance(existing_disputed, list)
    ):
        raise TopologyFusionError(
            "fused result is missing routing audit containers"
        )
    audit_links = copy.deepcopy(disputed_links)
    semantic_graph["links"].extend(copy.deepcopy(audit_links))
    existing_disputed.extend(audit_links)
    semantic_graph["links"].sort(key=_relation_sort_key)
    existing_disputed.sort(key=_relation_sort_key)
    summary["semantic_link_count"] = len(semantic_graph["links"])
    summary["disputed_link_count"] = len(existing_disputed)


def _relation_sort_key(item: Mapping[str, Any]) -> tuple[str, str, str]:
    return (
        str(item.get("source", "")).casefold(),
        str(item.get("target", "")).casefold(),
        str(item.get("relation_id", "")).casefold(),
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Run local CV, conditionally request CodeAgent assistance, then "
            "fuse topology evidence. Intermediate artifacts are retained."
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
        "--requested-profile",
        choices=sorted(TASK_PROFILES),
        default="auto",
        help=(
            "result policy; auto classifies the image and skips connection "
            "inference for scatter scenes"
        ),
    )
    parser.add_argument(
        "--reuse-cv",
        action="store_true",
        help=(
            "reuse cv-result.json only after metadata verifies image and CV "
            "hashes, dimensions, source-id and adapter version"
        ),
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
            requested_profile=args.requested_profile,
        )
        routing_path = paths["routing"]
        if routing_path is None:
            raise TopologyArtifactCLIError("routing artifact was not produced")
        routing = load_json(routing_path)
        insufficient = routing.get("requirement_satisfied") is not True
        print(
            json.dumps(
                {
                    "status": (
                        "insufficient_evidence" if insufficient else "ok"
                    ),
                    "routing_decision": routing.get("decision"),
                    "scene_type": routing.get("scene_type"),
                    "requested_profile": routing.get("requested_profile"),
                    "effective_profile": routing.get("effective_profile"),
                    **{
                        name: (
                            str(path.resolve()) if path is not None else None
                        )
                        for name, path in paths.items()
                    },
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 4 if insufficient else 0
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
