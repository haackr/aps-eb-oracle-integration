from __future__ import annotations

import os
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, Protocol

from ebuilder_api import EBuilderClient, EBuilderConfig
from ebuilder_api.exceptions import EBuilderRequestError
from integration_utils import convert_xls_to_xlsx
from oracle_ftp import OracleFTPClient, OracleFTPConfig


class EBuilderUploadPort(Protocol):
    def upload_document_file(
        self,
        *,
        local_path: str | Path,
        folder_path: str,
        project_name: str,
        file_name: str | None = None,
        create_folder: bool = False,
    ) -> Any: ...


class OracleFTPDownloadPort(Protocol):
    def list_files(self, remote_dir: str = ".") -> list[str]: ...

    def download_file(
        self,
        remote_path: str,
        local_path: str | Path,
        *,
        overwrite: bool = False,
    ) -> Path: ...

    def delete_file(self, remote_path: str) -> str: ...


class XLSConverter(Protocol):
    def __call__(
        self,
        source_path: str | Path,
        destination_path: str | Path | None = None,
        *,
        overwrite: bool = False,
    ) -> Path: ...


@dataclass(frozen=True, slots=True)
class OracleToEBuilderImportConfig:
    oracle_remote_dir: str
    ebuilder_project_name: str
    ebuilder_folder_path: str
    latest_only: bool = False
    create_folder: bool = False


@dataclass(frozen=True, slots=True)
class OracleToEBuilderImportResult:
    uploaded_file_names: list[str]
    uploaded_support_ids: list[str | None]

    @property
    def uploaded_count(self) -> int:
        return len(self.uploaded_file_names)


DEFAULT_PROJECT_NAME = "Lawson Integration"
DEFAULT_REQ_ORACLE_REMOTE_DIR = "outbound/REQ"
DEFAULT_REQ_EBUILDER_FOLDER = "Documents/01A CA & DPI Imports/CA Import/PREQ Updated"
DEFAULT_PO_ORACLE_REMOTE_DIR = "outbound/PO"
DEFAULT_PO_EBUILDER_FOLDER = "Documents/01A CA & DPI Imports/CA Import/PO Issued"
DEFAULT_AP_INVOICE_ORACLE_REMOTE_DIR = "outbound/APInvoices"
DEFAULT_AP_INVOICE_EBUILDER_FOLDER = "Documents/02A Purchase Order Invoice Imports"


class OracleToEBuilderImportJob:
    """Imports .xls files from Oracle FTP, converts to .xlsx, uploads to e-Builder."""

    def __init__(
        self,
        ebuilder: EBuilderUploadPort,
        oracle_ftp: OracleFTPDownloadPort,
        config: OracleToEBuilderImportConfig,
        *,
        converter: XLSConverter = convert_xls_to_xlsx,
    ) -> None:
        self._ebuilder = ebuilder
        self._oracle_ftp = oracle_ftp
        self._config = config
        self._converter = converter

    def run(self) -> OracleToEBuilderImportResult:
        remote_candidates = self._oracle_ftp.list_files(self._config.oracle_remote_dir)
        xls_files = self._select_xls_files(remote_candidates, latest_only=self._config.latest_only)
        uploaded_names: list[str] = []
        support_ids: list[str | None] = []

        with TemporaryDirectory(prefix="oracle-ebuilder-import-") as temp_dir:
            temp_root = Path(temp_dir)
            for remote_file in xls_files:
                original_name = Path(remote_file).name
                if not original_name:
                    raise EBuilderRequestError(f"Invalid FTP entry name: {remote_file}")

                local_xls = temp_root / original_name
                self._oracle_ftp.download_file(remote_file, local_xls, overwrite=True)

                local_xlsx = self._converter(local_xls, overwrite=True)
                upload_response = self._ebuilder.upload_document_file(
                    local_path=local_xlsx,
                    folder_path=self._config.ebuilder_folder_path,
                    project_name=self._config.ebuilder_project_name,
                    file_name=local_xlsx.name,
                    create_folder=self._config.create_folder,
                )

                if not getattr(upload_response, "success", False):
                    raise EBuilderRequestError(
                        f"Documents upload reported failure for file: {local_xlsx.name}"
                    )

                self._oracle_ftp.delete_file(remote_file)

                uploaded_names.append(local_xlsx.name)
                support_ids.append(getattr(upload_response, "support_id", None))

        return OracleToEBuilderImportResult(
            uploaded_file_names=uploaded_names,
            uploaded_support_ids=support_ids,
        )

    @staticmethod
    def _select_xls_files(entries: Sequence[str], *, latest_only: bool) -> list[str]:
        xls_files = [entry for entry in entries if Path(entry).suffix.lower() == ".xls"]
        if not xls_files:
            return []

        ordered = sorted(xls_files, key=lambda entry: Path(entry).name, reverse=True)
        if latest_only:
            return [ordered[0]]
        return ordered


def req_numbers_import_config_from_env() -> OracleToEBuilderImportConfig:
    return OracleToEBuilderImportConfig(
        oracle_remote_dir=_get_env_non_empty(
            "INTEGRATION_REQ_IMPORT_ORACLE_REMOTE_DIR", DEFAULT_REQ_ORACLE_REMOTE_DIR
        ),
        ebuilder_project_name=_get_env_non_empty(
            "INTEGRATION_REQ_IMPORT_EBUILDER_PROJECT_NAME", DEFAULT_PROJECT_NAME
        ),
        ebuilder_folder_path=_get_env_non_empty(
            "INTEGRATION_REQ_IMPORT_EBUILDER_FOLDER", DEFAULT_REQ_EBUILDER_FOLDER
        ),
        latest_only=_get_env_bool("INTEGRATION_REQ_IMPORT_LATEST_ONLY", default=False),
        create_folder=_get_env_bool("INTEGRATION_REQ_IMPORT_CREATE_FOLDER", default=False),
    )


def po_numbers_import_config_from_env() -> OracleToEBuilderImportConfig:
    return OracleToEBuilderImportConfig(
        oracle_remote_dir=_get_env_non_empty(
            "INTEGRATION_PO_IMPORT_ORACLE_REMOTE_DIR", DEFAULT_PO_ORACLE_REMOTE_DIR
        ),
        ebuilder_project_name=_get_env_non_empty(
            "INTEGRATION_PO_IMPORT_EBUILDER_PROJECT_NAME", DEFAULT_PROJECT_NAME
        ),
        ebuilder_folder_path=_get_env_non_empty(
            "INTEGRATION_PO_IMPORT_EBUILDER_FOLDER", DEFAULT_PO_EBUILDER_FOLDER
        ),
        latest_only=_get_env_bool("INTEGRATION_PO_IMPORT_LATEST_ONLY", default=False),
        create_folder=_get_env_bool("INTEGRATION_PO_IMPORT_CREATE_FOLDER", default=False),
    )


def ap_invoice_import_config_from_env() -> OracleToEBuilderImportConfig:
    return OracleToEBuilderImportConfig(
        oracle_remote_dir=_get_env_non_empty(
            "INTEGRATION_AP_INVOICE_IMPORT_ORACLE_REMOTE_DIR",
            DEFAULT_AP_INVOICE_ORACLE_REMOTE_DIR,
        ),
        ebuilder_project_name=_get_env_non_empty(
            "INTEGRATION_AP_INVOICE_IMPORT_EBUILDER_PROJECT_NAME", DEFAULT_PROJECT_NAME
        ),
        ebuilder_folder_path=_get_env_non_empty(
            "INTEGRATION_AP_INVOICE_IMPORT_EBUILDER_FOLDER",
            DEFAULT_AP_INVOICE_EBUILDER_FOLDER,
        ),
        latest_only=_get_env_bool("INTEGRATION_AP_INVOICE_IMPORT_LATEST_ONLY", default=False),
        create_folder=_get_env_bool("INTEGRATION_AP_INVOICE_IMPORT_CREATE_FOLDER", default=False),
    )


def run_req_numbers_import_with_clients(
    ebuilder_client: EBuilderClient,
    oracle_ftp_client: OracleFTPClient,
    config: OracleToEBuilderImportConfig | None = None,
) -> OracleToEBuilderImportResult:
    effective_config = config or req_numbers_import_config_from_env()
    return OracleToEBuilderImportJob(ebuilder_client, oracle_ftp_client, effective_config).run()


def run_po_numbers_import_with_clients(
    ebuilder_client: EBuilderClient,
    oracle_ftp_client: OracleFTPClient,
    config: OracleToEBuilderImportConfig | None = None,
) -> OracleToEBuilderImportResult:
    effective_config = config or po_numbers_import_config_from_env()
    return OracleToEBuilderImportJob(ebuilder_client, oracle_ftp_client, effective_config).run()


def run_ap_invoice_import_with_clients(
    ebuilder_client: EBuilderClient,
    oracle_ftp_client: OracleFTPClient,
    config: OracleToEBuilderImportConfig | None = None,
) -> OracleToEBuilderImportResult:
    effective_config = config or ap_invoice_import_config_from_env()
    return OracleToEBuilderImportJob(ebuilder_client, oracle_ftp_client, effective_config).run()


def run_req_numbers_import_job() -> OracleToEBuilderImportResult:
    config = req_numbers_import_config_from_env()
    with EBuilderClient(EBuilderConfig.from_env()) as ebuilder_client, OracleFTPClient(
        OracleFTPConfig.from_env()
    ) as oracle_ftp_client:
        return OracleToEBuilderImportJob(ebuilder_client, oracle_ftp_client, config).run()


def run_po_numbers_import_job() -> OracleToEBuilderImportResult:
    config = po_numbers_import_config_from_env()
    with EBuilderClient(EBuilderConfig.from_env()) as ebuilder_client, OracleFTPClient(
        OracleFTPConfig.from_env()
    ) as oracle_ftp_client:
        return OracleToEBuilderImportJob(ebuilder_client, oracle_ftp_client, config).run()


def run_ap_invoice_import_job() -> OracleToEBuilderImportResult:
    config = ap_invoice_import_config_from_env()
    with EBuilderClient(EBuilderConfig.from_env()) as ebuilder_client, OracleFTPClient(
        OracleFTPConfig.from_env()
    ) as oracle_ftp_client:
        return OracleToEBuilderImportJob(ebuilder_client, oracle_ftp_client, config).run()


def _get_env_non_empty(name: str, default: str) -> str:
    value = os.getenv(name, default).strip()
    if not value:
        raise ValueError(f"{name} cannot be empty")
    return value


def _get_env_bool(name: str, *, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() != "false"
