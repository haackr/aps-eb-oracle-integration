from __future__ import annotations

import posixpath
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, Protocol

from ebuilder_api.exceptions import EBuilderClientError, EBuilderRequestError
from ebuilder_api.models import DocumentsQueryRequest, DocumentsQueryResponse

DEFAULT_SELECTED_FIELDS: tuple[str, ...] = (
    "Document/FileName",
    "Document/DownloadURL",
    "Document/FileId",
)
FALLBACK_SELECTED_FIELDS: tuple[str, ...] = (
    "Document/FileName",
    "Document/FileId",
)


class EBuilderDocumentsPort(Protocol):
    def query_documents(
        self,
        request: DocumentsQueryRequest | Mapping[str, Any],
    ) -> DocumentsQueryResponse: ...

    def download_file_bytes(self, download_url: str) -> bytes: ...


class OracleFTPUploadPort(Protocol):
    def upload_bytes(
        self,
        file_bytes: bytes,
        remote_path: str,
    ) -> str: ...


class DocumentQueryService:
    """Queries an e-Builder folder and handles its DownloadURL result limit."""

    def __init__(
        self,
        ebuilder: EBuilderDocumentsPort,
        *,
        project_name: str,
        folder: str,
    ) -> None:
        self._ebuilder = ebuilder
        self._project_name = project_name
        self._folder = folder

    def find_documents(self, file_name_pattern: str) -> list[dict[str, Any]]:
        try:
            response = self._query_documents(
                file_name_pattern=file_name_pattern,
                operation="LIKE",
                selected_fields=DEFAULT_SELECTED_FIELDS,
            )
        except EBuilderClientError as exc:
            if not _is_download_url_limit_error(exc):
                raise
            response = self._query_documents(
                file_name_pattern=file_name_pattern,
                operation="LIKE",
                selected_fields=FALLBACK_SELECTED_FIELDS,
            )
        return response.records

    def download_url(self, record: Mapping[str, Any], *, file_name: str) -> str:
        download_url = extract_optional_document_field(record, "DownloadURL")
        if download_url is not None:
            return download_url

        response = self._query_documents(
            file_name_pattern=file_name,
            operation="=",
            selected_fields=DEFAULT_SELECTED_FIELDS,
        )
        for item in response.records:
            if extract_optional_document_field(item, "FileName") != file_name:
                continue
            resolved = extract_optional_document_field(item, "DownloadURL")
            if resolved is not None:
                return resolved

        raise EBuilderRequestError(
            f"Documents Query did not return Document/DownloadURL for file: {file_name}"
        )

    def _query_documents(
        self,
        *,
        file_name_pattern: str,
        operation: str,
        selected_fields: tuple[str, ...],
    ) -> DocumentsQueryResponse:
        return self._ebuilder.query_documents(
            DocumentsQueryRequest.model_validate(
                {
                    "selectedFields": list(selected_fields),
                    "Filters": [
                        {
                            "Field": "Project/ProjectName",
                            "Operation": "=",
                            "Value": self._project_name,
                        },
                        {
                            "Field": "Document/Folder",
                            "Operation": "=",
                            "Value": self._folder,
                        },
                        {
                            "Field": "Document/FileName",
                            "Operation": operation,
                            "Value": file_name_pattern,
                        },
                    ],
                }
            )
        )


def export_daily_documents(
    documents: DocumentQueryService,
    ebuilder: EBuilderDocumentsPort,
    oracle_ftp: OracleFTPUploadPort,
    *,
    remote_dir: str,
    daily_prefix: str,
    latest_only: bool,
    dry_run: bool = False,
    on_document_downloaded: Callable[[str, bytes], None] | None = None,
    transform_payload: Callable[[str, bytes], bytes] | None = None,
) -> list[str]:
    records = [
        record
        for record in documents.find_documents(f"{daily_prefix}%")
        if (extract_optional_document_field(record, "FileName") or "").startswith(
            daily_prefix
        )
    ]
    records.sort(
        key=lambda record: extract_optional_document_field(record, "FileName") or "",
        reverse=True,
    )
    if latest_only:
        records = records[:1]

    uploaded_paths: list[str] = []
    for record in records:
        file_name = extract_required_document_field(record, "FileName")
        payload = ebuilder.download_file_bytes(
            documents.download_url(record, file_name=file_name)
        )
        if on_document_downloaded is not None:
            on_document_downloaded(file_name, payload)
        if transform_payload is not None:
            payload = transform_payload(file_name, payload)
        remote_path = posixpath.join(remote_dir.strip("/"), safe_file_name(file_name))
        if dry_run:
            uploaded_paths.append(f"[DRY-RUN] {remote_path}")
        else:
            uploaded_paths.append(oracle_ftp.upload_bytes(payload, remote_path))
    return uploaded_paths


def extract_optional_document_field(
    record: Mapping[str, Any], field_name: str
) -> str | None:
    document_section = record.get("Document")
    if not isinstance(document_section, Mapping):
        return None
    value = document_section.get(field_name)
    if isinstance(value, str) and value.strip():
        return value
    return None


def extract_required_document_field(record: Mapping[str, Any], field_name: str) -> str:
    value = extract_optional_document_field(record, field_name)
    if value is None:
        raise EBuilderRequestError(
            f"Documents Query record is missing Document/{field_name}"
        )
    return value


def safe_file_name(file_name: str) -> str:
    candidate = Path(file_name.strip()).name
    if not candidate or candidate in {".", ".."}:
        raise EBuilderRequestError("Documents Query returned invalid Document/FileName")
    return candidate


def _is_download_url_limit_error(exc: EBuilderClientError) -> bool:
    payload = exc.payload
    if not isinstance(payload, Mapping):
        return False
    description = payload.get("errorDescription")
    return isinstance(description, str) and (
        "Too many documents found to include the Document/DownloadURL" in description
    )
