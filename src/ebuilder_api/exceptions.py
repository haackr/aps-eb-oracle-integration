from __future__ import annotations

from dataclasses import dataclass
from typing import Any


class EBuilderAPIError(Exception):
    """Base exception for all e-Builder wrapper errors."""


@dataclass(slots=True)
class EBuilderRequestError(EBuilderAPIError):
    """Raised when the request fails before a valid response is received."""

    message: str

    def __str__(self) -> str:
        return self.message


@dataclass(slots=True)
class EBuilderAuthError(EBuilderAPIError):
    """Raised for auth failures (401/403)."""

    status_code: int
    payload: Any

    def __str__(self) -> str:
        return f"Auth error ({self.status_code}): {self.payload}"


@dataclass(slots=True)
class EBuilderClientError(EBuilderAPIError):
    """Raised for non-auth 4xx errors."""

    status_code: int
    payload: Any

    def __str__(self) -> str:
        return f"Client error ({self.status_code}): {self.payload}"


@dataclass(slots=True)
class EBuilderServerError(EBuilderAPIError):
    """Raised for 5xx errors."""

    status_code: int
    payload: Any

    def __str__(self) -> str:
        return f"Server error ({self.status_code}): {self.payload}"
