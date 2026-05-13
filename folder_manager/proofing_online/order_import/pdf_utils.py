from __future__ import annotations

import os
from typing import List

from .config import WKHTMLTOPDF_PATH

PDFKIT_CONFIG = None


def get_pdfkit_config():
    global PDFKIT_CONFIG
    if PDFKIT_CONFIG is None:
        if not os.path.exists(WKHTMLTOPDF_PATH):
            raise FileNotFoundError(
                f"wkhtmltopdf executable not found at {WKHTMLTOPDF_PATH}"
            )
        try:
            import pdfkit  # type: ignore
        except Exception as exc:
            raise ModuleNotFoundError(
                "pdfkit is required for PDF rendering. Install pdfkit to enable order PDF generation."
            ) from exc
        PDFKIT_CONFIG = pdfkit.configuration(wkhtmltopdf=WKHTMLTOPDF_PATH)
    return PDFKIT_CONFIG


def combine_pdfs(pdf_files: List[str], output_path: str) -> None:
    try:
        import PyPDF2  # type: ignore
    except Exception as exc:
        raise ModuleNotFoundError(
            "PyPDF2 is required for PDF merge. Install PyPDF2 to enable combine PDFs."
        ) from exc
    merger = PyPDF2.PdfMerger()
    try:
        for pdf_file in pdf_files:
            merger.append(pdf_file)
        merger.write(output_path)
    finally:
        merger.close()
