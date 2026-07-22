from __future__ import annotations

import ftplib
import os
import posixpath
from io import BytesIO
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Literal

from .exceptions import OracleFTPConnectionError, OracleFTPTransferError

OracleFTPProtocol = Literal["ftp", "ftps"]


@dataclass(frozen=True, slots=True)
class OracleFTPConfig:
    """Runtime configuration for Oracle FTP/FTPS access."""

    host: str
    username: str
    password: str
    protocol: OracleFTPProtocol = "ftps"
    port: int = 21
    timeout_seconds: float = 30.0
    passive_mode: bool = True
    remote_base_dir: str = "/"

    @classmethod
    def from_env(cls) -> "OracleFTPConfig":
        """Create config from environment variables.

        Required:
        - ORACLE_FTP_HOST
        - ORACLE_FTP_USERNAME
        - ORACLE_FTP_PASSWORD

        Optional:
        - ORACLE_FTP_PROTOCOL (ftp|ftps, default: ftps)
        - ORACLE_FTP_PORT (default: 21)
        - ORACLE_FTP_TIMEOUT_SECONDS (default: 30)
        - ORACLE_FTP_PASSIVE_MODE (true|false, default: true)
        - ORACLE_FTP_REMOTE_BASE_DIR (default: /)
        """
        host = os.getenv("ORACLE_FTP_HOST", "").strip()
        username = os.getenv("ORACLE_FTP_USERNAME", "").strip()
        password = os.getenv("ORACLE_FTP_PASSWORD", "").strip()

        protocol_raw = os.getenv("ORACLE_FTP_PROTOCOL", "ftps").strip().lower()
        if protocol_raw not in {"ftp", "ftps"}:
            raise ValueError("ORACLE_FTP_PROTOCOL must be either 'ftp' or 'ftps'")
        protocol: OracleFTPProtocol = protocol_raw

        port = int(os.getenv("ORACLE_FTP_PORT", "21"))
        timeout_seconds = float(os.getenv("ORACLE_FTP_TIMEOUT_SECONDS", "30"))
        passive_mode = os.getenv("ORACLE_FTP_PASSIVE_MODE", "true").strip().lower() != "false"
        remote_base_dir = os.getenv("ORACLE_FTP_REMOTE_BASE_DIR", "/").strip() or "/"

        if not host:
            raise ValueError("Missing required env var: ORACLE_FTP_HOST")
        if not username:
            raise ValueError("Missing required env var: ORACLE_FTP_USERNAME")
        if not password:
            raise ValueError("Missing required env var: ORACLE_FTP_PASSWORD")

        return cls(
            host=host,
            username=username,
            password=password,
            protocol=protocol,
            port=port,
            timeout_seconds=timeout_seconds,
            passive_mode=passive_mode,
            remote_base_dir=remote_base_dir,
        )


class OracleFTPClient:
    """Lightweight FTP/FTPS client for uploading and downloading integration files."""

    def __init__(
        self,
        config: OracleFTPConfig,
        *,
        ftp_factory: Callable[[], ftplib.FTP] | None = None,
    ) -> None:
        self._config = config
        self._ftp_factory = ftp_factory
        self._ftp: ftplib.FTP | None = None

    def __enter__(self) -> "OracleFTPClient":
        self.connect()
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.close()

    def connect(self) -> None:
        """Open FTP/FTPS connection and authenticate."""
        if self._ftp is not None:
            return

        ftp = self._build_ftp_client()
        try:
            ftp.connect(
                host=self._config.host,
                port=self._config.port,
                timeout=self._config.timeout_seconds,
            )
            ftp.login(user=self._config.username, passwd=self._config.password)
            ftp.set_pasv(self._config.passive_mode)
            if self._config.protocol == "ftps" and isinstance(ftp, ftplib.FTP_TLS):
                ftp.prot_p()
        except ftplib.all_errors as exc:
            raise OracleFTPConnectionError(f"FTP connection/login failed: {exc}") from exc

        self._ftp = ftp

    def close(self) -> None:
        if self._ftp is None:
            return

        try:
            self._ftp.quit()
        except ftplib.all_errors:
            self._ftp.close()
        finally:
            self._ftp = None

    def upload_file(
        self,
        local_path: str | Path,
        remote_path: str,
        *,
        create_remote_dirs: bool = True,
    ) -> str:
        """Upload a local file to remote FTP path."""
        ftp = self._require_connection()
        source = Path(local_path)
        if not source.exists() or not source.is_file():
            raise OracleFTPTransferError(f"Local file does not exist: {source}")

        target = self._resolve_remote_path(remote_path)
        parent_dir = posixpath.dirname(target)
        if create_remote_dirs and parent_dir:
            self.ensure_remote_dir(parent_dir)

        try:
            with source.open("rb") as file_obj:
                ftp.storbinary(f"STOR {target}", file_obj)
        except (OSError, ftplib.all_errors) as exc:
            raise OracleFTPTransferError(f"Failed to upload {source} to {target}: {exc}") from exc

        return target

    def upload_bytes(
        self,
        file_bytes: bytes,
        remote_path: str,
        *,
        create_remote_dirs: bool = True,
    ) -> str:
        """Upload in-memory bytes to a remote FTP path."""
        ftp = self._require_connection()
        target = self._resolve_remote_path(remote_path)
        parent_dir = posixpath.dirname(target)
        if create_remote_dirs and parent_dir:
            self.ensure_remote_dir(parent_dir)

        payload = BytesIO(file_bytes)
        try:
            ftp.storbinary(f"STOR {target}", payload)
        except ftplib.all_errors as exc:
            raise OracleFTPTransferError(f"Failed to upload bytes to {target}: {exc}") from exc

        return target

    def download_file(
        self,
        remote_path: str,
        local_path: str | Path,
        *,
        overwrite: bool = False,
    ) -> Path:
        """Download a remote FTP file to local disk."""
        ftp = self._require_connection()
        source = self._resolve_remote_path(remote_path)
        target = Path(local_path)

        if target.exists() and not overwrite:
            raise OracleFTPTransferError(
                f"Destination already exists: {target}. Set overwrite=True to replace it."
            )

        target.parent.mkdir(parents=True, exist_ok=True)

        try:
            with target.open("wb") as file_obj:
                ftp.retrbinary(f"RETR {source}", file_obj.write)
        except (OSError, ftplib.all_errors) as exc:
            raise OracleFTPTransferError(f"Failed to download {source} to {target}: {exc}") from exc

        return target

    def list_files(self, remote_dir: str = ".") -> list[str]:
        """List files for a remote directory path."""
        ftp = self._require_connection()
        target = self._resolve_remote_path(remote_dir)
        try:
            return ftp.nlst(target)
        except ftplib.all_errors as exc:
            raise OracleFTPTransferError(f"Failed to list remote directory {target}: {exc}") from exc

    def delete_file(self, remote_path: str) -> str:
        """Delete a remote FTP file."""
        ftp = self._require_connection()
        target = self._resolve_remote_path(remote_path)
        try:
            ftp.delete(target)
        except ftplib.all_errors as exc:
            raise OracleFTPTransferError(f"Failed to delete remote file {target}: {exc}") from exc
        return target

    def ensure_remote_dir(self, remote_dir: str) -> str:
        """Create remote directory path recursively if needed."""
        ftp = self._require_connection()
        target = self._resolve_remote_path(remote_dir)

        parts = [part for part in target.split("/") if part]
        current = ""
        for part in parts:
            current = f"{current}/{part}"
            try:
                ftp.mkd(current)
            except ftplib.error_perm as exc:
                message = str(exc).lower()
                if "exists" in message or message.startswith("550"):
                    continue
                raise OracleFTPTransferError(f"Failed to create remote directory {current}: {exc}") from exc
            except ftplib.all_errors as exc:
                raise OracleFTPTransferError(f"Failed to create remote directory {current}: {exc}") from exc

        return target

    def _build_ftp_client(self) -> ftplib.FTP:
        if self._ftp_factory is not None:
            return self._ftp_factory()
        if self._config.protocol == "ftps":
            return ftplib.FTP_TLS()
        return ftplib.FTP()

    def _require_connection(self) -> ftplib.FTP:
        if self._ftp is None:
            self.connect()
        if self._ftp is None:
            raise OracleFTPConnectionError("FTP client is not connected")
        return self._ftp

    def _resolve_remote_path(self, remote_path: str) -> str:
        normalized = remote_path.replace("\\", "/").strip()
        if not normalized:
            normalized = "."

        base_dir = self._config.remote_base_dir.replace("\\", "/").strip() or "/"
        if not base_dir.startswith("/"):
            base_dir = f"/{base_dir}"

        if normalized.startswith("/"):
            target = normalized
        else:
            target = posixpath.join(base_dir, normalized)

        resolved = posixpath.normpath(target)
        if not resolved.startswith("/"):
            resolved = f"/{resolved}"
        return resolved
