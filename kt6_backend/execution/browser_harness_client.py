from __future__ import annotations

import importlib
import math
import os
from pathlib import Path
from typing import Any, Callable, Mapping
from urllib.parse import urlsplit


class BrowserHarnessError(RuntimeError):
    def __init__(self, error_code: str):
        super().__init__(error_code)
        self.error_code = error_code


class BrowserHarnessClient:
    """Small, fixed-capability client for the Browser Harness daemon."""

    def __init__(
        self,
        *,
        cdp_url: str,
        workspace: Path,
        cdp_call: Callable[..., Mapping[str, Any]] | None = None,
        click_call: Callable[[float, float], Any] | None = None,
        ensure_daemon: Callable[[], Any] | None = None,
    ):
        self.cdp_url = self._loopback_cdp_url(cdp_url)
        self.workspace = Path(workspace).resolve()
        self._cdp_call = cdp_call
        self._click_call = click_call
        self._ensure_daemon = ensure_daemon
        self._ready = False

    def click_backend_node(
        self,
        backend_node_id: int,
        *,
        expected_page_url: str,
    ) -> dict[str, Any]:
        if (
            isinstance(backend_node_id, bool)
            or not isinstance(backend_node_id, int)
            or backend_node_id < 1
        ):
            raise BrowserHarnessError("invalid_backend_node_id")
        try:
            self._load_runtime()
        except BrowserHarnessError:
            raise
        except Exception as exc:
            raise BrowserHarnessError("browser_harness_unavailable") from exc
        try:
            page = self._cdp_call("Page.getFrameTree")
            current_url = str(
                page.get("frameTree", {}).get("frame", {}).get("url", "")
            ).strip()
            if current_url != expected_page_url:
                raise BrowserHarnessError("browser_page_changed")
            response = self._cdp_call(
                "DOM.getBoxModel",
                backendNodeId=backend_node_id,
            )
            quad = response.get("model", {}).get("content")
            x, y = self._quad_center(quad)
            metrics = self._cdp_call("Page.getLayoutMetrics")
            viewport = metrics.get("cssVisualViewport") or metrics.get(
                "cssLayoutViewport"
            )
            if not self._inside_viewport(x, y, viewport):
                raise BrowserHarnessError("browser_target_not_visible")
            self._click_call(x, y)
        except BrowserHarnessError:
            raise
        except Exception as exc:
            raise BrowserHarnessError("browser_harness_click_failed") from exc
        return {
            "backend_node_id": backend_node_id,
            "x": x,
            "y": y,
        }

    def _load_runtime(self) -> None:
        if self._ready:
            return
        if self._cdp_call is None or self._click_call is None:
            self.workspace.mkdir(parents=True, exist_ok=True)
            if (self.workspace / "agent_helpers.py").exists():
                raise BrowserHarnessError("browser_harness_workspace_not_empty")
            os.environ["BH_AGENT_WORKSPACE"] = str(self.workspace)
            os.environ["BU_CDP_URL"] = self.cdp_url
            try:
                helpers = importlib.import_module("browser_harness.helpers")
                admin = importlib.import_module("browser_harness.admin")
            except (ImportError, OSError) as exc:
                raise BrowserHarnessError("browser_harness_not_installed") from exc
            loaded_workspace = Path(helpers.AGENT_WORKSPACE).resolve()
            if loaded_workspace != self.workspace:
                raise BrowserHarnessError("browser_harness_workspace_mismatch")
            self._cdp_call = helpers.cdp
            self._click_call = helpers.click_at_xy
            self._ensure_daemon = admin.ensure_daemon
        if self._ensure_daemon is not None:
            try:
                self._ensure_daemon()
            except Exception as exc:
                raise BrowserHarnessError("browser_harness_daemon_unavailable") from exc
        self._ready = True

    @staticmethod
    def _quad_center(value: Any) -> tuple[float, float]:
        if not isinstance(value, (list, tuple)) or len(value) != 8:
            raise BrowserHarnessError("browser_target_box_unavailable")
        numbers: list[float] = []
        for item in value:
            if isinstance(item, bool):
                raise BrowserHarnessError("browser_target_box_invalid")
            try:
                number = float(item)
            except (TypeError, ValueError, OverflowError) as exc:
                raise BrowserHarnessError("browser_target_box_invalid") from exc
            if not math.isfinite(number):
                raise BrowserHarnessError("browser_target_box_invalid")
            numbers.append(number)
        area = abs(
            sum(
                numbers[index] * numbers[(index + 3) % 8]
                - numbers[(index + 2) % 8] * numbers[(index + 1) % 8]
                for index in range(0, 8, 2)
            )
        ) / 2
        x = sum(numbers[0::2]) / 4
        y = sum(numbers[1::2]) / 4
        if area <= 0 or x < 0 or y < 0:
            raise BrowserHarnessError("browser_target_not_visible")
        return round(x, 3), round(y, 3)

    @staticmethod
    def _inside_viewport(x: float, y: float, value: Any) -> bool:
        if not isinstance(value, Mapping):
            return False
        try:
            width = float(value.get("clientWidth"))
            height = float(value.get("clientHeight"))
        except (TypeError, ValueError, OverflowError):
            return False
        return bool(
            math.isfinite(width)
            and math.isfinite(height)
            and width > 0
            and height > 0
            and 0 <= x < width
            and 0 <= y < height
        )

    @staticmethod
    def _loopback_cdp_url(value: str) -> str:
        raw = str(value).strip()
        parsed = urlsplit(raw)
        host = (parsed.hostname or "").casefold()
        if (
            parsed.scheme not in {"http", "https"}
            or host not in {"localhost", "127.0.0.1", "::1"}
            or parsed.username
            or parsed.password
            or parsed.fragment
        ):
            raise ValueError(
                "browser harness CDP URL must be an uncredentialed loopback URL"
            )
        return raw


__all__ = ["BrowserHarnessClient", "BrowserHarnessError"]
