from __future__ import annotations

import csv
import os
from dataclasses import dataclass
from datetime import datetime
from io import StringIO

from ebuilder_api import EBuilderClient, EBuilderConfig
from oracle_ftp import OracleFTPClient, OracleFTPConfig

from ._export_common import (
    DocumentQueryService,
    EBuilderDocumentsPort,
    OracleFTPUploadPort,
    export_daily_documents,
)

DEFAULT_PROJECT_NAME = "Lawson Integration"
DEFAULT_REQUISITIONS_FOLDER = "Documents/01 CA & DPI Exports/Oracle"
DEFAULT_ORACLE_REMOTE_DIR = "inbound/Requisitions"
DEFAULT_LINE_NUMBER_PADDING = 4


def pad_requisition_line_numbers(
    payload: bytes, padding_width: int = DEFAULT_LINE_NUMBER_PADDING
) -> bytes:
    """Ensure Line Number column in requisitions CSV is zero-padded.

    Preserves existing sequential file order while neutralizing text-sort issues in Oracle.
    """
    if padding_width <= 0:
        return payload

    try:
        text = payload.decode("utf-8-sig")
    except UnicodeDecodeError:
        return payload

    input_stream = StringIO(text, newline="")
    reader = csv.reader(input_stream)
    try:
        header = next(reader)
    except StopIteration:
        return payload

    line_num_idx = -1
    for idx, col in enumerate(header):
        if col.strip().lower() == "line number":
            line_num_idx = idx
            break

    if line_num_idx == -1:
        return payload

    output_stream = StringIO(newline="")
    writer = csv.writer(output_stream, quoting=csv.QUOTE_ALL, lineterminator="\r\n")
    writer.writerow(header)

    for row in reader:
        if line_num_idx < len(row):
            raw_val = row[line_num_idx].strip()
            if raw_val.isdigit():
                row[line_num_idx] = f"{int(raw_val):0{padding_width}d}"
        writer.writerow(row)

    return output_stream.getvalue().encode("utf-8")


@dataclass(frozen=True, slots=True)
class RequisitionsExportJobConfig:
    project_name: str = DEFAULT_PROJECT_NAME
    ebuilder_folder: str = DEFAULT_REQUISITIONS_FOLDER
    oracle_remote_dir: str = DEFAULT_ORACLE_REMOTE_DIR
    latest_only: bool = True
    line_number_padding: int = DEFAULT_LINE_NUMBER_PADDING

    @classmethod
    def from_env(cls) -> RequisitionsExportJobConfig:
        project_name = os.getenv(
            "INTEGRATION_EBUILDER_PROJECT_NAME", DEFAULT_PROJECT_NAME
        ).strip()
        ebuilder_folder = os.getenv(
            "INTEGRATION_EBUILDER_REQUISITIONS_FOLDER", DEFAULT_REQUISITIONS_FOLDER
        ).strip()
        oracle_remote_dir = os.getenv(
            "INTEGRATION_ORACLE_REQUISITIONS_REMOTE_DIR", DEFAULT_ORACLE_REMOTE_DIR
        ).strip()
        latest_only = (
            os.getenv("INTEGRATION_REQUISITIONS_LATEST_ONLY", "true").strip().lower()
            != "false"
        )
        raw_padding = os.getenv(
            "INTEGRATION_REQUISITIONS_LINE_NUMBER_PADDING",
            str(DEFAULT_LINE_NUMBER_PADDING),
        ).strip()
        try:
            line_number_padding = int(raw_padding)
        except ValueError:
            line_number_padding = DEFAULT_LINE_NUMBER_PADDING

        if not project_name:
            raise ValueError("INTEGRATION_EBUILDER_PROJECT_NAME cannot be empty")
        if not ebuilder_folder:
            raise ValueError("INTEGRATION_EBUILDER_REQUISITIONS_FOLDER cannot be empty")
        if not oracle_remote_dir:
            raise ValueError(
                "INTEGRATION_ORACLE_REQUISITIONS_REMOTE_DIR cannot be empty"
            )

        return cls(
            project_name=project_name,
            ebuilder_folder=ebuilder_folder,
            oracle_remote_dir=oracle_remote_dir,
            latest_only=latest_only,
            line_number_padding=line_number_padding,
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
        today_stamp = datetime.now().strftime("%Y%m%d")
        uploaded_paths = export_daily_documents(
            DocumentQueryService(
                self._ebuilder,
                project_name=self._config.project_name,
                folder=self._config.ebuilder_folder,
            ),
            self._ebuilder,
            self._oracle_ftp,
            remote_dir=self._config.oracle_remote_dir,
            daily_prefix=f"eB_Reqs_{today_stamp}",
            latest_only=self._config.latest_only,
            transform_payload=lambda _name, payload: pad_requisition_line_numbers(
                payload, padding_width=self._config.line_number_padding
            ),
        )
        return RequisitionsExportResult(uploaded_remote_paths=uploaded_paths)


def run_requisitions_export_job() -> RequisitionsExportResult:
    """Entrypoint for scheduler/CLI execution of requisitions export."""
    config = RequisitionsExportJobConfig.from_env()
    ebuilder_config = EBuilderConfig.from_env()
    oracle_config = OracleFTPConfig.from_env()

    with (
        EBuilderClient(ebuilder_config) as ebuilder_client,
        OracleFTPClient(oracle_config) as oracle_ftp_client,
    ):
        return RequisitionsExportJob(ebuilder_client, oracle_ftp_client, config).run()


def run_requisitions_export_with_clients(
    ebuilder_client: EBuilderClient,
    oracle_ftp_client: OracleFTPClient,
    config: RequisitionsExportJobConfig | None = None,
) -> RequisitionsExportResult:
    effective_config = config or RequisitionsExportJobConfig.from_env()
    return RequisitionsExportJob(
        ebuilder_client, oracle_ftp_client, effective_config
    ).run()
