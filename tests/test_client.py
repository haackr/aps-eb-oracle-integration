from __future__ import annotations

import base64
import json
from pathlib import Path
from urllib.parse import parse_qs

import httpx
import pytest

from ebuilder_api.client import EBuilderClient
from ebuilder_api.config import EBuilderConfig
from ebuilder_api.exceptions import (
    EBuilderAuthError,
    EBuilderClientError,
    EBuilderRequestError,
    EBuilderServerError,
)
from ebuilder_api.models import DocumentsQueryFilter, DocumentsQueryRequest


def _config() -> EBuilderConfig:
    return EBuilderConfig(
        base_url="https://example.ebuilder.test",
        api_token="token-123",
    )


def _credential_config() -> EBuilderConfig:
    return EBuilderConfig(
        base_url="https://api2.e-builder.net/api/v2",
        username="user@example.com",
        password="my-password",
    )


def _v2_token_config() -> EBuilderConfig:
    return EBuilderConfig(
        base_url="https://api2-us3.e-builder.net/api/v2",
        api_token="token-123",
    )


def test_get_success_returns_json() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "Bearer token-123"
        assert request.url.path == "/api/projects"
        return httpx.Response(200, json={"data": [{"id": 1}]})

    transport = httpx.MockTransport(handler)

    with EBuilderClient(_config(), transport=transport) as client:
        payload = client.get("/api/projects")

    assert payload["data"][0]["id"] == 1


@pytest.mark.parametrize(
    ("status_code", "error_type"),
    [
        (401, EBuilderAuthError),
        (404, EBuilderClientError),
        (500, EBuilderServerError),
    ],
)
def test_error_mapping(status_code: int, error_type: type[Exception]) -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, json={"error": "failed"})

    transport = httpx.MockTransport(handler)

    with EBuilderClient(_config(), transport=transport) as client:
        with pytest.raises(error_type):
            client.get("/api/projects")


def test_authenticate_uses_urlencoded_form_and_sets_bearer_token() -> None:
    seen_auth_header: str | None = None

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal seen_auth_header

        if request.url.path == "/api/v2/Authenticate":
            body = request.content.decode("utf-8")
            form = parse_qs(body)
            assert request.url.host == "api2.e-builder.net"
            assert request.headers["Content-Type"].startswith("application/x-www-form-urlencoded")
            assert form["grant_type"] == ["password"]
            assert form["username"] == ["user@example.com"]
            assert form["password"] == ["my-password"]
            return httpx.Response(
                200,
                json={
                    "access_token": "new-token-xyz",
                    "token_type": "bearer",
                    "expires_in": 14399,
                },
            )

        seen_auth_header = request.headers.get("Authorization")
        return httpx.Response(200, json={"ok": True})

    transport = httpx.MockTransport(handler)

    with EBuilderClient(_credential_config(), transport=transport) as client:
        token = client.authenticate()
        payload = client.get("/projects")

    assert token.access_token == "new-token-xyz"
    assert seen_auth_header == "Bearer new-token-xyz"
    assert payload["ok"] is True


def test_request_auto_authenticates_when_no_static_token() -> None:
    call_order: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        call_order.append(request.url.path)

        if request.url.path == "/api/v2/Authenticate":
            return httpx.Response(
                200,
                json={
                    "access_token": "auto-token-abc",
                    "token_type": "bearer",
                    "expires_in": 14399,
                },
            )

        assert request.headers["Authorization"] == "Bearer auto-token-abc"
        return httpx.Response(200, json={"data": []})

    transport = httpx.MockTransport(handler)

    with EBuilderClient(_credential_config(), transport=transport) as client:
        client.get("/projects")

    assert call_order == ["/api/v2/Authenticate", "/api/v2/projects"]


def test_documents_query_posts_expected_payload_and_parses_response() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v2/Documents/Query":
            payload = json.loads(request.content.decode("utf-8"))
            assert payload["selectedFields"] == [
                "Project/PortalId",
                "Project/ProjectName",
                "Document/FileName",
                "Document/FileId",
                "Document/DownloadURL",
                "Document/Folder",
            ]
            assert payload["Filters"] == [
                {
                    "Field": "Project/ProjectName",
                    "Operation": "=",
                    "Value": "Lawson Integration",
                },
                {
                    "Field": "Document/Folder",
                    "Operation": "=",
                    "Value": "Documents/01 CA & DPI Exports/Oracle",
                },
            ]
            return httpx.Response(
                200,
                json={
                    "meta": {"pageNumber": 0, "pageSize": 3000, "recordCount": 1},
                    "success": "true",
                    "records": [
                        {
                            "Document": {
                                "FileName": "eB_Reqs_202607211745.csv",
                                "Folder": "Documents/01 CA & DPI Exports/Oracle",
                                "FileId": "16369c0d-0d4c-4a71-a1b6-a7e620ac0657",
                                "DownloadURL": "https://example.com/object",
                            },
                            "Project": {
                                "ProjectName": "Lawson Integration",
                                "PortalId": "6343efda-7396-47e2-9a00-dd95a18c5656",
                            },
                        }
                    ],
                    "supportId": "07HK4LY",
                },
            )

        return httpx.Response(404, json={"error": "unexpected path"})

    transport = httpx.MockTransport(handler)
    request_model = DocumentsQueryRequest(
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

    with EBuilderClient(_v2_token_config(), transport=transport) as client:
        response = client.query_documents(request_model)

    assert response.meta.record_count == 1
    assert response.records[0]["Document"]["FileName"] == "eB_Reqs_202607211745.csv"
    assert response.support_id == "07HK4LY"


def test_find_documents_builds_expected_default_query_payload() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v2/Documents/Query"
        payload = json.loads(request.content.decode("utf-8"))
        assert payload["selectedFields"] == [
            "Project/PortalId",
            "Project/ProjectName",
            "Document/FileName",
            "Document/FileId",
            "Document/DownloadURL",
            "Document/Folder",
        ]
        assert payload["Filters"] == [
            {
                "Field": "Project/ProjectName",
                "Operation": "=",
                "Value": "Lawson Integration",
            },
            {
                "Field": "Document/Folder",
                "Operation": "=",
                "Value": "Documents/01 CA & DPI Exports/Oracle",
            },
        ]
        return httpx.Response(
            200,
            json={
                "meta": {"pageNumber": 0, "pageSize": 3000, "recordCount": 1},
                "success": "true",
                "records": [{"Document": {"FileName": "eB_Reqs_202607211745.csv"}}],
                "supportId": "07HK4LY",
            },
        )

    transport = httpx.MockTransport(handler)

    with EBuilderClient(_v2_token_config(), transport=transport) as client:
        response = client.find_documents(
            project_name="Lawson Integration",
            folder="Documents/01 CA & DPI Exports/Oracle",
        )

    assert response.records[0]["Document"]["FileName"] == "eB_Reqs_202607211745.csv"


def test_find_documents_allows_selected_fields_override() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content.decode("utf-8"))
        assert payload["selectedFields"] == ["Document/FileName"]
        return httpx.Response(
            200,
            json={
                "meta": {"pageNumber": 0, "pageSize": 3000, "recordCount": 1},
                "success": "true",
                "records": [{"Document": {"FileName": "only_name.csv"}}],
                "supportId": "07HK4LY",
            },
        )

    transport = httpx.MockTransport(handler)

    with EBuilderClient(_v2_token_config(), transport=transport) as client:
        response = client.find_documents(
            project_name="Lawson Integration",
            folder="Documents/01 CA & DPI Exports/Oracle",
            selected_fields=["Document/FileName"],
        )

    assert response.records[0]["Document"]["FileName"] == "only_name.csv"


def test_download_file_bytes_success_without_auth_header_leak() -> None:
    file_bytes = b"sample,csv\n1,2\n"

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "files.example.test"
        assert request.url.path == "/object"
        assert "Authorization" not in request.headers
        return httpx.Response(200, content=file_bytes)

    transport = httpx.MockTransport(handler)

    with EBuilderClient(_v2_token_config(), transport=transport) as client:
        content = client.download_file_bytes("https://files.example.test/object")

    assert content == file_bytes


def test_download_file_bytes_maps_4xx_errors() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"error": "not found"})

    transport = httpx.MockTransport(handler)

    with EBuilderClient(_v2_token_config(), transport=transport) as client:
        with pytest.raises(EBuilderClientError):
            client.download_file_bytes("https://files.example.test/missing")


def test_download_file_to_path_creates_parent_dirs_and_writes_file(tmp_path: Path) -> None:
    expected = b"a,b\n1,2\n"

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=expected)

    transport = httpx.MockTransport(handler)
    out_path = tmp_path / "nested" / "deeper" / "export.csv"

    with EBuilderClient(_v2_token_config(), transport=transport) as client:
        written_path = client.download_file_to_path(
            "https://files.example.test/object",
            out_path,
        )

    assert written_path == out_path
    assert out_path.exists()
    assert out_path.read_bytes() == expected


def test_download_file_to_path_blocks_overwrite_by_default(tmp_path: Path) -> None:
    existing = tmp_path / "existing.csv"
    existing.write_bytes(b"old")

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"new")

    transport = httpx.MockTransport(handler)

    with EBuilderClient(_v2_token_config(), transport=transport) as client:
        with pytest.raises(EBuilderRequestError):
            client.download_file_to_path("https://files.example.test/object", existing)

    assert existing.read_bytes() == b"old"


def test_download_file_to_path_overwrites_when_enabled(tmp_path: Path) -> None:
    existing = tmp_path / "existing.csv"
    existing.write_bytes(b"old")

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"new")

    transport = httpx.MockTransport(handler)

    with EBuilderClient(_v2_token_config(), transport=transport) as client:
        client.download_file_to_path(
            "https://files.example.test/object",
            existing,
            overwrite=True,
        )

    assert existing.read_bytes() == b"new"


def test_download_document_record_to_dir_uses_document_filename(tmp_path: Path) -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"payload")

    transport = httpx.MockTransport(handler)
    record = {
        "Document": {
            "FileName": "eB_Reqs_202607211745.csv",
            "DownloadURL": "https://files.example.test/object",
        }
    }

    with EBuilderClient(_v2_token_config(), transport=transport) as client:
        path = client.download_document_record_to_dir(record, tmp_path)

    assert path == tmp_path / "eB_Reqs_202607211745.csv"
    assert path.read_bytes() == b"payload"


def test_download_document_record_to_dir_sanitizes_filename(tmp_path: Path) -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"safe")

    transport = httpx.MockTransport(handler)
    record = {
        "Document": {
            "FileName": "../../oracle/export.csv",
            "DownloadURL": "https://files.example.test/object",
        }
    }

    with EBuilderClient(_v2_token_config(), transport=transport) as client:
        path = client.download_document_record_to_dir(record, tmp_path)

    assert path == tmp_path / "export.csv"
    assert path.read_bytes() == b"safe"


def test_download_document_record_to_dir_requires_document_fields(tmp_path: Path) -> None:
    transport = httpx.MockTransport(lambda _: httpx.Response(200, content=b"unused"))
    bad_record = {"Document": {"FileName": "missing-url.csv"}}

    with EBuilderClient(_v2_token_config(), transport=transport) as client:
        with pytest.raises(EBuilderRequestError):
            client.download_document_record_to_dir(bad_record, tmp_path)


def test_upload_documents_posts_expected_payload_and_parses_response() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v2/Documents/Upload"
        payload = json.loads(request.content.decode("utf-8"))
        assert payload == {
            "options": {"createFolder": False},
            "data": [
                {
                    "fileName": "OracleREQ_20260721.xlsx",
                    "folderPath": "Documents/01A CA & DPI Imports/CA Import/PREQ Updated",
                    "projectName": "Lawson Integration",
                    "base64EncodedFile": "VEVTVA==",
                }
            ],
        }
        return httpx.Response(200, json={"success": True, "supportId": "00T56U0"})

    transport = httpx.MockTransport(handler)
    request_body = {
        "options": {"createFolder": False},
        "data": [
            {
                "fileName": "OracleREQ_20260721.xlsx",
                "folderPath": "Documents/01A CA & DPI Imports/CA Import/PREQ Updated",
                "projectName": "Lawson Integration",
                "base64EncodedFile": "VEVTVA==",
            }
        ],
    }

    with EBuilderClient(_v2_token_config(), transport=transport) as client:
        response = client.upload_documents(request_body)

    assert response.success is True
    assert response.support_id == "00T56U0"


def test_upload_document_bytes_encodes_base64_and_posts() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content.decode("utf-8"))
        encoded = payload["data"][0]["base64EncodedFile"]
        assert base64.b64decode(encoded) == b"hello,xlsx"
        assert payload["options"]["createFolder"] is True
        return httpx.Response(200, json={"success": True, "supportId": "UPLOAD1"})

    transport = httpx.MockTransport(handler)

    with EBuilderClient(_v2_token_config(), transport=transport) as client:
        response = client.upload_document_bytes(
            file_name="OracleREQ_20260721.xlsx",
            folder_path="Documents/01A CA & DPI Imports/CA Import/PREQ Updated",
            project_name="Lawson Integration",
            file_bytes=b"hello,xlsx",
            create_folder=True,
        )

    assert response.success is True
    assert response.support_id == "UPLOAD1"


def test_upload_document_file_reads_local_file(tmp_path: Path) -> None:
    local_file = tmp_path / "OracleREQ_20260721.xlsx"
    local_file.write_bytes(b"xlsx-content")

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content.decode("utf-8"))
        assert payload["data"][0]["fileName"] == "OracleREQ_20260721.xlsx"
        assert base64.b64decode(payload["data"][0]["base64EncodedFile"]) == b"xlsx-content"
        return httpx.Response(200, json={"success": True, "supportId": "UPLOAD2"})

    transport = httpx.MockTransport(handler)

    with EBuilderClient(_v2_token_config(), transport=transport) as client:
        response = client.upload_document_file(
            local_path=local_file,
            folder_path="Documents/01A CA & DPI Imports/CA Import/PREQ Updated",
            project_name="Lawson Integration",
        )

    assert response.success is True
    assert response.support_id == "UPLOAD2"


def test_upload_document_file_requires_existing_file(tmp_path: Path) -> None:
    missing = tmp_path / "missing.xlsx"

    transport = httpx.MockTransport(lambda _: httpx.Response(200, json={"success": True}))

    with EBuilderClient(_v2_token_config(), transport=transport) as client:
        with pytest.raises(EBuilderRequestError):
            client.upload_document_file(
                local_path=missing,
                folder_path="Documents/01A CA & DPI Imports/CA Import/PREQ Updated",
                project_name="Lawson Integration",
            )
