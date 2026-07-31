from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from .asset_inventory import (
    AssetResolver,
    compact_text,
    identity_key,
    strong_identity_key,
)


def _digest(value: dict[str, Any]) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class DOMActionPolicy:
    action_id: str
    asset_types: frozenset[str]
    aliases: frozenset[str]
    permission: str
    risk_level: str
    control_action_ids: frozenset[str]
    allowed_asset_states: frozenset[str]
    requires_explicit_action_id: bool = False


DEFAULT_DOM_ACTION_POLICIES = (
    DOMActionPolicy(
        action_id="shutdown_ap",
        asset_types=frozenset({"ap"}),
        aliases=frozenset(
            {
                "shutdown_ap",
                "ap.shutdown",
                "device.shutdown",
                "shutdown",
                "disable",
                "turn_off",
                "关闭",
                "关闭ap",
                "停用",
                "下线",
            }
        ),
        permission="assets.ap.shutdown",
        risk_level="high-risk",
        control_action_ids=frozenset({"shutdown_ap", "ap.shutdown"}),
        allowed_asset_states=frozenset({"online", "degraded"}),
        requires_explicit_action_id=True,
    ),
    DOMActionPolicy(
        action_id="open_asset_details",
        asset_types=frozenset({"ap", "switch", "gateway", "device"}),
        aliases=frozenset(
            {
                "open_asset_details",
                "device.details",
                "open_details",
                "details",
                "查看详情",
                "详情",
            }
        ),
        permission="assets.read",
        risk_level="read-only",
        control_action_ids=frozenset(
            {"open_asset_details", "device.details"}
        ),
        allowed_asset_states=frozenset(
            {"online", "offline", "degraded", "unknown"}
        ),
    ),
)


class DOMActionBindingService:
    def __init__(
        self,
        assets: AssetResolver,
        policies: tuple[DOMActionPolicy, ...] = DEFAULT_DOM_ACTION_POLICIES,
    ):
        self.assets = assets
        self.policies = {policy.action_id: policy for policy in policies}
        self._aliases = {
            identity_key(alias): policy.action_id
            for policy in policies
            for alias in policy.aliases | {policy.action_id}
        }

    def canonical_action(self, value: str) -> str | None:
        return self._aliases.get(identity_key(value))

    def policy(self, value: str) -> DOMActionPolicy | None:
        return self.policies.get(self.canonical_action(value) or "")

    def bind(
        self,
        snapshot: dict[str, Any],
        asset_reference: str | dict[str, Any],
        action: str,
        scope: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        resolution = self.assets.resolve(asset_reference, scope)
        policy = self.policy(action)
        if not policy:
            return self._failure(
                "rejected", "unsupported_action", resolution, None
            )
        if resolution["status"] != "verified":
            return self._failure(
                resolution["status"], resolution["reason"], resolution, policy
            )

        asset = resolution["asset"]
        if policy.asset_types and asset["asset_type"] not in policy.asset_types:
            return self._failure(
                "rejected",
                "action_not_allowed_for_asset_type",
                resolution,
                policy,
            )

        page_url = compact_text(snapshot.get("page", {}).get("url"), 2048)
        page_origin = self._origin(page_url)
        allowed_origins = {
            self._origin(compact_text(value)).casefold()
            for value in asset.get("allowed_origins", [])
            if self._origin(compact_text(value))
        }
        if not allowed_origins:
            return self._failure(
                "rejected", "asset_origin_not_configured", resolution, policy
            )
        if page_origin.casefold() not in allowed_origins:
            return self._failure(
                "rejected", "page_origin_not_allowed", resolution, policy
            )

        elements = list(snapshot.get("dom", {}).get("elements", []))
        duplicate_reason = self._duplicate_dom_identity(elements)
        if duplicate_reason:
            return self._failure(
                "ambiguous",
                duplicate_reason,
                resolution,
                policy,
            )
        subjects = [
            element
            for element in elements
            if self._subject_matches_asset(element, asset)
        ]
        if not subjects:
            return self._failure(
                "unmatched", "asset_not_grounded_in_dom", resolution, policy
            )
        subjects = [
            subject
            for subject in subjects
            if self._origin(subject.get("frame_url")).casefold()
            in allowed_origins
        ]
        if not subjects:
            return self._failure(
                "rejected", "frame_origin_not_allowed", resolution, policy
            )

        parent_by_ref = {
            (
                compact_text(element.get("frame_id"), 100),
                compact_text(element.get("frame_url"), 2048),
                compact_text(element.get("document_id"), 200),
                compact_text(element.get("ref"), 500),
            ): compact_text(
                element.get("parent_ref"), 500
            )
            for element in elements
            if compact_text(element.get("ref"), 500)
        }
        pairs: list[
            tuple[dict[str, Any], dict[str, Any], str]
        ] = []
        for subject in subjects:
            for control in elements:
                action_evidence = self._control_action_evidence(control, policy)
                if not action_evidence:
                    continue
                if not self._same_document(subject, control):
                    continue
                if not self._belongs_to_subject(
                    control, subject, asset, parent_by_ref
                ):
                    continue
                pairs.append((subject, control, action_evidence))

        if not pairs:
            return self._failure(
                "unmatched",
                "action_control_not_owned_by_asset",
                resolution,
                policy,
            )
        if len(pairs) != 1:
            return {
                **self._failure(
                    "ambiguous",
                    "multiple_owned_action_controls",
                    resolution,
                    policy,
                ),
                "candidate_pair_count": len(pairs),
            }

        subject, control, action_evidence = pairs[0]
        binding = {
            "asset": copy.deepcopy(asset),
            "action_id": policy.action_id,
            "risk_level": policy.risk_level,
            "required_permission": policy.permission,
            "subject": self._element_snapshot(subject, include_action=False),
            "control": self._element_snapshot(control, include_action=True),
            "capture": {
                "capture_id": compact_text(snapshot.get("capture_id"), 200),
                "url": compact_text(
                    snapshot.get("page", {}).get("url"), 2048
                ),
                "created_at": float(snapshot.get("created_at", 0)),
                "content_hash": compact_text(
                    snapshot.get("content_hash"), 200
                ),
                "scene_revision": int(snapshot.get("scene_revision", 0)),
            },
            "evidence": [
                *resolution.get("evidence", []),
                "dom_subject_strong_identity",
                action_evidence,
                "same_frame_and_document",
                "control_owned_by_subject",
            ],
        }
        binding["target_fingerprint"] = self.target_fingerprint(binding)
        return {
            "status": "verified",
            "reason": "unique_asset_and_owned_control",
            "binding_verified": True,
            "safe_for_execution": False,
            "binding_id": f"binding_{_digest(binding)[:16]}",
            **binding,
            "asset_resolution": resolution,
        }

    @staticmethod
    def target_fingerprint(binding: dict[str, Any]) -> str:
        return _digest(
            {
                "asset_id": binding["asset"]["asset_id"],
                "asset_version": binding["asset"]["version"],
                "action_id": binding["action_id"],
                "page_url": binding["capture"]["url"],
                "subject": {
                    key: binding["subject"].get(key)
                    for key in (
                        "ref",
                        "selector",
                        "frame_id",
                        "frame_url",
                        "document_id",
                        "asset_id",
                        "business_id",
                        "management_ip",
                        "serial_number",
                    )
                },
                "control": {
                    key: binding["control"].get(key)
                    for key in (
                        "ref",
                        "selector",
                        "frame_id",
                        "frame_url",
                        "document_id",
                        "action_id",
                        "label",
                        "role",
                        "owner_business_id",
                    )
                },
            }
        )

    @staticmethod
    def _subject_matches_asset(
        element: dict[str, Any],
        asset: dict[str, Any],
    ) -> bool:
        if not all(
            compact_text(element.get(field))
            for field in ("ref", "frame_id", "document_id")
        ):
            return False
        expected = {
            "asset_id": asset["asset_id"],
            "business_id": asset["asset_id"],
            "management_ip": asset["management_ip"],
            "serial_number": asset["serial_number"],
        }
        strong_match = False
        for field, expected_value in expected.items():
            supplied = compact_text(element.get(field))
            if not supplied:
                continue
            identity_field = (
                "asset_id"
                if field in {"asset_id", "business_id"}
                else field
            )
            if (
                not expected_value
                or strong_identity_key(identity_field, supplied)
                != strong_identity_key(identity_field, expected_value)
            ):
                return False
            strong_match = True
        if element.get("site_id") and identity_key(
            element.get("site_id")
        ) != identity_key(asset["site_id"]):
            return False
        if element.get("asset_version") not in (None, ""):
            try:
                if int(element["asset_version"]) != int(asset["version"]):
                    return False
            except (TypeError, ValueError):
                return False
        return strong_match

    def _control_action_evidence(
        self,
        element: dict[str, Any],
        policy: DOMActionPolicy,
    ) -> str | None:
        if (
            not element.get("actionable")
            or element.get("disabled")
            or not compact_text(element.get("selector"))
            or not compact_text(element.get("ref"))
            or not compact_text(element.get("frame_id"))
            or not compact_text(element.get("document_id"))
        ):
            return None
        selector = compact_text(element.get("selector"), 500)
        if "@capture:" in selector:
            return None
        explicit = compact_text(element.get("action_id"), 200).casefold()
        if explicit:
            allowed = {
                compact_text(value, 200).casefold()
                for value in policy.control_action_ids
            }
            return (
                "explicit_action_id" if explicit in allowed else None
            )
        if policy.requires_explicit_action_id:
            return None
        label = identity_key(
            element.get("aria_label") or element.get("label")
        )
        safe_labels = {
            identity_key(alias)
            for alias in policy.aliases
            if "." not in alias and "_" not in alias
        }
        return "exact_action_label" if label in safe_labels else None

    @staticmethod
    def _origin(value: Any) -> str:
        parsed = urlsplit(compact_text(value, 2048))
        if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
            return ""
        return f"{parsed.scheme.lower()}://{parsed.netloc.lower()}"

    @staticmethod
    def _duplicate_dom_identity(
        elements: list[dict[str, Any]],
    ) -> str | None:
        for field in ("ref", "selector"):
            seen: set[tuple[str, str, str, str]] = set()
            for element in elements:
                value = compact_text(element.get(field), 500)
                if not value:
                    continue
                key = (
                    compact_text(element.get("frame_id"), 100),
                    compact_text(element.get("frame_url"), 2048),
                    compact_text(element.get("document_id"), 200),
                    value,
                )
                if key in seen:
                    return f"duplicate_dom_{field}"
                seen.add(key)
        return None

    @staticmethod
    def _same_document(
        subject: dict[str, Any],
        control: dict[str, Any],
    ) -> bool:
        return all(
            compact_text(subject.get(field))
            == compact_text(control.get(field))
            for field in ("frame_id", "frame_url", "document_id")
        )

    @staticmethod
    def _belongs_to_subject(
        control: dict[str, Any],
        subject: dict[str, Any],
        asset: dict[str, Any],
        parent_by_ref: dict[tuple[str, str, str, str], str],
    ) -> bool:
        owner = compact_text(control.get("owner_business_id"))
        if owner and strong_identity_key(
            "asset_id", owner
        ) != strong_identity_key(
            "asset_id", asset["asset_id"]
        ):
            return False
        context = (
            compact_text(control.get("frame_id"), 100),
            compact_text(control.get("frame_url"), 2048),
            compact_text(control.get("document_id"), 200),
        )
        subject_ref = compact_text(subject.get("ref"), 500)
        current = compact_text(control.get("ref"), 500)
        visited: set[str] = set()
        while current and current not in visited:
            if current == subject_ref:
                return True
            visited.add(current)
            current = parent_by_ref.get(
                (*context, current),
                "",
            )
        return False

    @staticmethod
    def _element_snapshot(
        element: dict[str, Any],
        *,
        include_action: bool,
    ) -> dict[str, Any]:
        fields = (
            "ref",
            "selector",
            "parent_ref",
            "label",
            "aria_label",
            "role",
            "tag",
            "frame_id",
            "frame_url",
            "document_id",
            "bbox",
            "asset_id",
            "business_id",
            "management_ip",
            "serial_number",
            "site_id",
            "asset_version",
            "owner_business_id",
        )
        result = {
            key: copy.deepcopy(element.get(key))
            for key in fields
        }
        if include_action:
            result["action_id"] = element.get("action_id")
        return result

    @staticmethod
    def _failure(
        status: str,
        reason: str,
        resolution: dict[str, Any],
        policy: DOMActionPolicy | None,
    ) -> dict[str, Any]:
        result = {
            "status": status,
            "reason": reason,
            "safe_for_execution": False,
            "asset_resolution": resolution,
        }
        if policy:
            result.update(
                {
                    "action_id": policy.action_id,
                    "risk_level": policy.risk_level,
                    "required_permission": policy.permission,
                }
            )
        return result
