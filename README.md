# e-Builder <-> Oracle Integration

This repository contains an in-development integration service between e-Builder and Oracle FTP.

The system runs scheduled jobs that move files in both directions:

- e-Builder -> Oracle FTP exports
- Oracle FTP -> e-Builder imports

Job count and workflows will continue to evolve.

## What This Integration Does

- Queries and downloads files from e-Builder.
- Uploads and downloads files via Oracle FTP/FTPS.
- Converts legacy `.xls` files to `.xlsx` when needed for imports.
- Applies filename and folder-based routing rules.

## Architecture

- `src/ebuilder_api`: e-Builder auth/query/upload/download client.
- `src/oracle_ftp`: FTP/FTPS client for Oracle-side file transport.
- `src/integration_utils`: shared helpers, including `.xls` -> `.xlsx` conversion.
- `src/integration_jobs`: orchestration jobs connecting e-Builder and Oracle FTP.
- `infra/terraform`: Cloud Run Jobs + Cloud Scheduler + IAM/secret wiring.

## Safety and Data Handling Rules

- Import paths convert `.xls` to `.xlsx` before e-Builder upload.
- Source files are removed from FTP only after successful upload.
- Client and job layers raise explicit errors on malformed records and paths.

## Quick Start (Local)

1. Create and activate a virtual environment.
2. Install dependencies:

```bash
pip install -e .[dev]
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

Core Oracle FTP variables:

- `ORACLE_FTP_HOST`
- `ORACLE_FTP_USERNAME`
- `ORACLE_FTP_PASSWORD`
- `ORACLE_FTP_PROTOCOL`
- `ORACLE_FTP_PORT`
- `ORACLE_FTP_REMOTE_BASE_DIR`

Job variables:

- `INTEGRATION_EBUILDER_*`
- `INTEGRATION_ORACLE_*`
- `INTEGRATION_REQ_IMPORT_*`
- `INTEGRATION_PO_IMPORT_*`

See `.env.example` for the current full variable list.

## Running Jobs Programmatically

```python
from integration_jobs import (
    run_requisitions_export_job,
    run_req_numbers_import_job,
)

requisitions_result = run_requisitions_export_job()
req_result = run_req_numbers_import_job()

print(requisitions_result.uploaded_count)
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
- `src/oracle_ftp` for Oracle FTP/FTPS file transfer operations.
