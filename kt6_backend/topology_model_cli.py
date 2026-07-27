from __future__ import annotations

import argparse
import json
import math
import sys
import time
from pathlib import Path
from typing import Any

from .codeagent_canvas_vision import (
    CodeAgentCanvasVisionAdapter,
    CodeAgentProgress,
    CodeAgentProcessResult,
    CodeAgentRunner,
    CodeAgentVisionError,
    SubprocessCodeAgentRunner,
)
from .topology_artifact_common import (
    TopologyArtifactCLIError,
    build_image_input,
    ensure_distinct_paths,
    normalize_cv_context,
    write_json,
)
from .topology_fusion_cli import load_json


DEFAULT_MODEL_IDLE_TIMEOUT_SECONDS = 300.0
DEFAULT_MODEL_MAX_ATTEMPTS = 2
MAX_MODEL_ATTEMPTS = 3


class RecordingCodeAgentRunner:
    """Persist CodeAgent stdout as it arrives, including failed attempts."""

    def __init__(
        self,
        output_path: Path,
        stderr_path: Path,
        *,
        delegate: CodeAgentRunner | None = None,
        heartbeat_seconds: float = 10.0,
        idle_timeout_seconds: float | None = None,
    ) -> None:
        self.output_path = output_path
        self.stderr_path = stderr_path
        self.delegate = delegate
        self.heartbeat_seconds = heartbeat_seconds
        self.idle_timeout_seconds = idle_timeout_seconds

    @staticmethod
    def _report_progress(progress: CodeAgentProgress) -> None:
        if (
            progress.idle_seconds is None
            and progress.stderr_idle_seconds is None
        ):
            state = "正在启动（尚无输出）"
        elif progress.idle_seconds is None:
            state = "正在启动（仅 stderr 有输出）"
        elif progress.idle_seconds >= 30:
            state = f"长时间无 stdout（{progress.idle_seconds:.0f}秒）"
        else:
            state = "正在运行"
        event = progress.last_event or "none"
        phase = {
            "starting": "启动",
            "reading": "读取图片",
            "post_read_inference": "图后推理",
            "terminal": "结束",
        }.get(progress.phase, progress.phase)
        print(
            f"[CodeAgent] {state}，已运行 {progress.elapsed_seconds:.0f} 秒，"
            f"阶段 {phase}，最后事件 {event}，stdout {progress.stdout_bytes} 字节，"
            f"stderr {progress.stderr_bytes} 字节",
            file=sys.stderr,
            flush=True,
        )

    def run(self, **kwargs: Any) -> CodeAgentProcessResult:
        self.output_path.parent.mkdir(parents=True, exist_ok=True)
        self.stderr_path.parent.mkdir(parents=True, exist_ok=True)
        sink = None
        stderr_sink = None
        delegate = self.delegate
        streaming = delegate is None
        if delegate is None:
            sink = self.output_path.open("wb")
            try:
                stderr_sink = self.stderr_path.open("wb")
                delegate = SubprocessCodeAgentRunner(
                    stdout_sink=sink,
                    stderr_sink=stderr_sink,
                    progress_callback=self._report_progress,
                    heartbeat_seconds=self.heartbeat_seconds,
                    idle_timeout_seconds=self.idle_timeout_seconds,
                )
            except BaseException:
                sink.close()
                raise
        try:
            result = delegate.run(**kwargs)
            if not streaming:
                self.output_path.write_bytes(result.stdout)
                self.stderr_path.write_bytes(result.stderr)
            return result
        finally:
            if sink is not None:
                sink.close()
            if stderr_sink is not None:
                stderr_sink.close()


def _attempt_artifact_path(path: Path, attempt: int) -> Path:
    return path.with_name(f"{path.stem}.attempt-{attempt}{path.suffix}")


def _archive_failed_attempt(
    events_path: Path,
    stderr_path: Path,
    attempt: int,
) -> None:
    for source in (events_path, stderr_path):
        if not source.exists():
            continue
        destination = _attempt_artifact_path(source, attempt)
        try:
            source.replace(destination)
        except OSError as exc:
            raise TopologyArtifactCLIError(
                f"cannot archive failed CodeAgent attempt: {source}"
            ) from exc


def generate_model_artifact(
    image_path: Path,
    *,
    source_id: str,
    output_path: Path,
    events_path: Path,
    stderr_path: Path | None = None,
    cv_path: Path | None = None,
    executable: str = "codeagent",
    agent: str | None = None,
    permission_mode: str = "dontAsk",
    timeout_seconds: float = 600.0,
    idle_timeout_seconds: float | None = DEFAULT_MODEL_IDLE_TIMEOUT_SECONDS,
    max_attempts: int = DEFAULT_MODEL_MAX_ATTEMPTS,
    workdir: Path | None = None,
    runner: CodeAgentRunner | None = None,
) -> dict[str, Any]:
    if (
        isinstance(max_attempts, bool)
        or not isinstance(max_attempts, int)
        or not 1 <= max_attempts <= MAX_MODEL_ATTEMPTS
    ):
        raise ValueError(
            f"max_attempts must be an integer from 1 to {MAX_MODEL_ATTEMPTS}"
        )
    total_timeout = float(timeout_seconds)
    if (
        not math.isfinite(total_timeout)
        or total_timeout <= 0
        or total_timeout > CodeAgentCanvasVisionAdapter.MAX_TIMEOUT_SECONDS
    ):
        raise ValueError(
            "timeout_seconds must be positive, finite, and no greater than "
            f"{CodeAgentCanvasVisionAdapter.MAX_TIMEOUT_SECONDS:g}"
        )
    normalized_idle_timeout: float | None
    if idle_timeout_seconds is None or float(idle_timeout_seconds) == 0:
        normalized_idle_timeout = None
    else:
        normalized_idle_timeout = float(idle_timeout_seconds)
        if (
            not math.isfinite(normalized_idle_timeout)
            or normalized_idle_timeout < 0
        ):
            raise ValueError(
                "idle_timeout_seconds must be non-negative and finite"
            )

    resolved_stderr_path = stderr_path or events_path.with_name(
        "codeagent-stderr.log"
    )
    attempt_paths = [
        _attempt_artifact_path(path, attempt)
        for attempt in range(1, max_attempts)
        for path in (events_path, resolved_stderr_path)
    ]
    ensure_distinct_paths(
        image_path,
        cv_path,
        output_path,
        events_path,
        resolved_stderr_path,
        *attempt_paths,
    )
    for stale_path in (
        output_path,
        events_path,
        resolved_stderr_path,
        *attempt_paths,
    ):
        try:
            stale_path.unlink(missing_ok=True)
        except OSError as exc:
            raise TopologyArtifactCLIError(
                f"cannot replace stale artifact: {stale_path}"
            ) from exc

    page, frames = build_image_input(image_path, source_id)
    cv_context = normalize_cv_context(load_json(cv_path)) if cv_path else None
    deadline = time.monotonic() + total_timeout
    for attempt in range(1, max_attempts + 1):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise CodeAgentVisionError(
                "CodeAgent model attempts exhausted the total timeout",
                error_code="model_retry_deadline_exhausted",
                category="transient_transport",
                retryable=True,
            )
        attempt_timeout = remaining
        recording_runner = RecordingCodeAgentRunner(
            events_path,
            resolved_stderr_path,
            delegate=runner,
            idle_timeout_seconds=normalized_idle_timeout,
        )
        adapter = CodeAgentCanvasVisionAdapter(
            workdir=(workdir or Path.cwd()),
            executable=executable,
            agent=agent,
            permission_mode=permission_mode,
            timeout_seconds=attempt_timeout,
            runner=recording_runner,
        )
        try:
            if cv_context is None:
                result = adapter.recognize_model(page=page, frames=frames)
            else:
                result = adapter.recognize_model_with_context(
                    page=page,
                    frames=frames,
                    cv_observations=cv_context,
                )
        except CodeAgentVisionError as exc:
            remaining = deadline - time.monotonic()
            if (
                attempt >= max_attempts
                or not exc.retryable
                or remaining <= 0
            ):
                raise
            _archive_failed_attempt(
                events_path,
                resolved_stderr_path,
                attempt,
            )
            print(
                f"[CodeAgent] 第 {attempt}/{max_attempts} 次模型尝试失败"
                f"（{exc.error_code}），将在剩余 {remaining:.0f} 秒内重试",
                file=sys.stderr,
                flush=True,
            )
            continue
        write_json(output_path, result)
        return result
    raise RuntimeError("unreachable CodeAgent attempt state")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Ask CodeAgent to inspect one topology image and save both its "
            "validated model JSON and raw stream-json events."
        )
    )
    parser.add_argument("image", type=Path)
    parser.add_argument("--source-id", required=True)
    parser.add_argument("--cv", type=Path, help="optional local-CV artifact")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--events", type=Path, required=True)
    parser.add_argument(
        "--stderr",
        type=Path,
        help="CodeAgent stderr log (default: codeagent-stderr.log beside --events)",
    )
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
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        result = generate_model_artifact(
            args.image,
            source_id=args.source_id,
            output_path=args.out,
            events_path=args.events,
            stderr_path=args.stderr,
            cv_path=args.cv,
            executable=args.executable,
            agent=args.agent,
            permission_mode=args.permission_mode,
            timeout_seconds=args.timeout,
            idle_timeout_seconds=args.idle_timeout,
            max_attempts=args.max_attempts,
            workdir=args.workdir,
        )
        print(
            json.dumps(
                {
                    "status": "ok",
                    "artifact": str(args.out.resolve()),
                    "events": str(args.events.resolve()),
                    "stderr": str(
                        (
                            args.stderr
                            or args.events.with_name("codeagent-stderr.log")
                        ).resolve()
                    ),
                    "object_count": len(
                        result.get("nodes", result.get("objects", []))
                    ),
                    "link_count": len(result.get("links", [])),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    except KeyboardInterrupt:
        print(
            json.dumps(
                {
                    "error": "interrupted; CodeAgent process tree was terminated",
                    "error_type": "KeyboardInterrupt",
                    "events": (
                        str(args.events.resolve()) if args.events.exists() else None
                    ),
                    "stderr": str(
                        (
                            args.stderr
                            or args.events.with_name("codeagent-stderr.log")
                        ).resolve()
                    ),
                },
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
        return 130
    except (CodeAgentVisionError, TopologyArtifactCLIError, OSError, ValueError) as exc:
        print(
            json.dumps(
                {
                    "error": str(exc),
                    "error_type": type(exc).__name__,
                    "error_code": getattr(exc, "error_code", None),
                    "category": getattr(exc, "category", None),
                    "retryable": getattr(exc, "retryable", False),
                    "events": (
                        str(args.events.resolve()) if args.events.exists() else None
                    ),
                    "stderr": str(
                        (
                            args.stderr
                            or args.events.with_name("codeagent-stderr.log")
                        ).resolve()
                    ),
                },
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
