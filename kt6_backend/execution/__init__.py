"""Controlled browser execution primitives for KT6."""

from .browser_executor import BrowserExecutor, HarnessBrowserExecutor
from .browser_harness_client import BrowserHarnessClient
from .models import (
    BrowserAction,
    BrowserExecutionResult,
    BrowserTarget,
)
from .target_resolver import TargetResolutionError, UIGraphTargetResolver
from .verifier import OutcomeVerifier

__all__ = [
    "BrowserAction",
    "BrowserExecutionResult",
    "BrowserExecutor",
    "BrowserHarnessClient",
    "BrowserTarget",
    "HarnessBrowserExecutor",
    "OutcomeVerifier",
    "TargetResolutionError",
    "UIGraphTargetResolver",
]
