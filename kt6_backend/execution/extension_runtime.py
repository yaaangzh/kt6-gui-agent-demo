from __future__ import annotations

import copy
import math
import re
import secrets
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from .browser_harness_client import BrowserHarnessClient, BrowserHarnessError
from .url_policy import ExecutionURLPolicy, ExecutionURLPolicyError


_RUNTIME_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{16,128}$")
_TOKEN_PATTERN = re.compile(r"^[A-Za-z0-9_-]{32,256}$")
_TARGET_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{8,128}$")
_DIRECT_CDP_METHODS = frozenset(
    {
        "Accessibility.getFullAXTree",
        "Accessibility.queryAXTree",
        "DOM.describeNode",
        "DOM.focus",
        "DOM.getBoxModel",
        "DOM.getNodeForLocation",
        "DOMSnapshot.captureSnapshot",
        "Input.dispatchKeyEvent",
        "Input.dispatchMouseEvent",
        "Input.insertText",
        "Page.captureScreenshot",
        "Page.getFrameTree",
        "Page.getLayoutMetrics",
    }
)
_COMMAND_KINDS = frozenset(
    {"cdp", "get_targets", "get_version", "ping", "switch_target"}
)


class ExtensionRuntimeError(RuntimeError):
    def __init__(self, error_code: str):
        super().__init__(error_code)
        self.error_code = error_code


@dataclass
class _PendingCommand:
    event: threading.Event = field(default_factory=threading.Event)
    result: dict[str, Any] | None = None
    error_code: str = ""


@dataclass
class _RuntimeRecord:
    runtime_id: str
    token: str
    target_id: str
    page_url: str
    title: str
    last_seen: float
    condition: threading.Condition = field(
        default_factory=lambda: threading.Condition(threading.RLock())
    )
    commands: deque[dict[str, Any]] = field(default_factory=deque)
    pending: dict[str, _PendingCommand] = field(default_factory=dict)
    polling: bool = False


class BrowserExtensionRuntimeManager:
    """Authenticated loopback relay for one explicitly attached Chrome tab."""

    def __init__(
        self,
        *,
        url_policy: ExecutionURLPolicy,
        clock=time.monotonic,
        command_timeout_seconds: float = 20.0,
        connection_ttl_seconds: float = 30.0,
    ):
        self.url_policy = url_policy
        self.clock = clock
        self.command_timeout_seconds = float(command_timeout_seconds)
        self.connection_ttl_seconds = float(connection_ttl_seconds)
        self._records: dict[str, _RuntimeRecord] = {}
        self._lock = threading.RLock()

    def register(
        self,
        *,
        runtime_id: str,
        token: str,
        target_id: str,
        page_url: str,
        title: str = "",
    ) -> dict[str, Any]:
        runtime_id = self._runtime_id(runtime_id)
        token = self._token(token)
        target_id = self._target_id(target_id)
        try:
            page_url = self.url_policy.validate(page_url)
        except ExecutionURLPolicyError as exc:
            raise ExtensionRuntimeError(exc.error_code) from exc
        now = self.clock()
        with self._lock:
            existing = self._records.get(runtime_id)
            if existing is not None and not secrets.compare_digest(
                existing.token, token
            ):
                raise ExtensionRuntimeError("browser_extension_runtime_conflict")
            if existing is None:
                existing = _RuntimeRecord(
                    runtime_id=runtime_id,
                    token=token,
                    target_id=target_id,
                    page_url=page_url,
                    title=str(title)[:300],
                    last_seen=now,
                )
                self._records[runtime_id] = existing
            else:
                with existing.condition:
                    existing.target_id = target_id
                    existing.page_url = page_url
                    existing.title = str(title)[:300]
                    existing.last_seen = now
                    existing.condition.notify_all()
        return {
            "runtime_id": runtime_id,
            "target_id": target_id,
            "page_url": page_url,
            "ready": True,
        }

    def unregister(self, *, runtime_id: str, token: str) -> bool:
        record = self._authenticated_record(runtime_id, token)
        with self._lock:
            if self._records.get(record.runtime_id) is not record:
                return False
            del self._records[record.runtime_id]
        with record.condition:
            for pending in record.pending.values():
                pending.error_code = "browser_extension_runtime_disconnected"
                pending.event.set()
            record.pending.clear()
            record.commands.clear()
            record.condition.notify_all()
        return True

    def poll(
        self,
        *,
        runtime_id: str,
        token: str,
        wait_milliseconds: int,
    ) -> dict[str, Any]:
        record = self._authenticated_record(runtime_id, token)
        if (
            isinstance(wait_milliseconds, bool)
            or not isinstance(wait_milliseconds, int)
            or not 0 <= wait_milliseconds <= 25_000
        ):
            raise ExtensionRuntimeError("browser_extension_poll_wait_invalid")
        with record.condition:
            if record.polling:
                raise ExtensionRuntimeError("browser_extension_poll_conflict")
            record.polling = True
            try:
                record.last_seen = self.clock()
                if not record.commands and wait_milliseconds:
                    record.condition.wait(wait_milliseconds / 1000)
                record.last_seen = self.clock()
                command = record.commands.popleft() if record.commands else None
                return {"command": copy.deepcopy(command)}
            finally:
                record.polling = False
                record.condition.notify_all()

    def complete(
        self,
        *,
        runtime_id: str,
        token: str,
        command_id: str,
        result: Mapping[str, Any] | None,
        error_code: str = "",
    ) -> bool:
        record = self._authenticated_record(runtime_id, token)
        command_id = str(command_id).strip()
        if not command_id or len(command_id) > 128:
            raise ExtensionRuntimeError("browser_extension_command_id_invalid")
        with record.condition:
            pending = record.pending.get(command_id)
            if pending is None:
                return False
            pending.result = copy.deepcopy(dict(result or {}))
            pending.error_code = str(error_code).strip()[:200]
            record.last_seen = self.clock()
            pending.event.set()
            record.condition.notify_all()
        return True

    def dispatch(
        self,
        runtime_id: str,
        *,
        kind: str,
        method: str = "",
        params: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        kind = str(kind).strip()
        method = str(method).strip()
        if kind not in _COMMAND_KINDS:
            raise ExtensionRuntimeError("browser_extension_command_not_allowed")
        if kind == "cdp" and method not in _DIRECT_CDP_METHODS:
            raise ExtensionRuntimeError("browser_extension_cdp_method_not_allowed")
        if kind != "cdp" and method:
            raise ExtensionRuntimeError("browser_extension_command_invalid")
        with self._lock:
            record = self._records.get(self._runtime_id(runtime_id))
        if record is None:
            raise ExtensionRuntimeError("browser_extension_runtime_not_found")
        if self.clock() - record.last_seen > self.connection_ttl_seconds:
            raise ExtensionRuntimeError("browser_extension_runtime_offline")
        command_id = f"cmd_{uuid.uuid4().hex}"
        pending = _PendingCommand()
        command = {
            "command_id": command_id,
            "kind": kind,
            "method": method,
            "params": copy.deepcopy(dict(params or {})),
        }
        with record.condition:
            record.pending[command_id] = pending
            record.commands.append(command)
            record.condition.notify_all()
        if not pending.event.wait(self.command_timeout_seconds):
            with record.condition:
                record.pending.pop(command_id, None)
            raise ExtensionRuntimeError("browser_extension_command_timeout")
        with record.condition:
            record.pending.pop(command_id, None)
            result = copy.deepcopy(pending.result or {})
            error_code = pending.error_code
        if error_code:
            raise ExtensionRuntimeError("browser_extension_command_failed")
        return result

    def bind(
        self,
        *,
        runtime_id: str,
        target_id: str,
        page_url: str,
    ) -> dict[str, str]:
        runtime_id = self._runtime_id(runtime_id)
        target_id = self._target_id(target_id)
        try:
            page_url = self.url_policy.validate(page_url)
        except ExecutionURLPolicyError as exc:
            raise ExtensionRuntimeError(exc.error_code) from exc
        with self._lock:
            record = self._records.get(runtime_id)
        if record is None:
            raise ExtensionRuntimeError("browser_extension_runtime_not_found")
        if self.clock() - record.last_seen > self.connection_ttl_seconds:
            raise ExtensionRuntimeError("browser_extension_runtime_offline")
        with record.condition:
            if record.target_id != target_id or record.page_url != page_url:
                raise ExtensionRuntimeError("browser_target_binding_mismatch")
        return {
            "runtime_id": runtime_id,
            "target_id": target_id,
            "page_url": page_url,
        }

    def update_binding(
        self,
        runtime_id: str,
        *,
        target_id: str,
        page_url: str,
    ) -> None:
        with self._lock:
            record = self._records.get(self._runtime_id(runtime_id))
        if record is None:
            raise ExtensionRuntimeError("browser_extension_runtime_not_found")
        with record.condition:
            record.target_id = self._target_id(target_id)
            record.page_url = self.url_policy.validate(page_url)
            record.last_seen = self.clock()

    def current_target_id(self, runtime_id: str) -> str:
        with self._lock:
            record = self._records.get(self._runtime_id(runtime_id))
        if record is None:
            raise ExtensionRuntimeError("browser_extension_runtime_not_found")
        with record.condition:
            return record.target_id

    def health(self) -> dict[str, Any]:
        now = self.clock()
        with self._lock:
            connected = sum(
                now - record.last_seen <= self.connection_ttl_seconds
                for record in self._records.values()
            )
        return {
            "ready": connected > 0,
            "transport": "chrome_extension",
            "extension": {
                "ready": connected > 0,
                "connected_runtimes": connected,
                "error": None if connected else "browser_extension_not_connected",
            },
        }

    def _authenticated_record(self, runtime_id: str, token: str) -> _RuntimeRecord:
        runtime_id = self._runtime_id(runtime_id)
        token = self._token(token)
        with self._lock:
            record = self._records.get(runtime_id)
        if record is None or not secrets.compare_digest(record.token, token):
            raise ExtensionRuntimeError("browser_extension_runtime_unauthorized")
        return record

    @staticmethod
    def _runtime_id(value: str) -> str:
        value = str(value).strip()
        if not _RUNTIME_ID_PATTERN.fullmatch(value):
            raise ExtensionRuntimeError("browser_extension_runtime_id_invalid")
        return value

    @staticmethod
    def _token(value: str) -> str:
        value = str(value).strip()
        if not _TOKEN_PATTERN.fullmatch(value):
            raise ExtensionRuntimeError("browser_extension_runtime_token_invalid")
        return value

    @staticmethod
    def _target_id(value: str) -> str:
        value = str(value).strip()
        if not _TARGET_ID_PATTERN.fullmatch(value):
            raise ExtensionRuntimeError("browser_target_id_invalid")
        return value


class BrowserExtensionClient(BrowserHarnessClient):
    """Reuse fixed BrowserHarnessClient safety checks over a Chrome extension relay."""

    executor_id = "chrome_extension"

    def __init__(
        self,
        *,
        manager: BrowserExtensionRuntimeManager,
        url_policy: ExecutionURLPolicy,
    ):
        self.manager = manager
        self._active_runtime_id = ""
        super().__init__(
            cdp_url="http://127.0.0.1:9222",
            workspace=Path("runtime_data/browser_extension_workspace"),
            url_policy=url_policy,
            cdp_call=self._relay_cdp,
            click_call=self._relay_click,
            switch_tab_call=self._relay_switch_target,
            current_tab_call=self._relay_current_target,
        )

    def open_or_bind_target(
        self,
        start_url: str,
        *,
        target_id: str = "",
        runtime_id: str = "",
    ) -> dict[str, str]:
        try:
            target_url = self.url_policy.validate(start_url)
            binding = self.manager.bind(
                runtime_id=runtime_id,
                target_id=target_id,
                page_url=target_url,
            )
            self._active_runtime_id = binding["runtime_id"]
            self._load_runtime()
            self._relay_cdp("Page.getFrameTree")
        except (ExecutionURLPolicyError, ExtensionRuntimeError) as exc:
            raise BrowserHarnessError(exc.error_code) from exc
        except BrowserHarnessError:
            raise
        except Exception as exc:
            raise BrowserHarnessError("browser_extension_binding_failed") from exc
        self._bound_target_id = binding["target_id"]
        self._bound_page_url = binding["page_url"]
        return {
            "target_id": self._bound_target_id,
            "page_url": self._bound_page_url,
            "runtime_id": self._active_runtime_id,
        }

    def runtime_health(self) -> dict[str, Any]:
        return self.manager.health()

    def _relay_cdp(self, method: str, **params: Any) -> Mapping[str, Any]:
        if not self._active_runtime_id:
            raise BrowserHarnessError("browser_extension_runtime_not_bound")
        try:
            if method == "Target.getTargets":
                return self.manager.dispatch(
                    self._active_runtime_id,
                    kind="get_targets",
                )
            if method == "Browser.getVersion":
                return self.manager.dispatch(
                    self._active_runtime_id,
                    kind="get_version",
                )
            return self.manager.dispatch(
                self._active_runtime_id,
                kind="cdp",
                method=method,
                params=params,
            )
        except ExtensionRuntimeError as exc:
            raise BrowserHarnessError(exc.error_code) from exc

    def _relay_click(self, x: float, y: float) -> None:
        if not math.isfinite(x) or not math.isfinite(y) or x < 0 or y < 0:
            raise BrowserHarnessError("browser_target_box_invalid")
        common = {
            "x": x,
            "y": y,
            "button": "left",
            "clickCount": 1,
        }
        self._relay_cdp("Input.dispatchMouseEvent", type="mousePressed", **common)
        self._relay_cdp("Input.dispatchMouseEvent", type="mouseReleased", **common)

    def _relay_switch_target(self, target_id: str) -> None:
        try:
            result = self.manager.dispatch(
                self._active_runtime_id,
                kind="switch_target",
                params={"target_id": target_id},
            )
            self.manager.update_binding(
                self._active_runtime_id,
                target_id=str(result.get("target_id", "")),
                page_url=str(result.get("page_url", "")),
            )
        except ExtensionRuntimeError as exc:
            raise BrowserHarnessError(exc.error_code) from exc

    def _relay_current_target(self) -> str:
        try:
            return self.manager.current_target_id(self._active_runtime_id)
        except ExtensionRuntimeError as exc:
            raise BrowserHarnessError(exc.error_code) from exc


__all__ = [
    "BrowserExtensionClient",
    "BrowserExtensionRuntimeManager",
    "ExtensionRuntimeError",
]
