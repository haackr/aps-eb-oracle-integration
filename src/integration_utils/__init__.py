"""Shared utility helpers for integration jobs."""

from .excel_conversion import ExcelConversionError, convert_xls_to_xlsx

__all__ = ["ExcelConversionError", "convert_xls_to_xlsx"]
