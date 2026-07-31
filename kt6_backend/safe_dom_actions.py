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
        plan = {
            "plan_id": plan_id,
            "task_id": compact_text(task_id, 200),
            "principal_id": compact_text(principal_id, 200),
            "scope": copy.deepcopy(scope or {}),
            "asset_id": binding["asset"]["asset_id"],
            "action_id": binding["action_id"],
            "binding": copy.deepcopy(binding),
            "created_at": self.clock(),
            "preflight_completed": False,
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
            return self._decision("rejected", "plan_expired")
        if not confirmed:
            return self._decision("rejected", "human_confirmation_required")
        if (
            strong_identity_key("asset_id", confirmed_asset_id)
            != strong_identity_key(
                "asset_id", plan["asset_id"]
            )
            or self.binder.canonical_action(confirmed_action)
            != plan["action_id"]
        ):
            return self._decision("rejected", "confirmation_target_mismatch")

        policy = self.binder.policy(plan["action_id"])
        granted = {compact_text(value, 200) for value in permissions}
        if not policy or (
            policy.permission not in granted
        ):
            return self._decision("rejected", "permission_denied")

        initial_capture_id = plan["binding"]["capture"]["capture_id"]
        if not current_capture_id or current_capture_id == initial_capture_id:
            return self._decision("rejected", "fresh_capture_required")
        snapshot = self.captures.get_action_snapshot(current_capture_id)
        if not snapshot:
            return self._decision("rejected", "current_capture_not_found")

        now = self.clock()
        created_at = float(snapshot.get("created_at", 0))
        if (
            created_at < plan["created_at"]
            or created_at > now
            or now - created_at > self.capture_max_age_seconds
        ):
            return self._decision("rejected", "current_capture_stale")

        current = self.binder.bind(
            snapshot,
            {"asset_id": plan["asset_id"]},
            plan["action_id"],
            plan["scope"],
        )
        if current.get("status") != "verified":
            return {
                **self._decision(
                    "rejected",
                    f"revalidation_{current.get('reason', 'failed')}",
                ),
                "binding": current,
            }

        initial = plan["binding"]
        if current["asset"]["version"] != initial["asset"]["version"]:
            return self._decision("rejected", "asset_version_changed")
        if current["asset"]["status"] not in policy.allowed_asset_states:
            return self._decision("rejected", "asset_state_not_allowed")
        if current["target_fingerprint"] != initial["target_fingerprint"]:
            return self._decision("rejected", "dom_target_changed")

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
            self._tokens[token_hash] = claims
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
            return self._audit_result(
                "rejected", "token_expired", claims
            )
        snapshot = self.captures.get_action_snapshot(claims["capture_id"])
        if not snapshot:
            return self._audit_result(
                "rejected", "capture_no_longer_available", claims
            )
        if now - float(snapshot.get("created_at", 0)) > self.capture_max_age_seconds:
            return self._audit_result(
                "rejected", "capture_became_stale", claims
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
            return self._audit_result(
                "rejected", "final_revalidation_failed", claims
            )
        if not dry_run:
            return self._audit_result(
                "rejected",
                "live_execution_channel_unavailable",
                claims,
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
        return {
            **event,
            "safe_for_execution": False,
            "dry_run": status == "dry_run_ok",
        }
