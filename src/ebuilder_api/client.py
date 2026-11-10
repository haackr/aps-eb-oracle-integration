from __future__ import annotations

import base64
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

import httpx
from pydantic import ValidationError

from .config import EBuilderConfig
from .exceptions import (
    EBuilderAuthError,
    EBuilderClientError,
    EBuilderRequestError,
    EBuilderServerError,
)
from .models import (
    AuthToken,
    DocumentsQueryFilter,
    DocumentsQueryRequest,
    DocumentsQueryResponse,
    DocumentsUploadItem,
    DocumentsUploadOptions,
    DocumentsUploadRequest,
    DocumentsUploadResponse,
    ProcessImportOptions,
    ProcessImportRequest,
    ProcessImportResponse,
    ProcessQueryRequest,
    ProcessQueryResponse,
)

DEFAULT_DOCUMENT_QUERY_FIELDS: tuple[str, ...] = (
    "Project/PortalId",
    "Project/ProjectName",
    "Document/FileName",
    "Document/FileId",
    "Document/DownloadURL",
    "Document/Folder",
)

PROCESS_QUERY_ENDPOINTS: dict[str, str] = {
    "PINV": "/CommitmentInvoiceProcesses/Query",
    "COP": "/CommitmentProcesses/Query",
    "CA": "/CommitmentProcesses/Query",
    "POC": "/CommitmentChangeProcesses/Query",
}

PROCESS_IMPORT_ENDPOINTS: dict[str, str] = {
    "PINV": "/InvoiceProcesses/Import",
    "COP": "/CommitmentProcesses/Import",
    "CA": "/CommitmentProcesses/Import",
    "POC": "/CommitmentChangeProcesses/Import",
}


class EBuilderClient:
    """Lightweight, synchronous e-Builder API client.

    Example:
        config = EBuilderConfig.from_env()
        with EBuilderClient(config) as client:
            projects = client.get("/projects")
    """

    def __init__(
        self,
        config: EBuilderConfig,
        *,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._config = config
        self._auth_token: AuthToken | None = None
        self._auth_path = "/Authenticate"
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": self._config.user_agent,
        }
        if self._config.api_token:
            headers["Authorization"] = f"Bearer {self._config.api_token}"

        self._client = httpx.Client(
            base_url=self._config.base_url,
            timeout=self._config.timeout_seconds,
            verify=self._config.verify_ssl,
            headers=headers,
            transport=transport,
        )
        self._download_client = httpx.Client(
            timeout=self._config.timeout_seconds,
            verify=self._config.verify_ssl,
            headers={"User-Agent": self._config.user_agent},
            transport=transport,
        )

    def __enter__(self) -> "EBuilderClient":
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.close()

    def close(self) -> None:
        self._client.close()
        self._download_client.close()

    @property
    def access_token(self) -> str | None:
        if self._auth_token:
            return self._auth_token.access_token
        return self._config.api_token

    def authenticate(self) -> AuthToken:
        """Authenticate with username/password and cache the bearer token."""
        if not self._config.username or not self._config.password:
            raise EBuilderRequestError(
                "Username/password not configured for Authenticate endpoint"
            )

        form_data = {
            "grant_type": self._config.grant_type,
            "username": self._config.username,
            "password": self._config.password,
        }

        try:
            response = self._client.post(
                self._normalize_target(self._auth_path),
                data=form_data,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
        except httpx.HTTPError as exc:
            raise EBuilderRequestError(f"Auth request failed: {exc}") from exc

        payload = self._handle_response(response)

        try:
            token = AuthToken.model_validate(payload)
        except ValidationError as exc:
            raise EBuilderRequestError(f"Auth response was not a valid token payload: {exc}") from exc

        self._auth_token = token
        self._client.headers["Authorization"] = f"Bearer {token.access_token}"
        return token

    def get(
        self,
        path: str,
        *,
        params: Mapping[str, str | int | float | bool] | None = None,
    ) -> Any:
        return self._request("GET", path, params=params)

    def post(
        self,
        path: str,
        *,
        json: Mapping[str, Any] | None = None,
        params: Mapping[str, str | int | float | bool] | None = None,
    ) -> Any:
        return self._request("POST", path, json=json, params=params)

    def put(
        self,
        path: str,
        *,
        json: Mapping[str, Any] | None = None,
        params: Mapping[str, str | int | float | bool] | None = None,
    ) -> Any:
        return self._request("PUT", path, json=json, params=params)

    def delete(
        self,
        path: str,
        *,
        params: Mapping[str, str | int | float | bool] | None = None,
    ) -> Any:
        return self._request("DELETE", path, params=params)

    def query_documents(
        self,
        request: DocumentsQueryRequest | Mapping[str, Any],
    ) -> DocumentsQueryResponse:
        """Run a Documents Query request via POST /Documents/Query."""
        if isinstance(request, DocumentsQueryRequest):
            payload = request.to_payload()
        else:
            payload = DocumentsQueryRequest.from_payload(request).to_payload()

        response_payload = self.post("/Documents/Query", json=payload)
        try:
            return DocumentsQueryResponse.model_validate(response_payload)
        except ValidationError as exc:
            raise EBuilderRequestError(
                f"Documents Query response was not in the expected format: {exc}"
            ) from exc

    def upload_documents(
        self,
        request: DocumentsUploadRequest | Mapping[str, Any],
    ) -> DocumentsUploadResponse:
        """Run a Documents Upload request via POST /Documents/Upload."""
        if isinstance(request, DocumentsUploadRequest):
            payload = request.to_payload()
        else:
            payload = DocumentsUploadRequest.from_payload(request).to_payload()

        response_payload = self.post("/Documents/Upload", json=payload)
        try:
            return DocumentsUploadResponse.model_validate(response_payload)
        except ValidationError as exc:
            raise EBuilderRequestError(
                f"Documents Upload response was not in the expected format: {exc}"
            ) from exc

    def upload_document_bytes(
        self,
        *,
        file_name: str,
        folder_path: str,
        project_name: str,
        file_bytes: bytes,
        create_folder: bool = False,
    ) -> DocumentsUploadResponse:
        """Upload one document from in-memory bytes to e-Builder Documents."""
        encoded_file = base64.b64encode(file_bytes).decode("ascii")
        request = DocumentsUploadRequest(
            options=DocumentsUploadOptions(create_folder=create_folder),
            data=[
                DocumentsUploadItem(
                    file_name=file_name,
                    folder_path=folder_path,
                    project_name=project_name,
                    base64_encoded_file=encoded_file,
                )
            ],
        )
        return self.upload_documents(request)

    def upload_document_file(
        self,
        *,
        local_path: str | Path,
        folder_path: str,
        project_name: str,
        file_name: str | None = None,
        create_folder: bool = False,
    ) -> DocumentsUploadResponse:
        """Upload one local file into e-Builder Documents/Upload endpoint."""
        source = Path(local_path)
        if not source.exists() or not source.is_file():
            raise EBuilderRequestError(f"Local file does not exist: {source}")

        effective_name = file_name or source.name
        return self.upload_document_bytes(
            file_name=effective_name,
            folder_path=folder_path,
            project_name=project_name,
            file_bytes=source.read_bytes(),
            create_folder=create_folder,
        )

    def find_documents(
        self,
        *,
        project_name: str,
        folder: str,
        selected_fields: list[str] | None = None,
    ) -> DocumentsQueryResponse:
        """Find documents by project name and folder using POST /Documents/Query."""
        fields = list(selected_fields) if selected_fields else list(DEFAULT_DOCUMENT_QUERY_FIELDS)
        request = DocumentsQueryRequest(
            selected_fields=fields,
            filters=[
                DocumentsQueryFilter(
                    field="Project/ProjectName",
                    operation="=",
                    value=project_name,
                ),
                DocumentsQueryFilter(
                    field="Document/Folder",
                    operation="=",
                    value=folder,
                ),
            ],
        )
        return self.query_documents(request)

    def resolve_process_query_endpoint(self, process_prefix: str) -> str:
        prefix = process_prefix.strip().upper()
        if prefix in PROCESS_QUERY_ENDPOINTS:
            return PROCESS_QUERY_ENDPOINTS[prefix]
        raise EBuilderRequestError(
            f"No default query endpoint configured for process prefix '{process_prefix}'. "
            f"Specify endpoint explicitly."
        )

    def resolve_process_import_endpoint(self, process_prefix: str) -> str:
        prefix = process_prefix.strip().upper()
        if prefix in PROCESS_IMPORT_ENDPOINTS:
            return PROCESS_IMPORT_ENDPOINTS[prefix]
        raise EBuilderRequestError(
            f"No default import endpoint configured for process prefix '{process_prefix}'. "
            f"Specify endpoint explicitly."
        )

    def query_process(
        self,
        process_prefix: str,
        request: ProcessQueryRequest | Mapping[str, Any],
        *,
        page_number: int | None = None,
        page_size: int | None = None,
        endpoint: str | None = None,
    ) -> ProcessQueryResponse:
        """Run a Process Query request via POST /*Processes/Query."""
        if isinstance(request, ProcessQueryRequest):
            payload = request.to_payload()
        else:
            payload = ProcessQueryRequest.from_payload(request).to_payload()

        target_endpoint = endpoint or self.resolve_process_query_endpoint(process_prefix)
        params: dict[str, str | int] = {"processPrefix": process_prefix.strip().upper()}
        if page_number is not None:
            params["pageNumber"] = page_number
        if page_size is not None:
            params["pageSize"] = page_size

        response_payload = self.post(target_endpoint, json=payload, params=params)
        try:
            return ProcessQueryResponse.model_validate(response_payload)
        except ValidationError as exc:
            raise EBuilderRequestError(
                f"Process Query response was not in the expected format: {exc}"
            ) from exc

    def query_all_process_records(
        self,
        process_prefix: str,
        request: ProcessQueryRequest | Mapping[str, Any],
        *,
        page_size: int = 1000,
        endpoint: str | None = None,
    ) -> list[dict[str, Any]]:
        """Query all matching process records across pages."""
        all_records: list[dict[str, Any]] = []
        page_number = 0

        while True:
            response = self.query_process(
                process_prefix,
                request,
                page_number=page_number,
                page_size=page_size,
                endpoint=endpoint,
            )
            records = response.records
            all_records.extend(records)

            if len(records) < page_size or response.meta.record_count < page_size:
                break
            page_number += 1

        return all_records

    def import_process(
        self,
        process_prefix: str,
        request: ProcessImportRequest | Mapping[str, Any],
        *,
        endpoint: str | None = None,
    ) -> ProcessImportResponse:
        """Import or update process instances via POST /*Processes/Import."""
        if isinstance(request, ProcessImportRequest):
            payload = request.to_payload()
        else:
            payload = ProcessImportRequest.from_payload(request).to_payload()

        target_endpoint = endpoint or self.resolve_process_import_endpoint(process_prefix)
        params = {"processPrefix": process_prefix.strip().upper()}

        response_payload = self.post(target_endpoint, json=payload, params=params)
        try:
            return ProcessImportResponse.model_validate(response_payload)
        except ValidationError as exc:
            raise EBuilderRequestError(
                f"Process Import response was not in the expected format: {exc}"
            ) from exc

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
    ) -> ProcessImportResponse:
        """Convenience method to import/update a list of process records."""
        prefix = process_prefix.strip().upper()
        request = ProcessImportRequest(
            options=ProcessImportOptions(
                project_identifier=project_identifier,
                process_prefix=prefix,
                import_mode=import_mode,
                create_as_draft=create_as_draft,
                abort_on_exception=abort_on_exception,
            ),
            data=data,
        )
        return self.import_process(prefix, request, endpoint=endpoint)

    def download_file_bytes(self, download_url: str) -> bytes:
        """Download file bytes from a document DownloadURL.

        This request intentionally uses a separate HTTP client that does not
        include the e-Builder bearer token header.
        """
        target = self._normalize_target(download_url)
        try:
            response = self._download_client.get(target, follow_redirects=True)
        except httpx.HTTPError as exc:
            raise EBuilderRequestError(f"Download failed for {target}: {exc}") from exc

        if response.status_code < 400:
            return response.content

        try:
            payload: Any = response.json()
        except ValueError:
            payload = {"raw": response.text}

        if response.status_code in (401, 403):
            raise EBuilderAuthError(response.status_code, payload)
        if response.status_code < 500:
            raise EBuilderClientError(response.status_code, payload)
        raise EBuilderServerError(response.status_code, payload)

    def download_file_to_path(
        self,
        download_url: str,
        destination_path: str | Path,
        *,
        overwrite: bool = False,
    ) -> Path:
        """Download a file from DownloadURL and write it to disk.

        Parent directories are created automatically. By default, this method
        refuses to overwrite an existing file unless overwrite=True.
        """
        target_path = Path(destination_path)

        if target_path.exists() and not overwrite:
            raise EBuilderRequestError(
                f"Destination already exists: {target_path}. Set overwrite=True to replace it."
            )

        target_path.parent.mkdir(parents=True, exist_ok=True)
        file_bytes = self.download_file_bytes(download_url)
        target_path.write_bytes(file_bytes)
        return target_path

    def download_document_record_to_dir(
        self,
        document_record: Mapping[str, Any],
        destination_dir: str | Path,
        *,
        overwrite: bool = False,
    ) -> Path:
        """Download a Documents Query record using Document/FileName and Document/DownloadURL.

        Expects a record shaped like the Documents Query response, where file
        metadata lives under `record["Document"]`.
        """
        document_section = document_record.get("Document")
        if not isinstance(document_section, Mapping):
            raise EBuilderRequestError("Invalid document record: missing Document section")

        raw_download_url = document_section.get("DownloadURL")
        raw_file_name = document_section.get("FileName")

        if not isinstance(raw_download_url, str) or not raw_download_url.strip():
            raise EBuilderRequestError("Invalid document record: missing Document/DownloadURL")
        if not isinstance(raw_file_name, str) or not raw_file_name.strip():
            raise EBuilderRequestError("Invalid document record: missing Document/FileName")

        safe_name = self._safe_file_name(raw_file_name)
        target_path = Path(destination_dir) / safe_name
        return self.download_file_to_path(raw_download_url, target_path, overwrite=overwrite)

    def _request(
        self,
        method: str,
        path: str,
        *,
        json: Mapping[str, Any] | None = None,
        params: Mapping[str, str | int | float | bool] | None = None,
    ) -> Any:
        if "Authorization" not in self._client.headers:
            self.authenticate()

        normalized_path = self._normalize_target(path)

        try:
            response = self._client.request(method, normalized_path, json=json, params=params)
        except httpx.HTTPError as exc:
            raise EBuilderRequestError(f"{method} {normalized_path} failed: {exc}") from exc

        return self._handle_response(response)

    def _handle_response(self, response: httpx.Response) -> Any:
        try:
            payload = response.json()
        except ValueError:
            payload = {"raw": response.text}

        if response.status_code < 400:
            return payload
        if response.status_code in (401, 403):
            raise EBuilderAuthError(response.status_code, payload)
        if response.status_code < 500:
            raise EBuilderClientError(response.status_code, payload)
        raise EBuilderServerError(response.status_code, payload)

    @staticmethod
    def _normalize_target(path: str) -> str:
        if path.startswith("http://") or path.startswith("https://"):
            return path
        return path.lstrip("/")

    @staticmethod
    def _safe_file_name(file_name: str) -> str:
        """Sanitize file names to avoid path traversal and invalid separators."""
        decoded = unquote(file_name).strip()
        if not decoded:
            raise EBuilderRequestError("Invalid document file name")

        # Keep only the final path component and replace any remaining separators.
        candidate = Path(decoded).name.replace("/", "_").replace("\\", "_")
        if candidate in {"", ".", ".."}:
            raise EBuilderRequestError("Invalid document file name")

        # If the response accidentally sends a URL-like value, use only basename.
        parsed = urlparse(candidate)
        if parsed.scheme and parsed.path:
            candidate = Path(parsed.path).name

        if not candidate:
            raise EBuilderRequestError("Invalid document file name")
        return candidate
