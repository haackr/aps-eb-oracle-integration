from __future__ import annotations

from pathlib import Path
from typing import Callable

import pytest

from oracle_ftp import OracleFTPClient, OracleFTPConfig
from oracle_ftp.exceptions import OracleFTPTransferError


class _FakeTransport:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


class _FakeRemoteFile:
    def __init__(self, on_close: Callable[[bytes], None] | None = None) -> None:
        self._data = bytearray()
        self._on_close = on_close

    def write(self, data: bytes) -> int:
        self._data.extend(data)
        return len(data)

    def read(self) -> bytes:
        return bytes(self._data)

    def __enter__(self) -> "_FakeRemoteFile":
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        if self._on_close is not None:
            self._on_close(bytes(self._data))
        return None


class _FakeSFTP:
    def __init__(self) -> None:
        self.files: dict[str, bytes] = {}
        self.directories: set[str] = {"/"}
        self.deleted: list[str] = []
        self.closed = False

    def close(self) -> None:
        self.closed = True

    def put(self, local_path: str, remote_path: str) -> None:
        parent = str(Path(remote_path).parent).replace("\\", "/")
        if parent not in self.directories:
            raise FileNotFoundError(parent)
        self.files[remote_path] = Path(local_path).read_bytes()

    def get(self, remote_path: str, local_path: str) -> None:
        if remote_path not in self.files:
            raise FileNotFoundError(remote_path)
        Path(local_path).write_bytes(self.files[remote_path])

    def listdir(self, remote_dir: str) -> list[str]:
        prefix = remote_dir.rstrip("/") + "/"
        names = [Path(path).name for path in self.files if path.startswith(prefix)]
        return sorted(names)

    def remove(self, path: str) -> None:
        if path not in self.files:
            raise FileNotFoundError(path)
        del self.files[path]
        self.deleted.append(path)

    def stat(self, remote_path: str) -> object:
        if remote_path in self.directories or remote_path in self.files:
            return object()
        raise FileNotFoundError(remote_path)

    def mkdir(self, remote_path: str) -> None:
        self.directories.add(remote_path)

    def file(self, remote_path: str, mode: str) -> _FakeRemoteFile:
        assert mode == "wb"
        parent = str(Path(remote_path).parent).replace("\\", "/")
        if parent not in self.directories:
            raise FileNotFoundError(parent)
        return _FakeRemoteFile(on_close=lambda data: self.files.__setitem__(remote_path, data))


def _factory(
    sftp: _FakeSFTP, transport: _FakeTransport
) -> Callable[[OracleFTPConfig], tuple[_FakeTransport, _FakeSFTP]]:
    def _build(_: OracleFTPConfig) -> tuple[_FakeTransport, _FakeSFTP]:
        return transport, sftp

    return _build


def test_oracle_ftp_config_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ORACLE_FTP_HOST", "ftp.example.com")
    monkeypatch.setenv("ORACLE_FTP_USERNAME", "integration-user")
    monkeypatch.setenv("ORACLE_FTP_PASSWORD", "integration-pass")
    monkeypatch.setenv("ORACLE_FTP_PROTOCOL", "sftp")
    monkeypatch.setenv("ORACLE_FTP_REMOTE_BASE_DIR", "/exports/oracle")

    config = OracleFTPConfig.from_env()

    assert config.host == "ftp.example.com"
    assert config.username == "integration-user"
    assert config.password == "integration-pass"
    assert config.protocol == "sftp"
    assert config.remote_base_dir == "/exports/oracle"


def test_upload_and_download_file_round_trip(tmp_path: Path) -> None:
    source = tmp_path / "source.csv"
    source.write_bytes(b"a,b\n1,2\n")
    destination = tmp_path / "downloads" / "source.csv"

    fake = _FakeSFTP()
    fake.directories.update({"/exports", "/exports/oracle", "/exports/oracle/daily"})
    transport = _FakeTransport()
    config = OracleFTPConfig(
        host="ftp.example.com",
        username="user",
        password="pass",
        protocol="sftp",
        remote_base_dir="/exports/oracle",
    )

    with OracleFTPClient(config, sftp_factory=_factory(fake, transport)) as client:
        remote_path = client.upload_file(source, "daily/source.csv")
        downloaded_path = client.download_file("daily/source.csv", destination)

    assert remote_path == "/exports/oracle/daily/source.csv"
    assert downloaded_path == destination
    assert destination.read_bytes() == b"a,b\n1,2\n"
    assert fake.closed is True
    assert transport.closed is True


def test_download_overwrite_guard(tmp_path: Path) -> None:
    destination = tmp_path / "existing.csv"
    destination.write_bytes(b"old")

    fake = _FakeSFTP()
    transport = _FakeTransport()
    fake.files["/exports/oracle/daily/source.csv"] = b"new"
    config = OracleFTPConfig(
        host="ftp.example.com",
        username="user",
        password="pass",
        protocol="sftp",
        remote_base_dir="/exports/oracle",
    )

    with OracleFTPClient(config, sftp_factory=_factory(fake, transport)) as client:
        with pytest.raises(OracleFTPTransferError):
            client.download_file("daily/source.csv", destination)


def test_list_files_returns_remote_entries() -> None:
    fake = _FakeSFTP()
    transport = _FakeTransport()
    fake.files["/exports/oracle/daily/a.csv"] = b"a"
    fake.files["/exports/oracle/daily/b.csv"] = b"b"

    config = OracleFTPConfig(
        host="ftp.example.com",
        username="user",
        password="pass",
        protocol="sftp",
        remote_base_dir="/exports/oracle",
    )

    with OracleFTPClient(config, sftp_factory=_factory(fake, transport)) as client:
        entries = client.list_files("daily")

    assert entries == [
        "/exports/oracle/daily/a.csv",
        "/exports/oracle/daily/b.csv",
    ]


def test_host_with_inline_port_is_split_when_env_port_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ORACLE_FTP_HOST", "ftp.example.com:2222")
    monkeypatch.setenv("ORACLE_FTP_USERNAME", "integration-user")
    monkeypatch.setenv("ORACLE_FTP_PASSWORD", "integration-pass")
    monkeypatch.setenv("ORACLE_FTP_PROTOCOL", "sftp")
    monkeypatch.delenv("ORACLE_FTP_PORT", raising=False)

    config = OracleFTPConfig.from_env()

    assert config.host == "ftp.example.com"
    assert config.port == 2222


def test_upload_bytes_writes_remote_file() -> None:
    fake = _FakeSFTP()
    fake.directories.update({"/exports", "/exports/oracle", "/exports/oracle/outbound", "/exports/oracle/outbound/reqs"})
    transport = _FakeTransport()
    config = OracleFTPConfig(
        host="ftp.example.com",
        username="user",
        password="pass",
        protocol="sftp",
        remote_base_dir="/exports/oracle",
    )

    with OracleFTPClient(config, sftp_factory=_factory(fake, transport)) as client:
        remote_path = client.upload_bytes(b"payload", "outbound/reqs/reqs.csv")

    assert remote_path == "/exports/oracle/outbound/reqs/reqs.csv"
    assert fake.files["/exports/oracle/outbound/reqs/reqs.csv"] == b"payload"


def test_upload_fails_when_remote_directory_is_missing() -> None:
    fake = _FakeSFTP()
    transport = _FakeTransport()
    config = OracleFTPConfig(
        host="ftp.example.com",
        username="user",
        password="pass",
        protocol="sftp",
        remote_base_dir="/exports/oracle",
    )

    with OracleFTPClient(config, sftp_factory=_factory(fake, transport)) as client:
        with pytest.raises(OracleFTPTransferError):
            client.upload_bytes(b"payload", "missing/reqs.csv")


def test_delete_file_removes_remote_entry() -> None:
    fake = _FakeSFTP()
    transport = _FakeTransport()
    fake.files["/exports/oracle/outbound/reqs/old.xls"] = b"legacy"
    config = OracleFTPConfig(
        host="ftp.example.com",
        username="user",
        password="pass",
        protocol="sftp",
        remote_base_dir="/exports/oracle",
    )

    with OracleFTPClient(config, sftp_factory=_factory(fake, transport)) as client:
        deleted_path = client.delete_file("outbound/reqs/old.xls")

    assert deleted_path == "/exports/oracle/outbound/reqs/old.xls"
    assert "/exports/oracle/outbound/reqs/old.xls" not in fake.files
    assert fake.deleted == ["/exports/oracle/outbound/reqs/old.xls"]
