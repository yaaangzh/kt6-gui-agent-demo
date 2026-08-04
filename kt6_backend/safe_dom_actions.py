from __future__ import annotations

import copy
import hashlib
import secrets
import threading
import time
import uuid
from typing import Any, Callable, Protocol

from .asset_inventory import (
    compact_text,
    strong_identity_key,
)
from .dom_action_binding import DOMActionBindingService


class ActionCaptureProvider(Protocol):
    def get_action_snapshot(self, capture_id: str) -> dict[str, Any] | None:
        ...


class SafeDOMActionService:
    """Two-phase DOM action guard.

    The current implementation intentionally stops at dry-run. It does not expose
    a browser click primitive until an authenticated target-system executor exists.
    """

    OPERATION_STEPS = (
        ("bind_target", "Bind the requested asset to one DOM control"),
        (
            "confirm_and_authorize",
            "Confirm the exact asset/action pair and verify permission",
        ),
        (
            "fresh_capture_revalidation",
            "Re-capture the page and revalidate the same DOM target",
        ),
        (
            "final_revalidation",
            "Revalidate capture freshness, asset version and target fingerprint",
        ),
        ("execute", "Validate the operation without a live side effect"),
        ("verify_outcome", "Verify the outcome when a live executor exists"),
    )

    def __init__(
        self,
        binder: DOMActionBindingService,
        captures: ActionCaptureProvider,
        *,
        clock: Callable[[], float] = time.time,
        capture_max_age_seconds: float = 30.0,
        token_ttl_seconds: float = 15.0,
        plan_ttl_seconds: float = 300.0,
    ):
        self.binder = binder
        self.captures = captures
        self.clock = clock
        self.capture_max_age_seconds = float(capture_max_age_seconds)
        self.token_ttl_seconds = float(token_ttl_seconds)
        self.plan_ttl_seconds = float(plan_ttl_seconds)
        self._plans: dict[str, dict[str, Any]] = {}
        self._tokens: dict[str, dict[str, Any]] = {}
        self._audit: list[dict[str, Any]] = []
        self._lock = threading.RLock()

    def prepare(
        self,
        *,
        asset_reference: str | dict[str, Any],
        action: str,
        page_capture_id: str,
        scope: dict[str, Any] | None = None,
        task_id: str = "",
        principal_id: str = "",
    ) -> dict[str, Any]:
        snapshot = self.captures.get_action_snapshot(page_capture_id)
        if not snapshot:
            return self._decision("rejected", "page_capture_not_found")
        coverage_reason = self._dom_coverage_reason(snapshot)
        if coverage_reason:
            return self._decision("rejected", coverage_reason)
        binding = self.binder.bind(
            snapshot,
            asset_reference,
            action,
            scope,
        )
        if binding.get("status") != "verified":
            return {
                **self._decision(
                    "rejected", binding.get("reason", "binding_failed")
                ),
                "binding": binding,
            }

        plan_id = f"plan_{uuid.uuid4().hex[:16]}"
        now = self.clock()
        operation_plan = {
            "plan_id": plan_id,
            "status": "prepared",
            "reason": "verified_binding_requires_fresh_preflight",
            "created_at": now,
            "updated_at": now,
            "expires_at": now + self.plan_ttl_seconds,
            "dry_run_only": True,
            "steps": [
                {
                    "step_id": step_id,
                    "name": name,
                    "status": "completed" if step_id == "bind_target" else "pending",
                    "reason": "verified_initial_binding"
                    if step_id == "bind_target"
                    else "",
                    "updated_at": now,
                }
                for step_id, name in self.OPERATION_STEPS
            ],
        }
        plan = {
            "plan_id": plan_id,
            "task_id": compact_text(task_id, 200),
            "principal_id": compact_text(principal_id, 200),
            "scope": copy.deepcopy(scope or {}),
            "asset_id": binding["asset"]["asset_id"],
            "action_id": binding["action_id"],
            "binding": copy.deepcopy(binding),
            "created_at": now,
            "preflight_completed": False,
            "operation_plan": operation_plan,
        }
        with self._lock:
            self._plans[plan_id] = plan
        return {
            "status": "prepared",
            "reason": "verified_binding_requires_fresh_preflight",
            "plan_id": plan_id,
            "binding": copy.deepcopy(binding),
            "safe_for_execution": False,
            "requires_fresh_capture": True,
            "requires_confirmation": True,
            "dry_run_only": True,
            "operation_plan": self._public_operation_plan(plan),
        }

    def get_plan(self, plan_id: str) -> dict[str, Any]:
        """Return operation progress without binding, scope, principal or tokens."""

        with self._lock:
            plan = self._plans.get(plan_id)
            if not plan:
                return self._decision("rejected", "plan_not_found")
            operation_plan = plan["operation_plan"]
            now = self.clock()
            if (
                not plan["preflight_completed"]
                and now - plan["created_at"] >= self.plan_ttl_seconds
                and operation_plan["status"] != "expired"
            ):
                self._update_operation_plan_locked(
                    plan,
                    status="expired",
                    reason="plan_expired",
                    step_id=self._first_incomplete_step(operation_plan),
                    step_status="blocked",
                )
            elif (
                plan["preflight_completed"]
                and plan.get("token_expires_at") is not None
                and now >= float(plan["token_expires_at"])
                and operation_plan["status"] == "ready"
            ):
                self._update_operation_plan_locked(
                    plan,
                    status="expired",
                    reason="token_expired",
                    step_id="final_revalidation",
                    step_status="blocked",
                )
            public = self._public_operation_plan(plan)
        return {
            "status": public["status"],
            "reason": public["reason"],
            "plan_id": public["plan_id"],
            "asset_id": public["asset_id"],
            "action_id": public["action_id"],
            "safe_for_execution": False,
            "dry_run_only": True,
            "operation_plan": public,
        }

    def preflight(
        self,
        *,
        plan_id: str,
        current_capture_id: str,
        confirmed: bool,
        confirmed_asset_id: str,
        confirmed_action: str,
        permissions: list[str] | tuple[str, ...] | set[str],
    ) -> dict[str, Any]:
        with self._lock:
            plan = copy.deepcopy(self._plans.get(plan_id))
        if not plan:
            return self._decision("rejected", "plan_not_found")
        if plan["preflight_completed"]:
            return self._decision("rejected", "plan_already_used")
        if self.clock() - plan["created_at"] >= self.plan_ttl_seconds:
            return self._plan_rejection(
                plan_id,
                "plan_expired",
                step_id=self._first_incomplete_step(plan["operation_plan"]),
                plan_status="expired",
            )
        if not confirmed:
            return self._plan_rejection(
                plan_id,
                "human_confirmation_required",
                step_id="confirm_and_authorize",
            )
        if (
            strong_identity_key("asset_id", confirmed_asset_id)
            != strong_identity_key(
                "asset_id", plan["asset_id"]
            )
            or self.binder.canonical_action(confirmed_action)
            != plan["action_id"]
        ):
            return self._plan_rejection(
                plan_id,
                "confirmation_target_mismatch",
                step_id="confirm_and_authorize",
            )

        policy = self.binder.policy(plan["action_id"])
        granted = {compact_text(value, 200) for value in permissions}
        if not policy or policy.permission not in granted:
            return self._plan_rejection(
                plan_id,
                "permission_denied",
                step_id="confirm_and_authorize",
            )

        self._update_operation_plan(
            plan_id,
            status="preflight_in_progress",
            reason="confirmation_and_permission_verified",
            step_id="confirm_and_authorize",
            step_status="completed",
            step_reason="confirmed_target_and_permission",
        )

        initial_capture_id = plan["binding"]["capture"]["capture_id"]
        if not current_capture_id or current_capture_id == initial_capture_id:
            return self._plan_rejection(
                plan_id,
                "fresh_capture_required",
                step_id="fresh_capture_revalidation",
            )
        snapshot = self.captures.get_action_snapshot(current_capture_id)
        if not snapshot:
            return self._plan_rejection(
                plan_id,
                "current_capture_not_found",
                step_id="fresh_capture_revalidation",
            )
        coverage_reason = self._dom_coverage_reason(snapshot)
        if coverage_reason:
            return self._plan_rejection(
                plan_id,
                coverage_reason,
                step_id="fresh_capture_revalidation",
            )

        now = self.clock()
        created_at = float(snapshot.get("created_at", 0))
        if (
            created_at < plan["created_at"]
            or created_at > now
            or now - created_at > self.capture_max_age_seconds
        ):
            return self._plan_rejection(
                plan_id,
                "current_capture_stale",
                step_id="fresh_capture_revalidation",
            )

        current = self.binder.bind(
            snapshot,
            {"asset_id": plan["asset_id"]},
            plan["action_id"],
            plan["scope"],
        )
        if current.get("status") != "verified":
            reason = f"revalidation_{current.get('reason', 'failed')}"
            return {
                **self._plan_rejection(
                    plan_id,
                    reason,
                    step_id="fresh_capture_revalidation",
                ),
                "binding": current,
            }

        initial = plan["binding"]
        if current["asset"]["version"] != initial["asset"]["version"]:
            return self._plan_rejection(
                plan_id,
                "asset_version_changed",
                step_id="fresh_capture_revalidation",
            )
        if current["asset"]["status"] not in policy.allowed_asset_states:
            return self._plan_rejection(
                plan_id,
                "asset_state_not_allowed",
                step_id="fresh_capture_revalidation",
            )
        if current["target_fingerprint"] != initial["target_fingerprint"]:
            return self._plan_rejection(
                plan_id,
                "dom_target_changed",
                step_id="fresh_capture_revalidation",
            )

        token = secrets.token_urlsafe(32)
        token_hash = self._token_hash(token)
        expires_at = now + self.token_ttl_seconds
        claims = {
            "token_id": f"token_{uuid.uuid4().hex[:16]}",
            "plan_id": plan_id,
            "task_id": plan["task_id"],
            "principal_id": plan["principal_id"],
            "scope": copy.deepcopy(plan["scope"]),
            "asset_id": current["asset"]["asset_id"],
            "asset_version": current["asset"]["version"],
            "action_id": current["action_id"],
            "capture_id": current_capture_id,
            "content_hash": current["capture"]["content_hash"],
            "target_fingerprint": current["target_fingerprint"],
            "issued_at": now,
            "expires_at": expires_at,
        }
        with self._lock:
            stored = self._plans.get(plan_id)
            if not stored or stored["preflight_completed"]:
                return self._decision("rejected", "plan_already_used")
            stored["preflight_completed"] = True
            stored["token_expires_at"] = expires_at
            stored["operation_plan"]["expires_at"] = expires_at
            self._tokens[token_hash] = claims
            self._update_operation_plan_locked(
                stored,
                status="ready",
                reason="fresh_capture_and_asset_revalidated",
                step_id="fresh_capture_revalidation",
                step_status="completed",
                step_reason="fresh_capture_and_target_match",
            )
            self._update_operation_plan_locked(
                stored,
                status="ready",
                reason="fresh_capture_and_asset_revalidated",
                step_id="final_revalidation",
                step_status="ready",
                step_reason="one_time_token_issued",
            )
        return {
            "status": "ready",
            "reason": "fresh_capture_and_asset_revalidated",
            "execution_token": token,
            "expires_at": expires_at,
            "asset_id": claims["asset_id"],
            "action_id": claims["action_id"],
            "capture_id": current_capture_id,
            "target": copy.deepcopy(current["control"]),
            "preflight_verified": True,
            "safe_for_execution": False,
            "dry_run_only": True,
            "operation_plan": self.get_plan(plan_id)["operation_plan"],
        }

    def execute(
        self,
        *,
        execution_token: str,
        dry_run: bool = True,
    ) -> dict[str, Any]:
        with self._lock:
            claims = self._tokens.pop(
                self._token_hash(execution_token), None
            )
        if not claims:
            return self._decision("rejected", "token_invalid_or_replayed")

        now = self.clock()
        if now >= claims["expires_at"]:
            self._mark_execution_blocked(claims, "token_expired")
            return self._audit_result(
                "rejected", "token_expired", claims
            )
        snapshot = self.captures.get_action_snapshot(claims["capture_id"])
        if not snapshot:
            self._mark_execution_blocked(
                claims, "capture_no_longer_available"
            )
            return self._audit_result(
                "rejected", "capture_no_longer_available", claims
            )
        if now - float(snapshot.get("created_at", 0)) > self.capture_max_age_seconds:
            self._mark_execution_blocked(claims, "capture_became_stale")
            return self._audit_result(
                "rejected", "capture_became_stale", claims
            )
        coverage_reason = self._dom_coverage_reason(snapshot)
        if coverage_reason:
            self._mark_execution_blocked(claims, coverage_reason)
            return self._audit_result(
                "rejected", coverage_reason, claims
            )

        current = self.binder.bind(
            snapshot,
            {"asset_id": claims["asset_id"]},
            claims["action_id"],
            claims["scope"],
        )
        policy = self.binder.policy(claims["action_id"])
        if (
            current.get("status") != "verified"
            or current.get("target_fingerprint")
            != claims["target_fingerprint"]
            or current.get("asset", {}).get("version")
            != claims["asset_version"]
            or not policy
            or current.get("asset", {}).get("status")
            not in policy.allowed_asset_states
        ):
            self._mark_execution_blocked(
                claims, "final_revalidation_failed"
            )
            return self._audit_result(
                "rejected", "final_revalidation_failed", claims
            )
        self._update_operation_plan(
            claims["plan_id"],
            status="executing_dry_run" if dry_run else "blocked",
            reason="final_revalidation_succeeded",
            step_id="final_revalidation",
            step_status="completed",
            step_reason="capture_asset_and_target_revalidated",
        )
        if not dry_run:
            self._update_operation_plan(
                claims["plan_id"],
                status="blocked",
                reason="live_execution_channel_unavailable",
                step_id="execute",
                step_status="blocked",
                step_reason="live_execution_channel_unavailable",
            )
            return self._audit_result(
                "rejected",
                "live_execution_channel_unavailable",
                claims,
            )
        self._update_operation_plan(
            claims["plan_id"],
            status="dry_run_ok",
            reason="validated_without_side_effect",
            step_id="execute",
            step_status="completed",
            step_reason="dry_run_validated_without_side_effect",
        )
        self._update_operation_plan(
            claims["plan_id"],
            status="dry_run_ok",
            reason="validated_without_side_effect",
            step_id="verify_outcome",
            step_status="skipped",
            step_reason="no_live_side_effect_to_verify",
        )
        return self._audit_result(
            "dry_run_ok",
            "validated_without_side_effect",
            claims,
            executed=False,
            target=copy.deepcopy(current["control"]),
        )

    def audit_events(self) -> list[dict[str, Any]]:
        with self._lock:
            return copy.deepcopy(self._audit)

    @staticmethod
    def _dom_coverage_reason(snapshot: dict[str, Any]) -> str | None:
        dom = snapshot.get("dom")
        if not isinstance(dom, dict):
            return None
        coverage = dom.get("coverage")
        stats = dom.get("stats")
        sources = [dom]
        if isinstance(coverage, dict):
            sources.append(coverage)
        if isinstance(stats, dict):
            sources.append(stats)
        if any(source.get("truncated") is True for source in sources):
            return "dom_evidence_truncated"
        if isinstance(stats, dict):
            if stats.get("scan_truncated") is True:
                return "dom_evidence_truncated"
            try:
                open_shadow_root_count = int(
                    stats.get("open_shadow_root_count", 0) or 0
                )
            except (TypeError, ValueError, OverflowError):
                return "dom_evidence_incomplete"
            if str(
                stats.get("frame_collection_error", "")
            ).strip() or open_shadow_root_count != 0:
                return "dom_evidence_incomplete"
        if isinstance(coverage, dict):
            action_binding_complete = coverage.get("action_binding_complete")
            if action_binding_complete is False:
                return "dom_evidence_incomplete"
            if action_binding_complete is True:
                return None
            if (
                coverage.get("source_complete") is False
                or coverage.get("complete") is False
            ):
                return "dom_evidence_incomplete"
        return None

    def _plan_rejection(
        self,
        plan_id: str,
        reason: str,
        *,
        step_id: str | None,
        plan_status: str = "blocked",
    ) -> dict[str, Any]:
        self._update_operation_plan(
            plan_id,
            status=plan_status,
            reason=reason,
            step_id=step_id,
            step_status="blocked",
            step_reason=reason,
        )
        result = self._decision("rejected", reason)
        plan = self.get_plan(plan_id)
        if "operation_plan" in plan:
            result["operation_plan"] = plan["operation_plan"]
        return result

    def _mark_execution_blocked(
        self,
        claims: dict[str, Any],
        reason: str,
    ) -> None:
        self._update_operation_plan(
            claims["plan_id"],
            status="rejected",
            reason=reason,
            step_id="final_revalidation",
            step_status="blocked",
            step_reason=reason,
        )

    def _update_operation_plan(
        self,
        plan_id: str,
        *,
        status: str,
        reason: str,
        step_id: str | None,
        step_status: str,
        step_reason: str,
    ) -> None:
        with self._lock:
            plan = self._plans.get(plan_id)
            if plan:
                self._update_operation_plan_locked(
                    plan,
                    status=status,
                    reason=reason,
                    step_id=step_id,
                    step_status=step_status,
                    step_reason=step_reason,
                )

    def _update_operation_plan_locked(
        self,
        plan: dict[str, Any],
        *,
        status: str,
        reason: str,
        step_id: str | None,
        step_status: str,
        step_reason: str = "",
    ) -> None:
        now = self.clock()
        operation_plan = plan["operation_plan"]
        operation_plan["status"] = status
        operation_plan["reason"] = reason
        operation_plan["updated_at"] = now
        if step_id:
            for step in operation_plan["steps"]:
                if step["step_id"] == step_id:
                    step["status"] = step_status
                    step["reason"] = step_reason or reason
                    step["updated_at"] = now
                    break

    @staticmethod
    def _first_incomplete_step(operation_plan: dict[str, Any]) -> str | None:
        for step in operation_plan["steps"]:
            if step["status"] not in {"completed", "skipped"}:
                return str(step["step_id"])
        return None

    @staticmethod
    def _public_operation_plan(plan: dict[str, Any]) -> dict[str, Any]:
        operation_plan = plan["operation_plan"]
        return {
            "plan_id": operation_plan["plan_id"],
            "status": operation_plan["status"],
            "reason": operation_plan["reason"],
            "asset_id": plan["asset_id"],
            "action_id": plan["action_id"],
            "created_at": operation_plan["created_at"],
            "updated_at": operation_plan["updated_at"],
            "expires_at": operation_plan["expires_at"],
            "dry_run_only": True,
            "steps": copy.deepcopy(operation_plan["steps"]),
        }

    @staticmethod
    def _decision(status: str, reason: str) -> dict[str, Any]:
        return {
            "status": status,
            "reason": reason,
            "safe_for_execution": False,
        }

    @staticmethod
    def _token_hash(token: str) -> str:
        return hashlib.sha256(
            compact_text(token, 1000).encode("utf-8")
        ).hexdigest()

    def _audit_result(
        self,
        status: str,
        reason: str,
        claims: dict[str, Any],
        **extra: Any,
    ) -> dict[str, Any]:
        event = {
            "status": status,
            "reason": reason,
            "token_id": claims["token_id"],
            "plan_id": claims["plan_id"],
            "task_id": claims["task_id"],
            "principal_id": claims["principal_id"],
            "asset_id": claims["asset_id"],
            "action_id": claims["action_id"],
            "capture_id": claims["capture_id"],
            "timestamp": self.clock(),
            **extra,
        }
        with self._lock:
            self._audit.append(copy.deepcopy(event))
        result = {
            **event,
            "safe_for_execution": False,
            "dry_run": status == "dry_run_ok",
        }
        plan = self.get_plan(claims["plan_id"])
        if "operation_plan" in plan:
            result["operation_plan"] = plan["operation_plan"]
        return result
