from __future__ import annotations

import csv
import os
import posixpath
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from io import StringIO
from typing import Any

from ebuilder_api import EBuilderClient, EBuilderConfig
from ebuilder_api.exceptions import EBuilderRequestError
from oracle_ftp import OracleFTPClient, OracleFTPConfig

from ._export_common import (
    DocumentQueryService,
    EBuilderDocumentsPort,
    OracleFTPUploadPort,
    extract_optional_document_field,
    extract_required_document_field,
)

DEFAULT_PROJECT_NAME = "Lawson Integration"
DEFAULT_PO_CHANGES_FOLDER = "Documents/03 CO & POC Exports"
DEFAULT_ORACLE_REMOTE_DIR = "inbound/POChangeOrder"
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
        documents = DocumentQueryService(
            self._ebuilder,
            project_name=self._config.project_name,
            folder=self._config.ebuilder_folder,
        )
        records = self._select_records(
            self._find_po_change_documents(documents, now.strftime("%Y%m%d")),
            day_stamp=now.strftime("%Y%m%d"),
        )
        if not records:
            return POChangesExportResult(uploaded_remote_paths=[], source_file_names=[])

        payloads: list[tuple[str, bytes]] = []
        for record in records:
            file_name = extract_required_document_field(record, "FileName")
            download_url = documents.download_url(record, file_name=file_name)
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
            file_name = extract_optional_document_field(record, "FileName")
            if file_name is None:
                continue
            timestamp = _po_change_timestamp(file_name)
            if timestamp is not None and timestamp.startswith(day_stamp):
                selected.append((timestamp, record))

        return [record for _, record in sorted(selected, key=lambda item: item[0])]

    @staticmethod
    def _find_po_change_documents(
        documents: DocumentQueryService, day_stamp: str
    ) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        for prefix in ("POC", "COP"):
            records.extend(documents.find_documents(f"{prefix}_{day_stamp}%"))
        return records


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
    return output.getvalue().encode("utf-8")
