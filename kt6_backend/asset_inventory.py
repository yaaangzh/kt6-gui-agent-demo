from __future__ import annotations

import copy
import ipaddress
import json
import threading
import unicodedata
from pathlib import Path
from typing import Any, Protocol


def compact_text(value: Any, maximum: int = 300) -> str:
    return " ".join(str(value or "").strip().split())[:maximum]


def identity_key(value: Any) -> str:
    return "".join(
        character
        for character in compact_text(value).casefold()
        if character.isalnum()
    )


def strong_identity_key(field: str, value: Any) -> str:
    text = unicodedata.normalize(
        "NFKC", compact_text(value, 500)
    ).casefold()
    if not text:
        return ""
    if field == "management_ip":
        try:
            return str(ipaddress.ip_address(text)).casefold()
        except ValueError:
            return ""
    return text


def normalize_asset(item: dict[str, Any]) -> dict[str, Any]:
    asset_id = compact_text(item.get("asset_id"), 200)
    if not asset_id:
        raise ValueError("asset_id is required")
    try:
        version = max(0, int(item.get("version", 0)))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"asset {asset_id} has an invalid version") from exc
    aliases = [
        alias
        for alias in (
            compact_text(value, 200) for value in item.get("aliases", [])
        )
        if alias
    ]
    management_ip = compact_text(
        item.get("management_ip") or item.get("ip"), 100
    )
    if management_ip and not strong_identity_key(
        "management_ip", management_ip
    ):
        raise ValueError(f"asset {asset_id} has an invalid management_ip")
    return {
        "asset_id": asset_id,
        "asset_type": compact_text(
            item.get("asset_type") or item.get("type"), 100
        ).lower(),
        "name": compact_text(item.get("name") or item.get("label"), 200),
        "aliases": list(dict.fromkeys(aliases)),
        "management_ip": strong_identity_key("management_ip", management_ip),
        "serial_number": compact_text(
            item.get("serial_number") or item.get("serial"), 200
        ),
        "site_id": compact_text(item.get("site_id"), 200),
        "site": compact_text(item.get("site"), 200),
        "floor": compact_text(item.get("floor"), 100),
        "status": compact_text(item.get("status") or "unknown", 100).lower(),
        "version": version,
        "allowed_origins": list(
            dict.fromkeys(
                compact_text(value, 500)
                for value in item.get("allowed_origins", [])
                if compact_text(value, 500)
            )
        ),
    }


class AssetInventoryAdapter(Protocol):
    def list_assets(self) -> list[dict[str, Any]]:
        ...


class JSONAssetInventoryAdapter:
    """File-backed demo adapter; production should use the NCE/FEBS API."""

    def __init__(self, path: Path):
        self.path = path

    def list_assets(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        payload = json.loads(self.path.read_text(encoding="utf-8"))
        raw_assets = payload.get("assets", []) if isinstance(payload, dict) else payload
        if not isinstance(raw_assets, list):
            raise ValueError("asset inventory must contain an assets array")
        return [
            normalize_asset(item)
            for item in raw_assets
            if isinstance(item, dict)
        ]


class InMemoryAssetInventoryAdapter:
    def __init__(self, assets: list[dict[str, Any]]):
        self._assets = copy.deepcopy(assets)
        self._lock = threading.RLock()

    def list_assets(self) -> list[dict[str, Any]]:
        with self._lock:
            return [normalize_asset(item) for item in self._assets]

    def replace(self, assets: list[dict[str, Any]]) -> None:
        with self._lock:
            self._assets = copy.deepcopy(assets)


class AssetResolver:
    def __init__(self, inventory: AssetInventoryAdapter):
        self.inventory = inventory

    def resolve(
        self,
        reference: str | dict[str, Any],
        scope: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        assets = sorted(
            self.inventory.list_assets(),
            key=lambda item: (
                strong_identity_key("asset_id", item["asset_id"]),
                identity_key(item["site_id"]),
                identity_key(item["name"]),
            ),
        )
        if not assets:
            return self._result("unmatched", "inventory_empty", [])
        duplicate = self._duplicate_inventory_identity(assets)
        if duplicate:
            return duplicate

        supplied = copy.deepcopy(reference) if isinstance(reference, dict) else {}
        if not isinstance(reference, dict):
            supplied["reference"] = compact_text(reference, 200)
        trusted_scope = copy.deepcopy(scope or {})
        supplied_scope = {
            key: supplied[key]
            for key in ("site_id", "site", "floor")
            if supplied.get(key)
        }
        for field, value in supplied_scope.items():
            if trusted_scope.get(field) and identity_key(
                trusted_scope[field]
            ) != identity_key(value):
                return self._result(
                    "conflict", "scope_conflict", []
                )
        effective_scope = {
            field: trusted_scope.get(field) or supplied_scope.get(field)
            for field in ("site_id", "site", "floor")
            if trusted_scope.get(field) or supplied_scope.get(field)
        }

        evidence_sets: list[tuple[str, set[int]]] = []
        for field, aliases in {
            "asset_id": ("asset_id",),
            "management_ip": ("management_ip", "ip"),
            "serial_number": ("serial_number", "serial"),
        }.items():
            value = next(
                (
                    compact_text(supplied.get(alias))
                    for alias in aliases
                    if supplied.get(alias)
                ),
                "",
            )
            if not value:
                continue
            canonical = strong_identity_key(field, value)
            if not canonical:
                return self._result(
                    "unmatched", f"{field}_invalid", []
                )
            matches = {
                index
                for index, asset in enumerate(assets)
                if strong_identity_key(field, asset[field]) == canonical
            }
            if not matches:
                return self._result("unmatched", f"{field}_not_found", [])
            evidence_sets.append((field, matches))

        name = compact_text(supplied.get("name") or supplied.get("label"))
        free_reference = compact_text(supplied.get("reference"))
        if free_reference:
            strong = {
                index
                for index, asset in enumerate(assets)
                if any(
                    strong_identity_key(field, free_reference)
                    and strong_identity_key(field, free_reference)
                    == strong_identity_key(field, asset[field])
                    for field in (
                        "asset_id",
                        "management_ip",
                        "serial_number",
                    )
                )
            }
            weak = {
                index
                for index, asset in enumerate(assets)
                if identity_key(free_reference)
                in {
                    identity_key(asset["name"]),
                    *(identity_key(alias) for alias in asset["aliases"]),
                }
                - {""}
            }
            if strong:
                evidence_sets.append(("reference_strong", strong))
            elif weak:
                name = free_reference
            else:
                return self._result("unmatched", "reference_not_found", [])

        name_matches = {
            index
            for index, asset in enumerate(assets)
            if name
            and identity_key(name)
            in {
                identity_key(asset["name"]),
                *(identity_key(alias) for alias in asset["aliases"]),
            }
        }
        if evidence_sets and name:
            evidence_sets.append(("name", name_matches))

        if evidence_sets:
            candidate_indexes = set.intersection(
                *(matches for _, matches in evidence_sets)
            )
            if not candidate_indexes:
                conflicting = sorted(
                    {
                        index
                        for _, matches in evidence_sets
                        for index in matches
                    }
                )
                return self._result(
                    "conflict",
                    "strong_identity_conflict",
                    [assets[index] for index in conflicting],
                )
            evidence = [field for field, _ in evidence_sets]
        else:
            if not name:
                return self._result("unmatched", "asset_reference_missing", [])
            candidate_indexes = name_matches
            evidence = ["name"]

        candidates = [
            assets[index]
            for index in sorted(candidate_indexes)
            if self._scope_matches(assets[index], effective_scope)
        ]
        if not candidates:
            return self._result("unmatched", "scope_mismatch", [])
        if len(candidates) != 1:
            return self._result(
                "ambiguous", "multiple_inventory_matches", candidates
            )
        if evidence == ["name"] and not any(
            compact_text(effective_scope.get(key)) for key in ("site_id", "site", "floor")
        ):
            return self._result("ambiguous", "name_requires_scope", candidates)
        return {
            **self._result("verified", "unique_inventory_match", candidates),
            "asset": copy.deepcopy(candidates[0]),
            "evidence": evidence
            + [
                f"scope:{key}"
                for key in ("site_id", "site", "floor")
                if compact_text(effective_scope.get(key))
            ],
        }

    def _duplicate_inventory_identity(
        self,
        assets: list[dict[str, Any]],
    ) -> dict[str, Any] | None:
        for field in ("asset_id", "management_ip", "serial_number"):
            indexes_by_value: dict[str, list[int]] = {}
            for index, asset in enumerate(assets):
                value = strong_identity_key(field, asset.get(field))
                if value:
                    indexes_by_value.setdefault(value, []).append(index)
            for indexes in indexes_by_value.values():
                if len(indexes) > 1:
                    return self._result(
                        "conflict",
                        f"inventory_duplicate_{field}",
                        [assets[index] for index in indexes],
                    )
        return None

    @staticmethod
    def _scope_matches(
        asset: dict[str, Any],
        scope: dict[str, Any],
    ) -> bool:
        return all(
            not compact_text(scope.get(field))
            or identity_key(asset.get(field)) == identity_key(scope.get(field))
            for field in ("site_id", "site", "floor")
        )

    @staticmethod
    def _result(
        status: str,
        reason: str,
        candidates: list[dict[str, Any]],
    ) -> dict[str, Any]:
        return {
            "status": status,
            "reason": reason,
            "identity_verified": status == "verified",
            "safe_for_execution": False,
            "candidate_count": len(candidates),
            "candidates": copy.deepcopy(candidates),
        }
