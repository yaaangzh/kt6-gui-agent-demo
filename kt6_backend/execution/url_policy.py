from __future__ import annotations

import ipaddress
import socket
from collections.abc import Callable, Iterable
from urllib.parse import urlsplit, urlunsplit


HostResolver = Callable[[str], Iterable[str]]
SYNTHETIC_DNS_NETWORK = ipaddress.ip_network("198.18.0.0/15")


class ExecutionURLPolicyError(ValueError):
    def __init__(self, error_code: str):
        super().__init__(error_code)
        self.error_code = error_code


class ExecutionURLPolicy:
    """Allow public HTTP(S) targets while blocking local network access."""

    def __init__(
        self,
        *,
        allow_private_networks: bool = False,
        resolver: HostResolver | None = None,
    ):
        self.allow_private_networks = bool(allow_private_networks)
        self._resolver = resolver or self._resolve_host

    def validate(self, value: str) -> str:
        raw = str(value).strip()
        if not raw or len(raw) > 2048 or any(char in raw for char in "\r\n"):
            raise ExecutionURLPolicyError("execution_url_invalid")
        try:
            parsed = urlsplit(raw)
            raw_host = parsed.hostname
            port = parsed.port
        except ValueError as exc:
            raise ExecutionURLPolicyError("execution_url_invalid") from exc
        if (
            parsed.scheme not in {"http", "https"}
            or not raw_host
            or parsed.username is not None
            or parsed.password is not None
            or parsed.fragment
        ):
            raise ExecutionURLPolicyError("execution_url_invalid")

        host = self._normalize_host(raw_host)
        addresses, resolved_hostname = self._resolved_addresses(host)
        if any(
            not self._address_allowed(
                address,
                resolved_hostname=resolved_hostname,
            )
            for address in addresses
        ):
            raise ExecutionURLPolicyError("execution_url_network_blocked")
        return urlunsplit(
            (
                parsed.scheme,
                self._netloc(host, port),
                parsed.path or "/",
                parsed.query,
                "",
            )
        )

    def health(self) -> dict[str, object]:
        return {
            "configured": True,
            "mode": "public_web",
            "network_scope": (
                "public_and_private_test"
                if self.allow_private_networks
                else "public_only"
            ),
            "allow_private_networks": self.allow_private_networks,
            "dns_revalidation": True,
            "synthetic_dns_compatible": True,
        }

    def _resolved_addresses(
        self,
        host: str,
    ) -> tuple[
        tuple[ipaddress.IPv4Address | ipaddress.IPv6Address, ...],
        bool,
    ]:
        try:
            literal = ipaddress.ip_address(host)
        except ValueError:
            literal = None
        if literal is not None:
            return (literal,), False

        try:
            raw_addresses = tuple(self._resolver(host))
        except (OSError, ValueError, TypeError) as exc:
            raise ExecutionURLPolicyError("execution_url_host_unresolved") from exc
        addresses = []
        for value in raw_addresses:
            try:
                addresses.append(ipaddress.ip_address(str(value).strip()))
            except ValueError as exc:
                raise ExecutionURLPolicyError(
                    "execution_url_resolution_invalid"
                ) from exc
        if not addresses:
            raise ExecutionURLPolicyError("execution_url_host_unresolved")
        return tuple(dict.fromkeys(addresses)), True

    def _address_allowed(
        self,
        address: ipaddress.IPv4Address | ipaddress.IPv6Address,
        *,
        resolved_hostname: bool,
    ) -> bool:
        if (
            address.is_unspecified
            or address.is_multicast
            or address.is_reserved
            or address.is_link_local
        ):
            return False
        if resolved_hostname and address in SYNTHETIC_DNS_NETWORK:
            return True
        return bool(
            address.is_global
            or (
                self.allow_private_networks
                and (address.is_private or address.is_loopback)
            )
        )

    @staticmethod
    def _resolve_host(host: str) -> tuple[str, ...]:
        try:
            results = socket.getaddrinfo(
                host,
                None,
                type=socket.SOCK_STREAM,
            )
        except socket.gaierror as exc:
            raise OSError("host resolution failed") from exc
        return tuple(
            str(sockaddr[0])
            for _family, _type, _protocol, _canonname, sockaddr in results
            if sockaddr
        )

    @staticmethod
    def _normalize_host(value: str) -> str:
        host = str(value).strip().rstrip(".").casefold()
        if not host or len(host) > 253 or any(
            char in host for char in ":/\\@?#[]*"
        ):
            raise ExecutionURLPolicyError("execution_url_host_invalid")
        try:
            return ipaddress.ip_address(host).compressed
        except ValueError:
            pass
        try:
            ascii_host = host.encode("idna").decode("ascii")
        except UnicodeError as exc:
            raise ExecutionURLPolicyError("execution_url_host_invalid") from exc
        labels = ascii_host.split(".")
        if any(
            not label
            or len(label) > 63
            or label.startswith("-")
            or label.endswith("-")
            or not all(char.isalnum() or char == "-" for char in label)
            for label in labels
        ):
            raise ExecutionURLPolicyError("execution_url_host_invalid")
        return ascii_host

    @staticmethod
    def _netloc(host: str, port: int | None) -> str:
        try:
            value = f"[{host}]" if ipaddress.ip_address(host).version == 6 else host
        except ValueError:
            value = host
        return f"{value}:{port}" if port is not None else value


__all__ = [
    "ExecutionURLPolicy",
    "ExecutionURLPolicyError",
    "HostResolver",
    "SYNTHETIC_DNS_NETWORK",
]
