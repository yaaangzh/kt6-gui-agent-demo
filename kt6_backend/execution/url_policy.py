from __future__ import annotations

import ipaddress
from collections.abc import Iterable
from urllib.parse import urlsplit, urlunsplit


class ExecutionURLPolicyError(ValueError):
    def __init__(self, error_code: str):
        super().__init__(error_code)
        self.error_code = error_code


class ExecutionURLPolicy:
    """Allow navigation only to explicitly approved HTTP(S) hosts."""

    def __init__(self, allowed_hosts: Iterable[str]):
        self.allowed_hosts = frozenset(
            self._normalize_host(value) for value in allowed_hosts
        )

    def validate(self, value: str) -> str:
        raw = str(value).strip()
        if not raw or len(raw) > 2048 or any(char in raw for char in "\r\n"):
            raise ExecutionURLPolicyError("execution_url_invalid")
        try:
            parsed = urlsplit(raw)
            host = parsed.hostname
            parsed.port
        except ValueError as exc:
            raise ExecutionURLPolicyError("execution_url_invalid") from exc
        if (
            parsed.scheme not in {"http", "https"}
            or not host
            or parsed.username is not None
            or parsed.password is not None
            or parsed.fragment
        ):
            raise ExecutionURLPolicyError("execution_url_invalid")
        if self._normalize_host(host) not in self.allowed_hosts:
            raise ExecutionURLPolicyError("execution_url_host_not_allowed")
        return urlunsplit(
            (parsed.scheme, parsed.netloc, parsed.path or "/", parsed.query, "")
        )

    @staticmethod
    def _normalize_host(value: str) -> str:
        host = str(value).strip().rstrip(".").casefold()
        if (
            not host
            or len(host) > 253
            or any(char in host for char in ":/\\@?#[]*")
        ):
            raise ExecutionURLPolicyError("execution_allowed_host_invalid")
        try:
            return ipaddress.ip_address(host).compressed
        except ValueError:
            pass
        try:
            ascii_host = host.encode("idna").decode("ascii")
        except UnicodeError as exc:
            raise ExecutionURLPolicyError("execution_allowed_host_invalid") from exc
        labels = ascii_host.split(".")
        if any(
            not label
            or len(label) > 63
            or label.startswith("-")
            or label.endswith("-")
            or not all(char.isalnum() or char == "-" for char in label)
            for label in labels
        ):
            raise ExecutionURLPolicyError("execution_allowed_host_invalid")
        return ascii_host

    def health(self) -> dict[str, object]:
        return {
            "configured": bool(self.allowed_hosts),
            "allowed_host_count": len(self.allowed_hosts),
        }


__all__ = ["ExecutionURLPolicy", "ExecutionURLPolicyError"]
