from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timedelta
from typing import Any

import pytest

from ebuilder_api.exceptions import EBuilderClientError, EBuilderRequestError
from ebuilder_api.models import DocumentsQueryRequest, DocumentsQueryResponse
from integration_jobs.invoices_export import (
    InvoicesExportJob,
    InvoicesExportJobConfig,
    ShadowDiffResult,
    compare_oracle_invoice_csvs,
    generate_oracle_invoice_csv,
)


class StubEBuilder:
    def __init__(self, records: list[dict[str, Any]]) -> None:
        self.records = records
        self.downloaded_urls: list[str] = []
        self.queries: list[DocumentsQueryRequest] = []

    def query_documents(self, request: DocumentsQueryRequest) -> DocumentsQueryResponse:
        self.queries.append(request)
        filters = {item.field: item for item in request.filters}
        assert filters["Project/ProjectName"].value == "Lawson Integration"
        assert (
            filters["Document/Folder"].value
            == "Documents/02 Purchase Order Invoice Exports"
        )
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
        return f"payload-for:{download_url}".encode()


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
                        {
                            "Document": {
                                "FileName": record["Document"]["FileName"],
                                "FileId": record["Document"].get("FileId", "1"),
                            }
                        }
                        for record in response.records
                    ],
                }
            )
        return response


def test_invoices_job_uploads_latest_file_only_by_default() -> None:
    today = datetime.now().strftime("%Y%m%d")
    yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y%m%d")
    records = [
        {
            "Document": {
                "FileName": f"eB_Invoices_{yesterday}1700.csv",
                "DownloadURL": "https://download.example/old",
            }
        },
        {
            "Document": {
                "FileName": f"eB_Invoices_{today}1745.csv",
                "DownloadURL": "https://download.example/new",
            }
        },
    ]

    ebuilder = StubEBuilder(records)
    oracle_ftp = StubOracleFTP()
    config = InvoicesExportJobConfig()

    result = InvoicesExportJob(ebuilder, oracle_ftp, config).run()

    assert result.uploaded_count == 1
    assert result.uploaded_remote_paths == [
        f"/exports/oracle/inbound/APInvoices/eB_Invoices_{today}1745.csv"
    ]
    assert ebuilder.downloaded_urls == ["https://download.example/new"]


def test_invoices_job_can_upload_all_records() -> None:
    today = datetime.now().strftime("%Y%m%d")
    yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y%m%d")
    records = [
        {
            "Document": {
                "FileName": f"eB_Invoices_{today}1700.csv",
                "DownloadURL": "https://download.example/old",
            }
        },
        {
            "Document": {
                "FileName": f"eB_Invoices_{today}1745.csv",
                "DownloadURL": "https://download.example/new",
            }
        },
        {
            "Document": {
                "FileName": f"eB_Invoices_{yesterday}1900.csv",
                "DownloadURL": "https://download.example/yesterday",
            }
        },
    ]

    ebuilder = StubEBuilder(records)
    oracle_ftp = StubOracleFTP()
    config = InvoicesExportJobConfig(latest_only=False)

    result = InvoicesExportJob(ebuilder, oracle_ftp, config).run()

    assert result.uploaded_count == 2
    assert result.uploaded_remote_paths == [
        f"/exports/oracle/inbound/APInvoices/eB_Invoices_{today}1745.csv",
        f"/exports/oracle/inbound/APInvoices/eB_Invoices_{today}1700.csv",
    ]


def test_invoices_job_queries_daily_prefix_before_large_folder_limit() -> None:
    today = datetime.now().strftime("%Y%m%d")
    records = [
        {
            "Document": {
                "FileName": f"historical-{index}.csv",
                "DownloadURL": f"https://download.example/historical-{index}",
            }
        }
        for index in range(3000)
    ]
    records.append(
        {
            "Document": {
                "FileName": f"eB_Invoices_{today}1745.csv",
                "DownloadURL": "https://download.example/today",
            }
        }
    )

    ebuilder = StubEBuilder(records)
    oracle_ftp = StubOracleFTP()

    result = InvoicesExportJob(ebuilder, oracle_ftp, InvoicesExportJobConfig()).run()

    assert result.uploaded_count == 1
    assert ebuilder.queries[0].filters[-1].operation == "LIKE"
    assert ebuilder.queries[0].filters[-1].value == f"eB_Invoices_{today}%"


def test_invoices_job_falls_back_when_download_url_is_limited() -> None:
    today = datetime.now().strftime("%Y%m%d")
    records = [
        {
            "Document": {
                "FileName": f"eB_Invoices_{today}1745.csv",
                "DownloadURL": "https://download.example/new",
                "FileId": "1",
            }
        }
    ]

    ebuilder = StubEBuilderDownloadUrlLimit(records)
    oracle_ftp = StubOracleFTP()

    result = InvoicesExportJob(ebuilder, oracle_ftp, InvoicesExportJobConfig()).run()

    assert result.uploaded_count == 1
    assert result.uploaded_remote_paths == [
        f"/exports/oracle/inbound/APInvoices/eB_Invoices_{today}1745.csv"
    ]


def test_invoices_job_skips_when_no_files_for_today() -> None:
    yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y%m%d")
    records = [
        {
            "Document": {
                "FileName": f"eB_Invoices_{yesterday}1745.csv",
                "DownloadURL": "https://download.example/old",
            }
        }
    ]

    ebuilder = StubEBuilder(records)
    oracle_ftp = StubOracleFTP()

    result = InvoicesExportJob(ebuilder, oracle_ftp, InvoicesExportJobConfig()).run()

    assert result.uploaded_count == 0
    assert result.uploaded_remote_paths == []
    assert ebuilder.downloaded_urls == []
    assert oracle_ftp.upload_calls == []


def test_invoices_job_returns_empty_when_no_records() -> None:
    ebuilder = StubEBuilder([])
    oracle_ftp = StubOracleFTP()

    result = InvoicesExportJob(ebuilder, oracle_ftp, InvoicesExportJobConfig()).run()

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
def test_invoices_job_validates_required_document_fields(
    bad_record: Mapping[str, Any],
) -> None:
    today = datetime.now().strftime("%Y%m%d")
    candidate = f"eB_Invoices_{today}1745.csv"
    record = dict(bad_record)
    document = dict(record.get("Document", {}))
    if document.get("FileName") == "__TODAY__":
        document["FileName"] = candidate
    record["Document"] = document

    ebuilder = StubEBuilder([record])
    oracle_ftp = StubOracleFTP()

    with pytest.raises(EBuilderRequestError):
        InvoicesExportJob(ebuilder, oracle_ftp, InvoicesExportJobConfig()).run()


def test_generate_oracle_invoice_csv_matches_oracle_format() -> None:
    from integration_jobs.invoices_export import (
        ORACLE_CSV_FIELDNAMES,
        generate_oracle_invoice_csv,
    )
    import csv
    from io import StringIO

    records = [
        {
            "CommitmentInvoice": {
                "InvoiceNumber": "INV-001",
                "Description": "Test Invoice 1",
                "CustomFields": {"Invoice Date": "10.22.2026"},
            },
            "Commitment": {"CommitmentNumber": "P26001"},
            "Company": {
                "CompanyName": "Vendor One",
                "CompanyNumber": "1001",
                "CustomFields": {"Oracle Vendor Name": "VENDOR ONE INC", "Oracle Pay Supplier Site": "Main"},
            },
            "ProcessInstance": {
                "InstanceCounter": "101",
                "DataFields": {
                    "Supplier Site": "Main",
                    "Oracle Attachment": "https://drive.google.com/test1",
                },
            },
            "Project": {"CustomFields": {"Project ID": "500"}},
            "LineItems": [
                {
                    "CommitmentItem": {"ItemNumber": "S-001", "Description": "Services"},
                    "CommitmentInvoiceItem": {"Amount": "1008.0000"},
                }
            ],
        },
        {
            "CommitmentInvoice": {
                "InvoiceNumber": "INV-002",
                "Description": "Credit Memo",
                "CustomFields": {"Invoice Date": "11/05/2026 12:00:00 AM"},
            },
            "Commitment": {"CommitmentNumber": "P26002"},
            "Company": {
                "CompanyName": "Vendor Two",
                "CompanyNumber": "1002",
                "CustomFields": {},
            },
            "ProcessInstance": {
                "InstanceCounter": "102",
                "DataFields": {},
            },
            "Project": {"CustomFields": {"Project ID": "501"}},
            "LineItems": [
                {
                    "CommitmentItem": {"ItemNumber": "2", "Description": "Credit Item"},
                    "CommitmentInvoiceItem": {"Amount": "-250.5000"},
                }
            ],
        },
    ]

    csv_output = generate_oracle_invoice_csv(records)
    reader = csv.DictReader(StringIO(csv_output))
    rows = list(reader)

    assert reader.fieldnames == list(ORACLE_CSV_FIELDNAMES)
    assert len(rows) == 2

    # Verify standard invoice row
    r1 = rows[0]
    assert r1["Invoice Number"] == "INV-001"
    assert r1["Vendor Name"] == "VENDOR ONE INC"
    assert r1["Vendor Number"] == "1001"
    assert r1["Invoice Type"] == "STANDARD"
    assert r1["Line Number"] == "1"
    assert r1["Line Amount"] == "1008"  # Trailing .00 stripped
    assert r1["PO Number"] == "P26001"
    assert r1["PO Line"] == "1"
    assert r1["FlexField1"] == "500"
    assert r1["FlexField2"] == "101"
    assert r1["Attachment File"] == "https://drive.google.com/test1"
    assert r1["Invoice Date"] == "10/22/2026"

    # Verify credit memo row
    r2 = rows[1]
    assert r2["Invoice Number"] == "INV-002"
    assert r2["Invoice Type"] == "CREDIT"
    assert r2["Line Amount"] == "-250.5"
    assert r2["Invoice Date"] == "11/05/2026"


class StubEBuilderProcess:
    def __init__(self, records: list[dict[str, Any]]) -> None:
        self.records = records
        self.queried_filters: list[Any] = []
        self.imported_data: list[dict[str, Any]] = []

    def query_all_process_records(
        self,
        process_prefix: str,
        request: Any,
        *,
        page_size: int = 1000,
        endpoint: str | None = None,
    ) -> list[dict[str, Any]]:
        self.queried_filters.append(request.get("filters", []))
        return self.records

    def import_process_records(
        self,
        *,
        process_prefix: str,
        data: list[dict[str, Any]],
        project_identifier: str = "ProjectName",
        import_mode: str = "Update",
        create_as_draft: bool = False,
        abort_on_exception: bool = False,
        endpoint: str | None = None,
    ) -> Any:
        self.imported_data.extend(data)
        return {"success": True}


def test_invoices_job_api_export_mode_queries_uploads_and_updates_status() -> None:
    records = [
        {
            "CommitmentInvoice": {
                "InvoiceNumber": "INV-100",
                "Description": "Ready Invoice",
                "CustomFields": {"Invoice Date": "11/09/2026"},
            },
            "Commitment": {"CommitmentNumber": "P26100"},
            "Company": {
                "CompanyName": "ACME Supplies",
                "CompanyNumber": "9999",
                "CustomFields": {"Oracle Vendor Name": "ACME SUPPLIES INC"},
            },
            "ProcessInstance": {
                "InstanceCounter": "500",
                "DataFields": {"Supplier Site": "Main", "Oracle Attachment": "https://file"},
            },
            "Project": {"CustomFields": {"Project ID": "123"}},
            "LineItems": [
                {
                    "CommitmentItem": {"ItemNumber": "S-001", "Description": "Item 1"},
                    "CommitmentInvoiceItem": {"Amount": "500.0000"},
                }
            ],
        }
    ]

    ebuilder = StubEBuilderProcess(records)
    oracle_ftp = StubOracleFTP()
    config = InvoicesExportJobConfig(export_source="api")

    result = InvoicesExportJob(ebuilder, oracle_ftp, config).run()  # type: ignore[arg-type]

    assert result.uploaded_count == 1
    assert result.exported_count == 1
    assert "/exports/oracle/inbound/APInvoices/eB_Invoices_" in result.uploaded_remote_paths[0]

    # Verify upload was called with CSV bytes
    assert len(oracle_ftp.upload_calls) == 1
    uploaded_bytes, remote_path = oracle_ftp.upload_calls[0]
    assert b"\"INV-100\"" in uploaded_bytes
    assert b"\"ACME SUPPLIES INC\"" in uploaded_bytes

    # Verify e-Builder status was updated
    assert len(ebuilder.imported_data) == 1
    update_header = ebuilder.imported_data[0]["header"]
    assert update_header["step"] == "Lawson Holding Step"
    assert update_header["action"] == "Processed"
    assert update_header["processDataFields"]["Integration Status"] == "Complete"


def test_invoices_job_api_export_mode_handles_zero_records_cleanly() -> None:
    ebuilder = StubEBuilderProcess([])
    oracle_ftp = StubOracleFTP()
    config = InvoicesExportJobConfig(export_source="api")

    result = InvoicesExportJob(ebuilder, oracle_ftp, config).run()  # type: ignore[arg-type]

    assert result.uploaded_count == 0
    assert result.exported_count == 0
    assert oracle_ftp.upload_calls == []
    assert ebuilder.imported_data == []


def test_invoices_job_api_export_mode_dry_run_skips_ftp_upload() -> None:
    records = [
        {
            "CommitmentInvoice": {
                "InvoiceNumber": "INV-TEST",
                "Description": "Dry run invoice",
                "CustomFields": {"Invoice Date": "11/09/2026"},
            },
            "Commitment": {"CommitmentNumber": "P26999"},
            "Company": {"CompanyName": "Test Vendor", "CompanyNumber": "100"},
            "ProcessInstance": {
                "InstanceCounter": "101",
                "DataFields": {},
            },
            "Project": {"ProjectName": "**Test Project**"},
            "LineItems": [],
        }
    ]
    ebuilder = StubEBuilderProcess(records)
    oracle_ftp = StubOracleFTP()
    config = InvoicesExportJobConfig(export_source="api", dry_run=True)

    result = InvoicesExportJob(ebuilder, oracle_ftp, config).run()  # type: ignore[arg-type]

    assert result.uploaded_count == 1
    assert "[DRY-RUN]" in result.uploaded_remote_paths[0]
    assert oracle_ftp.upload_calls == []  # Zero SFTP calls made!
    assert len(ebuilder.imported_data) == 1
    assert ebuilder.imported_data[0]["header"]["projectIdentifier"] == "**Test Project**"
    assert ebuilder.imported_data[0]["header"]["processCounter"] == 101
    assert ebuilder.imported_data[0]["header"]["processDataFields"]["Integration Status"] == "Complete"


def test_compare_oracle_invoice_csvs_exact_match() -> None:
    csv_data = (
        '"Invoice Number","Description","Vendor Name","Vendor Number","Supplier Site",'
        '"Invoice Type","Line Number","Line Type","Line Description","Quantity",'
        '"Line Amount","PO Number","PO Line","FlexField1","FlexField2","Attachment File","Invoice Date"\r\n'
        '"INV-001","Services","Vendor A","123","Main","STANDARD","1","ITEM","Desc 1","","100.5","PO1","1","FF1","10","file1.pdf","11/09/2026"\r\n'
    )
    diff = compare_oracle_invoice_csvs(csv_data, csv_data)
    assert diff.is_match is True
    assert diff.match_rate_pct == 100.0
    assert diff.total_reference_rows == 1
    assert diff.total_candidate_rows == 1
    assert diff.matched_rows == 1
    assert diff.total_fields == 17
    assert diff.matched_fields == 17
    assert diff.differences == []


def test_compare_oracle_invoice_csvs_field_mismatch() -> None:
    ref_csv = (
        '"Invoice Number","Description","Vendor Name","Vendor Number","Supplier Site",'
        '"Invoice Type","Line Number","Line Type","Line Description","Quantity",'
        '"Line Amount","PO Number","PO Line","FlexField1","FlexField2","Attachment File","Invoice Date"\r\n'
        '"INV-001","Services","Vendor A","123","Main","STANDARD","1","ITEM","Desc 1","","100.5","PO1","1","FF1","10","file1.pdf","11/09/2026"\r\n'
    )
    cand_csv = (
        '"Invoice Number","Description","Vendor Name","Vendor Number","Supplier Site",'
        '"Invoice Type","Line Number","Line Type","Line Description","Quantity",'
        '"Line Amount","PO Number","PO Line","FlexField1","FlexField2","Attachment File","Invoice Date"\r\n'
        '"INV-001","Services","Vendor B","123","Main","STANDARD","1","ITEM","Desc 1","","200.0","PO1","1","FF1","10","file1.pdf","11/09/2026"\r\n'
    )
    diff = compare_oracle_invoice_csvs(ref_csv, cand_csv)
    assert diff.is_match is False
    assert diff.matched_rows == 0
    assert diff.total_fields == 17
    assert diff.matched_fields == 15  # 15 matched, 2 mismatches: Vendor Name, Line Amount
    assert len(diff.differences) == 2
    assert any("Vendor Name" in d for d in diff.differences)
    assert any("Line Amount" in d for d in diff.differences)


def test_compare_oracle_invoice_csvs_missing_and_extra_rows() -> None:
    ref_csv = (
        '"Invoice Number","Description","Vendor Name","Vendor Number","Supplier Site",'
        '"Invoice Type","Line Number","Line Type","Line Description","Quantity",'
        '"Line Amount","PO Number","PO Line","FlexField1","FlexField2","Attachment File","Invoice Date"\r\n'
        '"INV-001","Services","Vendor A","123","Main","STANDARD","1","ITEM","Desc 1","","100.5","PO1","1","FF1","10","file1.pdf","11/09/2026"\r\n'
    )
    cand_csv = (
        '"Invoice Number","Description","Vendor Name","Vendor Number","Supplier Site",'
        '"Invoice Type","Line Number","Line Type","Line Description","Quantity",'
        '"Line Amount","PO Number","PO Line","FlexField1","FlexField2","Attachment File","Invoice Date"\r\n'
        '"INV-002","Other","Vendor B","456","Main","STANDARD","1","ITEM","Desc 2","","50.0","PO2","1","FF2","20","file2.pdf","11/09/2026"\r\n'
    )
    diff = compare_oracle_invoice_csvs(ref_csv, cand_csv)
    assert diff.is_match is False
    assert diff.total_reference_rows == 1
    assert diff.total_candidate_rows == 1
    assert len(diff.differences) == 2
    assert any("missing in generated CSV" in d for d in diff.differences)
    assert any("unexpectedly present" in d for d in diff.differences)


def test_compare_oracle_invoice_csvs_empty_csvs() -> None:
    diff = compare_oracle_invoice_csvs("", "")
    assert diff.is_match is True
    assert diff.match_rate_pct == 100.0


class StubEBuilderShadow(StubEBuilder):
    def __init__(
        self,
        doc_records: list[dict[str, Any]],
        doc_bytes: bytes,
        process_records: list[dict[str, Any]],
    ) -> None:
        super().__init__(doc_records)
        self._doc_bytes = doc_bytes
        self._process_records = process_records
        self.queried_filters: list[Any] = []

    def download_file_bytes(self, download_url: str) -> bytes:
        return self._doc_bytes

    def query_all_process_records(
        self,
        process_prefix: str,
        request: Any,
        *,
        page_size: int = 1000,
        endpoint: str | None = None,
    ) -> list[dict[str, Any]]:
        self.queried_filters.append(request.get("filters", []))
        return self._process_records


def test_invoices_job_shadow_mode_matches_and_uploads() -> None:
    today_stamp = datetime.now().strftime("%Y%m%d")
    today_date = datetime.now().strftime("%m/%d/%Y")
    file_name = f"eB_Invoices_{today_stamp}1200.csv"

    api_records = [
        {
            "CommitmentInvoice": {
                "InvoiceNumber": "INV-100",
                "Description": "Matching Invoice",
                "CustomFields": {"Invoice Date": today_date},
            },
            "Commitment": {"CommitmentNumber": "P26100"},
            "Company": {
                "CompanyName": "ACME Supplies",
                "CompanyNumber": "9999",
                "CustomFields": {"Oracle Vendor Name": "ACME SUPPLIES INC"},
            },
            "ProcessInstance": {
                "InstanceCounter": "500",
                "DataFields": {
                    "Supplier Site": "Main",
                    "Oracle Attachment": "https://file",
                    "Date Exported to Oracle": today_date,
                },
            },
            "Project": {"CustomFields": {"Project ID": "123"}},
            "LineItems": [
                {
                    "CommitmentItem": {"ItemNumber": "S-001", "Description": "Item 1"},
                    "CommitmentInvoiceItem": {"Amount": "500.0000"},
                }
            ],
        }
    ]

    matching_csv_bytes = generate_oracle_invoice_csv(api_records).encode("utf-8")

    doc_records = [
        {
            "Document": {
                "FileName": file_name,
                "DownloadURL": f"https://files/{file_name}",
                "FileId": "1",
            }
        }
    ]

    ebuilder = StubEBuilderShadow(doc_records, matching_csv_bytes, api_records)
    oracle_ftp = StubOracleFTP()
    config = InvoicesExportJobConfig(export_source="shadow")

    result = InvoicesExportJob(ebuilder, oracle_ftp, config).run()  # type: ignore[arg-type]

    # Live document upload proceeded to Oracle SFTP
    assert result.uploaded_count == 1
    assert result.uploaded_remote_paths == [f"/exports/oracle/inbound/APInvoices/{file_name}"]
    assert len(oracle_ftp.upload_calls) == 1

    # Shadow validation succeeded with 100% match
    assert result.shadow_matched is True
    assert result.shadow_diff is not None
    assert result.shadow_diff.is_match is True
    assert result.shadow_diff.match_rate_pct == 100.0


def test_invoices_job_shadow_mode_records_diff_without_interrupting_upload() -> None:
    today_stamp = datetime.now().strftime("%Y%m%d")
    today_date = datetime.now().strftime("%m/%d/%Y")
    file_name = f"eB_Invoices_{today_stamp}1200.csv"

    # Reference CSV downloaded from Document folder has Vendor B
    ref_csv = (
        '"Invoice Number","Description","Vendor Name","Vendor Number","Supplier Site",'
        '"Invoice Type","Line Number","Line Type","Line Description","Quantity",'
        '"Line Amount","PO Number","PO Line","FlexField1","FlexField2","Attachment File","Invoice Date"\r\n'
        f'"INV-100","Desc","Vendor B","9999","Main","STANDARD","1","ITEM","Item 1","","500","P26100","1","123","500","","{today_date}"\r\n'
    )

    # API query returns Vendor A
    api_records = [
        {
            "CommitmentInvoice": {
                "InvoiceNumber": "INV-100",
                "Description": "Desc",
                "CustomFields": {"Invoice Date": today_date},
            },
            "Commitment": {"CommitmentNumber": "P26100"},
            "Company": {
                "CompanyName": "Vendor A",
                "CompanyNumber": "9999",
            },
            "ProcessInstance": {
                "InstanceCounter": "500",
                "DataFields": {
                    "Supplier Site": "Main",
                    "Date Exported to Oracle": today_date,
                },
            },
            "Project": {"CustomFields": {"Project ID": "123"}},
            "LineItems": [
                {
                    "CommitmentItem": {"ItemNumber": "1", "Description": "Item 1"},
                    "CommitmentInvoiceItem": {"Amount": "500"},
                }
            ],
        }
    ]

    doc_records = [
        {
            "Document": {
                "FileName": file_name,
                "DownloadURL": f"https://files/{file_name}",
                "FileId": "1",
            }
        }
    ]

    ebuilder = StubEBuilderShadow(doc_records, ref_csv.encode("utf-8"), api_records)
    oracle_ftp = StubOracleFTP()
    config = InvoicesExportJobConfig(export_source="shadow")

    result = InvoicesExportJob(ebuilder, oracle_ftp, config).run()  # type: ignore[arg-type]

    # Live document upload STILL PROCEEDED (zero production impact)
    assert result.uploaded_count == 1
    assert len(oracle_ftp.upload_calls) == 1

    # Shadow check correctly flagged the mismatch
    assert result.shadow_matched is False
    assert result.shadow_diff is not None
    assert result.shadow_diff.is_match is False
    assert len(result.shadow_diff.differences) > 0
    assert any("Vendor Name" in d for d in result.shadow_diff.differences)


def test_invoices_job_shadow_mode_handles_no_documents_cleanly() -> None:
    ebuilder = StubEBuilderShadow([], b"", [])
    oracle_ftp = StubOracleFTP()
    config = InvoicesExportJobConfig(export_source="shadow")

    result = InvoicesExportJob(ebuilder, oracle_ftp, config).run()  # type: ignore[arg-type]

    assert result.uploaded_count == 0
    assert result.exported_count == 0
    assert result.shadow_matched is None


def test_invoices_job_shadow_mode_handles_api_exception_gracefully() -> None:
    today_stamp = datetime.now().strftime("%Y%m%d")
    file_name = f"eB_Invoices_{today_stamp}1200.csv"
    doc_records = [
        {
            "Document": {
                "FileName": file_name,
                "DownloadURL": f"https://files/{file_name}",
                "FileId": "1",
            }
        }
    ]

    class FailingAPIStub(StubEBuilder):
        def query_all_process_records(self, *args: Any, **kwargs: Any) -> Any:
            raise RuntimeError("API Timeout")

    ebuilder = FailingAPIStub(doc_records)
    oracle_ftp = StubOracleFTP()
    config = InvoicesExportJobConfig(export_source="shadow")

    result = InvoicesExportJob(ebuilder, oracle_ftp, config).run()  # type: ignore[arg-type]

    # Document upload still succeeds despite API failure
    assert result.uploaded_count == 1
    assert result.shadow_matched is False


