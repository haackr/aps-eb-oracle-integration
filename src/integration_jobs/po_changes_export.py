from __future__ import annotations

import csv
import os
import posixpath
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from io import StringIO
from typing import Any, Protocol

from ebuilder_api import EBuilderClient, EBuilderConfig
from ebuilder_api.exceptions import EBuilderClientError, EBuilderRequestError
from ebuilder_api.models import DocumentsQueryRequest, DocumentsQueryResponse
from oracle_ftp import OracleFTPClient, OracleFTPConfig


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


DEFAULT_PROJECT_NAME = "Lawson Integration"
DEFAULT_PO_CHANGES_FOLDER = "Documents/03 CO & POC Exports"
DEFAULT_ORACLE_REMOTE_DIR = "inbound/POChangeOrder"
DEFAULT_SELECTED_FIELDS: tuple[str, ...] = (
    "Document/FileName",
    "Document/DownloadURL",
    "Document/FileId",
)
FALLBACK_SELECTED_FIELDS: tuple[str, ...] = (
    "Document/FileName",
    "Document/FileId",
)
PO_CHANGE_FILE_NAME_PATTERN = re.compile(r"^(?:POC|COP)_(?P<timestamp>\d{12})\.csv$", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class POChangesExportJobConfig:
    project_name: str = DEFAULT_PROJECT_NAME
    ebuilder_folder: str = DEFAULT_PO_CHANGES_FOLDER
    oracle_remote_dir: str = DEFAULT_ORACLE_REMOTE_DIR

    @classmethod
    def from_env(cls) -> POChangesExportJobConfig:
        project_name = os.getenv("INTEGRATION_EBUILDER_PROJECT_NAME", DEFAULT_PROJECT_NAME).strip()
        ebuilder_folder = os.getenv(
            "INTEGRATION_EBUILDER_PO_CHANGES_FOLDER", DEFAULT_PO_CHANGES_FOLDER
        ).strip()
        oracle_remote_dir = os.getenv(
            "INTEGRATION_ORACLE_PO_CHANGES_REMOTE_DIR", DEFAULT_ORACLE_REMOTE_DIR
        ).strip()

        if not project_name:
            raise ValueError("INTEGRATION_EBUILDER_PROJECT_NAME cannot be empty")
        if not ebuilder_folder:
            raise ValueError("INTEGRATION_EBUILDER_PO_CHANGES_FOLDER cannot be empty")
        if not oracle_remote_dir:
            raise ValueError("INTEGRATION_ORACLE_PO_CHANGES_REMOTE_DIR cannot be empty")

        return cls(
            project_name=project_name,
            ebuilder_folder=ebuilder_folder,
            oracle_remote_dir=oracle_remote_dir,
        )


@dataclass(frozen=True, slots=True)
class POChangesExportResult:
    uploaded_remote_paths: list[str]
    source_file_names: list[str]

    @property
    def uploaded_count(self) -> int:
        return len(self.uploaded_remote_paths)


class POChangesExportJob:
    """Merges daily PO-change exports from e-Builder and uploads them to Oracle FTP."""

    def __init__(
        self,
        ebuilder: EBuilderDocumentsPort,
        oracle_ftp: OracleFTPUploadPort,
        config: POChangesExportJobConfig,
    ) -> None:
        self._ebuilder = ebuilder
        self._oracle_ftp = oracle_ftp
        self._config = config

    def run(self) -> POChangesExportResult:
        now = datetime.now()
        records = self._select_records(
            self._find_po_change_documents(now.strftime("%Y%m%d")),
            day_stamp=now.strftime("%Y%m%d"),
        )
        if not records:
            return POChangesExportResult(uploaded_remote_paths=[], source_file_names=[])

        payloads: list[tuple[str, bytes]] = []
        for record in records:
            file_name = _extract_required_document_field(record, "FileName")
            download_url = self._extract_download_url(record, file_name=file_name)
            payloads.append((file_name, self._ebuilder.download_file_bytes(download_url)))

        merged_payload = _merge_csv_payloads(payloads)
        merged_file_name = f"eB_Change_{now.strftime('%Y%m%d%H%M')}.csv"
        remote_target = posixpath.join(self._config.oracle_remote_dir.strip("/"), merged_file_name)
        uploaded = self._oracle_ftp.upload_bytes(merged_payload, remote_target)

        return POChangesExportResult(
            uploaded_remote_paths=[uploaded],
            source_file_names=[file_name for file_name, _ in payloads],
        )

    @staticmethod
    def _select_records(
        records: Sequence[Mapping[str, Any]],
        *,
        day_stamp: str,
    ) -> list[Mapping[str, Any]]:
        selected: list[tuple[str, Mapping[str, Any]]] = []
        for record in records:
            file_name = _extract_optional_document_field(record, "FileName")
            if file_name is None:
                continue
            timestamp = _po_change_timestamp(file_name)
            if timestamp is not None and timestamp.startswith(day_stamp):
                selected.append((timestamp, record))

        return [record for _, record in sorted(selected, key=lambda item: item[0])]

    def _find_po_change_documents(self, day_stamp: str) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        for prefix in ("POC", "COP"):
            try:
                response = self._query_po_change_documents(
                    prefix=prefix,
                    day_stamp=day_stamp,
                    selected_fields=DEFAULT_SELECTED_FIELDS,
                )
            except EBuilderClientError as exc:
                if not _is_download_url_limit_error(exc):
                    raise
                response = self._query_po_change_documents(
                    prefix=prefix,
                    day_stamp=day_stamp,
                    selected_fields=FALLBACK_SELECTED_FIELDS,
                )
            records.extend(response.records)
        return records

    def _query_po_change_documents(
        self,
        *,
        prefix: str,
        day_stamp: str,
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
                            "Value": self._config.project_name,
                        },
                        {
                            "Field": "Document/Folder",
                            "Operation": "=",
                            "Value": self._config.ebuilder_folder,
                        },
                        {
                            "Field": "Document/FileName",
                            "Operation": "LIKE",
                            "Value": f"{prefix}_{day_stamp}%",
                        },
                    ],
                }
            )
        )

    def _extract_download_url(self, record: Mapping[str, Any], *, file_name: str) -> str:
        download_url = _extract_optional_document_field(record, "DownloadURL")
        if download_url is not None:
            return download_url
        return self._lookup_download_url(file_name)

    def _lookup_download_url(self, file_name: str) -> str:
        request = DocumentsQueryRequest.model_validate(
            {
                "selectedFields": list(DEFAULT_SELECTED_FIELDS),
                "Filters": [
                    {
                        "Field": "Project/ProjectName",
                        "Operation": "=",
                        "Value": self._config.project_name,
                    },
                    {
                        "Field": "Document/Folder",
                        "Operation": "=",
                        "Value": self._config.ebuilder_folder,
                    },
                    {
                        "Field": "Document/FileName",
                        "Operation": "=",
                        "Value": file_name,
                    },
                ],
            }
        )

        response = self._ebuilder.query_documents(request)
        for item in response.records:
            if _extract_optional_document_field(item, "FileName") != file_name:
                continue
            resolved = _extract_optional_document_field(item, "DownloadURL")
            if resolved:
                return resolved

        raise EBuilderRequestError(
            f"Documents Query did not return Document/DownloadURL for file: {file_name}"
        )


def run_po_changes_export_job() -> POChangesExportResult:
    """Entrypoint for scheduler/CLI execution of the PO-change export."""
    config = POChangesExportJobConfig.from_env()
    with EBuilderClient(EBuilderConfig.from_env()) as ebuilder_client, OracleFTPClient(
        OracleFTPConfig.from_env()
    ) as oracle_ftp_client:
        return POChangesExportJob(ebuilder_client, oracle_ftp_client, config).run()


def run_po_changes_export_with_clients(
    ebuilder_client: EBuilderClient,
    oracle_ftp_client: OracleFTPClient,
    config: POChangesExportJobConfig | None = None,
) -> POChangesExportResult:
    effective_config = config or POChangesExportJobConfig.from_env()
    return POChangesExportJob(ebuilder_client, oracle_ftp_client, effective_config).run()


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


def _po_change_timestamp(file_name: str) -> str | None:
    match = PO_CHANGE_FILE_NAME_PATTERN.fullmatch(file_name)
    if match is None:
        return None

    timestamp = match.group("timestamp")
    try:
        datetime.strptime(timestamp, "%Y%m%d%H%M")
    except ValueError:
        return None
    return timestamp


def _merge_csv_payloads(payloads: Sequence[tuple[str, bytes]]) -> bytes:
    output = StringIO(newline="")
    csv_writer = csv.writer(output, lineterminator="\n")
    expected_header: list[str] | None = None

    for file_name, payload in payloads:
        try:
            rows = csv.reader(StringIO(payload.decode("utf-8-sig"), newline=""))
            header = next(rows)
        except UnicodeDecodeError as exc:
            raise EBuilderRequestError(f"PO-change CSV is not UTF-8 encoded: {file_name}") from exc
        except StopIteration as exc:
            raise EBuilderRequestError(f"PO-change CSV is empty: {file_name}") from exc

        if expected_header is None:
            expected_header = header
            csv_writer.writerow(header)
        elif header != expected_header:
            raise EBuilderRequestError(f"PO-change CSV has different headers: {file_name}")

        csv_writer.writerows(rows)

    return output.getvalue().encode("utf-8")


def _is_download_url_limit_error(exc: EBuilderClientError) -> bool:
    payload = exc.payload
    if not isinstance(payload, Mapping):
        return False
    description = payload.get("errorDescription")
    if not isinstance(description, str):
        return False
    return "Too many documents found to include the Document/DownloadURL" in description
