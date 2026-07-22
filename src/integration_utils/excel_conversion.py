from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

import xlrd
from openpyxl import Workbook


class ExcelConversionError(Exception):
    """Raised when converting legacy Excel files fails."""


def convert_xls_to_xlsx(
    source_path: str | Path,
    destination_path: str | Path | None = None,
    *,
    overwrite: bool = False,
) -> Path:
    """Convert a legacy .xls file into an .xlsx file.

    Args:
        source_path: Existing input .xls file path.
        destination_path: Output .xlsx file path. Defaults to source with .xlsx suffix.
        overwrite: Whether to overwrite destination if it already exists.
    """
    source = Path(source_path)
    if not source.exists() or not source.is_file():
        raise ExcelConversionError(f"Source file does not exist: {source}")
    if source.suffix.lower() != ".xls":
        raise ExcelConversionError(f"Source file must have .xls extension: {source}")

    destination = Path(destination_path) if destination_path is not None else source.with_suffix(".xlsx")
    if destination.exists() and not overwrite:
        raise ExcelConversionError(
            f"Destination already exists: {destination}. Set overwrite=True to replace it."
        )

    destination.parent.mkdir(parents=True, exist_ok=True)

    try:
        book = xlrd.open_workbook(source.as_posix())
        workbook = Workbook()
        default_sheet = workbook.active
        workbook.remove(default_sheet)

        for sheet_index, sheet in enumerate(book.sheets()):
            sheet_name = _safe_sheet_name(sheet.name, sheet_index)
            out_sheet = workbook.create_sheet(title=sheet_name)
            for row_index in range(sheet.nrows):
                for col_index in range(sheet.ncols):
                    value = _convert_cell_value(
                        ctype=sheet.cell(row_index, col_index).ctype,
                        value=sheet.cell(row_index, col_index).value,
                        datemode=book.datemode,
                    )
                    out_sheet.cell(row=row_index + 1, column=col_index + 1, value=value)

        if not workbook.sheetnames:
            workbook.create_sheet(title="Sheet1")

        workbook.save(destination)
    except Exception as exc:  # pragma: no cover - defensive catch around third-party libs
        raise ExcelConversionError(f"Failed to convert {source} to {destination}: {exc}") from exc

    return destination


def _convert_cell_value(*, ctype: int, value: Any, datemode: int) -> Any:
    if ctype in (xlrd.XL_CELL_EMPTY, xlrd.XL_CELL_BLANK):
        return None
    if ctype == xlrd.XL_CELL_BOOLEAN:
        return bool(value)
    if ctype == xlrd.XL_CELL_DATE:
        date_value = xlrd.xldate_as_datetime(value, datemode)
        if isinstance(date_value, datetime):
            return date_value
        return date_value
    if ctype == xlrd.XL_CELL_NUMBER and isinstance(value, float) and value.is_integer():
        return int(value)
    if ctype == xlrd.XL_CELL_ERROR:
        return f"#XLERR_{int(value)}"
    return value


def _safe_sheet_name(name: str, index: int) -> str:
    cleaned = name.strip() or f"Sheet{index + 1}"
    for disallowed in ("[", "]", "*", "?", "/", "\\", ":"):
        cleaned = cleaned.replace(disallowed, "_")
    return cleaned[:31]
