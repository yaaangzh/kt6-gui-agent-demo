from __future__ import annotations

import hashlib
import json
import secrets
import threading
import time
from dataclasses import asdict
from typing import Callable

from .models import BrowserTarget, VisualTarget


class ScenarioActionGuardError(ValueError):
    def __init__(self, error_code: str):
        super().__init__(error_code)
        self.error_code = error_code


class ScenarioActionGuard:
    """Issue and consume one short-lived token for one grounded click."""

    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.time,
        ttl_seconds: float = 15.0,
    ):
        self.clock = clock
        self.ttl_seconds = float(ttl_seconds)
        self._tokens: dict[str, tuple[float, str]] = {}
        self._lock = threading.Lock()

    def authorize(self, target: BrowserTarget | VisualTarget) -> str:
        token = secrets.token_urlsafe(32)
        with self._lock:
            self._tokens[self._hash(token)] = (
                self.clock() + self.ttl_seconds,
                self.fingerprint(target),
            )
        return token

    def consume(self, token: str, target: BrowserTarget | VisualTarget) -> None:
        with self._lock:
            claims = self._tokens.pop(self._hash(token), None)
        if claims is None:
            raise ScenarioActionGuardError("scenario_action_token_invalid")
        expires_at, fingerprint = claims
        if self.clock() >= expires_at:
            raise ScenarioActionGuardError("scenario_action_token_expired")
        if fingerprint != self.fingerprint(target):
            raise ScenarioActionGuardError("scenario_action_target_changed")

    @staticmethod
    def fingerprint(target: BrowserTarget | VisualTarget) -> str:
        payload = {
            "type": type(target).__name__,
            "target": asdict(target),
        }
        return hashlib.sha256(
            json.dumps(
                payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
        ).hexdigest()

    @staticmethod
    def _hash(token: str) -> str:
        return hashlib.sha256(str(token).encode("utf-8")).hexdigest()


__all__ = ["ScenarioActionGuard", "ScenarioActionGuardError"]
