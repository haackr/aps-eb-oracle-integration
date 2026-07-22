from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from openpyxl import load_workbook

from integration_utils.excel_conversion import ExcelConversionError, convert_xls_to_xlsx


class _FakeCell:
    def __init__(self, ctype: int, value: Any) -> None:
        self.ctype = ctype
        self.value = value


class _FakeSheet:
    def __init__(self, name: str, values: list[list[_FakeCell]]) -> None:
        self.name = name
        self._values = values
        self.nrows = len(values)
        self.ncols = len(values[0]) if values else 0

    def cell(self, row: int, col: int) -> _FakeCell:
        return self._values[row][col]


class _FakeBook:
    def __init__(self, sheets: list[_FakeSheet], datemode: int = 0) -> None:
        self._sheets = sheets
        self.datemode = datemode

    def sheets(self) -> list[_FakeSheet]:
        return self._sheets


def test_convert_xls_to_xlsx_writes_expected_cells(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import integration_utils.excel_conversion as mod

    source = tmp_path / "input.xls"
    source.write_bytes(b"legacy-binary")

    fake_sheet = _FakeSheet(
        "Legacy Sheet",
        [
            [_FakeCell(mod.xlrd.XL_CELL_TEXT, "Name"), _FakeCell(mod.xlrd.XL_CELL_NUMBER, 42.0)],
            [_FakeCell(mod.xlrd.XL_CELL_BOOLEAN, 1), _FakeCell(mod.xlrd.XL_CELL_EMPTY, "")],
        ],
    )
    fake_book = _FakeBook([fake_sheet])

    monkeypatch.setattr(mod.xlrd, "open_workbook", lambda _: fake_book)

    output = convert_xls_to_xlsx(source)

    assert output == tmp_path / "input.xlsx"
    assert output.exists()

    wb = load_workbook(output)
    ws = wb["Legacy Sheet"]
    assert ws.cell(row=1, column=1).value == "Name"
    assert ws.cell(row=1, column=2).value == 42
    assert ws.cell(row=2, column=1).value is True
    assert ws.cell(row=2, column=2).value is None


def test_convert_xls_to_xlsx_rejects_non_xls_extension(tmp_path: Path) -> None:
    source = tmp_path / "input.xlsx"
    source.write_bytes(b"new-format")

    with pytest.raises(ExcelConversionError):
        convert_xls_to_xlsx(source)


def test_convert_xls_to_xlsx_blocks_overwrite_by_default(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import integration_utils.excel_conversion as mod

    source = tmp_path / "input.xls"
    source.write_bytes(b"legacy-binary")
    destination = tmp_path / "input.xlsx"
    destination.write_bytes(b"existing")

    fake_sheet = _FakeSheet("Sheet1", [[_FakeCell(mod.xlrd.XL_CELL_TEXT, "x")]])
    fake_book = _FakeBook([fake_sheet])
    monkeypatch.setattr(mod.xlrd, "open_workbook", lambda _: fake_book)

    with pytest.raises(ExcelConversionError):
        convert_xls_to_xlsx(source, destination)

    assert destination.read_bytes() == b"existing"


def test_convert_xls_to_xlsx_allows_overwrite(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import integration_utils.excel_conversion as mod

    source = tmp_path / "input.xls"
    source.write_bytes(b"legacy-binary")
    destination = tmp_path / "input.xlsx"
    destination.write_bytes(b"existing")

    fake_sheet = _FakeSheet("Sheet1", [[_FakeCell(mod.xlrd.XL_CELL_TEXT, "updated")]])
    fake_book = _FakeBook([fake_sheet])
    monkeypatch.setattr(mod.xlrd, "open_workbook", lambda _: fake_book)

    output = convert_xls_to_xlsx(source, destination, overwrite=True)

    wb = load_workbook(output)
    assert wb["Sheet1"].cell(row=1, column=1).value == "updated"
