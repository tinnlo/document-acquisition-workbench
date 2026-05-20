from __future__ import annotations

from datetime import datetime, timezone
from io import BytesIO
from itertools import chain
from pathlib import Path
from typing import Any, Iterator

from doc_workbench.intake.extractor import ExtractionRecord
from doc_workbench.intake.models import ParseRecord
from doc_workbench.knowledge.models import ChunkRecord


def chunk_document(
    local_path: Path,
    manifest: dict[str, Any],
    parse_record: ParseRecord,
    extraction_record: ExtractionRecord,
    run_id: str,
) -> Iterator[ChunkRecord] | None:
    """Return an Iterator[ChunkRecord] for text PDFs, or None for non-chunkable artifacts.

    Returns None if the parse strategy is not ``native_pdf_text`` or if the PDF
    produces zero text chunks (all pages blank/unextractable).
    Callers must treat None as ``chunking_status=skipped``.

    The iterator is true-streaming: pages are processed lazily.  A single-item
    look-ahead is used to distinguish an empty result from a non-empty one
    without buffering the full document.
    """
    if parse_record.parse_strategy != "native_pdf_text":
        return None

    raw = _iter_pdf_chunks(local_path, manifest, parse_record, extraction_record, run_id)
    try:
        first = next(raw)
    except StopIteration:
        # No chunks produced (e.g. all pages are blank).
        return None
    return chain([first], raw)


def _iter_pdf_chunks(
    local_path: Path,
    manifest: dict[str, Any],
    parse_record: ParseRecord,
    extraction_record: ExtractionRecord,
    run_id: str,
) -> Iterator[ChunkRecord]:
    from pypdf import PdfReader

    now = datetime.now(timezone.utc).isoformat()
    document_id = parse_record.document_id
    entity_id = str(manifest.get("entity_id") or "")
    entity_name = str(manifest.get("entity_name") or "")
    artifact_family = str(manifest.get("artifact_family") or "")
    source_url = str(manifest.get("source_url") or "")
    reporting_period = str(extraction_record.fields.get("reporting_period") or "")

    reader = PdfReader(BytesIO(local_path.read_bytes()))
    chunk_index = 0
    for page_number, page in enumerate(reader.pages):
        try:
            text = (page.extract_text() or "").strip()
        except Exception:
            text = ""
        if not text:
            continue
        page_1based = page_number + 1
        yield ChunkRecord(
            document_id=document_id,
            chunk_id=f"{document_id}_chunk_{chunk_index:04d}",
            chunk_index=chunk_index,
            entity_id=entity_id,
            entity_name=entity_name,
            artifact_family=artifact_family,
            source_url=source_url,
            page_start=page_1based,
            page_end=page_1based,
            section_title=None,
            text=text,
            char_count=len(text),
            parser_version=parse_record.parser_version,
            extraction_version=extraction_record.extractor_version,
            audience="public",
            effective_from=reporting_period or None,
            effective_to=None,
            schema_version=1,
            created_at=now,
            run_id=run_id,
        )
        chunk_index += 1
