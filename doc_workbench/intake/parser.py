from __future__ import annotations

from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from doc_workbench.intake.detector import detect_parse_strategy
from doc_workbench.intake.models import ParseRecord


def _pypdf_version() -> str:
    try:
        from importlib.metadata import version
        return f"pypdf=={version('pypdf')}"
    except Exception:
        return "pypdf==unknown"


def _bs4_version() -> str:
    try:
        from importlib.metadata import version
        return f"beautifulsoup4=={version('beautifulsoup4')}"
    except Exception:
        return "beautifulsoup4==unknown"


@runtime_checkable
class OcrBackend(Protocol):
    """Contract for a future OCR implementation. Not bundled in this phase."""
    def extract_text(self, path: Path, page_number: int) -> str: ...


def run_parse(
    document_id: str,
    local_path: Path,
    manifest: dict[str, Any],
    run_id: str,
    ocr_backend: OcrBackend | None = None,
) -> ParseRecord:
    """Route artifact to the correct parse path and return a ParseRecord."""
    content_type = str(manifest.get("content_type") or "")
    now = datetime.now(timezone.utc).isoformat()

    modality, parse_strategy, text_layer_present, quality_signals = detect_parse_strategy(
        content_type, local_path
    )

    if parse_strategy == "native_pdf_text":
        return _parse_native_pdf(
            document_id=document_id,
            local_path=local_path,
            content_type=content_type,
            modality=modality,
            text_layer_present=text_layer_present,
            quality_signals=quality_signals,
            run_id=run_id,
            now=now,
        )

    if parse_strategy == "html_parse":
        return _parse_html(
            document_id=document_id,
            local_path=local_path,
            content_type=content_type,
            modality=modality,
            text_layer_present=text_layer_present,
            quality_signals=quality_signals,
            run_id=run_id,
            now=now,
        )

    if parse_strategy == "ocr_fallback":
        return _parse_ocr_fallback(
            document_id=document_id,
            local_path=local_path,
            content_type=content_type,
            modality=modality,
            quality_signals=quality_signals,
            ocr_backend=ocr_backend,
            run_id=run_id,
            now=now,
        )

    # skipped / unsupported
    return ParseRecord(
        document_id=document_id,
        schema_version=1,
        parser_name="none",
        parser_version="",
        content_type=content_type,
        modality=modality,
        text_layer_present=False,
        parse_strategy="skipped",
        parse_status="skipped",
        page_count=None,
        quality_signals={},
        errors=[],
        created_at=now,
        run_id=run_id,
    )


def _parse_native_pdf(
    *,
    document_id: str,
    local_path: Path,
    content_type: str,
    modality: str,
    text_layer_present: bool,
    quality_signals: dict[str, Any],
    run_id: str,
    now: str,
) -> ParseRecord:
    errors: list[str] = []
    page_count: int | None = None
    parse_status = "complete"

    try:
        from pypdf import PdfReader
        reader = PdfReader(BytesIO(local_path.read_bytes()))
        page_count = len(reader.pages)
    except Exception as exc:
        errors.append(f"{type(exc).__name__}: {exc}")
        parse_status = "failed"

    if not errors and page_count == 0:
        errors.append("PDF has zero pages")
        parse_status = "partial"

    return ParseRecord(
        document_id=document_id,
        schema_version=1,
        parser_name="pypdf",
        parser_version=_pypdf_version(),
        content_type=content_type,
        modality=modality,
        text_layer_present=text_layer_present,
        parse_strategy="native_pdf_text",
        parse_status=parse_status,
        page_count=page_count,
        quality_signals=quality_signals,
        errors=errors,
        created_at=now,
        run_id=run_id,
    )


def _parse_html(
    *,
    document_id: str,
    local_path: Path,
    content_type: str,
    modality: str,
    text_layer_present: bool,
    quality_signals: dict[str, Any],
    run_id: str,
    now: str,
) -> ParseRecord:
    errors: list[str] = []
    parse_status = "complete"
    updated_signals = dict(quality_signals)

    try:
        from bs4 import BeautifulSoup
        html_text = local_path.read_text(encoding="utf-8", errors="replace")
        soup = BeautifulSoup(html_text, "html.parser")
        heading_count = len(soup.find_all(["h1", "h2", "h3", "h4", "h5", "h6"]))
        paragraph_count = len(soup.find_all("p"))
        updated_signals["heading_count"] = heading_count
        updated_signals["paragraph_count"] = paragraph_count
    except Exception as exc:
        errors.append(f"{type(exc).__name__}: {exc}")
        parse_status = "failed"

    return ParseRecord(
        document_id=document_id,
        schema_version=1,
        parser_name="html_native",
        parser_version=_bs4_version(),
        content_type=content_type,
        modality=modality,
        text_layer_present=text_layer_present,
        parse_strategy="html_parse",
        parse_status=parse_status,
        page_count=None,
        quality_signals=updated_signals,
        errors=errors,
        created_at=now,
        run_id=run_id,
    )


def _parse_ocr_fallback(
    *,
    document_id: str,
    local_path: Path,
    content_type: str,
    modality: str,
    quality_signals: dict[str, Any],
    ocr_backend: OcrBackend | None,
    run_id: str,
    now: str,
) -> ParseRecord:
    if ocr_backend is None:
        return ParseRecord(
            document_id=document_id,
            schema_version=1,
            parser_name="none",
            parser_version="",
            content_type=content_type,
            modality=modality,
            text_layer_present=False,
            parse_strategy="ocr_fallback",
            parse_status="failed",
            page_count=None,
            quality_signals=quality_signals,
            errors=["OCR backend not configured; OCR is not included in this phase"],
            created_at=now,
            run_id=run_id,
        )

    # OCR backend provided — delegate per page.
    errors: list[str] = []
    page_count: int | None = None
    parse_status = "complete"

    try:
        from pypdf import PdfReader
        reader = PdfReader(BytesIO(local_path.read_bytes()))
        page_count = len(reader.pages)
        for i in range(page_count):
            try:
                ocr_backend.extract_text(local_path, i)
            except Exception as exc:
                errors.append(f"page {i}: {type(exc).__name__}: {exc}")
        if errors:
            parse_status = "partial"
    except Exception as exc:
        errors.append(f"{type(exc).__name__}: {exc}")
        parse_status = "failed"

    return ParseRecord(
        document_id=document_id,
        schema_version=1,
        parser_name="ocr_backend",
        parser_version="",
        content_type=content_type,
        modality=modality,
        text_layer_present=False,
        parse_strategy="ocr_fallback",
        parse_status=parse_status,
        page_count=page_count,
        quality_signals=quality_signals,
        errors=errors,
        created_at=now,
        run_id=run_id,
    )
