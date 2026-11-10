from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class APIEnvelope(BaseModel):
    """Generic response envelope.

    This model is intentionally permissive because e-Builder responses
    can vary by endpoint and account configuration.
    """

    model_config = ConfigDict(extra="allow")

    data: Any | None = None
    message: str | None = None
    success: bool | None = None


class AuthToken(BaseModel):
    """e-Builder bearer token payload from the Authenticate endpoint."""

    access_token: str
    token_type: str
    expires_in: int


class DocumentsQueryFilter(BaseModel):
    """Filter for the Documents Query endpoint."""

    model_config = ConfigDict(populate_by_name=True)

    field: str = Field(alias="Field")
    operation: str = Field(alias="Operation")
    value: str = Field(alias="Value")


class DocumentsQueryRequest(BaseModel):
    """Request payload for POST /Documents/Query."""

    model_config = ConfigDict(populate_by_name=True)

    selected_fields: list[str] = Field(alias="selectedFields")
    filters: list[DocumentsQueryFilter] = Field(alias="Filters")

    def to_payload(self) -> dict[str, Any]:
        return self.model_dump(by_alias=True, exclude_none=True)

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> DocumentsQueryRequest:
        return cls.model_validate(payload)


class DocumentsQueryMeta(BaseModel):
    """Metadata returned from the Documents Query endpoint."""

    page_number: int = Field(alias="pageNumber")
    page_size: int = Field(alias="pageSize")
    record_count: int = Field(alias="recordCount")


class DocumentsQueryResponse(BaseModel):
    """Response payload for POST /Documents/Query."""

    meta: DocumentsQueryMeta
    success: bool | str
    records: list[dict[str, Any]]
    support_id: str | None = Field(default=None, alias="supportId")


class DocumentsUploadOptions(BaseModel):
    """Options for Documents Upload endpoint."""

    model_config = ConfigDict(populate_by_name=True)

    create_folder: bool = Field(default=False, alias="createFolder")


class DocumentsUploadItem(BaseModel):
    """Single file item in Documents Upload request."""

    model_config = ConfigDict(populate_by_name=True)

    file_name: str = Field(alias="fileName")
    folder_path: str = Field(alias="folderPath")
    project_name: str = Field(alias="projectName")
    base64_encoded_file: str = Field(alias="base64EncodedFile")


class DocumentsUploadRequest(BaseModel):
    """Request payload for POST /Documents/Upload."""

    model_config = ConfigDict(populate_by_name=True)

    options: DocumentsUploadOptions = Field(default_factory=DocumentsUploadOptions)
    data: list[DocumentsUploadItem]

    def to_payload(self) -> dict[str, Any]:
        return self.model_dump(by_alias=True, exclude_none=True)

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> DocumentsUploadRequest:
        return cls.model_validate(payload)


class DocumentsUploadResponse(BaseModel):
    """Response payload for POST /Documents/Upload."""

    success: bool
    support_id: str | None = Field(default=None, alias="supportId")


class ProcessQueryFilter(BaseModel):
    """Filter for Process Query endpoints."""

    model_config = ConfigDict(populate_by_name=True)

    field: str = Field(alias="Field")
    operation: str = Field(alias="Operation")
    value: str = Field(alias="Value")


class ProcessQueryRequest(BaseModel):
    """Request payload for POST /*Processes/Query."""

    model_config = ConfigDict(populate_by_name=True)

    selected_fields: list[str] = Field(alias="selectedFields")
    filters: list[ProcessQueryFilter] = Field(default_factory=list, alias="filters")

    def to_payload(self) -> dict[str, Any]:
        return self.model_dump(by_alias=True, exclude_none=True)

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> ProcessQueryRequest:
        return cls.model_validate(payload)


class ProcessQueryMeta(BaseModel):
    """Metadata returned from Process Query endpoints."""

    model_config = ConfigDict(populate_by_name=True)

    page_number: int = Field(alias="pageNumber")
    page_size: int = Field(alias="pageSize")
    record_count: int = Field(alias="recordCount")


class ProcessQueryResponse(BaseModel):
    """Response payload from POST /*Processes/Query."""

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    meta: ProcessQueryMeta
    records: list[dict[str, Any]]
    support_id: str | None = Field(default=None, alias="supportId")
    success: bool | str | None = None


class ProcessImportOptions(BaseModel):
    """Options for Process Import endpoints."""

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    project_identifier: str = Field(default="ProjectName", alias="projectIdentifier")
    process_prefix: str = Field(alias="processPrefix")
    import_mode: str = Field(default="Entity", alias="importMode")
    create_as_draft: bool = Field(default=False, alias="createAsDraft")
    abort_on_exception: bool = Field(default=False, alias="abortOnException")


class ProcessImportRequest(BaseModel):
    """Request payload for POST /*Processes/Import."""

    model_config = ConfigDict(populate_by_name=True)

    options: ProcessImportOptions = Field(alias="options")
    data: list[dict[str, Any]] = Field(alias="data")

    def to_payload(self) -> dict[str, Any]:
        return self.model_dump(by_alias=True, exclude_none=True)

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> ProcessImportRequest:
        return cls.model_validate(payload)


class ProcessImportResponse(BaseModel):
    """Response payload from POST /*Processes/Import."""

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    success: bool | str | None = None
    support_id: str | None = Field(default=None, alias="supportId")
    message: str | None = None
    data: Any | None = None


class PaginationParams(BaseModel):
    """Common list query parameters."""

    page: int = Field(default=1, ge=1)
    page_size: int = Field(default=100, ge=1, le=1000)

    def to_query(self) -> dict[str, str]:
        return {
            "page": str(self.page),
            "pageSize": str(self.page_size),
        }
