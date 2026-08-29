from __future__ import annotations


PLANNER_FAILED = "planner_failed"
TARGET_NOT_FOUND = "target_not_found"
TARGET_AMBIGUOUS = "target_ambiguous"
PERCEPTION_FAILED = "perception_failed"
EXECUTION_FAILED = "execution_failed"
VERIFY_FAILED = "verify_failed"
PAGE_CHANGED = "page_changed"
UNCLASSIFIED = "unclassified"


# Ordered longest/most-specific first. A later generic prefix only matches codes
# that were not already claimed by an earlier, more specific rule.
_CATEGORY_RULES: tuple[tuple[str, str], ...] = (
    ("action_plan_", PLANNER_FAILED),
    ("execution_planner_", PLANNER_FAILED),
    ("execution_request_", PLANNER_FAILED),
    ("execution_planner_not_configured", PLANNER_FAILED),
    ("dom_grounding_target_missing", TARGET_NOT_FOUND),
    ("vision_grounding_target_missing", TARGET_NOT_FOUND),
    ("vision_grounding_not_configured", TARGET_NOT_FOUND),
    ("canvas_element_missing", TARGET_NOT_FOUND),
    ("browser_target_missing", TARGET_NOT_FOUND),
    ("dom_grounding_target_ambiguous", TARGET_AMBIGUOUS),
    ("vision_grounding_target_ambiguous", TARGET_AMBIGUOUS),
    ("canvas_element_ambiguous", TARGET_AMBIGUOUS),
    ("browser_target_ambiguous", TARGET_AMBIGUOUS),
    ("browser_page_changed", PAGE_CHANGED),
    ("browser_session_page_changed", PAGE_CHANGED),
    ("scenario_capture_incomplete", PERCEPTION_FAILED),
    ("grounding_ui_graph", PERCEPTION_FAILED),
    ("grounding_page_invalid", PERCEPTION_FAILED),
    ("vision_grounding_geometry_invalid", PERCEPTION_FAILED),
    ("browser_harness_capture_failed", PERCEPTION_FAILED),
    ("browser_page_unavailable", PERCEPTION_FAILED),
    ("browser_cdp_snapshot_invalid", PERCEPTION_FAILED),
    ("browser_frame_tree_invalid", PERCEPTION_FAILED),
    ("browser_viewport_unavailable", PERCEPTION_FAILED),
    ("browser_frame_limit_exceeded", PERCEPTION_FAILED),
    ("execution_canvas_capture", PERCEPTION_FAILED),
    ("browser_preview_capture_invalid", PERCEPTION_FAILED),
    ("browser_navigation", PERCEPTION_FAILED),
    ("scenario_expected_outcome_missing", VERIFY_FAILED),
    ("scenario_wait_timeout", VERIFY_FAILED),
    ("scenario_wait_without_action", VERIFY_FAILED),
    ("scenario_verification_without_action", VERIFY_FAILED),
    ("scenario_outcome_step_missing", VERIFY_FAILED),
    ("scenario_previous_outcome_unverified", VERIFY_FAILED),
    ("outcome_verifier", VERIFY_FAILED),
    ("outcome_ui_graph_missing", VERIFY_FAILED),
    ("browser_target_", EXECUTION_FAILED),
    ("browser_canvas_changed", EXECUTION_FAILED),
    ("browser_session", EXECUTION_FAILED),
    ("browser_extension_", EXECUTION_FAILED),
    ("browser_harness_", EXECUTION_FAILED),
    ("invalid_backend_node_id", EXECUTION_FAILED),
    ("invalid_canvas_target", EXECUTION_FAILED),
    ("invalid_visual_target", EXECUTION_FAILED),
    ("scenario_grounding", EXECUTION_FAILED),
    ("scenario_step_unsupported", EXECUTION_FAILED),
    ("unsupported_browser", EXECUTION_FAILED),
    ("execution_url_", EXECUTION_FAILED),
)


def classify_error(error_code: str) -> str:
    """Map a fine-grained execution error code to a stable failure category."""

    code = str(error_code or "").strip()
    if not code:
        return UNCLASSIFIED
    for prefix, category in _CATEGORY_RULES:
        if code.startswith(prefix):
            return category
    return UNCLASSIFIED


__all__ = [
    "EXECUTION_FAILED",
    "PAGE_CHANGED",
    "PERCEPTION_FAILED",
    "PLANNER_FAILED",
    "TARGET_AMBIGUOUS",
    "TARGET_NOT_FOUND",
    "UNCLASSIFIED",
    "VERIFY_FAILED",
    "classify_error",
]
