# e-Builder <-> Oracle Integration

This repository contains an in-development integration service between e-Builder and Oracle SFTP.

The system runs scheduled jobs that move files in both directions:

- e-Builder -> Oracle SFTP exports
- Oracle SFTP -> e-Builder imports

Job count and workflows will continue to evolve.

## What This Integration Does

- Queries and downloads files from e-Builder.
- Uploads and downloads files via Oracle SFTP.
- Converts legacy `.xls` files to `.xlsx` when needed for imports.
- Applies filename and folder-based routing rules.

## Architecture

- `src/ebuilder_api`: e-Builder auth/query/upload/download client.
- `src/oracle_ftp`: SFTP client for Oracle-side file transport.
- `src/integration_utils`: shared helpers, including `.xls` -> `.xlsx` conversion.
- `src/integration_jobs`: orchestration jobs connecting e-Builder and Oracle SFTP.
- `infra/terraform`: Cloud Run Jobs + Cloud Scheduler + IAM/secret wiring.

## Safety and Data Handling Rules

- Import paths convert `.xls` to `.xlsx` before e-Builder upload.
- Source files are removed from FTP only after successful upload.
- Uploads fail if the remote directory path does not already exist.
- Client and job layers raise explicit errors on malformed records and paths.

## Quick Start (Local)

1. Create and activate a virtual environment.
2. Install dependencies:

```bash
python -m pip install -e '.[dev]'
```

3. Copy environment template and fill values:

```bash
cp .env.example .env
```

4. Run tests:

```bash
pytest
```

## Environment Configuration

Core e-Builder variables:

- `EBUILDER_BASE_URL`
- `EBUILDER_USERNAME`
- `EBUILDER_PASSWORD`
- `EBUILDER_API_TOKEN` (optional override)

Core Oracle SFTP variables:

- `ORACLE_FTP_HOST`
- `ORACLE_FTP_USERNAME`
- `ORACLE_FTP_PASSWORD`
- `ORACLE_FTP_PROTOCOL`
- `ORACLE_FTP_PORT`
- `ORACLE_FTP_REMOTE_BASE_DIR`

Job variables:

- `INTEGRATION_EBUILDER_*`
- `INTEGRATION_ORACLE_*`
- `INTEGRATION_INVOICES_*`
- `INTEGRATION_PO_CHANGES_*`
- `INTEGRATION_REQ_IMPORT_*`
- `INTEGRATION_PO_IMPORT_*`
- `INTEGRATION_AP_INVOICE_IMPORT_*`

See `.env.example` for the current full variable list.

## Running Jobs Programmatically

```python
from integration_jobs import (
    run_invoices_export_job,
    run_po_changes_export_job,
    run_requisitions_export_job,
    run_ap_invoice_import_job,
    run_req_numbers_import_job,
)

invoices_result = run_invoices_export_job()
po_changes_result = run_po_changes_export_job()
requisitions_result = run_requisitions_export_job()
ap_invoice_result = run_ap_invoice_import_job()
req_result = run_req_numbers_import_job()

print(invoices_result.uploaded_count)
print(po_changes_result.uploaded_count)
print(requisitions_result.uploaded_count)
print(ap_invoice_result.uploaded_count)
print(req_result.uploaded_count)
```

Additional job entrypoints are available in `src/integration_jobs` and will expand over time.

## Deployment

Terraform under `infra/terraform` provisions:

- Cloud Run Jobs for integration workloads
- Cloud Scheduler triggers
- Service accounts, IAM bindings, and Secret Manager wiring

Use `terraform.tfvars` to set project, image, schedules, and secret names.

## API and Transport Clients

The integration jobs are built on two core clients:

- `src/ebuilder_api` for e-Builder authentication, document query/upload, and file download.
- `src/oracle_ftp` for Oracle SFTP file transfer operations.
