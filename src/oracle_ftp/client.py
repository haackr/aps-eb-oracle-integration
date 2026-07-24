from __future__ import annotations

import os
import posixpath
from io import BytesIO
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Literal

import paramiko

from .exceptions import OracleFTPConnectionError, OracleFTPTransferError

OracleFTPProtocol = Literal["sftp"]


@dataclass(frozen=True, slots=True)
class OracleFTPConfig:
    """Runtime configuration for Oracle SFTP access."""

    host: str
    username: str
    password: str
    protocol: OracleFTPProtocol = "sftp"
    port: int = 22
    timeout_seconds: float = 30.0
    remote_base_dir: str = "/"

    @classmethod
    def from_env(cls) -> "OracleFTPConfig":
        """Create config from environment variables.

        Required:
        - ORACLE_FTP_HOST
        - ORACLE_FTP_USERNAME
        - ORACLE_FTP_PASSWORD

        Optional:
        - ORACLE_FTP_PROTOCOL (sftp, default: sftp)
        - ORACLE_FTP_PORT (default: 22)
        - ORACLE_FTP_TIMEOUT_SECONDS (default: 30)
        - ORACLE_FTP_REMOTE_BASE_DIR (default: /)
        """
        host_raw = os.getenv("ORACLE_FTP_HOST", "").strip()
        username = os.getenv("ORACLE_FTP_USERNAME", "").strip()
        password = os.getenv("ORACLE_FTP_PASSWORD", "").strip()

        protocol_raw = os.getenv("ORACLE_FTP_PROTOCOL", "sftp").strip().lower()
        if protocol_raw != "sftp":
            raise ValueError("ORACLE_FTP_PROTOCOL must be 'sftp'")
        protocol: OracleFTPProtocol = protocol_raw

        host, host_port = _split_host_and_port(host_raw)
        raw_port = os.getenv("ORACLE_FTP_PORT", "").strip()
        if raw_port:
            port = int(raw_port)
        elif host_port is not None:
            port = host_port
        else:
            port = 22

        timeout_seconds = float(os.getenv("ORACLE_FTP_TIMEOUT_SECONDS", "30"))
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
            remote_base_dir=remote_base_dir,
        )


class OracleFTPClient:
    """Lightweight SFTP client for uploading and downloading integration files."""

    def __init__(
        self,
        config: OracleFTPConfig,
        *,
        sftp_factory: Callable[[OracleFTPConfig], tuple[Any, Any]] | None = None,
    ) -> None:
        self._config = config
        self._sftp_factory = sftp_factory
        self._transport: Any | None = None
        self._sftp: Any | None = None

    def __enter__(self) -> "OracleFTPClient":
        self.connect()
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.close()

    def connect(self) -> None:
        """Open SFTP connection and authenticate."""
        if self._sftp is not None:
            return

        try:
            if self._sftp_factory is not None:
                transport, sftp = self._sftp_factory(self._config)
            else:
                transport = paramiko.Transport((self._config.host, self._config.port))
                transport.banner_timeout = self._config.timeout_seconds
                transport.connect(username=self._config.username, password=self._config.password)
                sftp = paramiko.SFTPClient.from_transport(transport)

            self._transport = transport
            self._sftp = sftp
        except (OSError, paramiko.SSHException) as exc:
            raise OracleFTPConnectionError(f"SFTP connection/login failed: {exc}") from exc

    def close(self) -> None:
        if self._sftp is None and self._transport is None:
            return

        try:
            if self._sftp is not None:
                self._sftp.close()
        finally:
            self._sftp = None
            if self._transport is not None:
                self._transport.close()
                self._transport = None

    def upload_file(
        self,
        local_path: str | Path,
        remote_path: str,
    ) -> str:
        """Upload a local file to remote SFTP path."""
        sftp = self._require_connection()
        source = Path(local_path)
        if not source.exists() or not source.is_file():
            raise OracleFTPTransferError(f"Local file does not exist: {source}")

        target = self._resolve_remote_path(remote_path)

        try:
            sftp.put(str(source), target)
        except (OSError, paramiko.SSHException) as exc:
            raise OracleFTPTransferError(f"Failed to upload {source} to {target}: {exc}") from exc

        return target

    def upload_bytes(
        self,
        file_bytes: bytes,
        remote_path: str,
    ) -> str:
        """Upload in-memory bytes to a remote SFTP path."""
        sftp = self._require_connection()
        target = self._resolve_remote_path(remote_path)

        try:
            with sftp.file(target, "wb") as remote_file:
                payload = BytesIO(file_bytes)
                remote_file.write(payload.read())
        except (OSError, paramiko.SSHException) as exc:
            raise OracleFTPTransferError(f"Failed to upload bytes to {target}: {exc}") from exc

        return target

    def download_file(
        self,
        remote_path: str,
        local_path: str | Path,
        *,
        overwrite: bool = False,
    ) -> Path:
        """Download a remote SFTP file to local disk."""
        sftp = self._require_connection()
        source = self._resolve_remote_path(remote_path)
        target = Path(local_path)

        if target.exists() and not overwrite:
            raise OracleFTPTransferError(
                f"Destination already exists: {target}. Set overwrite=True to replace it."
            )

        target.parent.mkdir(parents=True, exist_ok=True)

        try:
            sftp.get(source, str(target))
        except (OSError, paramiko.SSHException) as exc:
            raise OracleFTPTransferError(f"Failed to download {source} to {target}: {exc}") from exc

        return target

    def list_files(self, remote_dir: str = ".") -> list[str]:
        """List files for a remote directory path."""
        sftp = self._require_connection()
        target = self._resolve_remote_path(remote_dir)
        try:
            names = sftp.listdir(target)
            return [posixpath.join(target, name) for name in names]
        except (OSError, paramiko.SSHException) as exc:
            raise OracleFTPTransferError(f"Failed to list remote directory {target}: {exc}") from exc

    def delete_file(self, remote_path: str) -> str:
        """Delete a remote SFTP file."""
        sftp = self._require_connection()
        target = self._resolve_remote_path(remote_path)
        try:
            sftp.remove(target)
        except (OSError, paramiko.SSHException) as exc:
            raise OracleFTPTransferError(f"Failed to delete remote file {target}: {exc}") from exc
        return target

    def ensure_remote_dir(self, remote_dir: str) -> str:
        """Create remote directory path recursively if needed."""
        sftp = self._require_connection()
        target = self._resolve_remote_path(remote_dir)

        parts = [part for part in target.split("/") if part]
        current = ""
        for part in parts:
            current = f"{current}/{part}"
            try:
                sftp.stat(current)
            except FileNotFoundError:
                try:
                    sftp.mkdir(current)
                except (OSError, paramiko.SSHException) as exc:
                    raise OracleFTPTransferError(
                        f"Failed to create remote directory {current}: {exc}"
                    ) from exc
            except (OSError, paramiko.SSHException) as exc:
                raise OracleFTPTransferError(f"Failed to create remote directory {current}: {exc}") from exc

        return target

    def _require_connection(self) -> Any:
        if self._sftp is None:
            self.connect()
        if self._sftp is None:
            raise OracleFTPConnectionError("SFTP client is not connected")
        return self._sftp

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


def _split_host_and_port(host: str) -> tuple[str, int | None]:
    """Extract host and optional port from ORACLE_FTP_HOST.

    Supports values like "hostname" and "hostname:2222".
    """
    if not host or host.startswith("["):
        return host, None

    if ":" not in host:
        return host, None

    host_part, possible_port = host.rsplit(":", 1)
    if not possible_port.isdigit():
        return host, None
    return host_part, int(possible_port)
