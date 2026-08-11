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
        self.queries: list[DocumentsQueryRequest] = []

    def query_documents(self, request: DocumentsQueryRequest) -> DocumentsQueryResponse:
        self.queries.append(request)
        filters = {item.field: item for item in request.filters}
        assert filters["Project/ProjectName"].value == "Lawson Integration"
        assert filters["Document/Folder"].value == "Documents/03 CO & POC Exports"
        file_name_filter = filters["Document/FileName"]
        selected_records = self.records
        if file_name_filter.operation == "LIKE":
            prefix = file_name_filter.value.rstrip("%")
            selected_records = [
                record
                for record in self.records
                if record["Document"]["FileName"].startswith(prefix)
            ]
        elif file_name_filter.operation == "=":
            selected_records = [
                record
                for record in self.records
                if record["Document"]["FileName"] == file_name_filter.value
            ]

        return DocumentsQueryResponse.model_validate(
            {
                "meta": {
                    "pageNumber": 0,
                    "pageSize": 3000,
                    "recordCount": len(selected_records),
                },
                "success": "true",
                "records": selected_records,
                "supportId": "SUPPORT",
            }
        )

    def download_file_bytes(self, download_url: str) -> bytes:
        self.downloaded_urls.append(download_url)
        return self.payloads[download_url]


class StubEBuilderDownloadUrlLimit(StubEBuilder):
    def query_documents(self, request: DocumentsQueryRequest) -> DocumentsQueryResponse:
        file_name_filter = next(
            item for item in request.filters if item.field == "Document/FileName"
        )
        if (
            file_name_filter.operation == "LIKE"
            and "Document/DownloadURL" in request.selected_fields
        ):
            raise EBuilderClientError(
                status_code=400,
                payload={
                    "errorDescription": (
                        "Too many documents found to include the Document/DownloadURL"
                    )
                },
            )
        response = super().query_documents(request)
        if "Document/DownloadURL" not in request.selected_fields:
            return DocumentsQueryResponse.model_validate(
                {
                    **response.model_dump(by_alias=True),
                    "records": [
                        {"Document": {"FileName": record["Document"]["FileName"]}}
                        for record in response.records
                    ],
                }
            )
        return response


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
    assert [query.filters[-1].value for query in ebuilder.queries[:2]] == [
        f"POC_{day_stamp}%",
        f"COP_{day_stamp}%",
    ]


def test_po_changes_job_queries_daily_prefixes_before_large_folder_limit() -> None:
    day_stamp = datetime.now().strftime("%Y%m%d")
    records = [
        _record(f"historical-{index}.csv", f"https://download.example/{index}")
        for index in range(3000)
    ]
    poc_url = "https://download.example/poc"
    cop_url = "https://download.example/cop"
    records.extend(
        [
            _record(f"POC_{day_stamp}1545.csv", poc_url),
            _record(f"COP_{day_stamp}1615.csv", cop_url),
        ]
    )
    ebuilder = StubEBuilder(
        records,
        {
            poc_url: b"Change,Value\nPOC,1\n",
            cop_url: b"Change,Value\nCOP,2\n",
        },
    )
    oracle_ftp = StubOracleFTP()

    result = POChangesExportJob(ebuilder, oracle_ftp, POChangesExportJobConfig()).run()

    assert result.uploaded_count == 1
    assert [query.filters[-1].value for query in ebuilder.queries] == [
        f"POC_{day_stamp}%",
        f"COP_{day_stamp}%",
    ]


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
    day_stamp = datetime.now().strftime("%Y%m%d")
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
