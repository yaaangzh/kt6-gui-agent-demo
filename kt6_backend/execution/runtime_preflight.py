from __future__ import annotations

import argparse
import json
import os
import time
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit, urlunsplit
from urllib.request import Request, urlopen


class ExecutionRuntimePreflightError(RuntimeError):
    def __init__(self, error_code: str, detail: str = ""):
        super().__init__(error_code)
        self.error_code = error_code
        self.detail = detail


def _normalize_cdp_url(value: str) -> str:
    parsed = urlsplit(str(value).strip())
    if (
        parsed.scheme != "http"
        or parsed.hostname not in {"localhost", "127.0.0.1", "::1"}
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
    ):
        raise ExecutionRuntimePreflightError("browser_cdp_url_invalid")
    try:
        port = parsed.port
    except ValueError as exc:
        raise ExecutionRuntimePreflightError("browser_cdp_url_invalid") from exc
    if port is None:
        raise ExecutionRuntimePreflightError("browser_cdp_url_invalid")
    host = f"[{parsed.hostname}]" if parsed.hostname == "::1" else parsed.hostname
    return urlunsplit(("http", f"{host}:{port}", "", "", ""))


def cdp_health(
    cdp_url: str,
    *,
    timeout_seconds: float = 0.75,
    opener: Callable[..., Any] = urlopen,
) -> dict[str, Any]:
    try:
        base_url = _normalize_cdp_url(cdp_url)
    except ExecutionRuntimePreflightError as exc:
        return {"ready": False, "error": exc.error_code}
    request = Request(f"{base_url}/json/version", method="GET")
    try:
        with opener(request, timeout=timeout_seconds) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except Exception:
        return {"ready": False, "error": "browser_cdp_unavailable"}
    ready = isinstance(payload, dict) and bool(payload.get("webSocketDebuggerUrl"))
    return {
        "ready": ready,
        "error": None if ready else "browser_cdp_invalid_response",
    }


def harness_health(
    *,
    admin_module: Any | None = None,
    ipc_module: Any | None = None,
) -> dict[str, Any]:
    try:
        if admin_module is None:
            from browser_harness import admin as admin_module
        if ipc_module is None:
            ipc_module = admin_module.ipc
    except (ImportError, OSError):
        return {"ready": False, "error": "browser_harness_not_installed"}
    try:
        if not admin_module.daemon_alive():
            return {
                "ready": False,
                "error": "browser_harness_daemon_unavailable",
            }
        connection, token = ipc_module.connect(
            getattr(admin_module, "NAME", "default"),
            timeout=1.0,
        )
        try:
            response = ipc_module.request(
                connection,
                token,
                {"method": "Target.getTargets", "params": {}},
            )
        finally:
            connection.close()
    except Exception:
        return {"ready": False, "error": "browser_harness_cdp_unavailable"}
    ready = isinstance(response, dict) and "result" in response
    return {
        "ready": ready,
        "error": None if ready else "browser_harness_cdp_unavailable",
    }


def execution_runtime_health(
    cdp_url: str,
    *,
    opener: Callable[..., Any] = urlopen,
    admin_module: Any | None = None,
    ipc_module: Any | None = None,
) -> dict[str, Any]:
    cdp = cdp_health(cdp_url, opener=opener)
    harness = harness_health(
        admin_module=admin_module,
        ipc_module=ipc_module,
    )
    return {
        "ready": cdp["ready"] and harness["ready"],
        "cdp": cdp,
        "harness": harness,
    }


def ensure_execution_runtime(
    cdp_url: str,
    workspace: Path,
    *,
    wait_seconds: float = 15.0,
    opener: Callable[..., Any] = urlopen,
    admin_module: Any | None = None,
    ipc_module: Any | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    base_url = _normalize_cdp_url(cdp_url)
    cdp = cdp_health(base_url, opener=opener)
    if not cdp["ready"]:
        raise ExecutionRuntimePreflightError(str(cdp["error"]))

    resolved_workspace = Path(workspace).resolve()
    resolved_workspace.mkdir(parents=True, exist_ok=True)
    os.environ["BU_CDP_URL"] = base_url
    os.environ["BH_AGENT_WORKSPACE"] = str(resolved_workspace)
    try:
        if admin_module is None:
            from browser_harness import admin as admin_module
        if ipc_module is None:
            ipc_module = admin_module.ipc
    except (ImportError, OSError) as exc:
        raise ExecutionRuntimePreflightError(
            "browser_harness_not_installed"
        ) from exc

    for attempt in range(2):
        try:
            admin_module.ensure_daemon(
                wait=wait_seconds,
                env={
                    "BU_CDP_URL": base_url,
                    "BH_AGENT_WORKSPACE": str(resolved_workspace),
                },
            )
            break
        except Exception as exc:
            cleanup_race = isinstance(exc, PermissionError) or (
                getattr(exc, "winerror", None) == 5
            )
            if attempt == 0 and cleanup_race:
                sleep(0.5)
                continue
            raise ExecutionRuntimePreflightError(
                "browser_harness_daemon_unavailable",
                str(exc),
            ) from exc

    runtime = execution_runtime_health(
        base_url,
        opener=opener,
        admin_module=admin_module,
        ipc_module=ipc_module,
    )
    if not runtime["ready"]:
        error = runtime["harness"]["error"] or runtime["cdp"]["error"]
        raise ExecutionRuntimePreflightError(str(error))
    return runtime


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Check or prepare the local KT6 browser execution runtime."
    )
    parser.add_argument("action", choices=("status", "ensure"))
    parser.add_argument("--cdp-url", default="http://127.0.0.1:9222")
    parser.add_argument(
        "--workspace",
        type=Path,
        default=Path("runtime_data/browser_harness_workspace"),
    )
    parser.add_argument("--wait", type=float, default=15.0)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        if args.action == "ensure":
            result = ensure_execution_runtime(
                args.cdp_url,
                args.workspace,
                wait_seconds=args.wait,
            )
        else:
            result = execution_runtime_health(args.cdp_url)
    except ExecutionRuntimePreflightError as exc:
        result = {
            "ready": False,
            "error": exc.error_code,
            "detail": exc.detail,
        }
        print(json.dumps(result, ensure_ascii=False))
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["ready"] or args.action == "status" else 1


if __name__ == "__main__":
    raise SystemExit(main())
