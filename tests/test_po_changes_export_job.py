from __future__ import annotations

import re
from datetime import datetime
from typing import Any

import pytest

from ebuilder_api.exceptions import EBuilderClientError, EBuilderRequestError
from ebuilder_api.models import DocumentsQueryRequest, DocumentsQueryResponse
from integration_jobs.po_changes_export import POChangesExportJob, POChangesExportJobConfig


class StubEBuilder:
    def __init__(self, records: list[dict[str, Any]], payloads: dict[str, bytes]) -> None:
        self.records = records
        self.payloads = payloads
        self.downloaded_urls: list[str] = []

    def find_documents(
        self,
        *,
        project_name: str,
        folder: str,
        selected_fields: list[str] | None = None,
    ) -> DocumentsQueryResponse:
        assert project_name == "Lawson Integration"
        assert folder == "Documents/03 CO & POC Exports"
        assert selected_fields is not None
        return DocumentsQueryResponse.model_validate(
            {
                "meta": {
                    "pageNumber": 0,
                    "pageSize": 3000,
                    "recordCount": len(self.records),
                },
                "success": "true",
                "records": self.records,
                "supportId": "SUPPORT",
            }
        )

    def download_file_bytes(self, download_url: str) -> bytes:
        self.downloaded_urls.append(download_url)
        return self.payloads[download_url]


class StubEBuilderDownloadUrlLimit(StubEBuilder):
    def find_documents(
        self,
        *,
        project_name: str,
        folder: str,
        selected_fields: list[str] | None = None,
    ) -> DocumentsQueryResponse:
        assert selected_fields is not None
        if "Document/DownloadURL" in selected_fields:
            raise EBuilderClientError(
                status_code=400,
                payload={
                    "errorDescription": (
                        "Too many documents found to include the Document/DownloadURL"
                    )
                },
            )
        return DocumentsQueryResponse.model_validate(
            {
                "meta": {
                    "pageNumber": 0,
                    "pageSize": 3000,
                    "recordCount": len(self.records),
                },
                "success": "true",
                "records": [
                    {"Document": {"FileName": record["Document"]["FileName"]}}
                    for record in self.records
                ],
                "supportId": "SUPPORT",
            }
        )

    def query_documents(self, request: DocumentsQueryRequest) -> DocumentsQueryResponse:
        file_name = next(
            item.value for item in request.filters if item.field == "Document/FileName"
        )
        return DocumentsQueryResponse.model_validate(
            {
                "meta": {
                    "pageNumber": 0,
                    "pageSize": 3000,
                    "recordCount": 1,
                },
                "success": "true",
                "records": [
                    {
                        "Document": {
                            "FileName": file_name,
                            "DownloadURL": f"https://download.example/{file_name}",
                        }
                    }
                ],
                "supportId": "SUPPORT",
            }
        )


class StubOracleFTP:
    def __init__(self) -> None:
        self.upload_calls: list[tuple[bytes, str]] = []

    def upload_bytes(self, file_bytes: bytes, remote_path: str) -> str:
        self.upload_calls.append((file_bytes, remote_path))
        return f"/exports/oracle/{remote_path.strip('/')}"


def _record(file_name: str, download_url: str) -> dict[str, Any]:
    return {"Document": {"FileName": file_name, "DownloadURL": download_url}}


def test_po_changes_job_merges_todays_csvs_in_timestamp_order() -> None:
    day_stamp = datetime.now().strftime("%Y%m%d")
    ebuilder = StubEBuilder(
        [
            _record(f"COP_{day_stamp}1615.csv", "https://download.example/cop"),
            _record(f"POC_{day_stamp}1545.csv", "https://download.example/poc"),
            _record("POC_202001011545.csv", "https://download.example/yesterday"),
            _record(f"POC_{day_stamp}1700.txt", "https://download.example/text"),
        ],
        {
            "https://download.example/cop": b"Change,Value\r\nCOP,2\r\n",
            "https://download.example/poc": b"Change,Value\r\nPOC,1\r\n",
        },
    )
    oracle_ftp = StubOracleFTP()

    result = POChangesExportJob(ebuilder, oracle_ftp, POChangesExportJobConfig()).run()

    assert result.uploaded_count == 1
    assert result.source_file_names == [
        f"POC_{day_stamp}1545.csv",
        f"COP_{day_stamp}1615.csv",
    ]
    assert ebuilder.downloaded_urls == [
        "https://download.example/poc",
        "https://download.example/cop",
    ]
    assert oracle_ftp.upload_calls[0][0] == b"Change,Value\nPOC,1\nCOP,2\n"
    assert re.fullmatch(
        r"inbound/POChangeOrder/eB_Change_\d{12}\.csv",
        oracle_ftp.upload_calls[0][1],
    )


def test_po_changes_job_skips_invalid_or_non_current_file_names() -> None:
    ebuilder = StubEBuilder(
        [
            _record("POC_202602291200.csv", "https://download.example/invalid-date"),
            _record("other.csv", "https://download.example/other"),
        ],
        {},
    )
    oracle_ftp = StubOracleFTP()

    result = POChangesExportJob(ebuilder, oracle_ftp, POChangesExportJobConfig()).run()

    assert result.uploaded_count == 0
    assert result.source_file_names == []
    assert oracle_ftp.upload_calls == []


def test_po_changes_job_rejects_mismatched_headers_before_upload() -> None:
    day_stamp = "20260809"
    ebuilder = StubEBuilder(
        [
            _record(f"POC_{day_stamp}1545.csv", "https://download.example/poc"),
            _record(f"COP_{day_stamp}1615.csv", "https://download.example/cop"),
        ],
        {
            "https://download.example/poc": b"Change,Value\nPOC,1\n",
            "https://download.example/cop": b"Different,Header\nCOP,2\n",
        },
    )
    oracle_ftp = StubOracleFTP()

    with pytest.raises(EBuilderRequestError, match="different headers"):
        POChangesExportJob(ebuilder, oracle_ftp, POChangesExportJobConfig()).run()

    assert oracle_ftp.upload_calls == []


def test_po_changes_job_resolves_download_urls_when_folder_query_is_limited() -> None:
    today_stamp = datetime.now().strftime("%Y%m%d")
    file_name = f"POC_{today_stamp}1545.csv"
    download_url = f"https://download.example/{file_name}"
    ebuilder = StubEBuilderDownloadUrlLimit(
        [_record(file_name, download_url)],
        {download_url: b"Change,Value\nPOC,1\n"},
    )
    oracle_ftp = StubOracleFTP()

    result = POChangesExportJob(ebuilder, oracle_ftp, POChangesExportJobConfig()).run()

    assert result.uploaded_count == 1
    assert ebuilder.downloaded_urls == [download_url]
