from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timedelta
from typing import Any

import pytest

from ebuilder_api.exceptions import EBuilderRequestError
from ebuilder_api.models import DocumentsQueryResponse
from integration_jobs.requisitions_export import (
    RequisitionsExportJob,
    RequisitionsExportJobConfig,
)


class StubEBuilder:
    def __init__(self, records: list[dict[str, Any]]) -> None:
        self.records = records
        self.downloaded_urls: list[str] = []

    def find_documents(
        self,
        *,
        project_name: str,
        folder: str,
        selected_fields: list[str] | None = None,
    ) -> DocumentsQueryResponse:
        assert project_name == "Lawson Integration"
        assert folder == "Documents/01 CA & DPI Exports/Oracle"
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
        return f"payload-for:{download_url}".encode("utf-8")


class StubOracleFTP:
    def __init__(self) -> None:
        self.upload_calls: list[tuple[bytes, str]] = []

    def upload_bytes(
        self,
        file_bytes: bytes,
        remote_path: str,
    ) -> str:
        self.upload_calls.append((file_bytes, remote_path))
        return f"/exports/oracle/{remote_path.strip('/')}"


def test_requisitions_job_uploads_latest_file_only_by_default() -> None:
    today = datetime.now().strftime("%Y%m%d")
    yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y%m%d")
    records = [
        {
            "Document": {
                "FileName": f"eB_Reqs_{yesterday}1700.csv",
                "DownloadURL": "https://download.example/old",
            }
        },
        {
            "Document": {
                "FileName": f"eB_Reqs_{today}1745.csv",
                "DownloadURL": "https://download.example/new",
            }
        },
    ]

    ebuilder = StubEBuilder(records)
    oracle_ftp = StubOracleFTP()
    config = RequisitionsExportJobConfig()

    result = RequisitionsExportJob(ebuilder, oracle_ftp, config).run()

    assert result.uploaded_count == 1
    assert result.uploaded_remote_paths == [f"/exports/oracle/outbound/reqs/eB_Reqs_{today}1745.csv"]
    assert ebuilder.downloaded_urls == ["https://download.example/new"]


def test_requisitions_job_can_upload_all_records() -> None:
    today = datetime.now().strftime("%Y%m%d")
    yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y%m%d")
    records = [
        {
            "Document": {
                "FileName": f"eB_Reqs_{today}1700.csv",
                "DownloadURL": "https://download.example/old",
            }
        },
        {
            "Document": {
                "FileName": f"eB_Reqs_{today}1745.csv",
                "DownloadURL": "https://download.example/new",
            }
        },
        {
            "Document": {
                "FileName": f"eB_Reqs_{yesterday}1900.csv",
                "DownloadURL": "https://download.example/yesterday",
            }
        },
    ]

    ebuilder = StubEBuilder(records)
    oracle_ftp = StubOracleFTP()
    config = RequisitionsExportJobConfig(latest_only=False)

    result = RequisitionsExportJob(ebuilder, oracle_ftp, config).run()

    assert result.uploaded_count == 2
    assert result.uploaded_remote_paths == [
        f"/exports/oracle/outbound/reqs/eB_Reqs_{today}1745.csv",
        f"/exports/oracle/outbound/reqs/eB_Reqs_{today}1700.csv",
    ]


def test_requisitions_job_skips_when_no_files_for_today() -> None:
    yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y%m%d")
    records = [
        {
            "Document": {
                "FileName": f"eB_Reqs_{yesterday}1745.csv",
                "DownloadURL": "https://download.example/old",
            }
        }
    ]

    ebuilder = StubEBuilder(records)
    oracle_ftp = StubOracleFTP()

    result = RequisitionsExportJob(ebuilder, oracle_ftp, RequisitionsExportJobConfig()).run()

    assert result.uploaded_count == 0
    assert result.uploaded_remote_paths == []
    assert ebuilder.downloaded_urls == []
    assert oracle_ftp.upload_calls == []


def test_requisitions_job_returns_empty_when_no_records() -> None:
    ebuilder = StubEBuilder([])
    oracle_ftp = StubOracleFTP()

    result = RequisitionsExportJob(ebuilder, oracle_ftp, RequisitionsExportJobConfig()).run()

    assert result.uploaded_count == 0
    assert result.uploaded_remote_paths == []
    assert oracle_ftp.upload_calls == []


@pytest.mark.parametrize(
    "bad_record",
    [
        {"Document": {"FileName": "__TODAY__", "DownloadURL": ""}},
        {"Document": {"FileName": "__TODAY__"}},
    ],
)
def test_requisitions_job_validates_required_document_fields(
    bad_record: Mapping[str, Any],
) -> None:
    today = datetime.now().strftime("%Y%m%d")
    candidate = f"eB_Reqs_{today}1745.csv"
    record = dict(bad_record)
    document = dict(record.get("Document", {}))
    if document.get("FileName") == "__TODAY__":
        document["FileName"] = candidate
    record["Document"] = document

    ebuilder = StubEBuilder([record])
    oracle_ftp = StubOracleFTP()

    with pytest.raises(EBuilderRequestError):
        RequisitionsExportJob(ebuilder, oracle_ftp, RequisitionsExportJobConfig()).run()
