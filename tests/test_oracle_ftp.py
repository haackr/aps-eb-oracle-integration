from __future__ import annotations

import ftplib
from pathlib import Path

import pytest

from oracle_ftp import OracleFTPClient, OracleFTPConfig
from oracle_ftp.exceptions import OracleFTPTransferError


class FakeFTP:
    def __init__(self) -> None:
        self.connected = False
        self.logged_in = False
        self.passive_mode: bool | None = None
        self.quit_called = False
        self.files: dict[str, bytes] = {}
        self.directories: set[str] = {"/"}
        self.deleted: list[str] = []

    def connect(self, host: str, port: int, timeout: float) -> None:
        self.connected = True
        self.host = host
        self.port = port
        self.timeout = timeout

    def login(self, user: str, passwd: str) -> None:
        self.logged_in = True
        self.user = user
        self.passwd = passwd

    def set_pasv(self, passive_mode: bool) -> None:
        self.passive_mode = passive_mode

    def quit(self) -> None:
        self.quit_called = True

    def close(self) -> None:
        self.quit_called = True

    def mkd(self, path: str) -> None:
        if path in self.directories:
            raise ftplib.error_perm("550 File exists")
        self.directories.add(path)

    def storbinary(self, command: str, file_obj: object) -> None:
        target = command.split(" ", 1)[1]
        assert hasattr(file_obj, "read")
        self.files[target] = file_obj.read()  # type: ignore[call-arg]

    def retrbinary(self, command: str, callback: object) -> None:
        source = command.split(" ", 1)[1]
        if source not in self.files:
            raise ftplib.error_perm("550 File not found")
        assert callable(callback)
        callback(self.files[source])  # type: ignore[misc]

    def nlst(self, path: str) -> list[str]:
        prefix = path.rstrip("/") + "/"
        return sorted([file_path for file_path in self.files if file_path.startswith(prefix)])

    def delete(self, path: str) -> None:
        if path not in self.files:
            raise ftplib.error_perm("550 File not found")
        del self.files[path]
        self.deleted.append(path)


class FakeFTPS(FakeFTP, ftplib.FTP_TLS):
    def __init__(self) -> None:
        FakeFTP.__init__(self)
        self.prot_p_called = False

    def prot_p(self) -> None:
        self.prot_p_called = True


def test_oracle_ftp_config_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ORACLE_FTP_HOST", "ftp.example.com")
    monkeypatch.setenv("ORACLE_FTP_USERNAME", "integration-user")
    monkeypatch.setenv("ORACLE_FTP_PASSWORD", "integration-pass")
    monkeypatch.setenv("ORACLE_FTP_PROTOCOL", "ftps")
    monkeypatch.setenv("ORACLE_FTP_REMOTE_BASE_DIR", "/exports/oracle")

    config = OracleFTPConfig.from_env()

    assert config.host == "ftp.example.com"
    assert config.username == "integration-user"
    assert config.password == "integration-pass"
    assert config.protocol == "ftps"
    assert config.remote_base_dir == "/exports/oracle"


def test_upload_and_download_file_round_trip(tmp_path: Path) -> None:
    source = tmp_path / "source.csv"
    source.write_bytes(b"a,b\n1,2\n")
    destination = tmp_path / "downloads" / "source.csv"

    fake = FakeFTP()
    config = OracleFTPConfig(
        host="ftp.example.com",
        username="user",
        password="pass",
        protocol="ftp",
        remote_base_dir="/exports/oracle",
    )

    with OracleFTPClient(config, ftp_factory=lambda: fake) as client:
        remote_path = client.upload_file(source, "daily/source.csv")
        downloaded_path = client.download_file("daily/source.csv", destination)

    assert remote_path == "/exports/oracle/daily/source.csv"
    assert downloaded_path == destination
    assert destination.read_bytes() == b"a,b\n1,2\n"
    assert fake.quit_called is True


def test_download_overwrite_guard(tmp_path: Path) -> None:
    destination = tmp_path / "existing.csv"
    destination.write_bytes(b"old")

    fake = FakeFTP()
    fake.files["/exports/oracle/daily/source.csv"] = b"new"
    config = OracleFTPConfig(
        host="ftp.example.com",
        username="user",
        password="pass",
        protocol="ftp",
        remote_base_dir="/exports/oracle",
    )

    with OracleFTPClient(config, ftp_factory=lambda: fake) as client:
        with pytest.raises(OracleFTPTransferError):
            client.download_file("daily/source.csv", destination)


def test_list_files_returns_remote_entries() -> None:
    fake = FakeFTP()
    fake.files["/exports/oracle/daily/a.csv"] = b"a"
    fake.files["/exports/oracle/daily/b.csv"] = b"b"

    config = OracleFTPConfig(
        host="ftp.example.com",
        username="user",
        password="pass",
        protocol="ftp",
        remote_base_dir="/exports/oracle",
    )

    with OracleFTPClient(config, ftp_factory=lambda: fake) as client:
        entries = client.list_files("daily")

    assert entries == [
        "/exports/oracle/daily/a.csv",
        "/exports/oracle/daily/b.csv",
    ]


def test_ftps_enables_data_channel_protection() -> None:
    fake = FakeFTPS()
    config = OracleFTPConfig(
        host="ftp.example.com",
        username="user",
        password="pass",
        protocol="ftps",
    )

    with OracleFTPClient(config, ftp_factory=lambda: fake):
        pass

    assert fake.prot_p_called is True


def test_upload_bytes_writes_remote_file() -> None:
    fake = FakeFTP()
    config = OracleFTPConfig(
        host="ftp.example.com",
        username="user",
        password="pass",
        protocol="ftp",
        remote_base_dir="/exports/oracle",
    )

    with OracleFTPClient(config, ftp_factory=lambda: fake) as client:
        remote_path = client.upload_bytes(b"payload", "outbound/reqs/reqs.csv")

    assert remote_path == "/exports/oracle/outbound/reqs/reqs.csv"
    assert fake.files["/exports/oracle/outbound/reqs/reqs.csv"] == b"payload"


def test_delete_file_removes_remote_entry() -> None:
    fake = FakeFTP()
    fake.files["/exports/oracle/outbound/reqs/old.xls"] = b"legacy"
    config = OracleFTPConfig(
        host="ftp.example.com",
        username="user",
        password="pass",
        protocol="ftp",
        remote_base_dir="/exports/oracle",
    )

    with OracleFTPClient(config, ftp_factory=lambda: fake) as client:
        deleted_path = client.delete_file("outbound/reqs/old.xls")

    assert deleted_path == "/exports/oracle/outbound/reqs/old.xls"
    assert "/exports/oracle/outbound/reqs/old.xls" not in fake.files
    assert fake.deleted == ["/exports/oracle/outbound/reqs/old.xls"]
