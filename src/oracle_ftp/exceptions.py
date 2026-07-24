from __future__ import annotations

from dataclasses import dataclass


class OracleFTPError(Exception):
    """Base exception for Oracle file transfer integration errors."""


@dataclass(slots=True)
class OracleFTPConnectionError(OracleFTPError):
    """Raised when the SFTP connection or login fails."""

    message: str

    def __str__(self) -> str:
        return self.message


@dataclass(slots=True)
class OracleFTPTransferError(OracleFTPError):
    """Raised when upload/download/list operations fail."""

    message: str

    def __str__(self) -> str:
        return self.message
