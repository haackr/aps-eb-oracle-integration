# e-Builder API Wrapper (Boilerplate)

Lightweight Python wrapper scaffold for integrating with the e-Builder REST API.

## What this includes

- Typed runtime config using environment variables
- Synchronous HTTP client with centralized auth headers
- Password-grant auth support via e-Builder Authenticate endpoint
- Consistent exception mapping for auth, client, and server errors
- Basic Pydantic models for envelope and pagination
- Unit tests using `httpx.MockTransport`
- Linting and typing defaults via Ruff and mypy

## Quick Start

1. Create and activate your virtual environment.
2. Install dependencies:

```bash
pip install -e .[dev]
```

3. Copy env vars:

```bash
cp .env.example .env
```

4. Set values in `.env` for your e-Builder account.

Auth behavior:

- If `EBUILDER_API_TOKEN` is set, the wrapper uses it directly.
- Otherwise, it calls `/Authenticate` on your configured `EBUILDER_BASE_URL` using
  URL-encoded `grant_type=password`, `username`, and `password`, then uses the returned bearer token.

Endpoint path behavior:

- The client sends relative endpoint paths against `EBUILDER_BASE_URL`.
- For Documents Query, it posts to `/Documents/Query` (for example:
  `https://api2-us3.e-builder.net/api/v2/Documents/Query` when your base URL is
  `https://api2-us3.e-builder.net/api/v2/`).
- For Documents Upload, it posts to `/Documents/Upload`.

## Usage

```python
from ebuilder_api import (
    DocumentsQueryFilter,
    DocumentsQueryRequest,
    EBuilderClient,
    EBuilderConfig,
)
from integration_jobs import (
    RequisitionsExportJobConfig,
    run_requisitions_export_with_clients,
    run_po_numbers_import_with_clients,
    run_req_numbers_import_with_clients,
)
from integration_utils import convert_xls_to_xlsx
from oracle_ftp import OracleFTPClient, OracleFTPConfig

config = EBuilderConfig.from_env()

with EBuilderClient(config) as client:
    # Optional explicit auth step. If skipped, first API request will authenticate automatically.
    client.authenticate()

    # Convenience method for the common project+folder document lookup
    quick_result = client.find_documents(
        project_name="Lawson Integration",
        folder="Documents/01 CA & DPI Exports/Oracle",
    )
    print(quick_result.records)

    request = DocumentsQueryRequest(
        selected_fields=[
            "Project/PortalId",
            "Project/ProjectName",
            "Document/FileName",
            "Document/FileId",
            "Document/DownloadURL",
            "Document/Folder",
        ],
        filters=[
            DocumentsQueryFilter(
                field="Project/ProjectName",
                operation="=",
                value="Lawson Integration",
            ),
            DocumentsQueryFilter(
                field="Document/Folder",
                operation="=",
                value="Documents/01 CA & DPI Exports/Oracle",
            ),
        ],
    )

    result = client.query_documents(request)
    print(result.records)

    # Upload a local file into e-Builder Documents/Upload
    upload_response = client.upload_document_file(
        local_path="./oracle-imports/OracleREQ_20260721.xlsx",
        folder_path="Documents/01A CA & DPI Imports/CA Import/PREQ Updated",
        project_name="Lawson Integration",
        create_folder=False,
    )
    print(upload_response.support_id)

    # Download the file bytes from a returned DownloadURL
    first_download_url = result.records[0]["Document"]["DownloadURL"]
    file_bytes = client.download_file_bytes(first_download_url)
    print(len(file_bytes))

    # Or write directly to disk (creates parent directories automatically)
    client.download_file_to_path(
        first_download_url,
        "./downloads/eB_Reqs_202607211745.csv",
        overwrite=True,
    )

    # Easiest: use the query record itself (uses Document/FileName automatically)
    saved_path = client.download_document_record_to_dir(
        result.records[0],
        "./downloads",
        overwrite=True,
    )
    print(saved_path)

# Oracle FTP upload/download
ftp_config = OracleFTPConfig.from_env()

with OracleFTPClient(ftp_config) as ftp_client:
    ftp_client.upload_file("./downloads/eB_Reqs_202607211745.csv", "incoming/eB_Reqs_202607211745.csv")
    ftp_client.download_file("outgoing/processed.csv", "./downloads/processed.csv", overwrite=True)

# Convert legacy .xls from Oracle FTP before uploading into e-Builder
converted_path = convert_xls_to_xlsx("./downloads/OracleREQ_20260721.xls", overwrite=True)
print(converted_path)

# Requisitions export job (e-Builder -> Oracle FTP outbound/reqs)
job_config = RequisitionsExportJobConfig.from_env()

with EBuilderClient(EBuilderConfig.from_env()) as ebuilder_client, OracleFTPClient(
    OracleFTPConfig.from_env()
) as oracle_client:
    result = run_requisitions_export_with_clients(ebuilder_client, oracle_client, job_config)
    print(result.uploaded_remote_paths)

# REQ numbers import job (Oracle FTP /outbound/REQ -> e-Builder PREQ Updated)
with EBuilderClient(EBuilderConfig.from_env()) as ebuilder_client, OracleFTPClient(
    OracleFTPConfig.from_env()
) as oracle_client:
    req_result = run_req_numbers_import_with_clients(ebuilder_client, oracle_client)
    print(req_result.uploaded_file_names)

# PO numbers import job (Oracle FTP /outbound/PO -> e-Builder PO Issued)
with EBuilderClient(EBuilderConfig.from_env()) as ebuilder_client, OracleFTPClient(
    OracleFTPConfig.from_env()
) as oracle_client:
    po_result = run_po_numbers_import_with_clients(ebuilder_client, oracle_client)
    print(po_result.uploaded_file_names)
```

## Oracle FTP Notes

- `OracleFTPClient` supports both `ftp` and `ftps` protocols.
- Best practice is `ftps` unless Oracle explicitly requires plain `ftp`.
- Remote paths are resolved against `ORACLE_FTP_REMOTE_BASE_DIR`.
- `upload_file` creates remote directories automatically by default.

## Excel Conversion

- Use `convert_xls_to_xlsx` to convert Oracle FTP legacy `.xls` files into `.xlsx` files.
- This is useful before calling `upload_document_file` for e-Builder Documents Upload.

## Requisitions Job

- The first scheduler job moves requisitions from e-Builder project `Lawson Integration` folder
  `Documents/01 CA & DPI Exports/Oracle` to Oracle FTP `outbound/reqs`.
- Job config env vars:
  - `INTEGRATION_EBUILDER_PROJECT_NAME`
  - `INTEGRATION_EBUILDER_REQUISITIONS_FOLDER`
  - `INTEGRATION_ORACLE_REQUISITIONS_REMOTE_DIR`
  - `INTEGRATION_REQUISITIONS_LATEST_ONLY`
- When `INTEGRATION_REQUISITIONS_LATEST_ONLY=true`, only the latest file by filename sort is sent.

## Oracle To e-Builder Import Jobs

- REQ job: reads `.xls` files from Oracle FTP `outbound/REQ`, converts each to `.xlsx`, uploads to e-Builder folder `Documents/01A CA & DPI Imports/CA Import/PREQ Updated` in project `Lawson Integration`.
- PO job: reads `.xls` files from Oracle FTP `outbound/PO`, converts each to `.xlsx`, uploads to e-Builder folder `Documents/01A CA & DPI Imports/CA Import/PO Issued` in project `Lawson Integration`.
- Each job is independent and intended for separate scheduler triggers.
- After a successful e-Builder upload, the source `.xls` file is deleted from Oracle FTP.

## Testing

```bash
pytest
```

## Suggested next steps

- Add endpoint-specific modules (for example: projects, companies, commitments).
- Replace generic dict payloads with endpoint-specific Pydantic models.
- Add retry/backoff logic with idempotency controls for GET/PUT requests.
- Add async support with `httpx.AsyncClient` if needed by your integration workload.
