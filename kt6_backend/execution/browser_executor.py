from __future__ import annotations

from typing import Protocol

from .browser_harness_client import BrowserHarnessClient, BrowserHarnessError
from .models import BrowserAction, BrowserExecutionResult


class BrowserExecutor(Protocol):
    executor_id: str

    def execute(self, action: BrowserAction) -> BrowserExecutionResult:
        ...


class HarnessBrowserExecutor:
    """Click-only KT6 adapter around Browser Harness.

    The adapter accepts a resolved backend node. It never accepts arbitrary CDP
    methods, JavaScript, selectors, or model-generated Python.
    """

    executor_id = "browser_harness"

    def __init__(self, client: BrowserHarnessClient):
        self.client = client

    def execute(self, action: BrowserAction) -> BrowserExecutionResult:
        if action.op != "click":
            return BrowserExecutionResult(False, "unsupported_browser_action")
        try:
            receipt = self.client.click_backend_node(
                action.target.backend_node_id,
                expected_page_url=action.target.page_url,
            )
        except BrowserHarnessError as exc:
            return BrowserExecutionResult(False, exc.error_code)
        return BrowserExecutionResult(
            True,
            "",
            backend_node_id=receipt["backend_node_id"],
            x=receipt["x"],
            y=receipt["y"],
        )


__all__ = ["BrowserExecutor", "HarnessBrowserExecutor"]
