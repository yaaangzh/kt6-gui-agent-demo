from __future__ import annotations

from typing import Protocol

from .browser_harness_client import BrowserHarnessClient, BrowserHarnessError
from .models import BrowserAction, BrowserExecutionResult, BrowserTarget, VisualTarget


class BrowserExecutor(Protocol):
    executor_id: str

    def execute(self, action: BrowserAction) -> BrowserExecutionResult:
        ...


class HarnessBrowserExecutor:
    """Fixed click/type KT6 adapter around Browser Harness.

    The adapter accepts a resolved backend node. It never accepts arbitrary CDP
    methods, JavaScript, selectors, or model-generated Python.
    """

    executor_id = "browser_harness"

    def __init__(self, client: BrowserHarnessClient):
        self.client = client
        self.executor_id = str(
            getattr(client, "executor_id", self.executor_id)
        )

    def execute(self, action: BrowserAction) -> BrowserExecutionResult:
        if action.op not in {"click", "type"}:
            return BrowserExecutionResult(False, "unsupported_browser_action")
        try:
            with self.client.exclusive_session():
                if action.op == "type" and isinstance(action.target, BrowserTarget):
                    receipt = self.client.type_backend_node(action.target, action.text)
                elif action.op == "click" and isinstance(action.target, BrowserTarget):
                    receipt = self.client.click_backend_node(action.target)
                elif action.op == "click" and isinstance(action.target, VisualTarget):
                    receipt = self.client.click_visual_target(action.target)
                else:
                    return BrowserExecutionResult(False, "unsupported_browser_target")
        except BrowserHarnessError as exc:
            return BrowserExecutionResult(False, exc.error_code)
        return BrowserExecutionResult(
            True,
            "",
            backend_node_id=receipt["backend_node_id"],
            x=receipt.get("x"),
            y=receipt.get("y"),
        )


__all__ = ["BrowserExecutor", "HarnessBrowserExecutor"]
