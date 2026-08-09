# Copilot Instructions

## Commands

Use Python 3.11. Create the local environment and install development dependencies with:

```bash
python -m pip install -e '.[dev]'
```

Run the full test suite with `pytest`. Run a single test with its node ID, for example:

```bash
pytest tests/test_oracle_to_ebuilder_import_jobs.py::test_import_job_does_not_delete_when_upload_fails
```

Lint and type-check the source with:

```bash
ruff check src tests
mypy src
```

Build the deployable container with:

```bash
docker build -t aps-eb-oracle-integration .
```

For infrastructure changes, work from `infra/terraform` and use `terraform init`, `terraform plan`, and `terraform apply`.

## Architecture

This is a synchronous Python service that transfers integration files between e-Builder and Oracle SFTP. `ebuilder_api` wraps e-Builder authentication plus Documents Query/Upload/download APIs; its Pydantic models encode the API's required aliases and payload shape. `oracle_ftp` owns SFTP connection lifecycle and resolves relative remote paths against `ORACLE_FTP_REMOTE_BASE_DIR`. `integration_utils` converts legacy `.xls` files to `.xlsx`.

`integration_jobs` is the orchestration layer:

- `invoices_export` and `requisitions_export` query e-Builder, select current-day files by their `eB_Invoices_YYYYMMDD` or `eB_Reqs_YYYYMMDD` prefixes, then upload their bytes to Oracle SFTP. `po_changes_export` merges same-day `POC_YYYYMMDDHHMM.csv` and `COP_YYYYMMDDHHMM.csv` exports in timestamp order before uploading one `eB_Change_YYYYMMDDHHMM.csv` file.
- `oracle_to_ebuilder_imports` powers the REQ, PO, and AP-invoice imports: it lists Oracle files, processes `.xls` files in reverse filename order, converts each to `.xlsx`, uploads it to e-Builder, and only then deletes the remote source.

The public `run_*_job` functions in `integration_jobs/__init__.py` are the production entrypoints. Terraform's `locals.jobs` map in `infra/terraform/main.tf` invokes those functions via Cloud Run Jobs and creates the matching Cloud Scheduler triggers. When adding or renaming a production job, update the module export, Terraform job map, schedules, and per-job environment/secret mappings together.

## Repository Conventions

- Keep orchestration independently testable: jobs accept narrow `Protocol` ports and immutable `@dataclass(frozen=True, slots=True)` configs. Use the `run_*_with_clients` pattern for injected real clients; reserve `run_*_job` for constructing environment-based clients and managing their context lifetimes.
- Validate malformed external data and preserve the existing explicit exception hierarchy (`EBuilder*Error`, `OracleFTP*Error`, `ExcelConversionError`) rather than returning partial success. e-Builder document records place file fields under `record["Document"]`.
- Preserve transfer safety: do not create Oracle remote directories implicitly for exports, sanitize externally supplied filenames before local/remote use, and never delete an Oracle import file until e-Builder reports a successful upload.
- Preserve the e-Builder large-folder fallback in export jobs: if a Documents Query rejects `Document/DownloadURL`, repeat the folder query with metadata fields and resolve each selected file's download URL with a filename-filtered query.
- Environment parsing belongs in `from_env` or the job-specific `*_config_from_env` functions. Required strings are stripped and rejected when empty; boolean overrides treat only the string `false` (case-insensitive) as false. Keep `.env.example` and Terraform plain/secret environment maps aligned when adding settings.
- Tests use hand-written protocol-compatible stubs and `httpx.MockTransport` rather than real e-Builder or SFTP services. Test observable transfer order, selected paths, and failure safety.
