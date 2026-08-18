from __future__ import annotations

import re
import time
from collections.abc import Callable, Mapping
from typing import Any
from urllib.parse import urlsplit

from ..cdp_snapshot import normalize_cdp_snapshot


_SIMPLE_DOM_ID = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]*$")


class LivePageCaptureError(RuntimeError):
    def __init__(self, error_code: str):
        super().__init__(error_code)
        self.error_code = error_code


def reset_execution_fixture(
    cdp_call: Callable[..., Mapping[str, Any]],
    page_url: str,
) -> None:
    parsed = urlsplit(str(page_url).strip())
    if (
        parsed.scheme != "http"
        or (parsed.hostname or "").casefold() not in {"localhost", "127.0.0.1"}
        or parsed.path != "/execution-test.html"
        or parsed.username
        or parsed.password
        or parsed.fragment
    ):
        raise LivePageCaptureError("execution_fixture_url_invalid")
    response = cdp_call("Page.navigate", url=page_url)
    if response.get("errorText"):
        raise LivePageCaptureError("execution_fixture_navigation_failed")
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        frame_tree = cdp_call("Page.getFrameTree")
        current_url = str(
            frame_tree.get("frameTree", {}).get("frame", {}).get("url", "")
        ).strip()
        if current_url == page_url:
            time.sleep(0.1)
            return
        time.sleep(0.05)
    raise LivePageCaptureError("execution_fixture_navigation_timeout")


def capture_live_page_payload(
    cdp_call: Callable[..., Mapping[str, Any]],
    *,
    expected_page_url: str,
) -> dict[str, Any]:
    page = cdp_call("Page.getFrameTree")
    frame_tree = page.get("frameTree")
    main_frame = frame_tree.get("frame") if isinstance(frame_tree, Mapping) else None
    if not isinstance(main_frame, Mapping):
        raise LivePageCaptureError("browser_page_unavailable")
    page_url = str(main_frame.get("url", "")).strip()
    if page_url != expected_page_url:
        raise LivePageCaptureError("browser_page_changed")
    frames = _flatten_frames(frame_tree)
    snapshot = cdp_call(
        "DOMSnapshot.captureSnapshot",
        computedStyles=[],
        includePaintOrder=True,
        includeDOMRects=True,
    )
    ax_nodes: list[Mapping[str, Any]] = []
    frame_errors = []
    for frame in frames:
        try:
            result = cdp_call(
                "Accessibility.getFullAXTree",
                frameId=frame["frame_id"],
            )
        except Exception:
            frame_errors.append(
                {
                    "frame_id": frame["frame_id"],
                    "code": "ax_tree_unavailable",
                }
            )
            continue
        values = result.get("nodes")
        if isinstance(values, list):
            ax_nodes.extend(
                dict(node, frameId=node.get("frameId") or frame["frame_id"])
                for node in values
                if isinstance(node, Mapping)
            )
    normalized = normalize_cdp_snapshot(snapshot, {"nodes": ax_nodes})
    version = cdp_call("Browser.getVersion")
    metrics = cdp_call("Page.getLayoutMetrics")
    viewport = metrics.get("cssVisualViewport") or metrics.get(
        "cssLayoutViewport"
    )
    if not isinstance(viewport, Mapping):
        raise LivePageCaptureError("browser_viewport_unavailable")
    width = int(float(viewport.get("clientWidth", 0)))
    height = int(float(viewport.get("clientHeight", 0)))
    if width < 1 or height < 1:
        raise LivePageCaptureError("browser_viewport_unavailable")
    title = "KT6 Execution Harness"
    for document in normalized.get("frames", []):
        if (
            isinstance(document, Mapping)
            and document.get("frame_id") == main_frame.get("id")
        ):
            title = str(document.get("title") or title)[:300]
            break
    envelope = {
        "schema_version": "kt6.cdp-page-snapshot.v1",
        "captured_at": time.time(),
        "page": {"url": page_url, "title": title},
        "source_metadata": {
            "source_type": "browser_harness_cdp_capture",
            "browser_product": str(version.get("product", ""))[:200],
            "protocol_version": str(version.get("protocolVersion", ""))[:100],
            "capture_only": True,
            "safe_for_execution": False,
        },
        "frames": frames,
        "frame_errors": frame_errors,
        "dom_snapshot": snapshot,
        "ax_tree": {"nodes": ax_nodes},
        "actionable_grounding": False,
        "safe_for_execution": False,
    }
    return {
        "page": {
            "url": page_url,
            "title": title,
            "language": "zh-CN",
            "ui_version": "execution-harness-v1",
            "viewport": {
                "width": width,
                "height": height,
                "device_pixel_ratio": 1,
            },
        },
        "dom": _dom_projection(normalized),
        "canvases": [],
        "adapter_scene": None,
        "cdp_snapshot": envelope,
        "captured_at": time.time(),
    }


def _flatten_frames(value: Any) -> list[dict[str, str]]:
    if not isinstance(value, Mapping):
        raise LivePageCaptureError("browser_frame_tree_invalid")
    frames: list[dict[str, str]] = []
    pending = [value]
    while pending:
        item = pending.pop(0)
        frame = item.get("frame") if isinstance(item, Mapping) else None
        if not isinstance(frame, Mapping) or not frame.get("id"):
            raise LivePageCaptureError("browser_frame_tree_invalid")
        frames.append(
            {
                "frame_id": str(frame["id"]),
                "parent_frame_id": str(frame.get("parentId", "")),
                "url": str(frame.get("url", ""))[:2048],
                "name": str(frame.get("name", ""))[:200],
                "security_origin": str(frame.get("securityOrigin", ""))[:500],
            }
        )
        if len(frames) > 64:
            raise LivePageCaptureError("browser_frame_limit_exceeded")
        children = item.get("childFrames")
        if isinstance(children, list):
            pending.extend(child for child in children if isinstance(child, Mapping))
    return frames


def _dom_projection(scene: Mapping[str, Any]) -> dict[str, Any]:
    raw_nodes = scene.get("nodes")
    raw_frames = scene.get("frames")
    if not isinstance(raw_nodes, list) or not isinstance(raw_frames, list):
        raise LivePageCaptureError("browser_cdp_snapshot_invalid")
    frame_records = {
        str(frame.get("frame_id", "")): frame
        for frame in raw_frames
        if isinstance(frame, Mapping) and frame.get("frame_id")
    }
    public_nodes = [
        node
        for node in raw_nodes
        if isinstance(node, Mapping) and node.get("dom_node_type") == 1
    ]
    refs: dict[str, str] = {}
    for node in public_nodes:
        node_id = str(node.get("node_id", ""))
        attributes = node.get("attributes")
        attributes = attributes if isinstance(attributes, Mapping) else {}
        dom_id = str(attributes.get("id", "")).strip()
        backend_id = node.get("backend_node_id")
        if dom_id and _SIMPLE_DOM_ID.fullmatch(dom_id):
            refs[node_id] = f"frame:{node.get('frame_id')}:#{dom_id}"
        else:
            refs[node_id] = f"frame:{node.get('frame_id')}:backend:{backend_id}"
    elements = []
    for order, node in enumerate(public_nodes):
        attributes = node.get("attributes")
        attributes = attributes if isinstance(attributes, Mapping) else {}
        frame_id = str(node.get("frame_id", ""))
        frame = frame_records.get(frame_id, {})
        dom_id = str(attributes.get("id", "")).strip()
        bounds = node.get("bounds")
        if not isinstance(bounds, list) or len(bounds) != 4:
            bounds = [0, 0, 0, 0]
        parent_ref = refs.get(str(node.get("parent_id", "")), "")
        elements.append(
            {
                "ref": refs[str(node.get("node_id", ""))],
                "selector": f"#{dom_id}"
                if dom_id and _SIMPLE_DOM_ID.fullmatch(dom_id)
                else "",
                "parent_ref": parent_ref,
                "parent_relation": "direct_parent" if parent_ref else "root",
                "document_order": order,
                "tag": str(node.get("dom_node_name", "")).casefold(),
                "role": str(node.get("role", "")),
                "label": str(node.get("name", ""))[:300],
                "aria_label": str(attributes.get("aria-label", ""))[:300],
                "business_id": str(
                    attributes.get("data-business-id")
                    or attributes.get("data-asset-id")
                    or ""
                )[:200],
                "asset_id": str(attributes.get("data-asset-id", ""))[:200],
                "management_ip": str(
                    attributes.get("data-management-ip", "")
                )[:100],
                "serial_number": str(
                    attributes.get("data-serial-number", "")
                )[:200],
                "site_id": str(attributes.get("data-site-id", ""))[:200],
                "asset_version": attributes.get("data-asset-version"),
                "action_id": str(attributes.get("data-action-id", ""))[:200],
                "owner_business_id": str(
                    attributes.get("data-owner-business-id", "")
                )[:200],
                "test_id": str(attributes.get("data-testid", ""))[:200],
                "bbox": list(bounds),
                "disabled": bool(node.get("disabled", False)),
                "actionable": bool(node.get("interaction_candidate", False)),
                "frame_id": frame_id,
                "frame_url": str(
                    frame.get("document_url") or frame.get("url") or ""
                )[:2048],
                "document_id": f"cdp-document:{node.get('document_index', 0)}",
            }
        )
    return {
        "elements": elements,
        "stats": {
            "frame_count": len(frame_records),
            "captured_element_count": len(elements),
            "truncated": False,
            "scan_truncated": False,
            "open_shadow_root_count": 0,
        },
        "coverage": {
            "source_complete": True,
            "action_binding_complete": True,
            "truncated": False,
        },
    }


__all__ = [
    "LivePageCaptureError",
    "capture_live_page_payload",
    "reset_execution_fixture",
]
