from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from ebuilder_api.exceptions import EBuilderRequestError
from integration_jobs.oracle_to_ebuilder_imports import (
    DEFAULT_PO_EBUILDER_FOLDER,
    DEFAULT_PO_ORACLE_REMOTE_DIR,
    DEFAULT_REQ_EBUILDER_FOLDER,
    DEFAULT_REQ_ORACLE_REMOTE_DIR,
    OracleToEBuilderImportConfig,
    OracleToEBuilderImportJob,
    po_numbers_import_config_from_env,
    req_numbers_import_config_from_env,
)


@dataclass(slots=True)
class _UploadResponse:
    success: bool
    support_id: str | None


class _StubOracleFTP:
    def __init__(self, files: list[str]) -> None:
        self.files = files
        self.downloads: list[tuple[str, Path, bool]] = []
        self.deletes: list[str] = []

    def list_files(self, remote_dir: str = ".") -> list[str]:
        self.list_dir = remote_dir
        return list(self.files)

    def download_file(self, remote_path: str, local_path: str | Path, *, overwrite: bool = False) -> Path:
        target = Path(local_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(f"xls:{remote_path}".encode("utf-8"))
        self.downloads.append((remote_path, target, overwrite))
        return target

    def delete_file(self, remote_path: str) -> str:
        self.deletes.append(remote_path)
        return remote_path


class _StubEBuilder:
    def __init__(self) -> None:
        self.uploads: list[dict[str, Any]] = []

    def upload_document_file(
        self,
        *,
        local_path: str | Path,
        folder_path: str,
        project_name: str,
        file_name: str | None = None,
        create_folder: bool = False,
    ) -> _UploadResponse:
        local = Path(local_path)
        self.uploads.append(
            {
                "local_path": local,
                "folder_path": folder_path,
                "project_name": project_name,
                "file_name": file_name,
                "create_folder": create_folder,
                "content": local.read_bytes(),
            }
        )
        return _UploadResponse(success=True, support_id=f"SUP-{local.name}")


class _FailingStubEBuilder(_StubEBuilder):
    def upload_document_file(
        self,
        *,
        local_path: str | Path,
        folder_path: str,
        project_name: str,
        file_name: str | None = None,
        create_folder: bool = False,
    ) -> _UploadResponse:
        _ = (local_path, folder_path, project_name, file_name, create_folder)
        return _UploadResponse(success=False, support_id="FAIL")


def _fake_converter(
    source_path: str | Path,
    destination_path: str | Path | None = None,
    *,
    overwrite: bool = False,
) -> Path:
    source = Path(source_path)
    target = Path(destination_path) if destination_path is not None else source.with_suffix(".xlsx")
    if target.exists() and not overwrite:
        raise RuntimeError("destination exists")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(source.read_bytes() + b"|converted")
    return target


def test_req_job_downloads_converts_and_uploads_all_xls_sorted() -> None:
    oracle_ftp = _StubOracleFTP(
        [
            "/outbound/REQ/OracleREQ_20260721.xls",
            "/outbound/REQ/ignore.txt",
            "/outbound/REQ/OracleREQ_20260722.xls",
        ]
    )
    ebuilder = _StubEBuilder()
    config = OracleToEBuilderImportConfig(
        oracle_remote_dir="outbound/REQ",
        ebuilder_project_name="Lawson Integration",
        ebuilder_folder_path="Documents/01A CA & DPI Imports/CA Import/PREQ Updated",
        latest_only=False,
    )

    result = OracleToEBuilderImportJob(
        ebuilder,
        oracle_ftp,
        config,
        converter=_fake_converter,
    ).run()

    assert result.uploaded_count == 2
    assert result.uploaded_file_names == ["OracleREQ_20260722.xlsx", "OracleREQ_20260721.xlsx"]
    assert oracle_ftp.list_dir == "outbound/REQ"
    assert len(oracle_ftp.downloads) == 2
    assert oracle_ftp.deletes == [
        "/outbound/REQ/OracleREQ_20260722.xls",
        "/outbound/REQ/OracleREQ_20260721.xls",
    ]
    assert ebuilder.uploads[0]["folder_path"].endswith("PREQ Updated")
    assert ebuilder.uploads[0]["project_name"] == "Lawson Integration"


def test_po_job_latest_only_uploads_single_file() -> None:
    oracle_ftp = _StubOracleFTP(
        [
            "outbound/PO/OraclePO_20260721.xls",
            "outbound/PO/OraclePO_20260722.xls",
        ]
    )
    ebuilder = _StubEBuilder()
    config = OracleToEBuilderImportConfig(
        oracle_remote_dir="outbound/PO",
        ebuilder_project_name="Lawson Integration",
        ebuilder_folder_path="Documents/01A CA & DPI Imports/CA Import/PO Issued",
        latest_only=True,
    )

    result = OracleToEBuilderImportJob(
        ebuilder,
        oracle_ftp,
        config,
        converter=_fake_converter,
    ).run()

    assert result.uploaded_count == 1
    assert result.uploaded_file_names == ["OraclePO_20260722.xlsx"]
    assert oracle_ftp.deletes == ["outbound/PO/OraclePO_20260722.xls"]
    assert ebuilder.uploads[0]["folder_path"].endswith("PO Issued")


def test_import_job_returns_empty_when_no_xls_files() -> None:
    oracle_ftp = _StubOracleFTP(["outbound/PO/readme.md", "outbound/PO/notes.txt"])
    ebuilder = _StubEBuilder()
    config = OracleToEBuilderImportConfig(
        oracle_remote_dir="outbound/PO",
        ebuilder_project_name="Lawson Integration",
        ebuilder_folder_path="Documents/01A CA & DPI Imports/CA Import/PO Issued",
    )

    result = OracleToEBuilderImportJob(ebuilder, oracle_ftp, config, converter=_fake_converter).run()

    assert result.uploaded_count == 0
    assert result.uploaded_file_names == []
    assert ebuilder.uploads == []
    assert oracle_ftp.deletes == []


def test_import_job_does_not_delete_when_upload_fails() -> None:
    oracle_ftp = _StubOracleFTP(["outbound/REQ/OracleREQ_20260722.xls"])
    ebuilder = _FailingStubEBuilder()
    config = OracleToEBuilderImportConfig(
        oracle_remote_dir="outbound/REQ",
        ebuilder_project_name="Lawson Integration",
        ebuilder_folder_path="Documents/01A CA & DPI Imports/CA Import/PREQ Updated",
    )

    with pytest.raises(EBuilderRequestError):
        OracleToEBuilderImportJob(ebuilder, oracle_ftp, config, converter=_fake_converter).run()

    assert oracle_ftp.deletes == []


def test_req_config_defaults_from_env(monkeypatch) -> None:
    monkeypatch.delenv("INTEGRATION_REQ_IMPORT_ORACLE_REMOTE_DIR", raising=False)
    monkeypatch.delenv("INTEGRATION_REQ_IMPORT_EBUILDER_PROJECT_NAME", raising=False)
    monkeypatch.delenv("INTEGRATION_REQ_IMPORT_EBUILDER_FOLDER", raising=False)
    monkeypatch.delenv("INTEGRATION_REQ_IMPORT_LATEST_ONLY", raising=False)
    monkeypatch.delenv("INTEGRATION_REQ_IMPORT_CREATE_FOLDER", raising=False)

    config = req_numbers_import_config_from_env()

    assert config.oracle_remote_dir == DEFAULT_REQ_ORACLE_REMOTE_DIR
    assert config.ebuilder_project_name == "Lawson Integration"
    assert config.ebuilder_folder_path == DEFAULT_REQ_EBUILDER_FOLDER
    assert config.latest_only is False
    assert config.create_folder is False


def test_po_config_defaults_from_env(monkeypatch) -> None:
    monkeypatch.delenv("INTEGRATION_PO_IMPORT_ORACLE_REMOTE_DIR", raising=False)
    monkeypatch.delenv("INTEGRATION_PO_IMPORT_EBUILDER_PROJECT_NAME", raising=False)
    monkeypatch.delenv("INTEGRATION_PO_IMPORT_EBUILDER_FOLDER", raising=False)
    monkeypatch.delenv("INTEGRATION_PO_IMPORT_LATEST_ONLY", raising=False)
    monkeypatch.delenv("INTEGRATION_PO_IMPORT_CREATE_FOLDER", raising=False)

    config = po_numbers_import_config_from_env()

    assert config.oracle_remote_dir == DEFAULT_PO_ORACLE_REMOTE_DIR
    assert config.ebuilder_project_name == "Lawson Integration"
    assert config.ebuilder_folder_path == DEFAULT_PO_EBUILDER_FOLDER
    assert config.latest_only is False
    assert config.create_folder is False
