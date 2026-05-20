"""Tests for doc_workbench.intake — parser, models, detector, validation."""
from __future__ import annotations

from io import BytesIO
from pathlib import Path

import pytest
from pypdf import PdfWriter

from doc_workbench.intake.models import ParseRecord
from doc_workbench.intake.parser import run_parse
from doc_workbench.intake.validation import validate_parse_record


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _minimal_manifest(
    document_id: str = "doc001",
    content_type: str = "application/pdf",
    local_path: str = "/tmp/doc001.pdf",
    modality: str = "text_selectable",
    page_count: int = 3,
) -> dict:
    return {
        "document_id": document_id,
        "entity_id": "ent001",
        "metadata": {"title": "Test Report", "year": "2024"},
        "content_type": content_type,
        "local_path": local_path,
        "pipeline_status": {"download_status": "complete"},
        "scan": {"modality": modality, "page_count": page_count},
    }


# ---------------------------------------------------------------------------
# ParseRecord model
# ---------------------------------------------------------------------------

def test_parse_record_round_trips_via_dict() -> None:
    record = ParseRecord(
        document_id="d1",
        schema_version=1,
        parser_name="pypdf",
        parser_version="pypdf==4.3.1",
        content_type="application/pdf",
        modality="text_selectable",
        text_layer_present=True,
        parse_strategy="native_pdf_text",
        parse_status="complete",
        page_count=5,
        quality_signals={"sampled_nonempty_page_ratio": 1.0},
        errors=[],
        created_at="2024-01-01T00:00:00+00:00",
        run_id="run001",
    )
    restored = ParseRecord.from_dict(record.to_dict())
    assert restored.document_id == "d1"
    assert restored.parse_status == "complete"
    assert restored.quality_signals == {"sampled_nonempty_page_ratio": 1.0}


def test_parse_record_from_dict_tolerates_missing_optional_fields() -> None:
    data = {"document_id": "d2", "parse_status": "failed"}
    record = ParseRecord.from_dict(data)
    assert record.document_id == "d2"
    assert record.parse_status == "failed"
    assert record.errors == []
    assert record.quality_signals == {}


# ---------------------------------------------------------------------------
# HTML parse strategy returns None from chunker
# ---------------------------------------------------------------------------

def test_html_parse_strategy_returns_none(tmp_path: Path) -> None:
    """HTML artifacts: chunker returns None (non-chunkable)."""
    html_path = tmp_path / "doc.html"
    html_path.write_text("<html><body><p>hello</p></body></html>", encoding="utf-8")
    manifest = _minimal_manifest(
        document_id="html_doc",
        content_type="text/html",
        local_path=str(html_path),
        modality="html",
        page_count=0,
    )
    record = run_parse(
        document_id="html_doc",
        local_path=html_path,
        manifest=manifest,
        run_id="test",
    )
    assert record.parse_strategy == "html_parse"

    from doc_workbench.knowledge.chunker import chunk_document
    from doc_workbench.intake.extractor import run_extraction
    extraction = run_extraction(
        document_id="html_doc",
        manifest=manifest,
        parse_record=record,
        parse_record_filename="parse_record.20240101T000000000000Z.json",
        run_id="test",
    )
    result = chunk_document(
        local_path=html_path,
        manifest=manifest,
        parse_record=record,
        extraction_record=extraction,
        run_id="test",
    )
    assert result is None


# ---------------------------------------------------------------------------
# validate_parse_record
# ---------------------------------------------------------------------------

def test_validate_parse_record_returns_empty_for_complete_record() -> None:
    record = ParseRecord(
        document_id="d3",
        schema_version=1,
        parser_name="pypdf",
        parser_version="pypdf==4.3.1",
        content_type="application/pdf",
        modality="text_selectable",
        text_layer_present=True,
        parse_strategy="native_pdf_text",
        parse_status="complete",
        page_count=2,
        quality_signals={"sampled_nonempty_page_ratio": 1.0, "sampled_avg_chars_per_page": 500},
        errors=[],
        created_at="2024-01-01T00:00:00+00:00",
        run_id="run001",
    )
    errs = validate_parse_record(record)
    assert errs == []


def test_validate_parse_record_flags_modality_strategy_mismatch() -> None:
    """A modality/parse_strategy mismatch should produce a validation error."""
    record = ParseRecord(
        document_id="d-mismatch",
        schema_version=1,
        parser_name="pypdf",
        parser_version="pypdf==4.3.1",
        content_type="application/pdf",
        modality="html",                   # html modality …
        text_layer_present=False,
        parse_strategy="native_pdf_text",  # … paired with PDF strategy
        parse_status="complete",
        page_count=1,
        quality_signals={"sampled_nonempty_page_ratio": 1.0},
        errors=[],
        created_at="2024-01-01T00:00:00+00:00",
        run_id="r",
    )
    errs = validate_parse_record(record)
    assert any("mismatch" in e for e in errs)


def test_validate_parse_record_flags_missing_page_count() -> None:
    record = ParseRecord(
        document_id="d4",
        schema_version=1,
        parser_name="pypdf",
        parser_version="pypdf==4.3.1",
        content_type="application/pdf",
        modality="text_selectable",
        text_layer_present=True,
        parse_strategy="native_pdf_text",
        parse_status="complete",
        page_count=None,
        quality_signals={},
        errors=[],
        created_at="2024-01-01T00:00:00+00:00",
        run_id="r",
    )
    errs = validate_parse_record(record)
    assert any("page_count" in e for e in errs)
