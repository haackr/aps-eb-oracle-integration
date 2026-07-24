from __future__ import annotations

import os
import posixpath
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol

from ebuilder_api import EBuilderClient, EBuilderConfig
from ebuilder_api.exceptions import EBuilderRequestError
from ebuilder_api.models import DocumentsQueryResponse
from oracle_ftp import OracleFTPClient, OracleFTPConfig


class EBuilderDocumentsPort(Protocol):
    def find_documents(
        self,
        *,
        project_name: str,
        folder: str,
        selected_fields: list[str] | None = None,
    ) -> DocumentsQueryResponse: ...

    def download_file_bytes(self, download_url: str) -> bytes: ...


class OracleFTPUploadPort(Protocol):
    def upload_bytes(
        self,
        file_bytes: bytes,
        remote_path: str,
    ) -> str: ...


DEFAULT_PROJECT_NAME = "Lawson Integration"
DEFAULT_INVOICES_FOLDER = "Documents/02 Purchase Order Invoice Exports/"
DEFAULT_ORACLE_REMOTE_DIR = "inbound/APInvoices"
DEFAULT_SELECTED_FIELDS: tuple[str, ...] = (
    "Document/FileName",
    "Document/DownloadURL",
    "Document/FileId",
)


@dataclass(frozen=True, slots=True)
class InvoicesExportJobConfig:
    project_name: str = DEFAULT_PROJECT_NAME
    ebuilder_folder: str = DEFAULT_INVOICES_FOLDER
    oracle_remote_dir: str = DEFAULT_ORACLE_REMOTE_DIR
    latest_only: bool = True

    @classmethod
    def from_env(cls) -> "InvoicesExportJobConfig":
        project_name = os.getenv("INTEGRATION_EBUILDER_PROJECT_NAME", DEFAULT_PROJECT_NAME).strip()
        ebuilder_folder = os.getenv(
            "INTEGRATION_EBUILDER_INVOICES_FOLDER", DEFAULT_INVOICES_FOLDER
        ).strip()
        oracle_remote_dir = os.getenv(
            "INTEGRATION_ORACLE_INVOICES_REMOTE_DIR", DEFAULT_ORACLE_REMOTE_DIR
        ).strip()
        latest_only = os.getenv("INTEGRATION_INVOICES_LATEST_ONLY", "true").strip().lower() != "false"

        if not project_name:
            raise ValueError("INTEGRATION_EBUILDER_PROJECT_NAME cannot be empty")
        if not ebuilder_folder:
            raise ValueError("INTEGRATION_EBUILDER_INVOICES_FOLDER cannot be empty")
        if not oracle_remote_dir:
            raise ValueError("INTEGRATION_ORACLE_INVOICES_REMOTE_DIR cannot be empty")

        return cls(
            project_name=project_name,
            ebuilder_folder=ebuilder_folder,
            oracle_remote_dir=oracle_remote_dir,
            latest_only=latest_only,
        )


@dataclass(frozen=True, slots=True)
class InvoicesExportResult:
    uploaded_remote_paths: list[str]

    @property
    def uploaded_count(self) -> int:
        return len(self.uploaded_remote_paths)


class InvoicesExportJob:
    """Moves invoices files from e-Builder Documents to Oracle FTP."""

    def __init__(
        self,
        ebuilder: EBuilderDocumentsPort,
        oracle_ftp: OracleFTPUploadPort,
        config: InvoicesExportJobConfig,
    ) -> None:
        self._ebuilder = ebuilder
        self._oracle_ftp = oracle_ftp
        self._config = config

    def run(self) -> InvoicesExportResult:
        response = self._ebuilder.find_documents(
            project_name=self._config.project_name,
            folder=self._config.ebuilder_folder,
            selected_fields=list(DEFAULT_SELECTED_FIELDS),
        )

        today_stamp = datetime.now().strftime("%Y%m%d")
        records = self._select_records(
            response.records,
            latest_only=self._config.latest_only,
            day_stamp=today_stamp,
        )
        uploaded_paths: list[str] = []

        for record in records:
            file_name = _extract_required_document_field(record, "FileName")
            download_url = _extract_required_document_field(record, "DownloadURL")
            safe_name = _safe_file_name(file_name)

            payload = self._ebuilder.download_file_bytes(download_url)
            remote_target = posixpath.join(self._config.oracle_remote_dir.strip("/"), safe_name)
            uploaded = self._oracle_ftp.upload_bytes(payload, remote_target)
            uploaded_paths.append(uploaded)

        return InvoicesExportResult(uploaded_remote_paths=uploaded_paths)

    @staticmethod
    def _select_records(
        records: Sequence[Mapping[str, Any]],
        *,
        latest_only: bool,
        day_stamp: str,
    ) -> list[Mapping[str, Any]]:
        if not records:
            return []

        daily_prefix = f"eB_Invoices_{day_stamp}"
        todays_records = [
            record
            for record in records
            if (_extract_optional_document_field(record, "FileName") or "").startswith(daily_prefix)
        ]
        if not todays_records:
            return []

        ordered = sorted(
            todays_records,
            key=lambda record: _extract_optional_document_field(record, "FileName") or "",
            reverse=True,
        )
        if latest_only:
            return [ordered[0]]
        return ordered


def run_invoices_export_job() -> InvoicesExportResult:
    """Entrypoint for scheduler/CLI execution of invoices export."""
    config = InvoicesExportJobConfig.from_env()
    ebuilder_config = EBuilderConfig.from_env()
    oracle_config = OracleFTPConfig.from_env()

    with EBuilderClient(ebuilder_config) as ebuilder_client, OracleFTPClient(
        oracle_config
    ) as oracle_ftp_client:
        return InvoicesExportJob(ebuilder_client, oracle_ftp_client, config).run()


def run_invoices_export_with_clients(
    ebuilder_client: EBuilderClient,
    oracle_ftp_client: OracleFTPClient,
    config: InvoicesExportJobConfig | None = None,
) -> InvoicesExportResult:
    effective_config = config or InvoicesExportJobConfig.from_env()
    return InvoicesExportJob(ebuilder_client, oracle_ftp_client, effective_config).run()


def _extract_optional_document_field(record: Mapping[str, Any], field_name: str) -> str | None:
    document_section = record.get("Document")
    if not isinstance(document_section, Mapping):
        return None
    value = document_section.get(field_name)
    if isinstance(value, str) and value.strip():
        return value
    return None


def _extract_required_document_field(record: Mapping[str, Any], field_name: str) -> str:
    value = _extract_optional_document_field(record, field_name)
    if value is None:
        raise EBuilderRequestError(f"Documents Query record is missing Document/{field_name}")
    return value


def _safe_file_name(file_name: str) -> str:
    candidate = Path(file_name.strip()).name
    if not candidate or candidate in {".", ".."}:
        raise EBuilderRequestError("Documents Query returned invalid Document/FileName")
    return candidate
