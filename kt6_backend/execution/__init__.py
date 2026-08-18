"""Controlled browser execution primitives for KT6."""

from .browser_executor import BrowserExecutor, HarnessBrowserExecutor
from .browser_harness_client import BrowserHarnessClient
from .grounding import TargetGrounderRegistry
from .models import (
    BrowserAction,
    BrowserExecutionResult,
    BrowserTarget,
    CanvasTarget,
)
from .natural_language_parser import NaturalLanguageIntentParser
from .plan_generator import FixturePlanGenerator
from .plan_validator import ActionPlanValidator
from .scenario_runner import ScenarioRunner
from .scenario_service import ExecutionScenarioService
from .target_resolver import TargetResolutionError, UIGraphTargetResolver
from .verifier import (
    AssetDetailOutcomeVerifier,
    CanvasSelectionVerifier,
    OutcomeVerifier,
    PageReadyVerifier,
)
from .verifier_registry import OutcomeVerifierRegistry

__all__ = [
    "BrowserAction",
    "BrowserExecutionResult",
    "BrowserExecutor",
    "BrowserHarnessClient",
    "BrowserTarget",
    "CanvasTarget",
    "AssetDetailOutcomeVerifier",
    "ActionPlanValidator",
    "CanvasSelectionVerifier",
    "ExecutionScenarioService",
    "FixturePlanGenerator",
    "HarnessBrowserExecutor",
    "NaturalLanguageIntentParser",
    "OutcomeVerifier",
    "OutcomeVerifierRegistry",
    "PageReadyVerifier",
    "ScenarioRunner",
    "TargetGrounderRegistry",
    "TargetResolutionError",
    "UIGraphTargetResolver",
]
