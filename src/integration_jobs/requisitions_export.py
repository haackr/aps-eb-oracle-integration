from __future__ import annotations

import os
import posixpath
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol, cast

from ebuilder_api import EBuilderClient, EBuilderConfig
from ebuilder_api.exceptions import EBuilderClientError, EBuilderRequestError
from ebuilder_api.models import DocumentsQueryFilter, DocumentsQueryRequest, DocumentsQueryResponse
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


class EBuilderQueryPort(Protocol):
    def query_documents(
        self,
        request: DocumentsQueryRequest | Mapping[str, Any],
    ) -> DocumentsQueryResponse: ...


class OracleFTPUploadPort(Protocol):
    def upload_bytes(
        self,
        file_bytes: bytes,
        remote_path: str,
    ) -> str: ...


DEFAULT_PROJECT_NAME = "Lawson Integration"
DEFAULT_REQUISITIONS_FOLDER = "Documents/01 CA & DPI Exports/Oracle"
DEFAULT_ORACLE_REMOTE_DIR = "outbound/reqs"
DEFAULT_SELECTED_FIELDS: tuple[str, ...] = (
    "Document/FileName",
    "Document/DownloadURL",
    "Document/FileId",
)
FALLBACK_SELECTED_FIELDS: tuple[str, ...] = (
    "Document/FileName",
    "Document/FileId",
)


@dataclass(frozen=True, slots=True)
class RequisitionsExportJobConfig:
    project_name: str = DEFAULT_PROJECT_NAME
    ebuilder_folder: str = DEFAULT_REQUISITIONS_FOLDER
    oracle_remote_dir: str = DEFAULT_ORACLE_REMOTE_DIR
    latest_only: bool = True

    @classmethod
    def from_env(cls) -> "RequisitionsExportJobConfig":
        project_name = os.getenv("INTEGRATION_EBUILDER_PROJECT_NAME", DEFAULT_PROJECT_NAME).strip()
        ebuilder_folder = os.getenv(
            "INTEGRATION_EBUILDER_REQUISITIONS_FOLDER", DEFAULT_REQUISITIONS_FOLDER
        ).strip()
        oracle_remote_dir = os.getenv(
            "INTEGRATION_ORACLE_REQUISITIONS_REMOTE_DIR", DEFAULT_ORACLE_REMOTE_DIR
        ).strip()
        latest_only = (
            os.getenv("INTEGRATION_REQUISITIONS_LATEST_ONLY", "true").strip().lower() != "false"
        )

        if not project_name:
            raise ValueError("INTEGRATION_EBUILDER_PROJECT_NAME cannot be empty")
        if not ebuilder_folder:
            raise ValueError("INTEGRATION_EBUILDER_REQUISITIONS_FOLDER cannot be empty")
        if not oracle_remote_dir:
            raise ValueError("INTEGRATION_ORACLE_REQUISITIONS_REMOTE_DIR cannot be empty")

        return cls(
            project_name=project_name,
            ebuilder_folder=ebuilder_folder,
            oracle_remote_dir=oracle_remote_dir,
            latest_only=latest_only,
        )


@dataclass(frozen=True, slots=True)
class RequisitionsExportResult:
    uploaded_remote_paths: list[str]

    @property
    def uploaded_count(self) -> int:
        return len(self.uploaded_remote_paths)


class RequisitionsExportJob:
    """Moves requisitions files from e-Builder Documents to Oracle FTP."""

    def __init__(
        self,
        ebuilder: EBuilderDocumentsPort,
        oracle_ftp: OracleFTPUploadPort,
        config: RequisitionsExportJobConfig,
    ) -> None:
        self._ebuilder = ebuilder
        self._oracle_ftp = oracle_ftp
        self._config = config

    def run(self) -> RequisitionsExportResult:
        response = self._find_requisition_documents()

        today_stamp = datetime.now().strftime("%Y%m%d")
        records = self._select_records(
            response.records,
            latest_only=self._config.latest_only,
            day_stamp=today_stamp,
        )
        uploaded_paths: list[str] = []

        for record in records:
            file_name = _extract_required_document_field(record, "FileName")
            download_url = self._extract_download_url(record, file_name=file_name)
            safe_name = _safe_file_name(file_name)

            payload = self._ebuilder.download_file_bytes(download_url)
            remote_target = posixpath.join(self._config.oracle_remote_dir.strip("/"), safe_name)
            uploaded = self._oracle_ftp.upload_bytes(payload, remote_target)
            uploaded_paths.append(uploaded)

        return RequisitionsExportResult(uploaded_remote_paths=uploaded_paths)

    @staticmethod
    def _select_records(
        records: Sequence[Mapping[str, Any]],
        *,
        latest_only: bool,
        day_stamp: str,
    ) -> list[Mapping[str, Any]]:
        if not records:
            return []

        daily_prefix = f"eB_Reqs_{day_stamp}"
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

    def _find_requisition_documents(self) -> DocumentsQueryResponse:
        try:
            return self._ebuilder.find_documents(
                project_name=self._config.project_name,
                folder=self._config.ebuilder_folder,
                selected_fields=list(DEFAULT_SELECTED_FIELDS),
            )
        except EBuilderClientError as exc:
            if not _is_download_url_limit_error(exc):
                raise

            # e-Builder can reject DownloadURL for large folders. Retry with metadata-only
            # fields and resolve DownloadURL per selected file.
            return self._ebuilder.find_documents(
                project_name=self._config.project_name,
                folder=self._config.ebuilder_folder,
                selected_fields=list(FALLBACK_SELECTED_FIELDS),
            )

    def _extract_download_url(self, record: Mapping[str, Any], *, file_name: str) -> str:
        download_url = _extract_optional_document_field(record, "DownloadURL")
        if download_url is not None:
            return download_url
        return self._lookup_download_url(file_name)

    def _lookup_download_url(self, file_name: str) -> str:
        if not hasattr(self._ebuilder, "query_documents"):
            raise EBuilderRequestError(
                "Unable to resolve Document/DownloadURL for requisitions export record"
            )

        request = DocumentsQueryRequest(
            selected_fields=list(DEFAULT_SELECTED_FIELDS),
            filters=[
                DocumentsQueryFilter(
                    field="Project/ProjectName",
                    operation="=",
                    value=self._config.project_name,
                ),
                DocumentsQueryFilter(
                    field="Document/Folder",
                    operation="=",
                    value=self._config.ebuilder_folder,
                ),
                DocumentsQueryFilter(
                    field="Document/FileName",
                    operation="=",
                    value=file_name,
                ),
            ],
        )

        query_client = cast(EBuilderQueryPort, self._ebuilder)
        response = query_client.query_documents(request)
        for item in response.records:
            if _extract_optional_document_field(item, "FileName") != file_name:
                continue
            resolved = _extract_optional_document_field(item, "DownloadURL")
            if resolved:
                return resolved

        raise EBuilderRequestError(
            f"Documents Query did not return Document/DownloadURL for file: {file_name}"
        )


def run_requisitions_export_job() -> RequisitionsExportResult:
    """Entrypoint for scheduler/CLI execution of requisitions export."""
    config = RequisitionsExportJobConfig.from_env()
    ebuilder_config = EBuilderConfig.from_env()
    oracle_config = OracleFTPConfig.from_env()

    with EBuilderClient(ebuilder_config) as ebuilder_client, OracleFTPClient(
        oracle_config
    ) as oracle_ftp_client:
        return RequisitionsExportJob(ebuilder_client, oracle_ftp_client, config).run()


def run_requisitions_export_with_clients(
    ebuilder_client: EBuilderClient,
    oracle_ftp_client: OracleFTPClient,
    config: RequisitionsExportJobConfig | None = None,
) -> RequisitionsExportResult:
    effective_config = config or RequisitionsExportJobConfig.from_env()
    return RequisitionsExportJob(ebuilder_client, oracle_ftp_client, effective_config).run()


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


def _is_download_url_limit_error(exc: EBuilderClientError) -> bool:
    payload = exc.payload
    if not isinstance(payload, Mapping):
        return False
    description = payload.get("errorDescription")
    if not isinstance(description, str):
        return False
    return "Too many documents found to include the Document/DownloadURL" in description
