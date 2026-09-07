from __future__ import annotations

import importlib
import math
import os
import re
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping
from urllib.parse import urljoin, urlsplit

from .live_page_capture import (
    LivePageCaptureError,
    capture_live_page_payload,
)
from .models import BrowserTarget, VisualTarget
from .url_policy import ExecutionURLPolicy, ExecutionURLPolicyError


class BrowserHarnessError(RuntimeError):
    def __init__(self, error_code: str):
        super().__init__(error_code)
        self.error_code = error_code


class BrowserHarnessClient:
    """Small, fixed-capability client for the Browser Harness daemon."""

    executor_id = "browser_harness"
    _TARGET_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{8,128}$")

    def __init__(
        self,
        *,
        cdp_url: str | None = None,
        workspace: Path,
        url_policy: ExecutionURLPolicy,
        cdp_call: Callable[..., Mapping[str, Any]] | None = None,
        click_call: Callable[[float, float], Any] | None = None,
        switch_tab_call: Callable[[str], Any] | None = None,
        current_tab_call: Callable[[], Any] | None = None,
        new_tab_call: Callable[[str], Any] | None = None,
        ensure_daemon_call: Callable[[], Any] | None = None,
    ):
        self.cdp_url = self._loopback_cdp_url(cdp_url) if cdp_url else None
        self.workspace = Path(workspace).resolve()
        self.url_policy = url_policy
        self._cdp_call = cdp_call
        self._click_call = click_call
        self._switch_tab_call = switch_tab_call
        self._current_tab_call = current_tab_call
        self._new_tab_call = new_tab_call
        self._ensure_daemon_call = ensure_daemon_call
        self._ready = False
        self._bound_target_id = ""
        self._bound_page_url = ""
        self._bound_session_id = ""
        self._session_lock = threading.RLock()

    @contextmanager
    def exclusive_session(self) -> Iterator[None]:
        """Keep planning, scenarios and the asset-action API from sharing a turn."""

        if not self._session_lock.acquire(blocking=False):
            raise BrowserHarnessError("execution_runner_busy")
        try:
            yield
        finally:
            self._session_lock.release()

    def open_or_bind_target(
        self,
        start_url: str,
        *,
        target_id: str = "",
    ) -> dict[str, str]:
        """Select, switch to, navigate and bind one target for this Scenario."""

        try:
            target_url = self.url_policy.validate(start_url)
        except ExecutionURLPolicyError as exc:
            raise BrowserHarnessError(exc.error_code) from exc
        try:
            # Reconnect only at a new binding boundary, never replay a type/click.
            if self._ready and not self.runtime_health()["ready"]:
                self._ready = False
                self._bound_session_id = ""
            self._load_runtime()
            requested_target_id = str(target_id).strip()
            if requested_target_id:
                if not self._TARGET_ID_PATTERN.fullmatch(requested_target_id):
                    raise BrowserHarnessError("browser_target_id_invalid")
                matches = self._page_targets_by_id(requested_target_id)
                if (
                    len(matches) != 1
                    or str(matches[0].get("url", "")).strip() != target_url
                ):
                    raise BrowserHarnessError("browser_target_binding_mismatch")
                bound_target_id = requested_target_id
                self._switch_tab(bound_target_id)
            else:
                matches = self._page_targets(target_url)
                if len(matches) == 1:
                    bound_target_id = str(matches[0].get("targetId", "")).strip()
                    self._switch_tab(bound_target_id)
                elif len(matches) > 1:
                    raise BrowserHarnessError("browser_target_ambiguous")
                else:
                    bound_target_id = self._open_new_target(target_url)
            self._confirm_active_target(bound_target_id)
            final_url = self._settle_page_url()
            if requested_target_id and final_url != target_url:
                raise BrowserHarnessError("browser_target_binding_mismatch")
        except BrowserHarnessError:
            raise
        except Exception as exc:
            raise BrowserHarnessError("browser_target_binding_failed") from exc

        self._bound_target_id = bound_target_id
        self._bound_page_url = final_url
        return {"target_id": bound_target_id, "page_url": final_url}

    def bind_page_target(self, page_url: str) -> dict[str, str]:
        """Switch the Browser Harness session to one exact live page target."""

        try:
            target_url = self.url_policy.validate(page_url)
        except ExecutionURLPolicyError as exc:
            raise BrowserHarnessError(exc.error_code) from exc
        try:
            self._load_runtime()
            matches = self._page_targets(target_url)
            if len(matches) != 1:
                raise BrowserHarnessError(
                    "browser_target_missing" if not matches else "browser_target_ambiguous"
                )
            target_id = str(matches[0].get("targetId", "")).strip()
            self._switch_tab(target_id)
            self._confirm_active_target(target_id)
        except BrowserHarnessError:
            raise
        except Exception as exc:
            raise BrowserHarnessError("browser_target_binding_failed") from exc
        self._bound_target_id = target_id
        self._bound_page_url = target_url
        return {"target_id": target_id, "page_url": target_url}

    def click_backend_node(
        self,
        target: BrowserTarget,
    ) -> dict[str, Any]:
        backend_node_id = target.backend_node_id
        click_backend_node_id = target.click_backend_node_id
        if (
            isinstance(backend_node_id, bool)
            or not isinstance(backend_node_id, int)
            or backend_node_id < 1
            or isinstance(click_backend_node_id, bool)
            or not isinstance(click_backend_node_id, int)
            or click_backend_node_id < 1
        ):
            raise BrowserHarnessError("invalid_backend_node_id")
        try:
            self._load_runtime()
            self._assert_bound_target(target.page_url)
        except BrowserHarnessError:
            raise
        except Exception as exc:
            raise BrowserHarnessError("browser_harness_unavailable") from exc
        try:
            expected_attributes = dict(target.expected_attributes)
            destination_url = self._destination_url(
                target.page_url,
                expected_attributes,
            )
            popup_url = (
                destination_url
                if expected_attributes.get("target", "").casefold() == "_blank"
                else ""
            )
            targets_before_click = (
                self._page_target_ids() if popup_url else frozenset()
            )
            page = self._cdp_call("Page.getFrameTree")
            current_url = str(
                page.get("frameTree", {}).get("frame", {}).get("url", "")
            ).strip()
            if current_url != target.page_url:
                raise BrowserHarnessError("browser_page_changed")
            live_frame = self._frame_by_id(page.get("frameTree"), target.frame_id)
            if live_frame is None or str(live_frame.get("url", "")).strip() != target.frame_url:
                raise BrowserHarnessError("browser_target_frame_changed")
            described = self._cdp_call(
                "DOM.describeNode",
                backendNodeId=backend_node_id,
                depth=3,
                pierce=True,
            )
            live_node = described.get("node")
            if not isinstance(live_node, Mapping):
                raise BrowserHarnessError("browser_target_changed")
            live_backend_id = live_node.get("backendNodeId")
            if live_backend_id != backend_node_id:
                raise BrowserHarnessError("browser_target_changed")
            attributes = self._attributes(live_node.get("attributes"))
            if (
                (target.dom_id and attributes.get("id") != target.dom_id)
                or any(
                    attributes.get(name) != value
                    for name, value in target.expected_attributes
                )
                or (
                    target.owner_business_id
                    and attributes.get("data-owner-business-id")
                    != target.owner_business_id
                )
                or (
                    target.action_id
                    and attributes.get("data-action-id") != target.action_id
                )
            ):
                raise BrowserHarnessError("browser_target_changed")
            authorized_backend_ids = self._described_subtree_backend_ids(
                live_node,
                root_backend_node_id=backend_node_id,
            )
            if not target.dom_id and not target.expected_attributes:
                self._validate_accessible_identity(
                    target,
                    authorized_backend_ids=authorized_backend_ids,
                )
            if click_backend_node_id not in authorized_backend_ids:
                raise BrowserHarnessError("browser_target_changed")
            click_backend_ids = self._described_subtree_backend_ids(
                live_node,
                root_backend_node_id=click_backend_node_id,
            )
            response = self._cdp_call(
                "DOM.getBoxModel",
                backendNodeId=click_backend_node_id,
            )
            quad = response.get("model", {}).get("content")
            x, y = self._quad_center(quad)
            metrics = self._cdp_call("Page.getLayoutMetrics")
            viewport = metrics.get("cssVisualViewport") or metrics.get(
                "cssLayoutViewport"
            )
            if not self._inside_viewport(x, y, viewport):
                raise BrowserHarnessError("browser_target_not_visible")
            hit = self._cdp_call(
                "DOM.getNodeForLocation",
                x=int(round(x)),
                y=int(round(y)),
            )
            if hit.get("backendNodeId") not in click_backend_ids:
                raise BrowserHarnessError("browser_target_occluded")
            self._click_call(x, y)
            if popup_url:
                self._bind_popup_target(
                    before_target_ids=targets_before_click,
                    opener_target_id=self._bound_target_id,
                    expected_url=popup_url,
                )
        except BrowserHarnessError:
            raise
        except Exception as exc:
            raise BrowserHarnessError("browser_harness_click_failed") from exc
        return {
            "backend_node_id": backend_node_id,
            "x": x,
            "y": y,
        }

    def type_backend_node(
        self,
        target: BrowserTarget,
        text: str,
    ) -> dict[str, Any]:
        backend_node_id = target.backend_node_id
        if (
            isinstance(backend_node_id, bool)
            or not isinstance(backend_node_id, int)
            or backend_node_id < 1
        ):
            raise BrowserHarnessError("invalid_backend_node_id")
        if (
            not isinstance(text, str)
            or not text
            or len(text) > 1_000
            or any(ord(character) < 32 or ord(character) == 127 for character in text)
        ):
            raise BrowserHarnessError("browser_input_text_invalid")
        if target.role not in {"textbox", "searchbox", "combobox"}:
            raise BrowserHarnessError("browser_input_target_invalid")
        try:
            self._load_runtime()
            self._assert_bound_target(target.page_url)
            page = self._cdp_call("Page.getFrameTree")
            current_url = str(
                page.get("frameTree", {}).get("frame", {}).get("url", "")
            ).strip()
            if current_url != target.page_url:
                raise BrowserHarnessError("browser_page_changed")
            live_frame = self._frame_by_id(page.get("frameTree"), target.frame_id)
            if (
                live_frame is None
                or str(live_frame.get("url", "")).strip() != target.frame_url
            ):
                raise BrowserHarnessError("browser_target_frame_changed")
            described = self._cdp_call(
                "DOM.describeNode",
                backendNodeId=backend_node_id,
                depth=0,
                pierce=True,
            )
            live_node = described.get("node")
            if (
                not isinstance(live_node, Mapping)
                or live_node.get("backendNodeId") != backend_node_id
            ):
                raise BrowserHarnessError("browser_target_changed")
            node_name = str(live_node.get("nodeName", "")).upper()
            attributes = self._attributes(live_node.get("attributes"))
            if (
                node_name not in {"INPUT", "TEXTAREA"}
                or attributes.get("type", "text").casefold()
                in {"password", "file", "hidden"}
                or "disabled" in attributes
                or "readonly" in attributes
                or (target.dom_id and attributes.get("id") != target.dom_id)
                or any(
                    attributes.get(name) != value
                    for name, value in target.expected_attributes
                )
                or (
                    target.owner_business_id
                    and attributes.get("data-owner-business-id")
                    != target.owner_business_id
                )
                or (
                    target.action_id
                    and attributes.get("data-action-id") != target.action_id
                )
            ):
                raise BrowserHarnessError("browser_input_target_invalid")
            if not target.dom_id and not target.expected_attributes:
                self._validate_accessible_identity(target)
            response = self._cdp_call(
                "DOM.getBoxModel",
                backendNodeId=backend_node_id,
            )
            x, y = self._quad_center(response.get("model", {}).get("content"))
            metrics = self._cdp_call("Page.getLayoutMetrics")
            viewport = metrics.get("cssVisualViewport") or metrics.get(
                "cssLayoutViewport"
            )
            if not self._inside_viewport(x, y, viewport):
                raise BrowserHarnessError("browser_target_not_visible")
            hit = self._cdp_call(
                "DOM.getNodeForLocation",
                x=int(round(x)),
                y=int(round(y)),
            )
            if hit.get("backendNodeId") != backend_node_id:
                raise BrowserHarnessError("browser_target_occluded")
            self._cdp_call("DOM.focus", backendNodeId=backend_node_id)
            self._cdp_call(
                "Input.dispatchKeyEvent",
                type="keyDown",
                modifiers=2,
                key="a",
                code="KeyA",
                windowsVirtualKeyCode=65,
            )
            self._cdp_call(
                "Input.dispatchKeyEvent",
                type="keyUp",
                modifiers=2,
                key="a",
                code="KeyA",
                windowsVirtualKeyCode=65,
            )
            self._cdp_call(
                "Input.dispatchKeyEvent",
                type="rawKeyDown",
                key="Backspace",
                code="Backspace",
                windowsVirtualKeyCode=8,
            )
            self._cdp_call(
                "Input.dispatchKeyEvent",
                type="keyUp",
                key="Backspace",
                code="Backspace",
                windowsVirtualKeyCode=8,
            )
            self._cdp_call("Input.insertText", text=text)
        except BrowserHarnessError:
            raise
        except Exception as exc:
            raise BrowserHarnessError("browser_harness_type_failed") from exc
        return {"backend_node_id": backend_node_id}

    def _destination_url(
        self,
        page_url: str,
        attributes: Mapping[str, str],
    ) -> str:
        """Validate a live link destination before dispatching its click."""

        href = attributes.get("href", "").strip()
        if not href or href.startswith("#"):
            return ""
        try:
            return self.url_policy.validate(urljoin(page_url, href))
        except ExecutionURLPolicyError as exc:
            raise BrowserHarnessError(exc.error_code) from exc

    def _page_target_ids(self) -> frozenset[str]:
        result = self._cdp_call("Target.getTargets")
        targets = result.get("targetInfos")
        if not isinstance(targets, list):
            return frozenset()
        return frozenset(
            str(item.get("targetId", "")).strip()
            for item in targets
            if isinstance(item, Mapping)
            and item.get("type") == "page"
            and str(item.get("targetId", "")).strip()
        )

    def _bind_popup_target(
        self,
        *,
        before_target_ids: frozenset[str],
        opener_target_id: str,
        expected_url: str,
    ) -> None:
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            result = self._cdp_call("Target.getTargets")
            targets = result.get("targetInfos")
            matches = []
            if isinstance(targets, list):
                for item in targets:
                    if not isinstance(item, Mapping) or item.get("type") != "page":
                        continue
                    target_id = str(item.get("targetId", "")).strip()
                    raw_url = str(item.get("url", "")).strip()
                    if not target_id or target_id in before_target_ids:
                        continue
                    if (
                        str(item.get("openerId", "")).strip() != opener_target_id
                        and raw_url != expected_url
                    ):
                        continue
                    try:
                        page_url = self.url_policy.validate(raw_url)
                    except ExecutionURLPolicyError:
                        continue
                    matches.append((target_id, page_url))
            if len(matches) > 1:
                raise BrowserHarnessError("browser_target_ambiguous")
            if len(matches) == 1:
                target_id, page_url = matches[0]
                self._switch_tab(target_id)
                self._confirm_active_target(target_id)
                self._bound_target_id = target_id
                self._bound_page_url = page_url
                return
            time.sleep(0.05)

    def _validate_accessible_identity(
        self,
        target: BrowserTarget,
        *,
        authorized_backend_ids: frozenset[int] | None = None,
    ) -> None:
        if not target.accessible_name or not target.role:
            raise BrowserHarnessError("browser_target_changed")
        if target.accessible_name_from_descendant:
            semantic_backend_id = target.accessible_name_backend_node_id
            if (
                authorized_backend_ids is None
                or not isinstance(semantic_backend_id, int)
                or isinstance(semantic_backend_id, bool)
                or semantic_backend_id < 1
                or semantic_backend_id not in authorized_backend_ids
            ):
                raise BrowserHarnessError("browser_target_semantic_identity_changed")
            return
        result = self._cdp_call(
            "Accessibility.queryAXTree",
            backendNodeId=target.backend_node_id,
        )
        nodes = result.get("nodes")
        if not isinstance(nodes, list):
            raise BrowserHarnessError("browser_target_changed")
        root_matches = [
            node
            for node in nodes
            if isinstance(node, Mapping)
            and node.get("backendDOMNodeId") == target.backend_node_id
            and self._ax_value(node.get("role")).casefold() == target.role
        ]
        matches = [
            node
            for node in root_matches
            if self._ax_value(node.get("name")) == target.accessible_name
        ]
        if len(matches) != 1:
            raise BrowserHarnessError("browser_target_changed")

    @classmethod
    def _described_subtree_backend_ids(
        cls,
        node: Mapping[str, Any],
        *,
        root_backend_node_id: int,
    ) -> frozenset[int]:
        pending = [node]
        root: Mapping[str, Any] | None = None
        inspected = 0
        while pending and inspected < 64:
            current = pending.pop(0)
            inspected += 1
            if current.get("backendNodeId") == root_backend_node_id:
                root = current
                break
            nested = current.get("children")
            if isinstance(nested, list):
                pending.extend(item for item in nested if isinstance(item, Mapping))
        if root is None:
            return frozenset()

        result: set[int] = set()
        pending = [root]
        while pending and len(result) < 64:
            current = pending.pop(0)
            backend_id = current.get("backendNodeId")
            if isinstance(backend_id, int) and not isinstance(backend_id, bool):
                result.add(backend_id)
            nested = current.get("children")
            if isinstance(nested, list):
                pending.extend(item for item in nested if isinstance(item, Mapping))
        return frozenset(result)

    @staticmethod
    def _ax_value(value: Any) -> str:
        if isinstance(value, Mapping):
            return str(value.get("value", "")).strip()
        return str(value or "").strip()

    def click_visual_target(self, target: VisualTarget) -> dict[str, Any]:
        backend_node_id = target.canvas_backend_node_id
        if (
            isinstance(backend_node_id, bool)
            or not isinstance(backend_node_id, int)
            or backend_node_id < 1
            or not 0 < target.x_ratio < 1
            or not 0 < target.y_ratio < 1
        ):
            raise BrowserHarnessError("invalid_canvas_target")
        try:
            self._load_runtime()
            self._assert_bound_target(target.page_url)
            page = self._cdp_call("Page.getFrameTree")
            current_url = str(
                page.get("frameTree", {}).get("frame", {}).get("url", "")
            ).strip()
            if current_url != target.page_url:
                raise BrowserHarnessError("browser_page_changed")
            live_frame = self._frame_by_id(page.get("frameTree"), target.frame_id)
            if live_frame is None or str(live_frame.get("url", "")).strip() != target.frame_url:
                raise BrowserHarnessError("browser_target_frame_changed")
            described = self._cdp_call(
                "DOM.describeNode",
                backendNodeId=backend_node_id,
                depth=0,
                pierce=True,
            )
            live_node = described.get("node")
            if not isinstance(live_node, Mapping):
                raise BrowserHarnessError("browser_canvas_changed")
            attributes = self._attributes(live_node.get("attributes"))
            if (
                live_node.get("backendNodeId") != backend_node_id
                or attributes.get("id") != target.canvas_dom_id
            ):
                raise BrowserHarnessError("browser_canvas_changed")
            response = self._cdp_call(
                "DOM.getBoxModel", backendNodeId=backend_node_id
            )
            quad = response.get("model", {}).get("content")
            x, y = self._quad_point(quad, target.x_ratio, target.y_ratio)
            metrics = self._cdp_call("Page.getLayoutMetrics")
            viewport = metrics.get("cssVisualViewport") or metrics.get(
                "cssLayoutViewport"
            )
            if not self._inside_viewport(x, y, viewport):
                raise BrowserHarnessError("browser_target_not_visible")
            hit = self._cdp_call(
                "DOM.getNodeForLocation", x=int(round(x)), y=int(round(y))
            )
            if hit.get("backendNodeId") != backend_node_id:
                raise BrowserHarnessError("browser_target_occluded")
            self._click_call(x, y)
        except BrowserHarnessError:
            raise
        except Exception as exc:
            raise BrowserHarnessError("browser_harness_click_failed") from exc
        return {"backend_node_id": backend_node_id, "x": x, "y": y}

    def capture_page_payload(
        self,
        *,
        include_canvas: bool = True,
    ) -> dict[str, Any]:
        """Capture the live page through fixed CDP methods for the E2E harness."""

        try:
            self._load_runtime()
            self._assert_bound_target(None)
            return capture_live_page_payload(
                self._cdp_call,
                url_policy=self.url_policy,
                include_canvas=include_canvas,
            )
        except BrowserHarnessError:
            raise
        except LivePageCaptureError as exc:
            raise BrowserHarnessError(exc.error_code) from exc
        except Exception as exc:
            raise BrowserHarnessError("browser_harness_capture_failed") from exc

    @staticmethod
    def _attributes(value: Any) -> dict[str, str]:
        if not isinstance(value, (list, tuple)) or len(value) % 2:
            raise BrowserHarnessError("browser_target_changed")
        attributes: dict[str, str] = {}
        for index in range(0, len(value), 2):
            name = str(value[index]).strip()
            if name:
                attributes[name] = str(value[index + 1])
        return attributes

    @classmethod
    def _frame_by_id(
        cls,
        value: Any,
        frame_id: str,
    ) -> Mapping[str, Any] | None:
        if not isinstance(value, Mapping):
            return None
        frame = value.get("frame")
        if isinstance(frame, Mapping) and str(frame.get("id", "")) == frame_id:
            return frame
        children = value.get("childFrames")
        if isinstance(children, list):
            for child in children:
                matched = cls._frame_by_id(child, frame_id)
                if matched is not None:
                    return matched
        return None

    def _load_runtime(self) -> None:
        if self._ready:
            return
        if self._cdp_call is None or self._click_call is None:
            self.workspace.mkdir(parents=True, exist_ok=True)
            if (self.workspace / "agent_helpers.py").exists():
                raise BrowserHarnessError("browser_harness_workspace_not_clean")
            os.environ["BH_AGENT_WORKSPACE"] = str(self.workspace)
            os.environ["BU_NAME"] = "kt6"
            if self.cdp_url:
                os.environ["BU_CDP_URL"] = self.cdp_url
                os.environ.pop("BU_CDP_WS", None)
            else:
                os.environ.pop("BU_CDP_URL", None)
                os.environ.pop("BU_CDP_WS", None)
            try:
                helpers = importlib.import_module("browser_harness.helpers")
                admin = importlib.import_module("browser_harness.admin")
            except ImportError as exc:
                raise BrowserHarnessError("browser_harness_not_installed") from exc
            helper_workspace = Path(helpers.AGENT_WORKSPACE).resolve()
            if helper_workspace != self.workspace:
                raise BrowserHarnessError("browser_harness_workspace_mismatch")

            def bound_cdp(method: str, **params: Any) -> Mapping[str, Any]:
                browser_command = method.startswith(("Target.", "Browser."))
                session_id = None if browser_command else self._bound_session_id
                if not browser_command and not session_id:
                    raise BrowserHarnessError("browser_session_not_bound")
                return helpers.cdp(method, session_id=session_id, **params)

            def switch_tab(target_id: str) -> None:
                session_id = helpers.switch_tab(target_id, activate=True)
                if not isinstance(session_id, str) or not session_id:
                    raise BrowserHarnessError("browser_target_binding_failed")
                self._bound_session_id = session_id

            def click(x: float, y: float) -> None:
                # Harness 0.1.9 click_at_xy cannot take a session ID. Dispatch
                # the same fixed pair through its session-aware CDP transport.
                for event_type in ("mousePressed", "mouseReleased"):
                    bound_cdp(
                        "Input.dispatchMouseEvent",
                        type=event_type,
                        x=x,
                        y=y,
                        button="left",
                        clickCount=1,
                    )

            self._cdp_call = bound_cdp
            self._click_call = click
            self._switch_tab_call = switch_tab
            self._current_tab_call = helpers.current_tab

            def open_visible_tab(url: str) -> Any:
                target_id = helpers.new_tab(url)
                switch_tab(target_id)
                return target_id

            self._new_tab_call = open_visible_tab
            self._ensure_daemon_call = admin.ensure_daemon
        try:
            if self._ensure_daemon_call is not None:
                self._ensure_daemon_call()
        except Exception as exc:
            message = str(exc).casefold()
            if "remote-debugging-setup" in message or "chrome://inspect" in message:
                error_code = "browser_harness_remote_debugging_required"
            elif "permission-blocked" in message or "allow remote debugging" in message:
                error_code = "browser_harness_permission_required"
            elif "chrome-not-running" in message:
                error_code = "browser_harness_chrome_not_running"
            else:
                error_code = "browser_harness_daemon_unavailable"
            raise BrowserHarnessError(error_code) from exc
        self._ready = True

    def runtime_health(self) -> dict[str, Any]:
        error_code = ""
        ready = False
        if self._ready and self._cdp_call is not None:
            try:
                version = self._cdp_call("Browser.getVersion")
                ready = isinstance(version, Mapping) and bool(version.get("product"))
            except Exception:
                error_code = "browser_harness_daemon_unavailable"
        elif self._cdp_call is None:
            error_code = "browser_harness_not_connected"
        return {
            "ready": ready,
            "transport": "browser_harness",
            "browser_harness": {
                "ready": ready,
                "mode": "existing_chrome",
                "error": error_code,
            },
        }

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

    @classmethod
    def _quad_point(
        cls,
        value: Any,
        x_ratio: float,
        y_ratio: float,
    ) -> tuple[float, float]:
        cls._quad_center(value)
        numbers = [float(item) for item in value]
        points = [(numbers[index], numbers[index + 1]) for index in range(0, 8, 2)]
        weights = (
            (1 - x_ratio) * (1 - y_ratio),
            x_ratio * (1 - y_ratio),
            x_ratio * y_ratio,
            (1 - x_ratio) * y_ratio,
        )
        x = sum(point[0] * weight for point, weight in zip(points, weights))
        y = sum(point[1] * weight for point, weight in zip(points, weights))
        if not math.isfinite(x) or not math.isfinite(y):
            raise BrowserHarnessError("browser_target_box_invalid")
        return round(x, 3), round(y, 3)

    def _assert_bound_target(self, expected_page_url: str | None) -> None:
        if not self._bound_target_id:
            raise BrowserHarnessError("browser_session_not_bound")
        if self._active_target_id() != self._bound_target_id:
            raise BrowserHarnessError("browser_session_target_changed")
        result = self._cdp_call("Target.getTargets")
        targets = result.get("targetInfos")
        matches = [
            item
            for item in targets
            if isinstance(item, Mapping)
            and str(item.get("targetId", "")).strip() == self._bound_target_id
            and str(item.get("type", "")) == "page"
        ] if isinstance(targets, list) else []
        if len(matches) != 1:
            raise BrowserHarnessError("browser_session_target_changed")
        current_url = str(matches[0].get("url", "")).strip()
        try:
            current_url = self.url_policy.validate(current_url)
        except ExecutionURLPolicyError as exc:
            raise BrowserHarnessError(exc.error_code) from exc
        if expected_page_url is not None and current_url != expected_page_url:
            raise BrowserHarnessError("browser_session_page_changed")

    def _page_targets(self, page_url: str) -> list[Mapping[str, Any]]:
        result = self._cdp_call("Target.getTargets")
        targets = result.get("targetInfos")
        if not isinstance(targets, list):
            return []
        return [
            item
            for item in targets
            if isinstance(item, Mapping)
            and str(item.get("type", "")) == "page"
            and str(item.get("url", "")).strip() == page_url
            and str(item.get("targetId", "")).strip()
        ]

    def _page_targets_by_id(self, target_id: str) -> list[Mapping[str, Any]]:
        result = self._cdp_call("Target.getTargets")
        targets = result.get("targetInfos")
        if not isinstance(targets, list):
            return []
        return [
            item
            for item in targets
            if isinstance(item, Mapping)
            and str(item.get("type", "")) == "page"
            and str(item.get("targetId", "")).strip() == target_id
        ]

    def _switch_tab(self, target_id: str) -> None:
        if self._switch_tab_call is None:
            raise BrowserHarnessError("browser_target_binding_failed")
        self._switch_tab_call(target_id)

    def _open_new_target(self, target_url: str) -> str:
        if self._new_tab_call is None:
            raise BrowserHarnessError("browser_target_missing")
        created = self._new_tab_call(target_url)
        target_id = self._target_id(created)
        if not target_id:
            target_id = self._active_target_id()
        if not target_id:
            raise BrowserHarnessError("browser_target_binding_failed")
        return target_id

    def _confirm_active_target(self, target_id: str) -> None:
        if self._active_target_id() != target_id:
            raise BrowserHarnessError("browser_session_target_changed")

    def _active_target_id(self) -> str:
        if self._current_tab_call is None:
            raise BrowserHarnessError("browser_harness_unavailable")
        return self._target_id(self._current_tab_call())

    def _settle_page_url(self) -> str:
        deadline = time.monotonic() + 15.0
        while time.monotonic() < deadline:
            frame_tree = self._cdp_call("Page.getFrameTree")
            current_url = str(
                frame_tree.get("frameTree", {}).get("frame", {}).get("url", "")
            ).strip()
            if current_url.startswith(("http://", "https://")):
                try:
                    return self.url_policy.validate(current_url)
                except ExecutionURLPolicyError as exc:
                    raise BrowserHarnessError(exc.error_code) from exc
            time.sleep(0.05)
        raise BrowserHarnessError("browser_navigation_timeout")

    def wait_for_page_settle(
        self, timeout_seconds: float = 3.0, *, previous_url: str = ""
    ) -> str:
        """Wait for a changed URL, then two consistent reads of that URL.

        This only observes navigation; fresh perception must still verify it.
        """

        try:
            timeout = float(timeout_seconds)
        except (TypeError, ValueError, OverflowError) as exc:
            raise BrowserHarnessError("browser_navigation_timeout") from exc
        if not math.isfinite(timeout) or not 0 < timeout <= 15.0:
            raise BrowserHarnessError("browser_navigation_timeout")
        self._load_runtime()
        deadline = time.monotonic() + timeout
        stable_url = ""
        stable_reads = 0
        last_error_code = ""
        while time.monotonic() < deadline:
            frame_tree = self._cdp_call("Page.getFrameTree")
            raw_url = str(
                frame_tree.get("frameTree", {}).get("frame", {}).get("url", "")
            ).strip()
            try:
                current_url = self.url_policy.validate(raw_url)
            except ExecutionURLPolicyError as exc:
                last_error_code = exc.error_code
                stable_url = ""
                stable_reads = 0
            else:
                last_error_code = ""
                if current_url == previous_url:
                    stable_url = ""
                    stable_reads = 0
                elif current_url == stable_url:
                    stable_reads += 1
                else:
                    stable_url = current_url
                    stable_reads = 1
                if stable_reads >= 2:
                    return current_url
            time.sleep(0.05)
        if last_error_code:
            raise BrowserHarnessError(last_error_code)
        raise BrowserHarnessError("browser_navigation_timeout")

    @staticmethod
    def _target_id(value: Any) -> str:
        if isinstance(value, Mapping):
            for key in ("targetId", "target_id", "id"):
                candidate = value.get(key)
                if isinstance(candidate, str) and candidate.strip():
                    return candidate.strip()
            return ""
        if isinstance(value, str):
            return value.strip()
        return ""

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
