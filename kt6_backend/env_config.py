"""Small dependency-free loader for the project-root ``.env`` file."""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import MutableMapping


MAX_ENV_FILE_BYTES = 64 * 1024
_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class EnvironmentFileError(ValueError):
    """Raised when a configured environment file is ambiguous or unsafe."""


def load_project_env(
    root: Path,
    *,
    environ: MutableMapping[str, str] | None = None,
) -> tuple[str, ...]:
    """Load ``root/.env`` without overriding variables already in the process.

    The function returns only the names it loaded. Values are intentionally
    never returned or logged. Missing files are valid so production can keep
    using a secret manager or service-level environment variables.
    """

    target = Path(root).resolve() / ".env"
    if not target.exists():
        return ()
    if not target.is_file() or target.is_symlink():
        raise EnvironmentFileError("project .env must be a regular file")
    try:
        raw = target.read_bytes()
    except OSError as exc:
        raise EnvironmentFileError("project .env cannot be read") from exc
    if len(raw) > MAX_ENV_FILE_BYTES:
        raise EnvironmentFileError("project .env exceeds 64 KiB")
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise EnvironmentFileError("project .env must be UTF-8") from exc

    destination = os.environ if environ is None else environ
    parsed: dict[str, str] = {}
    for line_number, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("export "):
            stripped = stripped[7:].lstrip()
        if "=" not in stripped:
            raise EnvironmentFileError(
                f"project .env line {line_number} must use KEY=VALUE"
            )
        key, raw_value = stripped.split("=", 1)
        key = key.strip()
        if not _KEY.fullmatch(key):
            raise EnvironmentFileError(
                f"project .env line {line_number} has an invalid key"
            )
        if key in parsed:
            raise EnvironmentFileError(
                f"project .env line {line_number} duplicates {key}"
            )
        value = _parse_value(raw_value.strip(), line_number)
        if "\x00" in value or "\r" in value or "\n" in value:
            raise EnvironmentFileError(
                f"project .env line {line_number} contains a multiline value"
            )
        parsed[key] = value

    loaded: list[str] = []
    for key, value in parsed.items():
        if key not in destination:
            destination[key] = value
            loaded.append(key)
    return tuple(loaded)


def _parse_value(value: str, line_number: int) -> str:
    if not value:
        return ""
    if value[0] not in {"'", '"'}:
        return value
    quote = value[0]
    if len(value) < 2 or value[-1] != quote:
        raise EnvironmentFileError(
            f"project .env line {line_number} has an unterminated quoted value"
        )
    return value[1:-1]


__all__ = ["EnvironmentFileError", "load_project_env"]
