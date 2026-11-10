from __future__ import annotations

import csv
import logging
import os
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from io import StringIO
from typing import Any, Protocol

from ebuilder_api import EBuilderClient, EBuilderConfig
from ebuilder_api.models import ProcessImportResponse, ProcessQueryRequest
from oracle_ftp import OracleFTPClient, OracleFTPConfig

from ._export_common import (
    DocumentQueryService,
    EBuilderDocumentsPort,
    OracleFTPUploadPort,
    export_daily_documents,
)

logger = logging.getLogger(__name__)

DEFAULT_PROJECT_NAME = "Lawson Integration"
DEFAULT_INVOICES_FOLDER = "Documents/02 Purchase Order Invoice Exports"
DEFAULT_ORACLE_REMOTE_DIR = "inbound/APInvoices"
DEFAULT_PROCESS_PREFIX = "PINV"
DEFAULT_HOLDING_STEP = "Lawson Holding Step"
DEFAULT_ACTION_NAME = "Processed"

PINV_QUERY_FIELDS: tuple[str, ...] = (
    "ProcessInstance/InstanceCounter",
    "CommitmentInvoice/InvoiceNumber",
    "CommitmentInvoice/Description",
    "CommitmentInvoice/CustomFields/Invoice Date",
    "Company/CompanyName",
    "Company/CompanyNumber",
    "Company/CustomFields/Oracle Vendor Name",
    "Company/CustomFields/Oracle Pay Supplier Site",
    "Commitment/CommitmentNumber",
    "Project/CustomFields/Project ID",
    "ProcessInstance/DataFields/Oracle Attachment",
    "ProcessInstance/DataFields/Supplier Site",
    "ProcessInstance/DataFields/Date Exported to Oracle",
    "LineItems/CommitmentItem/ItemNumber",
    "LineItems/CommitmentItem/Description",
    "LineItems/CommitmentInvoiceItem/Amount",
    "LineItems/CommitmentInvoiceItem/Quantity",
)

ORACLE_CSV_FIELDNAMES: tuple[str, ...] = (
    "Invoice Number",
    "Description",
    "Vendor Name",
    "Vendor Number",
    "Supplier Site",
    "Invoice Type",
    "Line Number",
    "Line Type",
    "Line Description",
    "Quantity",
    "Line Amount",
    "PO Number",
    "PO Line",
    "FlexField1",
    "FlexField2",
    "Attachment File",
    "Invoice Date",
)


class EBuilderProcessPort(Protocol):
    def query_all_process_records(
        self,
        process_prefix: str,
        request: ProcessQueryRequest | dict[str, Any],
        *,
        page_size: int = 1000,
        endpoint: str | None = None,
    ) -> list[dict[str, Any]]: ...

    def import_process_records(
        self,
        *,
        process_prefix: str,
        data: list[dict[str, Any]],
        project_identifier: str = "ProjectName",
        import_mode: str = "Entity",
        create_as_draft: bool = False,
        abort_on_exception: bool = False,
        endpoint: str | None = None,
    ) -> ProcessImportResponse: ...


def format_oracle_date(raw_val: str | None) -> str:
    """Format date strings into MM/DD/YYYY format expected by Oracle."""
    if not raw_val:
        return ""
    cleaned = raw_val.strip()
    match = re.match(r"^(\d{1,2})[./-](\d{1,2})[./-](\d{4})", cleaned)
    if match:
        month, day, year = match.groups()
        return f"{int(month):02d}/{int(day):02d}/{year}"
    return cleaned


def format_oracle_amount(raw_val: str | float | int | None) -> str:
    """Format amounts without unnecessary trailing zeros, matching e-Builder export reports."""
    if raw_val is None or raw_val == "":
        return ""
    try:
        val = float(raw_val)
        formatted = f"{val:.2f}"
        if "." in formatted:
            formatted = formatted.rstrip("0").rstrip(".")
        return formatted
    except (ValueError, TypeError):
        return str(raw_val)


def extract_po_line_number(item_number: str | None, default_line: int) -> str:
    """Extract numeric line index from item identifiers such as 'S-001' or '1'."""
    if not item_number:
        return str(default_line)
    match = re.search(r"(\d+)", item_number)
    if match:
        return str(int(match.group(1)))
    return str(default_line)


def generate_oracle_invoice_rows(records: list[dict[str, Any]]) -> list[dict[str, str]]:
    """Transform e-Builder PINV process query records into Oracle AP invoice rows."""
    rows: list[dict[str, str]] = []

    for record in records:
        invoice = record.get("CommitmentInvoice") or {}
        commitment = record.get("Commitment") or {}
        company = record.get("Company") or {}
        instance = record.get("ProcessInstance") or {}
        project = record.get("Project") or {}

        data_fields = instance.get("DataFields") or {}
        invoice_custom_fields = invoice.get("CustomFields") or {}
        company_custom_fields = company.get("CustomFields") or {}
        project_custom_fields = project.get("CustomFields") or {}
        line_items = record.get("LineItems") or []

        # Sort line items by Commitment Item Number
        sorted_lines = sorted(
            line_items,
            key=lambda li: (li.get("CommitmentItem") or {}).get("ItemNumber") or "",
        )

        vendor_name = (
            company_custom_fields.get("Oracle Vendor Name")
            or company.get("CompanyName")
            or ""
        )
        supplier_site = (
            data_fields.get("Supplier Site")
            or company_custom_fields.get("Oracle Pay Supplier Site")
            or "Main"
        )

        total_amount = sum(
            float((li.get("CommitmentInvoiceItem") or {}).get("Amount") or 0)
            for li in sorted_lines
        )
        invoice_type = "CREDIT" if total_amount < 0 else "STANDARD"

        for idx, line in enumerate(sorted_lines, start=1):
            commitment_item = line.get("CommitmentItem") or {}
            invoice_item = line.get("CommitmentInvoiceItem") or {}

            row = {
                "Invoice Number": str(invoice.get("InvoiceNumber") or ""),
                "Description": str(invoice.get("Description") or ""),
                "Vendor Name": str(vendor_name),
                "Vendor Number": str(company.get("CompanyNumber") or ""),
                "Supplier Site": str(supplier_site),
                "Invoice Type": invoice_type,
                "Line Number": str(idx),
                "Line Type": "ITEM",
                "Line Description": str(commitment_item.get("Description") or ""),
                "Quantity": "",
                "Line Amount": format_oracle_amount(invoice_item.get("Amount")),
                "PO Number": str(commitment.get("CommitmentNumber") or ""),
                "PO Line": extract_po_line_number(commitment_item.get("ItemNumber"), idx),
                "FlexField1": str(project_custom_fields.get("Project ID") or ""),
                "FlexField2": str(instance.get("InstanceCounter") or ""),
                "Attachment File": str(data_fields.get("Oracle Attachment") or ""),
                "Invoice Date": format_oracle_date(invoice_custom_fields.get("Invoice Date")),
            }
            rows.append(row)

    return rows


def generate_oracle_invoice_csv(records: list[dict[str, Any]]) -> str:
    """Generate RFC 4180 CSV string with Oracle AP Invoice columns."""
    rows = generate_oracle_invoice_rows(records)
    output = StringIO()
    writer = csv.DictWriter(
        output,
        fieldnames=list(ORACLE_CSV_FIELDNAMES),
        quoting=csv.QUOTE_ALL,
        lineterminator="\r\n",
    )
    writer.writeheader()
    writer.writerows(rows)
    return output.getvalue()


@dataclass(frozen=True, slots=True)
class ShadowDiffResult:
    total_reference_rows: int
    total_candidate_rows: int
    matched_rows: int
    total_fields: int
    matched_fields: int
    differences: list[str]

    @property
    def is_match(self) -> bool:
        return (
            self.total_reference_rows == self.total_candidate_rows
            and len(self.differences) == 0
            and self.total_fields == self.matched_fields
        )

    @property
    def match_rate_pct(self) -> float:
        if self.total_fields == 0:
            return (
                100.0
                if self.total_reference_rows == 0 and self.total_candidate_rows == 0
                else 0.0
            )
        return (self.matched_fields / self.total_fields) * 100.0


def compare_oracle_invoice_csvs(
    reference_csv: str,
    candidate_csv: str,
) -> ShadowDiffResult:
    """Perform field-by-field diff between reference and candidate Oracle AP invoice CSVs."""
    ref_lines = [
        line for line in reference_csv.lstrip("\ufeff").splitlines() if line.strip()
    ]
    cand_lines = [
        line for line in candidate_csv.lstrip("\ufeff").splitlines() if line.strip()
    ]

    ref_reader = csv.DictReader(ref_lines)
    cand_reader = csv.DictReader(cand_lines)

    ref_rows = list(ref_reader)
    cand_rows = list(cand_reader)

    def _index_rows(
        rows: list[dict[str, str]],
    ) -> dict[tuple[str, str, int], dict[str, str]]:
        indexed: dict[tuple[str, str, int], dict[str, str]] = {}
        counts: dict[tuple[str, str], int] = {}
        for r in rows:
            base_key = (
                (r.get("Invoice Number") or "").strip(),
                (r.get("Line Number") or "").strip(),
            )
            counts[base_key] = counts.get(base_key, 0) + 1
            indexed[(base_key[0], base_key[1], counts[base_key])] = r
        return indexed

    ref_by_key = _index_rows(ref_rows)
    cand_by_key = _index_rows(cand_rows)

    all_keys = set(ref_by_key.keys()) | set(cand_by_key.keys())
    differences: list[str] = []
    matched_rows = 0
    total_fields = 0
    matched_fields = 0

    for key in sorted(all_keys):
        inv_no, line_no, occ = key
        key_desc = f"(Invoice: {inv_no}, Line: {line_no}" + (
            f", #{occ})" if occ > 1 else ")"
        )

        if key not in cand_by_key:
            differences.append(f"{key_desc}: missing in generated CSV")
            total_fields += len(ORACLE_CSV_FIELDNAMES)
        elif key not in ref_by_key:
            differences.append(
                f"{key_desc}: unexpectedly present in generated CSV (not in reference)"
            )
            total_fields += len(ORACLE_CSV_FIELDNAMES)
        else:
            ref_row = ref_by_key[key]
            cand_row = cand_by_key[key]
            row_has_diff = False
            for field in ORACLE_CSV_FIELDNAMES:
                total_fields += 1
                r_val = (ref_row.get(field) or "").strip()
                c_val = (cand_row.get(field) or "").strip()
                if r_val == c_val:
                    matched_fields += 1
                else:
                    row_has_diff = True
                    differences.append(
                        f"{key_desc} field '{field}': generated {c_val!r} != reference {r_val!r}"
                    )
            if not row_has_diff:
                matched_rows += 1

    return ShadowDiffResult(
        total_reference_rows=len(ref_rows),
        total_candidate_rows=len(cand_rows),
        matched_rows=matched_rows,
        total_fields=total_fields,
        matched_fields=matched_fields,
        differences=differences,
    )


@dataclass(frozen=True, slots=True)
class InvoicesExportJobConfig:
    project_name: str = DEFAULT_PROJECT_NAME
    ebuilder_folder: str = DEFAULT_INVOICES_FOLDER
    oracle_remote_dir: str = DEFAULT_ORACLE_REMOTE_DIR
    latest_only: bool = True
    export_source: str = "documents"  # "documents", "api", or "shadow"
    holding_step: str = DEFAULT_HOLDING_STEP
    action_name: str = DEFAULT_ACTION_NAME
    dry_run: bool = False
    shadow_filter_field: str = "ProcessInstance/DataFields/Date Exported to Oracle"
    shadow_filter_value: str = ""

    @classmethod
    def from_env(cls) -> InvoicesExportJobConfig:
        project_name = os.getenv(
            "INTEGRATION_EBUILDER_PROJECT_NAME", DEFAULT_PROJECT_NAME
        ).strip()
        ebuilder_folder = os.getenv(
            "INTEGRATION_EBUILDER_INVOICES_FOLDER", DEFAULT_INVOICES_FOLDER
        ).strip()
        oracle_remote_dir = os.getenv(
            "INTEGRATION_ORACLE_INVOICES_REMOTE_DIR", DEFAULT_ORACLE_REMOTE_DIR
        ).strip()
        latest_only = (
            os.getenv("INTEGRATION_INVOICES_LATEST_ONLY", "true").strip().lower()
            != "false"
        )
        export_source = os.getenv(
            "INTEGRATION_INVOICES_EXPORT_SOURCE", "documents"
        ).strip().lower()
        holding_step = os.getenv(
            "INTEGRATION_INVOICES_HOLDING_STEP", DEFAULT_HOLDING_STEP
        ).strip()
        action_name = os.getenv(
            "INTEGRATION_INVOICES_ACTION_NAME", DEFAULT_ACTION_NAME
        ).strip()
        dry_run = (
            os.getenv("INTEGRATION_INVOICES_DRY_RUN", "").strip().lower()
            in ("true", "1", "yes")
            or os.getenv("INTEGRATION_DRY_RUN", "").strip().lower()
            in ("true", "1", "yes")
        )
        shadow_filter_field = os.getenv(
            "INTEGRATION_INVOICES_SHADOW_FILTER_FIELD",
            "ProcessInstance/DataFields/Date Exported to Oracle",
        ).strip()
        shadow_filter_value = os.getenv(
            "INTEGRATION_INVOICES_SHADOW_FILTER_VALUE",
            "",
        ).strip()

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
            export_source=export_source,
            holding_step=holding_step,
            action_name=action_name,
            dry_run=dry_run,
            shadow_filter_field=shadow_filter_field,
            shadow_filter_value=shadow_filter_value,
        )


@dataclass(frozen=True, slots=True)
class InvoicesExportResult:
    uploaded_remote_paths: list[str]
    exported_count: int = 0
    shadow_matched: bool | None = None
    shadow_diff: ShadowDiffResult | None = None

    @property
    def uploaded_count(self) -> int:
        return len(self.uploaded_remote_paths)


class InvoicesExportJob:
    """Exports invoices from e-Builder to Oracle FTP via Documents drop or direct API query."""

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
        if self._config.export_source == "api":
            return self._run_api_export()
        elif self._config.export_source == "shadow":
            return self._run_shadow_export()
        return self._run_documents_export()

    def _run_documents_export(
        self,
        on_download: Callable[[str, bytes], None] | None = None,
    ) -> InvoicesExportResult:
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
            daily_prefix=f"eB_Invoices_{today_stamp}",
            latest_only=self._config.latest_only,
            dry_run=self._config.dry_run,
            on_document_downloaded=on_download,
        )
        return InvoicesExportResult(
            uploaded_remote_paths=uploaded_paths,
            exported_count=len(uploaded_paths),
        )

    def _run_api_export(self) -> InvoicesExportResult:
        client = getattr(self._ebuilder, "query_all_process_records", None)
        if not callable(client):
            raise TypeError("e-Builder client does not implement EBuilderProcessPort")

        # 1. Query records with Integration Status == "Ready to Send"
        records = self._ebuilder.query_all_process_records(  # type: ignore[attr-defined]
            DEFAULT_PROCESS_PREFIX,
            {
                "selectedFields": list(PINV_QUERY_FIELDS),
                "filters": [
                    {
                        "Field": "ProcessInstance/DataFields/Integration Status",
                        "Operation": "=",
                        "Value": "Ready to Send",
                    }
                ],
            },
        )

        if not records:
            logger.info("Found 0 PINV records with Integration Status == 'Ready to Send'. Exiting.")
            return InvoicesExportResult(uploaded_remote_paths=[], exported_count=0)

        # 2. Build Oracle AP Invoices CSV
        csv_content = generate_oracle_invoice_csv(records)
        csv_bytes = csv_content.encode("utf-8")

        # 3. Upload to Oracle SFTP (or skip in dry-run mode)
        now = datetime.now()
        timestamp = now.strftime("%Y%m%d%H%M")
        file_name = f"eB_Invoices_{timestamp}.csv"
        remote_path = f"{self._config.oracle_remote_dir.strip('/')}/{file_name}"

        if self._config.dry_run:
            uploaded_path = f"[DRY-RUN] {remote_path}"
            logger.info(
                "[DRY-RUN] Would upload %d invoice records (%d bytes) to Oracle SFTP: %s",
                len(records),
                len(csv_bytes),
                remote_path,
            )
        else:
            uploaded_path = self._oracle_ftp.upload_bytes(csv_bytes, remote_path)
            logger.info("Uploaded %d invoice lines to Oracle SFTP: %s", len(records), uploaded_path)

        # 4. Update status in e-Builder to Complete and take Processed action
        update_data = [
            {
                "header": {
                    "projectIdentifier": (
                        record.get("Project", {}).get("ProjectName")
                        or self._config.project_name
                    ),
                    "processCounter": int(
                        record.get("ProcessInstance", {}).get("InstanceCounter") or 0
                    ),
                    "step": self._config.holding_step,
                    "action": self._config.action_name,
                    "processDataFields": {
                        "Integration Status": "Complete",
                    },
                }
            }
            for record in records
        ]
        self._ebuilder.import_process_records(  # type: ignore[attr-defined]
            process_prefix=DEFAULT_PROCESS_PREFIX,
            data=update_data,
            import_mode="Entity",
        )

        return InvoicesExportResult(
            uploaded_remote_paths=[uploaded_path],
            exported_count=len(records),
        )

    def _run_shadow_export(self) -> InvoicesExportResult:
        """Run standard document export while validating API query output in parallel."""
        downloaded_docs: list[tuple[str, bytes]] = []

        def _record_download(file_name: str, payload: bytes) -> None:
            downloaded_docs.append((file_name, payload))

        # 1. Run live document export
        doc_result = self._run_documents_export(on_download=_record_download)

        # 2. In shadow mode, test query API without updating e-Builder state
        client = getattr(self._ebuilder, "query_all_process_records", None)
        if not callable(client):
            logger.warning(
                "e-Builder client does not implement query_all_process_records; "
                "skipping shadow diff."
            )
            return doc_result

        filter_value = (
            self._config.shadow_filter_value
            or datetime.now().strftime("%m/%d/%Y")
        )
        filter_field = (
            self._config.shadow_filter_field
            or "ProcessInstance/DataFields/Date Exported to Oracle"
        )
        operation = ">=" if filter_field.endswith("Date Exported to Oracle") else "="

        try:
            api_records = self._ebuilder.query_all_process_records(  # type: ignore[attr-defined]
                DEFAULT_PROCESS_PREFIX,
                {
                    "selectedFields": list(PINV_QUERY_FIELDS),
                    "filters": [
                        {
                            "Field": filter_field,
                            "Operation": operation,
                            "Value": filter_value,
                        }
                    ],
                },
            )
            generated_csv = generate_oracle_invoice_csv(api_records)
            logger.info(
                "Shadow check: generated %d CSV lines from %d API records.",
                len(generated_csv.splitlines()) - 1 if generated_csv.strip() else 0,
                len(api_records),
            )

            if not downloaded_docs:
                logger.info(
                    "Shadow check: No document drop was downloaded today to diff against."
                )
                return doc_result

            # Diff the downloaded document CSV against the generated API CSV
            _, doc_bytes = downloaded_docs[0]
            ref_csv = doc_bytes.decode("utf-8", errors="replace")
            diff_result = compare_oracle_invoice_csvs(
                reference_csv=ref_csv, candidate_csv=generated_csv
            )

            if diff_result.is_match:
                logger.info(
                    "Shadow check PASSED: 100.00%% match across %d rows and %d fields.",
                    diff_result.total_candidate_rows,
                    diff_result.total_fields,
                )
            else:
                logger.warning(
                    "Shadow check MISMATCH: match rate %.2f%% (%d/%d fields matched). "
                    "%d differences found.",
                    diff_result.match_rate_pct,
                    diff_result.matched_fields,
                    diff_result.total_fields,
                    len(diff_result.differences),
                )
                for diff in diff_result.differences[:10]:
                    logger.warning("  Diff: %s", diff)

            return InvoicesExportResult(
                uploaded_remote_paths=doc_result.uploaded_remote_paths,
                exported_count=doc_result.exported_count,
                shadow_matched=diff_result.is_match,
                shadow_diff=diff_result,
            )

        except Exception as exc:  # noqa: BLE001
            logger.warning("Shadow check failed with exception: %s", exc)
            return InvoicesExportResult(
                uploaded_remote_paths=doc_result.uploaded_remote_paths,
                exported_count=doc_result.exported_count,
                shadow_matched=False,
            )


def run_invoices_export_job() -> InvoicesExportResult:
    """Entrypoint for scheduler/CLI execution of invoices export."""
    config = InvoicesExportJobConfig.from_env()
    ebuilder_config = EBuilderConfig.from_env()
    oracle_config = OracleFTPConfig.from_env()

    with (
        EBuilderClient(ebuilder_config) as ebuilder_client,
        OracleFTPClient(oracle_config) as oracle_ftp_client,
    ):
        return InvoicesExportJob(ebuilder_client, oracle_ftp_client, config).run()


def run_invoices_export_with_clients(
    ebuilder_client: EBuilderClient,
    oracle_ftp_client: OracleFTPClient,
    config: InvoicesExportJobConfig | None = None,
) -> InvoicesExportResult:
    effective_config = config or InvoicesExportJobConfig.from_env()
    return InvoicesExportJob(ebuilder_client, oracle_ftp_client, effective_config).run()
