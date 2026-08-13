"""Immutable local evidence bundles for KT6 evaluation runs.

The report index stays small: every JSONL run points at one manifest, while the
manifest owns hashes and relative paths for screenshots, perception outputs,
model outputs, action traces and validation evidence.  Nothing in this module
uploads data or executes a browser/model.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shutil
import stat
import tempfile
import time
import zlib
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Mapping, Sequence

from .topology_cv_routing import (
    ROUTE_DECISIONS,
    ROUTING_SCHEMA_VERSION,
    TopologyCVRoutingError,
    prepare_cv_payload_for_route,
)
from .topology_fusion import (
    FUSION_SCHEMA_VERSION,
    TopologyFusionError,
    fuse_topology_payloads,
)
from .topology_model_contract import (
    MODEL_SCHEMA_VERSION,
    TopologyModelContract,
    TopologyModelResponseError,
)
from .topology_vision_contract import (
    RESPONSE_SCHEMA_VERSION,
    CanvasVisionResponseError,
    TopologyVisionContract,
)
from .ui_graph import SCHEMA_VERSION as UI_GRAPH_SCHEMA_VERSION


ARTIFACT_MANIFEST_SCHEMA_VERSION = "kt6.evaluation-artifact-manifest.v1"
ACTION_EVENT_SCHEMA_VERSION = "kt6.evaluation-action-event.v1"
PLANNER_CALL_SCHEMA_VERSION = "kt6.evaluation-planner-call.v1"
UI_TARS_RESPONSE_SCHEMA_VERSION = "kt6.evaluation-ui-tars-response.v1"
VISION_MODEL_CALL_SCHEMA_VERSION = "kt6.evaluation-vision-model-call.v1"
VALIDATION_RESULT_SCHEMA_VERSION = "kt6.evaluation-validation-result.v1"
BROWSER_USE_DOM_SCHEMA_VERSION = "kt6.browser-use-dom-snapshot.v1"
CDP_PAGE_SNAPSHOT_SCHEMA_VERSION = "kt6.cdp-page-snapshot.v1"
CV_METADATA_SCHEMA_VERSION = "kt6.cv-artifact-metadata.v1"

MAX_ARTIFACTS_PER_RUN = 200
MAX_ARTIFACT_BYTES = 128 * 1024 * 1024
MAX_TOTAL_ARTIFACT_BYTES = 1024 * 1024 * 1024
MAX_MANIFEST_BYTES = 4 * 1024 * 1024

_SAFE_IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,199}$")
_ARTIFACT_ID_PATTERN = re.compile(r"^[a-z][a-z0-9-]{0,199}$")
_WINDOWS_DEVICE_NAMES = frozenset(
    {"CON", "PRN", "AUX", "NUL", "CLOCK$"}
    | {f"COM{index}" for index in range(1, 10)}
    | {f"LPT{index}" for index in range(1, 10)}
)
_FILE_ATTRIBUTE_REPARSE_POINT = 0x0400

EVIDENCE_PROFILES = frozenset({"current_hybrid", "browser_use", "ui_tars"})
VISION_MODEL_CALL_STATUSES = frozenset(
    {"success", "timeout", "invalid_response", "transport_error", "error", "cancelled"}
)

ROLE_FORMATS: dict[str, frozenset[str]] = {
    "original_screenshot": frozenset({"image"}),
    "processed_screenshot": frozenset({"image"}),
    "cv_result": frozenset({"json"}),
    "cv_metadata": frozenset({"json"}),
    "model_result": frozenset({"json"}),
    "vision_model_call": frozenset({"jsonl"}),
    "planner_result": frozenset({"jsonl"}),
    "routing_result": frozenset({"json"}),
    "fused_result": frozenset({"json"}),
    "ui_graph": frozenset({"json"}),
    "dom_snapshot": frozenset({"json"}),
    "cdp_snapshot": frozenset({"json"}),
    "browser_use_elements": frozenset({"json"}),
    "ui_tars_response": frozenset({"jsonl"}),
    "ui_tars_actions": frozenset({"json", "jsonl"}),
    "action_trace": frozenset({"jsonl"}),
    "validation_result": frozenset({"json"}),
    "model_events": frozenset({"jsonl"}),
    "model_stderr": frozenset({"log"}),
    "run_metadata": frozenset({"json"}),
}

REPEATABLE_ROLES = frozenset(
    {
        "original_screenshot",
        "processed_screenshot",
        "cv_result",
        "cv_metadata",
        "model_result",
        "routing_result",
        "fused_result",
        "ui_tars_actions",
        "model_events",
        "model_stderr",
    }
)

FORMAT_SUFFIXES: dict[str, frozenset[str]] = {
    "image": frozenset({".png", ".jpg", ".jpeg", ".webp"}),
    "json": frozenset({".json"}),
    "jsonl": frozenset({".jsonl"}),
    "log": frozenset({".log", ".txt"}),
}

MEDIA_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".json": "application/json",
    ".jsonl": "application/x-ndjson",
    ".log": "text/plain",
    ".txt": "text/plain",
}


class EvaluationArtifactError(ValueError):
    """Raised when a run evidence bundle is incomplete or unsafe."""


def parse_artifact_arguments(values: Sequence[str]) -> list[tuple[str, Path]]:
    """Parse repeated ``role=path`` CLI arguments without exposing file data."""

    artifacts: list[tuple[str, Path]] = []
    for index, raw in enumerate(values, start=1):
        if not isinstance(raw, str) or "=" not in raw:
            raise EvaluationArtifactError(
                f"artifact #{index} must use role=path syntax"
            )
        role_raw, separator, path_raw = raw.partition("=")
        role = role_raw.strip()
        path_text = path_raw.strip()
        if not separator or not role or not path_text:
            raise EvaluationArtifactError(
                f"artifact #{index} must use non-empty role=path syntax"
            )
        if role not in ROLE_FORMATS or role == "run_metadata":
            allowed = ", ".join(
                sorted(item for item in ROLE_FORMATS if item != "run_metadata")
            )
            raise EvaluationArtifactError(
                f"unsupported artifact role {role!r}; allowed roles: {allowed}"
            )
        artifacts.append((role, Path(path_text).expanduser()))
    if not artifacts:
        raise EvaluationArtifactError("at least one --artifact role=path is required")
    if len(artifacts) > MAX_ARTIFACTS_PER_RUN - 1:
        raise EvaluationArtifactError(
            f"a run may contain at most {MAX_ARTIFACTS_PER_RUN - 1} input artifacts"
        )
    return artifacts


def required_role_groups(
    suite: Mapping[str, Any], run: Mapping[str, Any]
) -> list[dict[str, Any]]:
    """Return evidence requirements for the scheme, scenario and actual calls."""

    scheme = _find_item(suite.get("schemes", []), "scheme_id", run.get("scheme_id"))
    task = _find_item(suite.get("tasks", []), "task_id", run.get("task_id"))
    profile = scheme.get("evidence_profile")
    if profile not in EVIDENCE_PROFILES:
        raise EvaluationArtifactError(
            f"unsupported evidence profile for scheme {run.get('scheme_id')!r}: {profile!r}"
        )
    groups: list[dict[str, Any]] = [
        {
            "any_of": ["action_trace"],
            "reason": "replay every browser operation and timing",
        },
        {
            "any_of": ["validation_result"],
            "reason": "preserve the deterministic final task verdict",
        },
    ]
    metrics = run.get("metrics", {})
    cv_calls = _non_negative_int(metrics.get("cv_calls"), "run.metrics.cv_calls")
    vision_model_calls = _non_negative_int(
        metrics.get("vision_model_calls"), "run.metrics.vision_model_calls"
    )
    planner_model_calls = _non_negative_int(
        metrics.get("planner_model_calls"), "run.metrics.planner_model_calls"
    )
    scenario = task.get("scenario_type")

    if planner_model_calls > 0:
        groups.append(
            {
                "any_of": ["planner_result"],
                "reason": "planner model calls require their structured output",
            }
        )

    if profile == "current_hybrid":
        if run.get("outcome") == "success":
            groups.append(
                {
                    "any_of": ["ui_graph"],
                    "reason": "preserve the final multi-source UI Graph",
                }
            )
        if cv_calls > 0 or (
            run.get("outcome") == "success" and scenario in {"canvas", "mixed"}
        ):
            cv_groups = [
                    {
                        "any_of": ["original_screenshot"],
                        "reason": "preserve the pixels used for visual recognition",
                    },
                    {
                        "any_of": ["cv_result"],
                        "reason": "preserve raw OpenCV/OCR output",
                    },
                    {
                        "any_of": ["cv_metadata"],
                        "reason": "bind CV output to exact screenshot bytes and adapter",
                    },
                    {
                        "any_of": ["routing_result"],
                        "reason": "preserve the CV/model routing decision",
                    },
                ]
            if run.get("outcome") == "success":
                cv_groups.append(
                    {
                        "any_of": ["fused_result"],
                        "reason": "preserve deterministic fusion output",
                    }
                )
            groups.extend(cv_groups)
        if vision_model_calls > 0:
            groups.append(
                {
                    "any_of": ["vision_model_call"],
                    "reason": (
                        "vision model calls require per-call success/failure evidence"
                    ),
                }
            )
    elif profile == "browser_use":
        groups.append(
            {
                "any_of": ["dom_snapshot", "cdp_snapshot"],
                "reason": "preserve the DOM/CDP state used by Browser Use",
            }
        )
    elif profile == "ui_tars":
        groups.extend(
            [
                {
                    "any_of": ["original_screenshot"],
                    "reason": "preserve every UI-TARS visual observation",
                },
                {
                    "any_of": ["ui_tars_response"],
                    "reason": "preserve UI-TARS raw structured responses",
                },
            ]
        )

    custom_groups = scheme.get("required_artifact_groups", [])
    if not isinstance(custom_groups, list):
        raise EvaluationArtifactError("scheme.required_artifact_groups must be an array")
    for index, group in enumerate(custom_groups):
        if not isinstance(group, list) or not group:
            raise EvaluationArtifactError(
                f"scheme.required_artifact_groups[{index}] must be a non-empty array"
            )
        roles: list[str] = []
        for role in group:
            if not isinstance(role, str) or role not in ROLE_FORMATS:
                raise EvaluationArtifactError(
                    f"scheme.required_artifact_groups[{index}] has unsupported role"
                )
            roles.append(role)
        groups.append(
            {
                "any_of": sorted(set(roles)),
                "reason": "suite-defined evidence requirement",
            }
        )
    return _deduplicate_groups(groups)


def _role_cardinality_errors(
    suite: Mapping[str, Any],
    run: Mapping[str, Any],
    role_counts: Mapping[str, int],
) -> list[str]:
    """Require enough observations/results to substantiate reported calls."""

    scheme = _find_item(suite.get("schemes", []), "scheme_id", run.get("scheme_id"))
    profile = scheme.get("evidence_profile")
    metrics = run.get("metrics", {})
    cv_calls = _non_negative_int(metrics.get("cv_calls"), "run.metrics.cv_calls")
    vision_calls = _non_negative_int(
        metrics.get("vision_model_calls"), "run.metrics.vision_model_calls"
    )
    step_count = _non_negative_int(run.get("step_count"), "run.step_count")
    errors: list[str] = []

    def require_count(role: str, minimum: int, reason: str) -> None:
        actual = int(role_counts.get(role, 0))
        if actual < minimum:
            errors.append(f"{role} requires at least {minimum}, found {actual} ({reason})")

    def require_exact(role: str, expected: int, reason: str) -> None:
        actual = int(role_counts.get(role, 0))
        if actual != expected:
            errors.append(f"{role} requires exactly {expected}, found {actual} ({reason})")

    if profile == "current_hybrid" and cv_calls > 0:
        require_exact("original_screenshot", cv_calls, "one input image per CV call")
        require_exact("cv_result", cv_calls, "one raw result per CV call")
        require_exact("cv_metadata", cv_calls, "one binding record per CV call")
        require_exact("routing_result", cv_calls, "one routing decision per CV call")
        if run.get("outcome") == "success":
            require_exact("fused_result", cv_calls, "one final fusion result per CV call")
        elif int(role_counts.get("fused_result", 0)) > cv_calls:
            errors.append("fused_result cannot outnumber CV calls")
    if profile == "current_hybrid":
        require_exact(
            "vision_model_call",
            1 if vision_calls > 0 else 0,
            "one JSONL envelope covers all vision model calls",
        )
        if int(role_counts.get("model_result", 0)) > vision_calls:
            errors.append("model_result cannot outnumber vision model calls")
    if profile == "ui_tars":
        require_exact(
            "ui_tars_response",
            1,
            "one JSONL envelope covers all UI-TARS model calls",
        )
        require_exact(
            "original_screenshot",
            vision_calls,
            "one artifact-identified observation per UI-TARS model call",
        )
        if vision_calls < max(1, step_count):
            errors.append("UI-TARS vision model calls must cover every reported step")
    return errors


def archive_run_evidence(
    evaluation_root: Path,
    suite: Mapping[str, Any],
    run: Mapping[str, Any],
    artifact_sources: Sequence[tuple[str, Path]],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Copy one run's evidence into an immutable canonical directory.

    The final directory is created by atomic rename and never overwritten.  The
    returned run contains only an index entry for the manifest, not source paths
    or embedded screenshots/model output.
    """

    archive_started_at = time.perf_counter()
    root = _prepare_evaluation_root(evaluation_root, create=True)
    _validate_run_identifiers(run)
    final_dir = _canonical_run_dir(root, run)
    _assert_below_root(root, final_dir, "evidence directory")
    if _path_lexists(final_dir):
        raise EvaluationArtifactError(
            f"refusing to overwrite existing evidence directory: {final_dir}"
        )
    final_dir.parent.mkdir(parents=True, exist_ok=True)
    _assert_no_reparse_components(
        final_dir.parent,
        "evidence directory parent",
    )
    if _path_lexists(final_dir):
        raise EvaluationArtifactError(
            f"refusing to overwrite existing evidence directory: {final_dir}"
        )

    if not artifact_sources:
        raise EvaluationArtifactError("run evidence must contain input artifacts")
    if len(artifact_sources) > MAX_ARTIFACTS_PER_RUN - 1:
        raise EvaluationArtifactError("run evidence contains too many artifacts")

    normalized_sources = _validate_sources(artifact_sources)
    role_counts = Counter(role for role, _ in normalized_sources)
    repeated_invalid = sorted(
        role
        for role, count in role_counts.items()
        if count > 1 and role not in REPEATABLE_ROLES
    )
    if repeated_invalid:
        raise EvaluationArtifactError(
            "non-repeatable artifact roles supplied more than once: "
            + ", ".join(repeated_invalid)
        )

    requirements = required_role_groups(suite, run)
    present_roles = set(role_counts)
    missing = [
        group
        for group in requirements
        if not present_roles.intersection(group["any_of"])
    ]
    if missing:
        rendered = "; ".join(
            f"one of {','.join(group['any_of'])} ({group['reason']})"
            for group in missing
        )
        raise EvaluationArtifactError(f"run evidence is incomplete: {rendered}")
    cardinality_errors = _role_cardinality_errors(suite, run, role_counts)
    if cardinality_errors:
        raise EvaluationArtifactError(
            "run evidence has insufficient per-step/per-call artifacts: "
            + "; ".join(cardinality_errors)
        )

    temporary_dir: Path | None = Path(
        tempfile.mkdtemp(prefix=f".{final_dir.name}.", dir=final_dir.parent)
    )
    try:
        artifacts: list[dict[str, Any]] = []
        total_bytes = 0
        role_sequences: Counter[str] = Counter()
        for role, source in normalized_sources:
            role_sequences[role] += 1
            suffix = source.suffix.casefold()
            target_name = (
                f"{role.replace('_', '-')}-{role_sequences[role]:03d}{suffix}"
            )
            target = temporary_dir / target_name
            artifact = _copy_and_describe(
                source,
                target,
                role=role,
                relative_base=temporary_dir,
                run=run,
            )
            total_bytes += artifact["size_bytes"]
            if total_bytes > MAX_TOTAL_ARTIFACT_BYTES:
                raise EvaluationArtifactError(
                    f"run evidence exceeds {MAX_TOTAL_ARTIFACT_BYTES} total bytes"
                )
            artifacts.append(artifact)

        _validate_bundle_contracts(
            artifacts,
            base_dir=temporary_dir,
            run=run,
        )

        run_metadata_path = temporary_dir / "run-metadata-001.json"
        run_metadata = _run_metadata_payload(run)
        _write_new_json(run_metadata_path, run_metadata)
        metadata_artifact = _describe_existing_file(
            run_metadata_path,
            role="run_metadata",
            relative_base=temporary_dir,
            source_name="generated-run-metadata.json",
        )
        total_bytes += metadata_artifact["size_bytes"]
        if total_bytes > MAX_TOTAL_ARTIFACT_BYTES:
            raise EvaluationArtifactError(
                f"run evidence exceeds {MAX_TOTAL_ARTIFACT_BYTES} total bytes"
            )
        artifacts.append(metadata_artifact)

        created_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        archive_duration_ms = max(
            0,
            round((time.perf_counter() - archive_started_at) * 1000),
        )
        manifest = {
            "schema_version": ARTIFACT_MANIFEST_SCHEMA_VERSION,
            "created_at": created_at,
            "run": {
                "run_id": run["run_id"],
                "suite_id": run["suite_id"],
                "scheme_id": run["scheme_id"],
                "task_id": run["task_id"],
                "repetition": run["repetition"],
                "outcome": run["outcome"],
            },
            "implementation": dict(run["implementation"]),
            "planner": dict(run["planner"]),
            "task_prompt_version": run["task_prompt_version"],
            "environment": dict(run["environment"]),
            "storage_policy": {
                "scope": "test_area_only",
                "contains_sensitive_page_data": True,
                "git_allowed": False,
                "external_upload_allowed": False,
            },
            "requirements": requirements,
            "present_roles": sorted(present_roles),
            "missing_required_groups": [],
            "complete": True,
            "artifact_count": len(artifacts),
            "total_bytes": total_bytes,
            "archive_duration_ms": archive_duration_ms,
            "artifacts": artifacts,
        }
        manifest_path = temporary_dir / "manifest.json"
        _write_new_json(manifest_path, manifest)
        manifest_sha256 = _sha256_file(
            manifest_path,
            max_bytes=MAX_MANIFEST_BYTES,
        )

        if _path_lexists(final_dir):
            raise EvaluationArtifactError(
                f"refusing to overwrite existing evidence directory: {final_dir}"
            )
        try:
            os.rename(temporary_dir, final_dir)
        except OSError as exc:
            raise EvaluationArtifactError(
                f"cannot publish evidence directory without overwrite: {final_dir}"
            ) from exc
        temporary_dir = None

        manifest_ref = (final_dir / "manifest.json").relative_to(root).as_posix()
        archived_run = dict(run)
        archived_run["evidence"] = {
            "status": "archived",
            "manifest_ref": manifest_ref,
            "manifest_sha256": manifest_sha256,
            "artifact_count": len(artifacts),
            "total_bytes": total_bytes,
            "archive_duration_ms": archive_duration_ms,
            "complete": True,
        }
        return archived_run, manifest
    except Exception:
        if temporary_dir is not None and _path_lexists(temporary_dir):
            try:
                _assert_below_root(
                    final_dir.parent,
                    temporary_dir,
                    "temporary evidence directory",
                )
                if not _is_reparse_point(temporary_dir):
                    shutil.rmtree(temporary_dir, ignore_errors=True)
            except (OSError, EvaluationArtifactError):
                # Cleanup is best effort.  Never broaden it when a path is unsafe.
                pass
        raise


def verify_run_evidence(
    evaluation_root: Path,
    suite: Mapping[str, Any],
    run: Mapping[str, Any],
) -> dict[str, Any]:
    """Re-hash every file referenced by a run manifest and verify its identity."""

    errors: list[str] = []
    try:
        root = _prepare_evaluation_root(evaluation_root, create=False)
        _validate_run_identifiers(run)
        expected_manifest_path = _canonical_run_dir(root, run) / "manifest.json"
        expected_manifest_ref = expected_manifest_path.relative_to(root).as_posix()
    except (OSError, EvaluationArtifactError) as exc:
        return _verification_failure(run, [str(exc)])
    evidence = run.get("evidence")
    if not isinstance(evidence, Mapping) or evidence.get("status") != "archived":
        return _verification_failure(run, ["run evidence is not archived"])
    if evidence.get("complete") is not True:
        errors.append("runs.jsonl evidence is not marked complete")
    manifest_ref = evidence.get("manifest_ref")
    if not isinstance(manifest_ref, str) or not manifest_ref:
        return _verification_failure(run, ["manifest_ref is missing"])
    if manifest_ref != expected_manifest_ref:
        errors.append("manifest_ref does not use the canonical run directory")
    try:
        manifest_path = _resolve_relative_ref(root, manifest_ref, "manifest_ref")
        if manifest_path != expected_manifest_path:
            errors.append("manifest_ref does not use the canonical run directory")
        manifest_bytes = _read_limited_bytes(
            manifest_path,
            max_bytes=MAX_MANIFEST_BYTES,
            field="manifest",
        )
    except (OSError, EvaluationArtifactError) as exc:
        return _verification_failure(run, [str(exc)])
    actual_manifest_sha256 = hashlib.sha256(manifest_bytes).hexdigest()
    if actual_manifest_sha256 != evidence.get("manifest_sha256"):
        errors.append("manifest SHA-256 does not match runs.jsonl")
    try:
        manifest = _strict_json_loads(manifest_bytes.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError, RecursionError):
        return _verification_failure(run, [*errors, "manifest is not valid UTF-8 JSON"])
    if not isinstance(manifest, dict):
        return _verification_failure(run, [*errors, "manifest root is not an object"])
    if manifest.get("schema_version") != ARTIFACT_MANIFEST_SCHEMA_VERSION:
        errors.append("unsupported artifact manifest schema_version")

    identity = manifest.get("run")
    if not isinstance(identity, Mapping):
        errors.append("manifest.run is not an object")
    else:
        for field in ("run_id", "suite_id", "scheme_id", "task_id", "repetition", "outcome"):
            if identity.get(field) != run.get(field):
                errors.append(f"manifest run identity mismatch: {field}")
    if manifest.get("complete") is not True:
        errors.append("manifest is not marked complete")
    archive_duration_ms = manifest.get("archive_duration_ms")
    if (
        isinstance(archive_duration_ms, bool)
        or not isinstance(archive_duration_ms, int)
        or archive_duration_ms < 0
    ):
        errors.append("manifest archive_duration_ms must be a non-negative integer")
    if archive_duration_ms != evidence.get("archive_duration_ms"):
        errors.append("runs.jsonl archive_duration_ms does not match manifest")
    if manifest.get("implementation") != run.get("implementation"):
        errors.append("manifest implementation does not match run")
    if manifest.get("planner") != run.get("planner"):
        errors.append("manifest planner does not match run")
    if manifest.get("task_prompt_version") != run.get("task_prompt_version"):
        errors.append("manifest task_prompt_version does not match run")
    if manifest.get("environment") != run.get("environment"):
        errors.append("manifest environment does not match run")

    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, list) or not artifacts:
        errors.append("manifest.artifacts must be a non-empty array")
        artifacts = []
    if len(artifacts) > MAX_ARTIFACTS_PER_RUN:
        errors.append("manifest contains too many artifacts")

    present_roles: set[str] = set()
    role_counts: Counter[str] = Counter()
    artifact_ids: set[str] = set()
    artifact_paths: set[str] = set()
    total_bytes = 0
    run_dir = expected_manifest_path.parent
    for index, artifact in enumerate(artifacts):
        prefix = f"manifest.artifacts[{index}]"
        if not isinstance(artifact, Mapping):
            errors.append(f"{prefix} is not an object")
            continue
        role = artifact.get("role")
        if role not in ROLE_FORMATS:
            errors.append(f"{prefix}.role is unsupported")
            continue
        present_roles.add(str(role))
        role_counts[str(role)] += 1
        artifact_id = artifact.get("artifact_id")
        if not isinstance(artifact_id, str) or not artifact_id:
            errors.append(f"{prefix}.artifact_id is missing")
        elif artifact_id in artifact_ids:
            errors.append(f"{prefix}.artifact_id is duplicated")
        else:
            artifact_ids.add(artifact_id)
        relative_path = artifact.get("path")
        if not isinstance(relative_path, str) or not relative_path:
            errors.append(f"{prefix}.path is missing")
            continue
        if relative_path in artifact_paths:
            errors.append(f"{prefix}.path is duplicated")
            continue
        artifact_paths.add(relative_path)
        try:
            file_path = _resolve_relative_ref(run_dir, relative_path, f"{prefix}.path")
            _assert_below_root(run_dir, file_path, f"{prefix}.path")
            file_stat = file_path.stat()
            if not file_path.is_file():
                raise EvaluationArtifactError(f"{prefix}.path is not a file")
            if file_path == expected_manifest_path:
                raise EvaluationArtifactError(
                    f"{prefix}.path must not reference the manifest itself"
                )
            size = file_stat.st_size
            if size != artifact.get("size_bytes"):
                errors.append(f"{prefix} size does not match manifest")
            if size > MAX_ARTIFACT_BYTES:
                errors.append(f"{prefix} exceeds the per-file size limit")
            total_bytes += size
            if total_bytes > MAX_TOTAL_ARTIFACT_BYTES:
                errors.append("manifest files exceed the total size limit")
                break
            expected_media_type = MEDIA_TYPES.get(file_path.suffix.casefold())
            if artifact.get("media_type") != expected_media_type:
                errors.append(f"{prefix}.media_type does not match its extension")
            _detect_format(file_path, str(role))
            digest = _sha256_file(file_path, max_bytes=MAX_ARTIFACT_BYTES)
            if digest != artifact.get("sha256"):
                errors.append(f"{prefix} SHA-256 does not match manifest")
            _validate_archived_content(file_path, str(role), run=run)
        except (OSError, EvaluationArtifactError) as exc:
            errors.append(str(exc))

    if total_bytes != manifest.get("total_bytes"):
        errors.append("manifest total_bytes does not match archived files")
    if len(artifacts) != manifest.get("artifact_count"):
        errors.append("manifest artifact_count does not match artifacts")
    if len(artifacts) != evidence.get("artifact_count"):
        errors.append("runs.jsonl artifact_count does not match manifest")
    if total_bytes != evidence.get("total_bytes"):
        errors.append("runs.jsonl total_bytes does not match manifest")

    repeated_invalid = sorted(
        role
        for role, count in role_counts.items()
        if count > 1 and role not in REPEATABLE_ROLES
    )
    if repeated_invalid:
        errors.append(
            "manifest repeats non-repeatable roles: " + ", ".join(repeated_invalid)
        )
    errors.extend(_role_cardinality_errors(suite, run, role_counts))
    if role_counts.get("run_metadata") != 1:
        errors.append("manifest must contain exactly one run_metadata artifact")
    try:
        _validate_bundle_contracts(
            artifacts,
            base_dir=run_dir,
            run=run,
        )
    except EvaluationArtifactError as exc:
        errors.append(str(exc))

    try:
        requirements = required_role_groups(suite, run)
        if manifest.get("requirements") != requirements:
            errors.append("manifest requirements do not match the current suite/run")
        manifest_present_roles = manifest.get("present_roles")
        expected_present_roles = sorted(present_roles - {"run_metadata"})
        if manifest_present_roles != expected_present_roles:
            errors.append("manifest present_roles does not match archived artifacts")
        if manifest.get("missing_required_groups") != []:
            errors.append("manifest missing_required_groups must be empty")
        missing = [
            group
            for group in requirements
            if not present_roles.intersection(group["any_of"])
        ]
        if missing:
            errors.append(
                "required artifact roles are missing: "
                + "; ".join("/".join(group["any_of"]) for group in missing)
            )
    except EvaluationArtifactError as exc:
        errors.append(str(exc))

    return {
        "run_id": run.get("run_id"),
        "verified": not errors,
        "manifest_ref": manifest_ref,
        "manifest_sha256": actual_manifest_sha256,
        "artifact_count": len(artifacts),
        "total_bytes": total_bytes,
        "present_roles": sorted(present_roles),
        "errors": errors,
    }


def remove_new_evidence_archive(evaluation_root: Path, run: Mapping[str, Any]) -> None:
    """Rollback only the exact canonical directory created for this run."""

    root = _prepare_evaluation_root(evaluation_root, create=False)
    _validate_run_identifiers(run)
    target = _canonical_run_dir(root, run)
    _assert_below_root(root / "artifacts", target, "evidence rollback directory")
    if not _path_lexists(target):
        return
    _assert_no_reparse_components(target, "evidence rollback directory")
    if not target.is_dir():
        raise EvaluationArtifactError(
            "evidence rollback target is not a directory"
        )
    _assert_no_reparse_tree(target, "evidence rollback directory")
    shutil.rmtree(target)


def _validate_sources(
    artifact_sources: Sequence[tuple[str, Path]],
) -> list[tuple[str, Path]]:
    normalized: list[tuple[str, Path]] = []
    total_bytes = 0
    source_paths: set[str] = set()
    source_identities: set[tuple[int, int]] = set()
    for index, item in enumerate(artifact_sources, start=1):
        if not isinstance(item, tuple) or len(item) != 2:
            raise EvaluationArtifactError(f"artifact source #{index} is invalid")
        role, raw_path = item
        if role not in ROLE_FORMATS or role == "run_metadata":
            raise EvaluationArtifactError(f"unsupported artifact role: {role!r}")
        path = _absolute_lexical_path(Path(raw_path).expanduser())
        _assert_no_reparse_components(path, "artifact source")
        try:
            source_stat = path.stat()
        except OSError as exc:
            raise EvaluationArtifactError(
                f"cannot read artifact source: {path.name}"
            ) from exc
        if not path.is_file():
            raise EvaluationArtifactError(
                f"artifact source is not a file: {path.name}"
            )
        path_key = os.path.normcase(os.fspath(path))
        identity = (
            int(getattr(source_stat, "st_dev", 0)),
            int(getattr(source_stat, "st_ino", 0)),
        )
        if path_key in source_paths or (
            identity != (0, 0) and identity in source_identities
        ):
            raise EvaluationArtifactError(
                f"the same source file cannot satisfy multiple artifact roles: {path.name}"
            )
        source_paths.add(path_key)
        if identity != (0, 0):
            source_identities.add(identity)
        if source_stat.st_size <= 0:
            raise EvaluationArtifactError(f"artifact source is empty: {path.name}")
        if source_stat.st_size > MAX_ARTIFACT_BYTES:
            raise EvaluationArtifactError(
                f"artifact source exceeds {MAX_ARTIFACT_BYTES} bytes: {path.name}"
            )
        total_bytes += source_stat.st_size
        if total_bytes > MAX_TOTAL_ARTIFACT_BYTES:
            raise EvaluationArtifactError("artifact sources exceed the total size limit")
        _detect_format(path, role)
        normalized.append((role, path))
    return normalized


def _copy_and_describe(
    source: Path,
    target: Path,
    *,
    role: str,
    relative_base: Path,
    run: Mapping[str, Any],
) -> dict[str, Any]:
    _assert_no_reparse_components(source, "artifact source")
    before = source.stat()
    digest = hashlib.sha256()
    size = 0
    try:
        with source.open("rb") as source_stream, target.open("xb") as target_stream:
            opened = os.fstat(source_stream.fileno())
            if not _same_file_identity(before, opened):
                raise EvaluationArtifactError(
                    f"artifact source changed before copying: {source.name}"
                )
            while True:
                chunk = source_stream.read(1024 * 1024)
                if not chunk:
                    break
                size += len(chunk)
                if size > MAX_ARTIFACT_BYTES:
                    raise EvaluationArtifactError(
                        f"artifact grew beyond size limit while copying: {source.name}"
                    )
                digest.update(chunk)
                target_stream.write(chunk)
    except OSError as exc:
        raise EvaluationArtifactError(
            f"cannot archive artifact source: {source.name}"
        ) from exc
    after = source.stat()
    if (
        not _same_file_identity(before, after)
        or before.st_size != after.st_size
        or before.st_mtime_ns != after.st_mtime_ns
        or size != before.st_size
    ):
        raise EvaluationArtifactError(
            f"artifact source changed while being archived: {source.name}"
        )
    _validate_archived_content(target, role, run=run)
    return {
        "artifact_id": (
            f"{role.replace('_', '-')}-{target.stem.rsplit('-', 1)[-1]}"
        ),
        "role": role,
        "path": target.relative_to(relative_base).as_posix(),
        "source_name": source.name[:255],
        "media_type": MEDIA_TYPES[target.suffix.casefold()],
        "size_bytes": size,
        "sha256": digest.hexdigest(),
        "sensitive": True,
    }


def _describe_existing_file(
    path: Path,
    *,
    role: str,
    relative_base: Path,
    source_name: str,
) -> dict[str, Any]:
    return {
        "artifact_id": "run-metadata-001",
        "role": role,
        "path": path.relative_to(relative_base).as_posix(),
        "source_name": source_name,
        "media_type": MEDIA_TYPES[path.suffix.casefold()],
        "size_bytes": path.stat().st_size,
        "sha256": _sha256_file(path),
        "sensitive": True,
    }


def _validate_archived_content(
    path: Path,
    role: str,
    *,
    run: Mapping[str, Any],
) -> None:
    file_format = _detect_format(path, role)
    if file_format == "image":
        try:
            raw = _read_limited_bytes(
                path,
                max_bytes=MAX_ARTIFACT_BYTES,
                field=f"{role} image",
            )
            mime_type = MEDIA_TYPES[path.suffix.casefold()]
            width, height = TopologyVisionContract.image_dimensions(raw, mime_type)
            if (
                width > 100_000
                or height > 100_000
                or width * height > TopologyVisionContract.MAX_IMAGE_PIXELS
            ):
                raise ValueError("image dimensions exceed the evaluation limit")
            _validate_image_container(raw, mime_type)
        except (ValueError, zlib.error) as exc:
            raise EvaluationArtifactError(
                "artifact has invalid image dimensions or an incomplete image body: "
                f"{path.name}"
            ) from exc
    elif file_format == "json":
        try:
            payload = _strict_json_loads(path.read_text(encoding="utf-8-sig"))
        except (
            OSError,
            UnicodeError,
            json.JSONDecodeError,
            ValueError,
            RecursionError,
        ) as exc:
            raise EvaluationArtifactError(
                f"artifact is not valid UTF-8 JSON: {path.name}"
            ) from exc
        if not isinstance(payload, (dict, list)):
            raise EvaluationArtifactError(
                f"artifact JSON root must be an object or array: {path.name}"
            )
        if role == "validation_result":
            _validate_validation_result_payload(payload, run, path.name)
        elif role == "run_metadata":
            if payload != _run_metadata_payload(run):
                raise EvaluationArtifactError(
                    f"run metadata does not match the indexed run: {path.name}"
                )
        else:
            _validate_json_role_payload(payload, role, run, path)
    elif file_format == "jsonl":
        try:
            stream = path.open("r", encoding="utf-8-sig", newline=None)
        except (OSError, UnicodeError) as exc:
            raise EvaluationArtifactError(
                f"artifact is not valid UTF-8 JSONL: {path.name}"
            ) from exc
        parsed_count = 0
        parsed_records: list[dict[str, Any]] = []
        try:
            with stream:
                for line_number, line in enumerate(stream, start=1):
                    if not line.strip():
                        continue
                    try:
                        payload = _strict_json_loads(line)
                    except (json.JSONDecodeError, ValueError, RecursionError) as exc:
                        raise EvaluationArtifactError(
                            f"invalid JSONL artifact at {path.name}:{line_number}"
                        ) from exc
                    if not isinstance(payload, dict):
                        raise EvaluationArtifactError(
                            "JSONL artifact line must be an object: "
                            f"{path.name}:{line_number}"
                        )
                    parsed_count += 1
                    if role in {
                        "action_trace",
                        "planner_result",
                        "ui_tars_response",
                        "vision_model_call",
                    }:
                        parsed_records.append(payload)
        except UnicodeError as exc:
            raise EvaluationArtifactError(
                f"artifact is not valid UTF-8 JSONL: {path.name}"
            ) from exc
        if parsed_count == 0:
            raise EvaluationArtifactError(f"JSONL artifact has no records: {path.name}")
        if role == "action_trace":
            _validate_action_trace_records(parsed_records, run, path.name)
        elif role == "planner_result":
            _validate_planner_result_records(parsed_records, run, path.name)
        elif role == "ui_tars_response":
            _validate_ui_tars_response_records(parsed_records, run, path.name)
        elif role == "vision_model_call":
            _validate_vision_model_call_records(parsed_records, run, path.name)


def _validate_action_trace_records(
    records: Sequence[Mapping[str, Any]],
    run: Mapping[str, Any],
    source_name: str,
) -> None:
    """Bind an action trace to its run and cover every reported task step."""

    run_id = run.get("run_id")
    step_count = run.get("step_count")
    if isinstance(step_count, bool) or not isinstance(step_count, int) or step_count < 0:
        raise EvaluationArtifactError("run.step_count must be a non-negative integer")
    observed_steps: set[int] = set()
    safety_violation_count = 0
    for line_number, record in enumerate(records, start=1):
        if record.get("schema_version") != ACTION_EVENT_SCHEMA_VERSION:
            raise EvaluationArtifactError(
                f"action_trace.schema_version must be {ACTION_EVENT_SCHEMA_VERSION}: "
                f"{source_name}:{line_number}"
            )
        if record.get("run_id") != run_id:
            raise EvaluationArtifactError(
                f"action_trace.run_id does not match run: {source_name}:{line_number}"
            )
        if not any(
            isinstance(record.get(field), str) and record.get(field).strip()
            for field in ("action", "event_type")
        ):
            raise EvaluationArtifactError(
                f"action_trace requires action or event_type: {source_name}:{line_number}"
            )
        safety_violation = record.get("safety_violation")
        if not isinstance(safety_violation, bool):
            raise EvaluationArtifactError(
                f"action_trace.safety_violation must be boolean: "
                f"{source_name}:{line_number}"
            )
        safety_violation_count += int(safety_violation)
        step_index = record.get("step_index")
        if step_index is None and step_count == 0:
            continue
        if (
            isinstance(step_index, bool)
            or not isinstance(step_index, int)
            or step_index < 1
            or step_index > step_count
        ):
            raise EvaluationArtifactError(
                f"action_trace.step_index is outside run.step_count: "
                f"{source_name}:{line_number}"
            )
        observed_steps.add(step_index)
    expected_steps = set(range(1, step_count + 1))
    if observed_steps != expected_steps:
        missing = sorted(expected_steps - observed_steps)
        raise EvaluationArtifactError(
            f"action_trace does not cover every run step; missing {missing}: {source_name}"
        )
    metrics = run.get("metrics")
    if not isinstance(metrics, Mapping):
        raise EvaluationArtifactError("run.metrics must be an object")
    expected_safety_violations = _non_negative_int(
        metrics.get("safety_violation_count"),
        "run.metrics.safety_violation_count",
    )
    if safety_violation_count != expected_safety_violations:
        raise EvaluationArtifactError(
            "action_trace safety violations do not match "
            "run.metrics.safety_violation_count"
        )


def _validate_planner_result_records(
    records: Sequence[Mapping[str, Any]],
    run: Mapping[str, Any],
    source_name: str,
) -> None:
    metrics = run.get("metrics")
    if not isinstance(metrics, Mapping):
        raise EvaluationArtifactError("run.metrics must be an object")
    expected_calls = _non_negative_int(
        metrics.get("planner_model_calls"),
        "run.metrics.planner_model_calls",
    )
    observed_calls: set[int] = set()
    for line_number, record in enumerate(records, start=1):
        if record.get("schema_version") != PLANNER_CALL_SCHEMA_VERSION:
            raise EvaluationArtifactError(
                f"planner_result.schema_version must be {PLANNER_CALL_SCHEMA_VERSION}: "
                f"{source_name}:{line_number}"
            )
        if record.get("run_id") != run.get("run_id"):
            raise EvaluationArtifactError(
                f"planner_result.run_id does not match run: {source_name}:{line_number}"
            )
        call_index = record.get("call_index")
        if (
            isinstance(call_index, bool)
            or not isinstance(call_index, int)
            or call_index < 1
            or call_index > expected_calls
        ):
            raise EvaluationArtifactError(
                f"planner_result.call_index is outside planner_model_calls: "
                f"{source_name}:{line_number}"
            )
        if call_index in observed_calls:
            raise EvaluationArtifactError(
                f"planner_result.call_index is duplicated: {source_name}:{line_number}"
            )
        observed_calls.add(call_index)
        producer = record.get("producer")
        if not isinstance(producer, Mapping) or (
            producer.get("provider") != run.get("planner", {}).get("provider")
            or producer.get("model") != run.get("planner", {}).get("model")
        ):
            raise EvaluationArtifactError(
                f"planner_result.producer does not match planner: "
                f"{source_name}:{line_number}"
            )
        response = record.get("response")
        if not isinstance(response, (Mapping, list)) or len(response) == 0:
            raise EvaluationArtifactError(
                f"planner_result.response must be a non-empty object or array: "
                f"{source_name}:{line_number}"
            )
        input_refs = record.get("input_refs")
        if (
            not isinstance(input_refs, list)
            or not input_refs
            or any(not isinstance(item, str) or not item.strip() for item in input_refs)
        ):
            raise EvaluationArtifactError(
                f"planner_result.input_refs must be a non-empty string array: "
                f"{source_name}:{line_number}"
            )
        if len(set(input_refs)) != len(input_refs):
            raise EvaluationArtifactError(
                f"planner_result.input_refs must not repeat values: "
                f"{source_name}:{line_number}"
            )
    expected = set(range(1, expected_calls + 1))
    if observed_calls != expected:
        raise EvaluationArtifactError(
            "planner_result does not cover every planner model call; "
            f"missing {sorted(expected - observed_calls)}: {source_name}"
        )


def _validate_ui_tars_response_records(
    records: Sequence[Mapping[str, Any]],
    run: Mapping[str, Any],
    source_name: str,
) -> None:
    step_count = _non_negative_int(run.get("step_count"), "run.step_count")
    metrics = run.get("metrics")
    if not isinstance(metrics, Mapping):
        raise EvaluationArtifactError("run.metrics must be an object")
    expected_calls = _non_negative_int(
        metrics.get("vision_model_calls"), "run.metrics.vision_model_calls"
    )
    if expected_calls < 1:
        raise EvaluationArtifactError(
            "ui_tars_response requires run.metrics.vision_model_calls >= 1"
        )
    observed_steps: set[int] = set()
    observed_calls: set[int] = set()
    expected_steps = set(range(1, step_count + 1)) if step_count else {0}
    for line_number, record in enumerate(records, start=1):
        if record.get("schema_version") != UI_TARS_RESPONSE_SCHEMA_VERSION:
            raise EvaluationArtifactError(
                f"ui_tars_response.schema_version must be "
                f"{UI_TARS_RESPONSE_SCHEMA_VERSION}: {source_name}:{line_number}"
            )
        if record.get("run_id") != run.get("run_id"):
            raise EvaluationArtifactError(
                f"ui_tars_response.run_id does not match run: "
                f"{source_name}:{line_number}"
            )
        step_index = record.get("step_index")
        if (
            isinstance(step_index, bool)
            or not isinstance(step_index, int)
            or step_index not in expected_steps
        ):
            raise EvaluationArtifactError(
                f"ui_tars_response.step_index is outside run.step_count: "
                f"{source_name}:{line_number}"
            )
        response = record.get("response")
        if not isinstance(response, (Mapping, list)) or len(response) == 0:
            raise EvaluationArtifactError(
                f"ui_tars_response.response must be a non-empty object or array: "
                f"{source_name}:{line_number}"
            )
        call_index = record.get("call_index")
        if (
            isinstance(call_index, bool)
            or not isinstance(call_index, int)
            or call_index < 1
            or call_index > expected_calls
            or call_index in observed_calls
        ):
            raise EvaluationArtifactError(
                f"ui_tars_response.call_index is invalid or duplicated: "
                f"{source_name}:{line_number}"
            )
        producer = record.get("producer")
        if not isinstance(producer, Mapping):
            raise EvaluationArtifactError(
                f"ui_tars_response.producer must be an object: "
                f"{source_name}:{line_number}"
            )
        for field in ("provider", "model"):
            _require_non_empty_text(
                producer.get(field),
                f"ui_tars_response.producer.{field}",
                f"{source_name}:{line_number}",
            )
        _require_sha256(
            record.get("screenshot_sha256"),
            "ui_tars_response.screenshot_sha256",
            f"{source_name}:{line_number}",
        )
        _require_artifact_id(
            record.get("screenshot_artifact_id"),
            "ui_tars_response.screenshot_artifact_id",
            f"{source_name}:{line_number}",
        )
        observed_steps.add(step_index)
        observed_calls.add(call_index)
    if observed_steps != expected_steps:
        raise EvaluationArtifactError(
            "ui_tars_response does not cover every run step; "
            f"missing {sorted(expected_steps - observed_steps)}: {source_name}"
        )
    expected_call_indexes = set(range(1, expected_calls + 1))
    if observed_calls != expected_call_indexes:
        raise EvaluationArtifactError(
            "ui_tars_response does not cover every vision model call; "
            f"missing {sorted(expected_call_indexes - observed_calls)}: {source_name}"
        )


def _validate_vision_model_call_records(
    records: Sequence[Mapping[str, Any]],
    run: Mapping[str, Any],
    source_name: str,
) -> None:
    """Validate a current-scheme model call ledger, including failed calls."""

    metrics = run.get("metrics")
    if not isinstance(metrics, Mapping):
        raise EvaluationArtifactError("run.metrics must be an object")
    expected_calls = _non_negative_int(
        metrics.get("vision_model_calls"), "run.metrics.vision_model_calls"
    )
    if expected_calls < 1:
        raise EvaluationArtifactError(
            "vision_model_call requires run.metrics.vision_model_calls >= 1"
        )
    observed_calls: set[int] = set()
    for line_number, record in enumerate(records, start=1):
        context = f"{source_name}:{line_number}"
        if record.get("schema_version") != VISION_MODEL_CALL_SCHEMA_VERSION:
            raise EvaluationArtifactError(
                "vision_model_call.schema_version must be "
                f"{VISION_MODEL_CALL_SCHEMA_VERSION}: {context}"
            )
        if record.get("run_id") != run.get("run_id"):
            raise EvaluationArtifactError(
                f"vision_model_call.run_id does not match run: {context}"
            )
        call_index = record.get("call_index")
        if (
            isinstance(call_index, bool)
            or not isinstance(call_index, int)
            or call_index < 1
            or call_index > expected_calls
            or call_index in observed_calls
        ):
            raise EvaluationArtifactError(
                f"vision_model_call.call_index is invalid or duplicated: {context}"
            )
        observed_calls.add(call_index)
        producer = record.get("producer")
        if not isinstance(producer, Mapping):
            raise EvaluationArtifactError(
                f"vision_model_call.producer must be an object: {context}"
            )
        for field in ("provider", "model"):
            _require_non_empty_text(
                producer.get(field), f"vision_model_call.producer.{field}", context
            )
        status = record.get("status")
        if status not in VISION_MODEL_CALL_STATUSES:
            raise EvaluationArtifactError(
                f"vision_model_call.status is invalid: {context}"
            )
        duration_ms = record.get("duration_ms")
        if (
            isinstance(duration_ms, bool)
            or not isinstance(duration_ms, (int, float))
            or not math.isfinite(float(duration_ms))
            or duration_ms < 0
        ):
            raise EvaluationArtifactError(
                f"vision_model_call.duration_ms must be non-negative: {context}"
            )
        _require_artifact_id(
            record.get("screenshot_artifact_id"),
            "vision_model_call.screenshot_artifact_id",
            context,
        )
        _require_sha256(
            record.get("screenshot_sha256"),
            "vision_model_call.screenshot_sha256",
            context,
        )
        _require_artifact_id(
            record.get("routing_artifact_id"),
            "vision_model_call.routing_artifact_id",
            context,
        )
        diagnostic_ids = record.get("diagnostic_artifact_ids", [])
        if (
            not isinstance(diagnostic_ids, list)
            or any(
                not isinstance(item, str) or not _ARTIFACT_ID_PATTERN.fullmatch(item)
                for item in diagnostic_ids
            )
            or len(diagnostic_ids) != len(set(diagnostic_ids))
        ):
            raise EvaluationArtifactError(
                f"vision_model_call.diagnostic_artifact_ids is invalid: {context}"
            )
        if status == "success":
            _require_artifact_id(
                record.get("model_result_artifact_id"),
                "vision_model_call.model_result_artifact_id",
                context,
            )
            _require_sha256(
                record.get("model_result_sha256"),
                "vision_model_call.model_result_sha256",
                context,
            )
            if record.get("error_code") not in (None, ""):
                raise EvaluationArtifactError(
                    f"successful vision_model_call cannot contain error_code: {context}"
                )
        else:
            if record.get("model_result_artifact_id") is not None or record.get(
                "model_result_sha256"
            ) is not None:
                raise EvaluationArtifactError(
                    f"failed vision_model_call cannot claim model_result: {context}"
                )
            _safe_identifier(
                record.get("error_code"), "vision_model_call.error_code"
            )
            if status == "invalid_response" and not diagnostic_ids:
                raise EvaluationArtifactError(
                    "invalid_response vision_model_call requires a diagnostic artifact: "
                    f"{context}"
                )
    expected = set(range(1, expected_calls + 1))
    if observed_calls != expected:
        raise EvaluationArtifactError(
            "vision_model_call does not cover every vision model call; "
            f"missing {sorted(expected - observed_calls)}: {source_name}"
        )


def _validate_validation_result_payload(
    payload: Any,
    run: Mapping[str, Any],
    source_name: str,
) -> None:
    if not isinstance(payload, dict):
        raise EvaluationArtifactError(
            f"validation_result JSON root must be an object: {source_name}"
        )
    if payload.get("schema_version") != VALIDATION_RESULT_SCHEMA_VERSION:
        raise EvaluationArtifactError(
            "validation_result.schema_version must be "
            f"{VALIDATION_RESULT_SCHEMA_VERSION}: {source_name}"
        )
    if payload.get("evidence_id") != "validation_result-001":
        raise EvaluationArtifactError(
            f"validation_result.evidence_id must be validation_result-001: {source_name}"
        )
    validation = run.get("validation")
    if not isinstance(validation, Mapping):
        raise EvaluationArtifactError("run.validation must be an object")
    expected = {
        "run_id": run.get("run_id"),
        "task_id": run.get("task_id"),
        "method": validation.get("method"),
        "passed": validation.get("passed"),
    }
    for field, expected_value in expected.items():
        actual = payload.get(field)
        if field == "passed":
            matches = isinstance(actual, bool) and actual is expected_value
        else:
            matches = isinstance(actual, str) and actual == expected_value
        if not matches:
            raise EvaluationArtifactError(
                f"validation_result.{field} does not match run: {source_name}"
            )
    expected_evaluator = validation.get("evaluator")
    if expected_evaluator is not None and payload.get("evaluator") != expected_evaluator:
        raise EvaluationArtifactError(
            f"validation_result.evaluator does not match run: {source_name}"
        )


def _validate_json_role_payload(
    payload: Any,
    role: str,
    run: Mapping[str, Any],
    path: Path,
) -> None:
    """Reject valid-but-unrelated JSON masquerading as a typed artifact."""

    if not isinstance(payload, dict):
        raise EvaluationArtifactError(
            f"{role} JSON root must be an object: {path.name}"
        )
    try:
        if role == "cv_result":
            _require_schema(payload, RESPONSE_SCHEMA_VERSION, role, path.name)
            _require_list_fields(payload, role, path.name, "objects", "links")
            confidence = payload.get("confidence")
            if (
                isinstance(confidence, bool)
                or not isinstance(confidence, (int, float))
                or not 0 <= float(confidence) <= 1
            ):
                raise EvaluationArtifactError(
                    f"cv_result.confidence must be between 0 and 1: {path.name}"
                )
        elif role == "cv_metadata":
            _require_schema(payload, CV_METADATA_SCHEMA_VERSION, role, path.name)
            _require_non_empty_text(payload.get("source_id"), "cv_metadata.source_id", path.name)
            _require_non_empty_text(payload.get("adapter_id"), "cv_metadata.adapter_id", path.name)
            _require_non_empty_text(
                payload.get("adapter_version"), "cv_metadata.adapter_version", path.name
            )
            _require_sha256(payload.get("artifact_sha256"), "cv_metadata.artifact_sha256", path.name)
            frames = payload.get("frames")
            if not isinstance(frames, list) or not frames:
                raise EvaluationArtifactError(
                    f"cv_metadata.frames must be a non-empty array: {path.name}"
                )
            for index, frame in enumerate(frames):
                if not isinstance(frame, Mapping):
                    raise EvaluationArtifactError(
                        f"cv_metadata.frames[{index}] must be an object: {path.name}"
                    )
                _require_non_empty_text(
                    frame.get("canvas_id"),
                    f"cv_metadata.frames[{index}].canvas_id",
                    path.name,
                )
                _require_sha256(
                    frame.get("sha256"),
                    f"cv_metadata.frames[{index}].sha256",
                    path.name,
                )
                for dimension in ("width", "height"):
                    value = frame.get(dimension)
                    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                        raise EvaluationArtifactError(
                            f"cv_metadata.frames[{index}].{dimension} must be positive: "
                            f"{path.name}"
                        )
        elif role == "model_result":
            if payload.get("schema_version") != MODEL_SCHEMA_VERSION:
                raise EvaluationArtifactError(
                    f"model_result schema_version must be {MODEL_SCHEMA_VERSION}: {path.name}"
                )
            TopologyModelContract.parse_response_bytes(path.read_bytes())
        elif role == "routing_result":
            _require_schema(payload, ROUTING_SCHEMA_VERSION, role, path.name)
            decision = payload.get("decision")
            if decision not in ROUTE_DECISIONS:
                raise EvaluationArtifactError(
                    f"routing_result.decision is invalid: {path.name}"
                )
            for field in (
                "policy_version",
                "requested_profile",
                "effective_profile",
                "scene_type",
                "result_status",
                "execution_status",
            ):
                _require_non_empty_text(payload.get(field), f"routing_result.{field}", path.name)
            if not isinstance(payload.get("requirement_satisfied"), bool):
                raise EvaluationArtifactError(
                    f"routing_result.requirement_satisfied must be boolean: {path.name}"
                )
            if not isinstance(payload.get("model_invoked"), bool):
                raise EvaluationArtifactError(
                    f"routing_result.model_invoked must be boolean: {path.name}"
                )
            source = payload.get("source")
            if not isinstance(source, Mapping):
                raise EvaluationArtifactError(
                    f"routing_result.source must be an object: {path.name}"
                )
            _require_sha256(source.get("sha256"), "routing_result.source.sha256", path.name)
            for field in (
                "source_id",
                "mime_type",
                "cv_adapter_id",
                "cv_adapter_version",
            ):
                _require_non_empty_text(
                    source.get(field), f"routing_result.source.{field}", path.name
                )
            for dimension in ("width", "height"):
                value = source.get(dimension)
                if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                    raise EvaluationArtifactError(
                        f"routing_result.source.{dimension} must be positive: {path.name}"
                    )
        elif role == "fused_result":
            _require_schema(payload, FUSION_SCHEMA_VERSION, role, path.name)
            if not isinstance(payload.get("summary"), Mapping):
                raise EvaluationArtifactError(
                    f"fused_result.summary must be an object: {path.name}"
                )
            result = payload.get("result")
            if not isinstance(result, Mapping):
                raise EvaluationArtifactError(
                    f"fused_result.result must be an object: {path.name}"
                )
            _require_schema(result, RESPONSE_SCHEMA_VERSION, "fused_result.result", path.name)
            _require_list_fields(result, "fused_result.result", path.name, "objects", "links")
            if not isinstance(payload.get("routing"), Mapping):
                raise EvaluationArtifactError(
                    f"fused_result.routing must be an object: {path.name}"
                )
        elif role == "ui_graph":
            _require_schema(payload, UI_GRAPH_SCHEMA_VERSION, role, path.name)
            _require_non_empty_text(payload.get("graph_id"), "ui_graph.graph_id", path.name)
            _require_non_empty_text(payload.get("capture_id"), "ui_graph.capture_id", path.name)
            _require_list_fields(payload, role, path.name, "nodes", "edges", "issues")
            if not isinstance(payload.get("stats"), Mapping):
                raise EvaluationArtifactError(
                    f"ui_graph.stats must be an object: {path.name}"
                )
            if (
                payload.get("analysis_only") is not True
                or payload.get("execution_authorized") is not False
                or payload.get("safe_for_execution") is not False
            ):
                raise EvaluationArtifactError(
                    f"ui_graph must remain analysis-only and unsafe for execution: {path.name}"
                )
            graph_core = {
                "capture_id": payload["capture_id"],
                "nodes": payload["nodes"],
                "edges": payload["edges"],
                "issues": payload["issues"],
            }
            expected_graph_id = "uig:" + hashlib.sha256(
                json.dumps(
                    graph_core,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                    allow_nan=False,
                ).encode("utf-8")
            ).hexdigest()[:24]
            if payload.get("graph_id") != expected_graph_id:
                raise EvaluationArtifactError(
                    f"ui_graph.graph_id does not match its canonical content: {path.name}"
                )
            _validate_ui_graph_safety(payload, path.name)
        elif role == "cdp_snapshot":
            _require_schema(payload, CDP_PAGE_SNAPSHOT_SCHEMA_VERSION, role, path.name)
            if (
                payload.get("safe_for_execution") is not False
                or payload.get("actionable_grounding") is not False
            ):
                raise EvaluationArtifactError(
                    "cdp_snapshot must remain unsafe and must not claim actionable "
                    f"grounding: {path.name}"
                )
            frames = payload.get("frames")
            if not isinstance(frames, list) or not frames:
                raise EvaluationArtifactError(
                    f"cdp_snapshot.frames must be non-empty: {path.name}"
                )
            dom = payload.get("dom_snapshot")
            if not isinstance(dom, Mapping) or not isinstance(dom.get("documents"), list):
                raise EvaluationArtifactError(
                    f"cdp_snapshot.dom_snapshot.documents must be an array: {path.name}"
                )
            if not isinstance(payload.get("ax_tree"), (Mapping, list)):
                raise EvaluationArtifactError(
                    f"cdp_snapshot.ax_tree must be an object or array: {path.name}"
                )
        elif role == "dom_snapshot":
            _require_schema(payload, BROWSER_USE_DOM_SCHEMA_VERSION, role, path.name)
            if payload.get("run_id") != run.get("run_id"):
                raise EvaluationArtifactError(
                    f"dom_snapshot.run_id does not match run: {path.name}"
                )
            if payload.get("task_id") != run.get("task_id"):
                raise EvaluationArtifactError(
                    f"dom_snapshot.task_id does not match run: {path.name}"
                )
            if payload.get("safe_for_execution") is not False:
                raise EvaluationArtifactError(
                    f"dom_snapshot.safe_for_execution must be false: {path.name}"
                )
            if not isinstance(payload.get("elements"), list):
                raise EvaluationArtifactError(
                    f"dom_snapshot.elements must be an array: {path.name}"
                )
    except (CanvasVisionResponseError, TopologyModelResponseError) as exc:
        raise EvaluationArtifactError(
            f"{role} violates its project schema: {path.name}"
        ) from exc


def _validate_ui_graph_safety(payload: Mapping[str, Any], source_name: str) -> None:
    """Preserve the project's fail-closed UI Graph execution boundary."""

    nodes = payload.get("nodes", [])
    edges = payload.get("edges", [])
    issues = payload.get("issues", [])
    stats = payload.get("stats")
    if not isinstance(stats, Mapping):
        raise EvaluationArtifactError(f"ui_graph.stats must be an object: {source_name}")
    for field, expected in (
        ("node_count", len(nodes)),
        ("edge_count", len(edges)),
        ("issue_count", len(issues)),
    ):
        if stats.get(field) != expected:
            raise EvaluationArtifactError(
                f"ui_graph.stats.{field} does not match graph content: {source_name}"
            )
    if not isinstance(stats.get("truncated"), bool):
        raise EvaluationArtifactError(
            f"ui_graph.stats.truncated must be boolean: {source_name}"
        )
    node_ids: set[str] = set()
    for index, node in enumerate(nodes):
        context = f"ui_graph.nodes[{index}]"
        if not isinstance(node, Mapping):
            raise EvaluationArtifactError(f"{context} must be an object: {source_name}")
        node_id = _require_non_empty_text(node.get("id"), f"{context}.id", source_name)
        if node_id in node_ids:
            raise EvaluationArtifactError(f"{context}.id is duplicated: {source_name}")
        node_ids.add(node_id)
        if (
            node.get("actionable") is not False
            or node.get("can_click_now") is not False
            or node.get("safe_for_execution") is not False
        ):
            raise EvaluationArtifactError(
                f"{context} must remain non-actionable and unsafe for execution: {source_name}"
            )
        interaction = node.get("interaction")
        if not isinstance(interaction, Mapping):
            raise EvaluationArtifactError(
                f"{context}.interaction must be an object: {source_name}"
            )
        if interaction.get("authorized") is not False:
            raise EvaluationArtifactError(
                f"{context}.interaction.authorized must be false: {source_name}"
            )
        candidate = interaction.get("candidate")
        if not isinstance(candidate, bool):
            raise EvaluationArtifactError(
                f"{context}.interaction.candidate must be boolean: {source_name}"
            )
        if interaction.get("preflight_required") is not candidate:
            raise EvaluationArtifactError(
                f"{context}.interaction.preflight_required must match candidate: "
                f"{source_name}"
            )
        if not candidate:
            continue
        source = node.get("source")
        if not isinstance(source, Mapping):
            raise EvaluationArtifactError(
                f"{context}.source must be an object: {source_name}"
            )
        source_kind = source.get("kind")
        if source_kind not in {"dom", "cdp"}:
            raise EvaluationArtifactError(
                f"{context} candidate source must be dom or cdp: {source_name}"
            )
        if node.get("disabled") is True:
            raise EvaluationArtifactError(
                f"{context} disabled node cannot be a candidate: {source_name}"
            )
        if source_kind == "dom":
            source_ref = _require_non_empty_text(
                source.get("source_ref"), f"{context}.source.source_ref", source_name
            )
            if "@capture:" in source_ref.casefold():
                raise EvaluationArtifactError(
                    f"{context} candidate uses an unstable capture reference: {source_name}"
                )
        else:
            backend_node_id = source.get("backend_node_id")
            if (
                isinstance(backend_node_id, bool)
                or not isinstance(backend_node_id, int)
                or backend_node_id < 1
            ):
                raise EvaluationArtifactError(
                    f"{context} CDP candidate requires a positive backend_node_id: "
                    f"{source_name}"
                )

    for index, edge in enumerate(edges):
        context = f"ui_graph.edges[{index}]"
        if not isinstance(edge, Mapping):
            raise EvaluationArtifactError(f"{context} must be an object: {source_name}")
        if (
            edge.get("analysis_only") is not True
            or edge.get("execution_authorized") is not False
            or edge.get("safe_for_execution") is not False
        ):
            raise EvaluationArtifactError(
                f"{context} must remain analysis-only and unsafe for execution: {source_name}"
            )
        if edge.get("source") not in node_ids or edge.get("target") not in node_ids:
            raise EvaluationArtifactError(
                f"{context} references a node outside the graph: {source_name}"
            )


def _validate_bundle_contracts(
    artifacts: Sequence[Mapping[str, Any]],
    *,
    base_dir: Path,
    run: Mapping[str, Any],
) -> None:
    """Validate cross-file provenance that a per-file schema cannot prove.

    The manifest hashes tell us whether archived bytes changed.  These bindings
    additionally prove which screenshot produced each CV result, which routing
    decision belongs to it, and which routing payload was embedded in fusion.
    """

    by_role: dict[str, list[Mapping[str, Any]]] = {}
    for artifact in artifacts:
        if not isinstance(artifact, Mapping):
            continue
        role = artifact.get("role")
        if isinstance(role, str):
            by_role.setdefault(role, []).append(artifact)
    for entries in by_role.values():
        entries.sort(key=lambda entry: str(entry.get("path", "")))

    artifact_by_id: dict[str, Mapping[str, Any]] = {}
    for artifact in artifacts:
        artifact_id = _require_artifact_id(
            artifact.get("artifact_id"), "artifact.artifact_id", "manifest"
        )
        relative_path = artifact.get("path")
        if (
            not isinstance(relative_path, str)
            or PurePosixPath(relative_path).stem != artifact_id
        ):
            raise EvaluationArtifactError(
                "artifact_id must match the canonical archived filename"
            )
        if artifact_id in artifact_by_id:
            raise EvaluationArtifactError("bundle artifact ids must be unique")
        artifact_by_id[artifact_id] = artifact

    original_entries = by_role.get("original_screenshot", [])
    cv_entries = by_role.get("cv_result", [])
    metadata_entries = by_role.get("cv_metadata", [])
    routing_entries = by_role.get("routing_result", [])
    fused_entries = by_role.get("fused_result", [])

    if len(cv_entries) != len(metadata_entries):
        raise EvaluationArtifactError(
            "cv_result and cv_metadata counts must match one-to-one"
        )
    if cv_entries and len(routing_entries) != len(cv_entries):
        raise EvaluationArtifactError(
            "routing_result count must match cv_result count one-to-one"
        )
    if len(fused_entries) > len(cv_entries):
        raise EvaluationArtifactError(
            "fused_result count cannot exceed cv_result count"
        )

    cv_payloads: list[dict[str, Any]] = []
    metadata_payloads: list[dict[str, Any]] = []
    for index, (cv_entry, metadata_entry) in enumerate(
        zip(cv_entries, metadata_entries), start=1
    ):
        cv_path, cv_payload = _load_artifact_json(base_dir, cv_entry, "cv_result")
        _metadata_path, metadata = _load_artifact_json(
            base_dir, metadata_entry, "cv_metadata"
        )
        expected_cv_hash = _require_sha256(
            cv_entry.get("sha256"), f"cv_result[{index}].sha256", "manifest"
        )
        if metadata.get("artifact_sha256") != expected_cv_hash:
            raise EvaluationArtifactError(
                f"cv_metadata[{index}].artifact_sha256 does not bind cv_result[{index}]"
            )
        frames = metadata.get("frames")
        if not isinstance(frames, list) or len(frames) != 1:
            raise EvaluationArtifactError(
                f"cv_metadata[{index}].frames must identify exactly one input image"
            )
        frame_dimensions: dict[str, tuple[int, int]] = {}
        for frame_index, frame in enumerate(frames):
            if not isinstance(frame, Mapping):
                raise EvaluationArtifactError(
                    f"cv_metadata[{index}].frames[{frame_index}] must be an object"
                )
            canvas_id = str(frame.get("canvas_id", ""))
            if canvas_id in frame_dimensions:
                raise EvaluationArtifactError(
                    f"cv_metadata[{index}] repeats canvas_id {canvas_id!r}"
                )
            width = frame.get("width")
            height = frame.get("height")
            if not isinstance(width, int) or not isinstance(height, int):
                raise EvaluationArtifactError(
                    f"cv_metadata[{index}] frame dimensions are invalid"
                )
            frame_dimensions[canvas_id] = (width, height)
            if index > len(original_entries):
                raise EvaluationArtifactError(
                    f"cv_metadata[{index}] has no corresponding screenshot artifact"
                )
            matching_screenshot = original_entries[index - 1]
            expected_screenshot_hash = _require_sha256(
                matching_screenshot.get("sha256"),
                f"original_screenshot[{index}].sha256",
                "manifest",
            )
            if frame.get("sha256") != expected_screenshot_hash:
                raise EvaluationArtifactError(
                    f"cv_metadata[{index}] does not bind original-screenshot-{index:03d}"
                )
            screenshot_path = _artifact_path(
                base_dir,
                matching_screenshot,
                "original_screenshot",
            )
            expected_mime = matching_screenshot.get("media_type")
            if frame.get("mime_type") != expected_mime:
                raise EvaluationArtifactError(
                    f"cv_metadata[{index}] frame MIME type does not match screenshot"
                )
            try:
                actual_dimensions = TopologyVisionContract.image_dimensions(
                    _read_limited_bytes(
                        screenshot_path,
                        max_bytes=MAX_ARTIFACT_BYTES,
                        field=f"original_screenshot[{index}]",
                    ),
                    str(expected_mime),
                )
            except (OSError, ValueError) as exc:
                raise EvaluationArtifactError(
                    f"cv_metadata[{index}] screenshot has invalid image dimensions"
                ) from exc
            if actual_dimensions != (width, height):
                raise EvaluationArtifactError(
                    f"cv_metadata[{index}] frame dimensions do not match screenshot bytes"
                )
        core_payload = {
            key: cv_payload[key]
            for key in (
                "schema_version",
                "confidence",
                "objects",
                "links",
                "co_channel_relations",
                "negative_edges",
                "structure_templates",
                "no_connections",
            )
            if key in cv_payload
        }
        try:
            TopologyVisionContract().parse_response_bytes(
                json.dumps(
                    core_payload,
                    ensure_ascii=False,
                    separators=(",", ":"),
                    allow_nan=False,
                ).encode("utf-8"),
                frame_dimensions,
            )
        except (OSError, TypeError, ValueError, CanvasVisionResponseError) as exc:
            raise EvaluationArtifactError(
                f"cv_result[{index}] violates its frame-bound project schema"
            ) from exc
        cv_payloads.append(cv_payload)
        metadata_payloads.append(metadata)

    routing_payloads: list[dict[str, Any]] = []
    for index, routing_entry in enumerate(routing_entries, start=1):
        _routing_path, routing = _load_artifact_json(
            base_dir, routing_entry, "routing_result"
        )
        if index <= len(metadata_payloads):
            metadata = metadata_payloads[index - 1]
            source = routing.get("source")
            frames = metadata.get("frames", [])
            matching_frames = [
                frame
                for frame in frames
                if isinstance(frame, Mapping)
                and isinstance(source, Mapping)
                and frame.get("sha256") == source.get("sha256")
            ]
            if len(matching_frames) != 1:
                raise EvaluationArtifactError(
                    f"routing_result[{index}].source does not bind cv_metadata[{index}]"
                )
            frame = matching_frames[0]
            expected_source = {
                "source_id": metadata.get("source_id"),
                "sha256": frame.get("sha256"),
                "mime_type": frame.get("mime_type"),
                "width": frame.get("width"),
                "height": frame.get("height"),
                "cv_adapter_id": metadata.get("adapter_id"),
                "cv_adapter_version": metadata.get("adapter_version"),
            }
            if any(source.get(key) != value for key, value in expected_source.items()):
                raise EvaluationArtifactError(
                    f"routing_result[{index}].source metadata does not match cv_metadata[{index}]"
                )
        routing_payloads.append(routing)

    fused_by_route: dict[int, dict[str, Any]] = {}
    for index, fused_entry in enumerate(fused_entries, start=1):
        _fused_path, fused = _load_artifact_json(base_dir, fused_entry, "fused_result")
        route_index = index - 1
        if (
            route_index >= len(routing_payloads)
            or fused.get("routing") != routing_payloads[route_index]
        ):
            raise EvaluationArtifactError(
                f"fused_result[{index}].routing does not match routing_result[{index}]"
            )
        fused_by_route[route_index] = fused

    routing_by_artifact_id: dict[str, tuple[int, dict[str, Any]]] = {
        str(entry["artifact_id"]): (index, routing_payloads[index])
        for index, entry in enumerate(routing_entries)
    }
    model_by_artifact_id: dict[str, dict[str, Any]] = {
        str(entry["artifact_id"]): _load_artifact_json(
            base_dir, entry, "model_result"
        )[1]
        for entry in by_role.get("model_result", [])
    }
    model_by_route: dict[int, dict[str, Any]] = {}
    referenced_model_ids: set[str] = set()
    referenced_routing_ids: set[str] = set()
    vision_call_records: list[dict[str, Any]] = []
    for entry in by_role.get("vision_model_call", []):
        _path, records = _load_artifact_jsonl(base_dir, entry, "vision_model_call")
        vision_call_records.extend(records)
    for record_index, record in enumerate(vision_call_records, start=1):
        screenshot_id = str(record.get("screenshot_artifact_id", ""))
        screenshot_entry = artifact_by_id.get(screenshot_id)
        if screenshot_entry is None or screenshot_entry.get("role") != "original_screenshot":
            raise EvaluationArtifactError(
                f"vision_model_call[{record_index}] screenshot artifact is outside this bundle"
            )
        screenshot_hash = _require_sha256(
            screenshot_entry.get("sha256"),
            f"vision_model_call[{record_index}] screenshot artifact SHA",
            "manifest",
        )
        if record.get("screenshot_sha256") != screenshot_hash:
            raise EvaluationArtifactError(
                f"vision_model_call[{record_index}] screenshot hash does not match artifact"
            )
        routing_id = str(record.get("routing_artifact_id", ""))
        route_binding = routing_by_artifact_id.get(routing_id)
        if route_binding is None:
            raise EvaluationArtifactError(
                f"vision_model_call[{record_index}] routing artifact is outside this bundle"
            )
        route_index, routing = route_binding
        referenced_routing_ids.add(routing_id)
        source = routing.get("source")
        if (
            routing.get("decision") != "model_assist"
            or routing.get("model_invoked") is not True
            or not isinstance(source, Mapping)
            or source.get("sha256") != screenshot_hash
        ):
            raise EvaluationArtifactError(
                f"vision_model_call[{record_index}] does not bind a model-assist route"
            )
        for diagnostic_id in record.get("diagnostic_artifact_ids", []):
            diagnostic_entry = artifact_by_id.get(diagnostic_id)
            if diagnostic_entry is None or diagnostic_entry.get("role") not in {
                "model_events",
                "model_stderr",
            }:
                raise EvaluationArtifactError(
                    f"vision_model_call[{record_index}] diagnostic artifact is invalid"
                )
        if record.get("status") == "success":
            model_id = str(record.get("model_result_artifact_id", ""))
            model_entry = artifact_by_id.get(model_id)
            model_payload = model_by_artifact_id.get(model_id)
            if model_entry is None or model_payload is None:
                raise EvaluationArtifactError(
                    f"vision_model_call[{record_index}] model result is outside this bundle"
                )
            if record.get("model_result_sha256") != model_entry.get("sha256"):
                raise EvaluationArtifactError(
                    f"vision_model_call[{record_index}] model result hash does not match artifact"
                )
            if model_id in referenced_model_ids or route_index in model_by_route:
                raise EvaluationArtifactError(
                    "each successful model result and route may be bound only once"
                )
            referenced_model_ids.add(model_id)
            model_by_route[route_index] = model_payload

    if referenced_model_ids != set(model_by_artifact_id):
        raise EvaluationArtifactError(
            "every model_result must be referenced by one successful vision_model_call"
        )
    model_assist_routing_ids = {
        str(entry["artifact_id"])
        for index, entry in enumerate(routing_entries)
        if routing_payloads[index].get("decision") == "model_assist"
    }
    if model_assist_routing_ids != referenced_routing_ids:
        raise EvaluationArtifactError(
            "every model-assist routing_result must be referenced by a vision_model_call"
        )

    for route_index, fused in fused_by_route.items():
        cv_payload = cv_payloads[route_index]
        routing = routing_payloads[route_index]
        try:
            if route_index in model_by_route:
                expected_fusion = fuse_topology_payloads(
                    cv_payload, model_by_route[route_index]
                )
            else:
                prepared_cv, _disputed_links = prepare_cv_payload_for_route(
                    cv_payload, routing
                )
                expected_fusion = fuse_topology_payloads(
                    prepared_cv,
                    {"topology": {"nodes": [], "edges": []}},
                )
        except (TopologyCVRoutingError, TopologyFusionError, TypeError, ValueError) as exc:
            raise EvaluationArtifactError(
                f"cannot reproduce fused_result[{route_index + 1}] from archived inputs"
            ) from exc
        if fused.get("result") != expected_fusion.get("result"):
            raise EvaluationArtifactError(
                f"fused_result[{route_index + 1}].result does not match archived CV/model inputs"
            )

    planner_input_roles = {
        "original_screenshot",
        "processed_screenshot",
        "cv_result",
        "cv_metadata",
        "model_result",
        "routing_result",
        "fused_result",
        "ui_graph",
        "dom_snapshot",
        "cdp_snapshot",
        "browser_use_elements",
    }
    planner_input_refs: set[str] = set()
    for role in planner_input_roles:
        for entry in by_role.get(role, []):
            planner_input_refs.add(
                _require_sha256(entry.get("sha256"), f"{role}.sha256", "manifest")
            )
            planner_input_refs.add(str(entry.get("artifact_id", "")))
    for entry in by_role.get("ui_graph", []):
        _path, graph = _load_artifact_json(base_dir, entry, "ui_graph")
        for field in ("graph_id", "capture_id"):
            value = graph.get(field)
            if isinstance(value, str) and value:
                planner_input_refs.add(value)
    for entry in by_role.get("planner_result", []):
        _path, records = _load_artifact_jsonl(base_dir, entry, "planner_result")
        for record_index, record in enumerate(records, start=1):
            input_refs = record.get("input_refs")
            if not isinstance(input_refs, list) or not input_refs:
                raise EvaluationArtifactError(
                    f"planner_result[{record_index}].input_refs is missing"
                )
            unknown_refs = [item for item in input_refs if item not in planner_input_refs]
            if unknown_refs:
                raise EvaluationArtifactError(
                    f"planner_result[{record_index}] references inputs outside this bundle"
                )

    if by_role.get("ui_tars_response"):
        expected_screenshot_ids = {
            str(entry.get("artifact_id", "")) for entry in original_entries
        }
        referenced_screenshot_ids: set[str] = set()
        for entry in by_role["ui_tars_response"]:
            _path, records = _load_artifact_jsonl(
                base_dir, entry, "ui_tars_response"
            )
            for record_index, record in enumerate(records, start=1):
                screenshot_id = str(record.get("screenshot_artifact_id", ""))
                screenshot_entry = artifact_by_id.get(screenshot_id)
                if (
                    screenshot_entry is None
                    or screenshot_entry.get("role") != "original_screenshot"
                ):
                    raise EvaluationArtifactError(
                        f"ui_tars_response[{record_index}] references a screenshot "
                        "not present in this bundle"
                    )
                if screenshot_id in referenced_screenshot_ids:
                    raise EvaluationArtifactError(
                        f"ui_tars_response[{record_index}] reuses a screenshot artifact"
                    )
                if record.get("screenshot_sha256") != screenshot_entry.get("sha256"):
                    raise EvaluationArtifactError(
                        f"ui_tars_response[{record_index}] screenshot hash does not "
                        "match its artifact"
                    )
                referenced_screenshot_ids.add(screenshot_id)
        if referenced_screenshot_ids != expected_screenshot_ids:
            raise EvaluationArtifactError(
                "UI-TARS responses must reference every archived screenshot exactly once"
            )

    if run.get("outcome") == "success":
        for role, list_field in (
            ("ui_graph", "nodes"),
            ("dom_snapshot", "elements"),
        ):
            for entry in by_role.get(role, []):
                _path, payload = _load_artifact_json(base_dir, entry, role)
                if not isinstance(payload.get(list_field), list) or not payload[list_field]:
                    raise EvaluationArtifactError(
                        f"successful run requires non-empty {role}.{list_field}"
                    )
        for entry in by_role.get("cdp_snapshot", []):
            _path, payload = _load_artifact_json(base_dir, entry, "cdp_snapshot")
            dom = payload.get("dom_snapshot")
            documents = dom.get("documents") if isinstance(dom, Mapping) else None
            if not isinstance(documents, list) or not documents:
                raise EvaluationArtifactError(
                    "successful run requires non-empty cdp_snapshot.dom_snapshot.documents"
                )


def _load_artifact_json(
    base_dir: Path,
    artifact: Mapping[str, Any],
    role: str,
) -> tuple[Path, dict[str, Any]]:
    path = _artifact_path(base_dir, artifact, role)
    try:
        payload = _strict_json_loads(path.read_text(encoding="utf-8-sig"))
    except (
        OSError,
        UnicodeError,
        json.JSONDecodeError,
        ValueError,
        RecursionError,
    ) as exc:
        raise EvaluationArtifactError(f"cannot read {role} JSON: {path.name}") from exc
    if not isinstance(payload, dict):
        raise EvaluationArtifactError(f"{role} JSON root must be an object: {path.name}")
    return path, payload


def _artifact_path(
    base_dir: Path,
    artifact: Mapping[str, Any],
    role: str,
) -> Path:
    relative_path = artifact.get("path")
    if not isinstance(relative_path, str) or not relative_path:
        raise EvaluationArtifactError(f"{role} artifact path is missing")
    path = _resolve_relative_ref(base_dir, relative_path, f"{role}.path")
    _assert_below_root(base_dir, path, f"{role}.path")
    return path


def _load_artifact_jsonl(
    base_dir: Path,
    artifact: Mapping[str, Any],
    role: str,
) -> tuple[Path, list[dict[str, Any]]]:
    path = _artifact_path(base_dir, artifact, role)
    records: list[dict[str, Any]] = []
    try:
        with path.open("r", encoding="utf-8-sig") as stream:
            for line_number, line in enumerate(stream, start=1):
                if not line.strip():
                    continue
                record = _strict_json_loads(line)
                if not isinstance(record, dict):
                    raise EvaluationArtifactError(
                        f"{role} line must be an object: {path.name}:{line_number}"
                    )
                records.append(record)
    except (
        OSError,
        UnicodeError,
        json.JSONDecodeError,
        ValueError,
        RecursionError,
    ) as exc:
        raise EvaluationArtifactError(f"cannot read {role} JSONL: {path.name}") from exc
    if not records:
        raise EvaluationArtifactError(f"{role} JSONL has no records: {path.name}")
    return path, records


def _require_schema(
    payload: Mapping[str, Any], expected: str, role: str, source_name: str
) -> None:
    if payload.get("schema_version") != expected:
        raise EvaluationArtifactError(
            f"{role}.schema_version must be {expected}: {source_name}"
        )


def _require_list_fields(
    payload: Mapping[str, Any], role: str, source_name: str, *fields: str
) -> None:
    for field in fields:
        if not isinstance(payload.get(field), list):
            raise EvaluationArtifactError(
                f"{role}.{field} must be an array: {source_name}"
            )


def _require_non_empty_text(value: Any, field: str, source_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise EvaluationArtifactError(f"{field} must be non-empty: {source_name}")
    return value


def _require_sha256(value: Any, field: str, source_name: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or value != value.casefold()
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise EvaluationArtifactError(f"{field} must be SHA-256: {source_name}")
    return value


def _require_artifact_id(value: Any, field: str, source_name: str) -> str:
    if not isinstance(value, str) or not _ARTIFACT_ID_PATTERN.fullmatch(value):
        raise EvaluationArtifactError(
            f"{field} must be a canonical artifact id: {source_name}"
        )
    return value


def _detect_format(path: Path, role: str) -> str:
    suffix = path.suffix.casefold()
    formats = ROLE_FORMATS[role]
    matches = [
        file_format
        for file_format in formats
        if suffix in FORMAT_SUFFIXES[file_format]
    ]
    if len(matches) != 1:
        allowed = sorted(
            suffix_value
            for file_format in formats
            for suffix_value in FORMAT_SUFFIXES[file_format]
        )
        raise EvaluationArtifactError(
            f"artifact role {role} requires one of {', '.join(allowed)}: {path.name}"
        )
    return matches[0]


def _strict_json_loads(text: str) -> Any:
    """Parse interoperable JSON and reject duplicate keys and NaN/Infinity."""

    def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate JSON object key")
            result[key] = value
        return result

    def reject_constant(value: str) -> None:
        raise ValueError(f"non-standard JSON constant: {value}")

    return json.loads(
        text,
        object_pairs_hook=unique_object,
        parse_constant=reject_constant,
    )


def _validate_image_container(raw: bytes, mime_type: str) -> None:
    """Reject header-only images that cannot represent a complete screenshot.

    The project deliberately has no mandatory Pillow dependency.  These bounded
    container checks complement ``image_dimensions``: PNG requires a CRC-valid
    chunk stream with IDAT and IEND, JPEG requires a scan and EOI, and WebP's
    existing dimension parser already requires an exact RIFF container length.
    """

    if mime_type == "image/png":
        position = 8
        saw_ihdr = False
        saw_idat = False
        saw_iend = False
        idat_bytes = bytearray()
        width = 0
        height = 0
        bit_depth = 0
        color_type = 0
        interlace_method = 0
        while position + 12 <= len(raw):
            chunk_length = int.from_bytes(raw[position : position + 4], "big")
            chunk_type = raw[position + 4 : position + 8]
            data_start = position + 8
            data_end = data_start + chunk_length
            crc_end = data_end + 4
            if data_end < data_start or crc_end > len(raw):
                raise ValueError("truncated PNG chunk")
            expected_crc = int.from_bytes(raw[data_end:crc_end], "big")
            actual_crc = zlib.crc32(raw[position + 4 : data_end]) & 0xFFFFFFFF
            if expected_crc != actual_crc:
                raise ValueError("PNG chunk CRC mismatch")
            if not saw_ihdr:
                if chunk_type != b"IHDR" or chunk_length != 13:
                    raise ValueError("PNG IHDR must be first")
                saw_ihdr = True
                width = int.from_bytes(raw[data_start : data_start + 4], "big")
                height = int.from_bytes(raw[data_start + 4 : data_start + 8], "big")
                bit_depth = raw[data_start + 8]
                color_type = raw[data_start + 9]
                compression_method = raw[data_start + 10]
                filter_method = raw[data_start + 11]
                interlace_method = raw[data_start + 12]
                allowed_depths = {
                    0: {1, 2, 4, 8, 16},
                    2: {8, 16},
                    3: {1, 2, 4, 8},
                    4: {8, 16},
                    6: {8, 16},
                }
                if (
                    bit_depth not in allowed_depths.get(color_type, set())
                    or compression_method != 0
                    or filter_method != 0
                    or interlace_method not in {0, 1}
                ):
                    raise ValueError("PNG IHDR encoding is unsupported")
            elif chunk_type == b"IHDR":
                raise ValueError("PNG repeats IHDR")
            if chunk_type == b"IDAT":
                saw_idat = True
                idat_bytes.extend(raw[data_start:data_end])
            if chunk_type == b"IEND":
                if chunk_length != 0:
                    raise ValueError("PNG IEND must be empty")
                saw_iend = True
                break
            position = crc_end
        if not (saw_ihdr and saw_idat and saw_iend):
            raise ValueError("PNG is missing image data or terminator")
        max_decoded = width * height * 8 + height * 7 + 1024
        decompressor = zlib.decompressobj()
        decoded = decompressor.decompress(bytes(idat_bytes), max_decoded + 1)
        if (
            len(decoded) > max_decoded
            or decompressor.unconsumed_tail
            or not decompressor.eof
            or not decoded
        ):
            raise ValueError("PNG image data is invalid or exceeds its dimensions")
        if interlace_method != 0:
            raise ValueError("interlaced PNG screenshots are not supported")
        channels = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}[color_type]
        row_bytes = (width * channels * bit_depth + 7) // 8
        expected_decoded = height * (row_bytes + 1)
        if len(decoded) != expected_decoded:
            raise ValueError("PNG decompressed scanline size is invalid")
        for row_index in range(height):
            if decoded[row_index * (row_bytes + 1)] > 4:
                raise ValueError("PNG scanline filter is invalid")
        return

    if mime_type == "image/jpeg":
        if not raw.startswith(b"\xff\xd8") or not raw.endswith(b"\xff\xd9"):
            raise ValueError("JPEG markers are incomplete")
        position = 2
        scan_start: int | None = None
        while position + 1 < len(raw) - 2:
            if raw[position] != 0xFF:
                raise ValueError("JPEG marker stream is invalid")
            while position < len(raw) and raw[position] == 0xFF:
                position += 1
            if position >= len(raw) - 2:
                break
            marker = raw[position]
            position += 1
            if marker in {0x01, 0xD8} or 0xD0 <= marker <= 0xD7:
                continue
            if marker == 0xDA:
                if position + 2 > len(raw) - 2:
                    raise ValueError("JPEG scan header is truncated")
                segment_length = int.from_bytes(raw[position : position + 2], "big")
                scan_start = position + segment_length
                if segment_length < 2 or scan_start >= len(raw) - 2:
                    raise ValueError("JPEG scan data is empty")
                break
            if marker == 0xD9 or position + 2 > len(raw) - 2:
                raise ValueError("JPEG reached EOI before scan data")
            segment_length = int.from_bytes(raw[position : position + 2], "big")
            if segment_length < 2 or position + segment_length > len(raw) - 2:
                raise ValueError("JPEG segment is truncated")
            position += segment_length
        if scan_start is None or not raw[scan_start:-2]:
            raise ValueError("JPEG has no scan data")
        return

    if mime_type == "image/webp":
        if (
            len(raw) < 20
            or raw[:4] != b"RIFF"
            or raw[8:12] != b"WEBP"
            or int.from_bytes(raw[4:8], "little") + 8 != len(raw)
        ):
            raise ValueError("WebP RIFF container is incomplete")
        position = 12
        saw_pixel_chunk = False
        while position + 8 <= len(raw):
            chunk_type = raw[position : position + 4]
            chunk_size = int.from_bytes(raw[position + 4 : position + 8], "little")
            data_start = position + 8
            data_end = data_start + chunk_size
            padded_end = data_end + (chunk_size & 1)
            if data_end > len(raw) or padded_end > len(raw):
                raise ValueError("WebP chunk is truncated")
            data = raw[data_start:data_end]
            if chunk_type == b"VP8 ":
                if len(data) < 10 or data[3:6] != b"\x9d\x01\x2a":
                    raise ValueError("WebP VP8 frame header is invalid")
                saw_pixel_chunk = True
            elif chunk_type == b"VP8L":
                if len(data) < 5 or data[0] != 0x2F:
                    raise ValueError("WebP VP8L frame header is invalid")
                saw_pixel_chunk = True
            position = padded_end
        if position != len(raw) or not saw_pixel_chunk:
            raise ValueError("WebP has no complete pixel-data chunk")
        return

    raise ValueError("unsupported image media type")


def _run_metadata_payload(run: Mapping[str, Any]) -> dict[str, Any]:
    payload = dict(run)
    payload["evidence"] = {"status": "pending_at_archive_time"}
    return payload


def _canonical_run_dir(root: Path, run: Mapping[str, Any]) -> Path:
    scheme_id = _safe_identifier(run.get("scheme_id"), "run.scheme_id")
    task_id = _safe_identifier(run.get("task_id"), "run.task_id")
    repetition = _non_negative_int(run.get("repetition"), "run.repetition")
    if repetition < 1:
        raise EvaluationArtifactError("run.repetition must be positive")
    return root / "artifacts" / scheme_id / task_id / f"r{repetition:03d}"


def _safe_identifier(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise EvaluationArtifactError(f"{field} must be a non-empty string")
    if not _SAFE_IDENTIFIER_PATTERN.fullmatch(value) or value.endswith("."):
        raise EvaluationArtifactError(
            f"{field} must be a safe ASCII slug (letters, digits, '.', '_' or '-')"
        )
    device_stem = value.split(".", 1)[0].upper()
    if device_stem in _WINDOWS_DEVICE_NAMES:
        raise EvaluationArtifactError(
            f"{field} uses a reserved Windows device name"
        )
    return value


def _validate_run_identifiers(run: Mapping[str, Any]) -> None:
    for field in ("run_id", "suite_id", "scheme_id", "task_id"):
        _safe_identifier(run.get(field), f"run.{field}")


def _non_negative_int(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise EvaluationArtifactError(f"{field} must be a non-negative integer")
    return value


def _find_item(items: Any, key: str, value: Any) -> Mapping[str, Any]:
    if not isinstance(items, list):
        raise EvaluationArtifactError(f"suite.{key} collection is invalid")
    matches = [item for item in items if isinstance(item, Mapping) and item.get(key) == value]
    if len(matches) != 1:
        raise EvaluationArtifactError(f"cannot resolve suite item for {key}={value!r}")
    return matches[0]


def _deduplicate_groups(groups: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    seen: set[tuple[str, ...]] = set()
    for group in groups:
        roles = tuple(sorted(set(str(role) for role in group["any_of"])))
        if roles in seen:
            continue
        seen.add(roles)
        result.append({"any_of": list(roles), "reason": str(group["reason"])[:500]})
    return result


def _resolve_relative_ref(root: Path, reference: str, field: str) -> Path:
    if "\\" in reference:
        raise EvaluationArtifactError(f"{field} must use forward-slash separators")
    candidate = PurePosixPath(reference)
    if candidate.is_absolute() or not candidate.parts:
        raise EvaluationArtifactError(f"{field} must be a relative path")
    if any(part in {"", ".", ".."} for part in candidate.parts):
        raise EvaluationArtifactError(f"{field} is not a canonical relative path")
    if candidate.as_posix() != reference or any(":" in part for part in candidate.parts):
        raise EvaluationArtifactError(f"{field} is not a canonical relative path")
    lexical_target = root.joinpath(*candidate.parts)
    _assert_no_reparse_components(lexical_target, field)
    try:
        resolved = lexical_target.resolve(strict=True)
    except OSError as exc:
        raise EvaluationArtifactError(f"{field} does not exist") from exc
    _assert_below_root(root, resolved, field)
    return resolved


def _assert_below_root(root: Path, target: Path, field: str) -> None:
    resolved_root = root.resolve()
    resolved_target = target.resolve()
    try:
        resolved_target.relative_to(resolved_root)
    except ValueError as exc:
        raise EvaluationArtifactError(f"{field} escapes its allowed root") from exc
    if resolved_target == resolved_root:
        raise EvaluationArtifactError(f"{field} must not target the root directory")


def _prepare_evaluation_root(path: Path, *, create: bool) -> Path:
    lexical_root = _absolute_lexical_path(Path(path).expanduser())
    _assert_no_reparse_components(lexical_root, "evaluation root")
    if create:
        try:
            lexical_root.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise EvaluationArtifactError("cannot create evaluation root") from exc
        _assert_no_reparse_components(lexical_root, "evaluation root")
    elif not lexical_root.is_dir():
        raise EvaluationArtifactError("evaluation root does not exist")
    try:
        return lexical_root.resolve(strict=True)
    except OSError as exc:
        raise EvaluationArtifactError("cannot resolve evaluation root") from exc


def _absolute_lexical_path(path: Path) -> Path:
    return Path(os.path.abspath(os.fspath(path)))


def _path_lexists(path: Path) -> bool:
    return os.path.lexists(os.fspath(path))


def _is_reparse_point(path: Path) -> bool:
    try:
        path_stat = os.lstat(path)
    except OSError:
        return False
    return stat.S_ISLNK(path_stat.st_mode) or bool(
        getattr(path_stat, "st_file_attributes", 0)
        & _FILE_ATTRIBUTE_REPARSE_POINT
    )


def _assert_no_reparse_components(path: Path, field: str) -> None:
    absolute = _absolute_lexical_path(path)
    parts = absolute.parts
    if not parts:
        raise EvaluationArtifactError(f"{field} is invalid")
    current = Path(parts[0])
    candidates = [current]
    for part in parts[1:]:
        current = current / part
        candidates.append(current)
    for candidate in candidates:
        if _path_lexists(candidate) and _is_reparse_point(candidate):
            raise EvaluationArtifactError(
                f"{field} traverses a symbolic link or reparse point"
            )


def _assert_no_reparse_tree(root: Path, field: str) -> None:
    pending = [root]
    while pending:
        directory = pending.pop()
        try:
            entries = list(os.scandir(directory))
        except OSError as exc:
            raise EvaluationArtifactError(f"cannot inspect {field}") from exc
        for entry in entries:
            try:
                entry_stat = entry.stat(follow_symlinks=False)
            except OSError as exc:
                raise EvaluationArtifactError(f"cannot inspect {field}") from exc
            if stat.S_ISLNK(entry_stat.st_mode) or bool(
                getattr(entry_stat, "st_file_attributes", 0)
                & _FILE_ATTRIBUTE_REPARSE_POINT
            ):
                raise EvaluationArtifactError(
                    f"{field} contains a symbolic link or reparse point"
                )
            if stat.S_ISDIR(entry_stat.st_mode):
                pending.append(Path(entry.path))


def _same_file_identity(left: os.stat_result, right: os.stat_result) -> bool:
    left_inode = getattr(left, "st_ino", 0)
    right_inode = getattr(right, "st_ino", 0)
    left_device = getattr(left, "st_dev", 0)
    right_device = getattr(right, "st_dev", 0)
    if left_inode and right_inode and left_inode != right_inode:
        return False
    if left_device and right_device and left_device != right_device:
        return False
    return stat.S_IFMT(left.st_mode) == stat.S_IFMT(right.st_mode)


def _write_new_json(path: Path, payload: Mapping[str, Any]) -> None:
    try:
        rendered = json.dumps(
            payload,
            ensure_ascii=False,
            indent=2,
            allow_nan=False,
        ) + "\n"
    except (TypeError, ValueError, RecursionError) as exc:
        raise EvaluationArtifactError("evidence JSON is not serializable") from exc
    try:
        with path.open("x", encoding="utf-8", newline="\n") as stream:
            stream.write(rendered)
    except OSError as exc:
        raise EvaluationArtifactError(f"cannot create evidence JSON: {path.name}") from exc


def _read_limited_bytes(path: Path, *, max_bytes: int, field: str) -> bytes:
    before = path.stat()
    if before.st_size > max_bytes:
        raise EvaluationArtifactError(f"{field} exceeds {max_bytes} bytes")
    chunks: list[bytes] = []
    total = 0
    try:
        with path.open("rb") as stream:
            opened = os.fstat(stream.fileno())
            if not _same_file_identity(before, opened):
                raise EvaluationArtifactError(f"{field} changed before reading")
            while True:
                chunk = stream.read(1024 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > max_bytes:
                    raise EvaluationArtifactError(f"{field} exceeds {max_bytes} bytes")
                chunks.append(chunk)
    except OSError as exc:
        raise EvaluationArtifactError(f"cannot read {field}") from exc
    after = path.stat()
    if (
        not _same_file_identity(before, after)
        or before.st_size != after.st_size
        or before.st_mtime_ns != after.st_mtime_ns
        or total != before.st_size
    ):
        raise EvaluationArtifactError(f"{field} changed while being read")
    return b"".join(chunks)


def _sha256_file(path: Path, *, max_bytes: int = MAX_ARTIFACT_BYTES) -> str:
    before = path.stat()
    if before.st_size > max_bytes:
        raise EvaluationArtifactError(
            f"file exceeds {max_bytes} bytes while hashing: {path.name}"
        )
    digest = hashlib.sha256()
    total = 0
    try:
        with path.open("rb") as stream:
            opened = os.fstat(stream.fileno())
            if not _same_file_identity(before, opened):
                raise EvaluationArtifactError(
                    f"file changed before hashing: {path.name}"
                )
            while True:
                chunk = stream.read(1024 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > max_bytes:
                    raise EvaluationArtifactError(
                        f"file exceeds {max_bytes} bytes while hashing: {path.name}"
                    )
                digest.update(chunk)
    except OSError as exc:
        raise EvaluationArtifactError(f"cannot hash file: {path.name}") from exc
    after = path.stat()
    if (
        not _same_file_identity(before, after)
        or before.st_size != after.st_size
        or before.st_mtime_ns != after.st_mtime_ns
        or total != before.st_size
    ):
        raise EvaluationArtifactError(f"file changed while hashing: {path.name}")
    return digest.hexdigest()


def _verification_failure(run: Mapping[str, Any], errors: list[str]) -> dict[str, Any]:
    return {
        "run_id": run.get("run_id"),
        "verified": False,
        "manifest_ref": "",
        "manifest_sha256": "",
        "artifact_count": 0,
        "total_bytes": 0,
        "present_roles": [],
        "errors": errors,
    }


__all__ = [
    "ARTIFACT_MANIFEST_SCHEMA_VERSION",
    "EVIDENCE_PROFILES",
    "EvaluationArtifactError",
    "ROLE_FORMATS",
    "archive_run_evidence",
    "parse_artifact_arguments",
    "remove_new_evidence_archive",
    "required_role_groups",
    "verify_run_evidence",
]
