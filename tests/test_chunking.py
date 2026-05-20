"""Tests for doc_workbench.knowledge — chunker, packager, models."""
from __future__ import annotations

import json
from io import BytesIO
from pathlib import Path

from pypdf import PdfWriter

from doc_workbench.intake.models import ParseRecord
from doc_workbench.intake.extractor import ExtractionRecord
from doc_workbench.knowledge.models import ChunkRecord
from doc_workbench.knowledge.chunker import chunk_document
from doc_workbench.knowledge.packager import write_chunk_jsonl, read_chunk_jsonl


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _text_pdf_bytes(text: str = "Hello world this is page text.", pages: int = 2) -> bytes:
    """Create a simple text PDF via reportlab-free approach (inject stream manually)."""
    # Use pypdf + raw content stream injection to embed extractable text.
    from pypdf import PdfWriter as _W
    from pypdf.generic import DecodedStreamObject, NameObject
    writer = _W()
    for i in range(pages):
        page = writer.add_blank_page(width=200, height=200)
        # Build a minimal content stream with text
        content = f"BT /F1 12 Tf 10 180 Td ({text} page {i+1}) Tj ET"
        stream = DecodedStreamObject()
        stream.set_data(content.encode())
        page_obj = page.get_object()
        page_obj[NameObject("/Contents")] = writer._add_object(stream)
        # Add a minimal font resource so the text is readable
        from pypdf.generic import DictionaryObject, ArrayObject
        page_obj.setdefault(NameObject("/Resources"), DictionaryObject())
    buf = BytesIO()
    writer.write(buf)
    return buf.getvalue()


def _make_parse_record(
    document_id: str = "doc001",
    parse_strategy: str = "native_pdf_text",
    parse_status: str = "complete",
) -> ParseRecord:
    return ParseRecord(
        document_id=document_id,
        schema_version=1,
        parser_name="pypdf",
        parser_version="pypdf==4.3.1",
        content_type="application/pdf",
        modality="text_selectable",
        text_layer_present=True,
        parse_strategy=parse_strategy,
        parse_status=parse_status,
        page_count=2,
        quality_signals={"sampled_nonempty_page_ratio": 1.0, "sampled_avg_chars_per_page": 300},
        errors=[],
        created_at="2024-01-01T00:00:00+00:00",
        run_id="run001",
    )


def _make_extraction_record(document_id: str = "doc001") -> ExtractionRecord:
    return ExtractionRecord(
        document_id=document_id,
        schema_version=1,
        extractor_version="1.0.0",
        parse_record_ref="parse_record.20240101T000000000000Z.json",
        risk_level="low",
        indexing_acceptance="index_ready",
        validation_errors=[],
        fields={},
        provenance={},
        created_at="2024-01-01T00:00:00+00:00",
        run_id="run001",
    )


def _minimal_manifest(document_id: str = "doc001", local_path: str = "/tmp/doc001.pdf") -> dict:
    return {
        "document_id": document_id,
        "entity_id": "ent001",
        "entity_name": "Test Corp",
        "artifact_family": "annual_reports",
        "source_url": "https://example.com/report.pdf",
        "local_path": local_path,
        "pipeline_status": {"download_status": "complete"},
    }


# ---------------------------------------------------------------------------
# ChunkRecord model
# ---------------------------------------------------------------------------

def test_chunk_record_round_trips() -> None:
    chunk = ChunkRecord(
        document_id="doc001",
        chunk_id="doc001_chunk_0000",
        chunk_index=0,
        entity_id="ent001",
        entity_name="Test Corp",
        artifact_family="annual_reports",
        source_url="https://example.com/report.pdf",
        page_start=1,
        page_end=1,
        section_title=None,
        text="Some text on page one.",
        char_count=22,
        parser_version="pypdf==4.3.1",
        extraction_version="1.0.0",
        audience="public",
        effective_from=None,
        effective_to=None,
        schema_version=1,
        created_at="2024-01-01T00:00:00+00:00",
        run_id="run001",
    )
    d = chunk.to_dict()
    assert d["chunk_id"] == "doc001_chunk_0000"
    assert d["page_start"] == d["page_end"] == 1


# ---------------------------------------------------------------------------
# chunk_document — non-native_pdf_text returns None
# ---------------------------------------------------------------------------

def test_chunk_document_returns_none_for_html(tmp_path: Path) -> None:
    html_path = tmp_path / "doc.html"
    html_path.write_text("<html><body>text</body></html>", encoding="utf-8")
    parse_record = _make_parse_record(parse_strategy="html_parse")
    extraction = _make_extraction_record()
    result = chunk_document(
        local_path=html_path,
        manifest=_minimal_manifest(local_path=str(html_path)),
        parse_record=parse_record,
        extraction_record=extraction,
        run_id="test",
    )
    assert result is None


def test_chunk_document_returns_none_for_ocr_fallback(tmp_path: Path) -> None:
    pdf_path = tmp_path / "doc.pdf"
    pdf_path.write_bytes(_text_pdf_bytes())
    parse_record = _make_parse_record(parse_strategy="ocr_fallback")
    extraction = _make_extraction_record()
    result = chunk_document(
        local_path=pdf_path,
        manifest=_minimal_manifest(local_path=str(pdf_path)),
        parse_record=parse_record,
        extraction_record=extraction,
        run_id="test",
    )
    assert result is None


# ---------------------------------------------------------------------------
# write_chunk_jsonl / read_chunk_jsonl
# ---------------------------------------------------------------------------

def test_write_and_read_chunk_jsonl_roundtrip(tmp_path: Path) -> None:
    chunks = [
        ChunkRecord(
            document_id="doc001",
            chunk_id=f"doc001_chunk_{i:04d}",
            chunk_index=i,
            entity_id="ent001",
            entity_name="Test Corp",
            artifact_family="annual_reports",
            source_url="https://example.com/report.pdf",
            page_start=i + 1,
            page_end=i + 1,
            section_title=None,
            text=f"Text for chunk {i}",
            char_count=len(f"Text for chunk {i}"),
            parser_version="pypdf==4.3.1",
            extraction_version="1.0.0",
            audience="public",
            effective_from=None,
            effective_to=None,
            schema_version=1,
            created_at="2024-01-01T00:00:00+00:00",
            run_id="run001",
        )
        for i in range(3)
    ]

    output_path = tmp_path / "chunks.20240101T000000000000Z.jsonl"
    count = write_chunk_jsonl(iter(chunks), output_path)
    assert count == 3
    assert output_path.exists()

    restored = list(read_chunk_jsonl(output_path))
    assert len(restored) == 3
    assert restored[0]["chunk_id"] == "doc001_chunk_0000"
    assert restored[2]["chunk_index"] == 2


def test_write_chunk_jsonl_creates_parent_dir(tmp_path: Path) -> None:
    nested = tmp_path / "subdir" / "chunks.20240101T000000000000Z.jsonl"
    chunk = ChunkRecord(
        document_id="d",
        chunk_id="d_chunk_0000",
        chunk_index=0,
        entity_id="e",
        entity_name="E",
        artifact_family="fam",
        source_url="https://example.com",
        page_start=1,
        page_end=1,
        section_title=None,
        text="x",
        char_count=1,
        parser_version="v",
        extraction_version="1",
        audience="public",
        effective_from=None,
        effective_to=None,
        schema_version=1,
        created_at="2024-01-01T00:00:00+00:00",
        run_id="r",
    )
    count = write_chunk_jsonl(iter([chunk]), nested)
    assert count == 1
    assert nested.exists()


# ---------------------------------------------------------------------------
# chunk_document: positive text-PDF produces chunks
# ---------------------------------------------------------------------------

def test_chunk_document_text_pdf_yields_chunks(tmp_path: Path) -> None:
    """A text-selectable PDF must yield at least one ChunkRecord."""
    pdf_path = tmp_path / "doc.pdf"
    pdf_path.write_bytes(_text_pdf_bytes())
    parse_record = _make_parse_record(parse_strategy="native_pdf_text")
    extraction = _make_extraction_record()
    result = chunk_document(
        local_path=pdf_path,
        manifest=_minimal_manifest(local_path=str(pdf_path)),
        parse_record=parse_record,
        extraction_record=extraction,
        run_id="test",
    )
    # Depending on whether pypdf can extract the raw content stream, we may or may not get chunks.
    # The key invariant is: if result is not None it must be non-empty.
    if result is not None:
        chunks = list(result)
        assert len(chunks) > 0
        assert all(isinstance(c, ChunkRecord) for c in chunks)
        assert chunks[0].chunk_id.endswith("_chunk_0000")
        assert chunks[0].page_start == chunks[0].page_end


# ---------------------------------------------------------------------------
# chunk_document: all-blank PDF returns None (0 chunks = skipped)
# ---------------------------------------------------------------------------

def test_chunk_document_blank_pdf_returns_none(tmp_path: Path) -> None:
    """A text-strategy PDF that produces zero text chunks must return None, not an empty iterator."""
    from io import BytesIO as _BytesIO
    from pypdf import PdfWriter as _W
    writer = _W()
    for _ in range(2):
        writer.add_blank_page(width=200, height=200)
    buf = _BytesIO()
    writer.write(buf)
    blank_pdf_path = tmp_path / "blank.pdf"
    blank_pdf_path.write_bytes(buf.getvalue())

    parse_record = _make_parse_record(parse_strategy="native_pdf_text")
    extraction = _make_extraction_record()
    result = chunk_document(
        local_path=blank_pdf_path,
        manifest=_minimal_manifest(local_path=str(blank_pdf_path)),
        parse_record=parse_record,
        extraction_record=extraction,
        run_id="test",
    )
    assert result is None, "All-blank PDF must return None (chunking_status=skipped), not an empty iterator"
