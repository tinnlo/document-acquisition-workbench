"""Tests for doc_workbench.intake.extractor."""
from __future__ import annotations

import dataclasses

from doc_workbench.intake.models import ParseRecord
from doc_workbench.intake.extractor import ExtractionRecord, run_extraction, _score_risk, _classify_acceptance


def _make_complete_parse_record(document_id: str = "doc001") -> ParseRecord:
    return ParseRecord(
        document_id=document_id,
        schema_version=1,
        parser_name="pypdf",
        parser_version="pypdf==4.3.1",
        content_type="application/pdf",
        modality="text_selectable",
        text_layer_present=True,
        parse_strategy="native_pdf_text",
        parse_status="complete",
        page_count=5,
        quality_signals={
            "sampled_nonempty_page_ratio": 1.0,
            "sampled_avg_chars_per_page": 500,
        },
        errors=[],
        created_at="2024-01-01T00:00:00+00:00",
        run_id="run001",
    )


def _minimal_manifest(document_id: str = "doc001") -> dict:
    return {
        "document_id": document_id,
        "entity_id": "ent001",
        "metadata": {"title": "Annual Report 2024", "year": "2024"},
        "content_type": "application/pdf",
        "local_path": "/tmp/doc001.pdf",
        "pipeline_status": {"download_status": "complete"},
        "scan": {"modality": "text_selectable", "page_count": 5},
    }


# ---------------------------------------------------------------------------
# ExtractionRecord model
# ---------------------------------------------------------------------------

def test_extraction_record_round_trips() -> None:
    rec = ExtractionRecord(
        document_id="d1",
        schema_version=1,
        extractor_version="1.0.0",
        parse_record_ref="parse_record.20240101T000000000000Z.json",
        risk_level="low",
        indexing_acceptance="index_ready",
        validation_errors=[],
        fields={},
        provenance={},
        created_at="2024-01-01T00:00:00+00:00",
        run_id="r1",
    )
    restored = ExtractionRecord.from_dict(rec.to_dict())
    assert restored.document_id == "d1"
    assert restored.indexing_acceptance == "index_ready"


# ---------------------------------------------------------------------------
# Risk scoring
# ---------------------------------------------------------------------------

def test_score_risk_failed_parse_is_high() -> None:
    record = dataclasses.replace(_make_complete_parse_record(), parse_status="failed")
    assert _score_risk(record, title="T", page_count=5, validation_errors=[]) == "high"


def test_score_risk_no_text_layer_is_medium() -> None:
    record = dataclasses.replace(_make_complete_parse_record(), text_layer_present=False)
    assert _score_risk(record, title="T", page_count=5, validation_errors=[]) == "medium"


def test_score_risk_complete_text_pdf_is_low() -> None:
    record = _make_complete_parse_record()
    assert _score_risk(record, title="T", page_count=5, validation_errors=[]) == "low"


# ---------------------------------------------------------------------------
# Acceptance classification
# ---------------------------------------------------------------------------

def test_classify_acceptance_complete_low_no_errors_is_index_ready() -> None:
    record = _make_complete_parse_record()
    result = _classify_acceptance(record, "low", [])
    assert result == "index_ready"


def test_classify_acceptance_failed_is_rejected() -> None:
    record = dataclasses.replace(_make_complete_parse_record(), parse_status="failed")
    result = _classify_acceptance(record, "high", [])
    assert result == "rejected_for_indexing"


def test_classify_acceptance_with_validation_errors_is_needs_review() -> None:
    record = _make_complete_parse_record()
    result = _classify_acceptance(record, "low", ["page_count is None"])
    assert result == "needs_document_review"


# ---------------------------------------------------------------------------
# run_extraction integration
# ---------------------------------------------------------------------------

def test_run_extraction_produces_extraction_record() -> None:
    parse_record = _make_complete_parse_record()
    manifest = _minimal_manifest()
    extraction = run_extraction(
        document_id="doc001",
        manifest=manifest,
        parse_record=parse_record,
        parse_record_filename="parse_record.20240101T000000000000Z.json",
        run_id="test",
    )
    assert extraction.document_id == "doc001"
    assert extraction.parse_record_ref == "parse_record.20240101T000000000000Z.json"
    assert extraction.indexing_acceptance in {"index_ready", "needs_document_review", "rejected_for_indexing"}
    assert extraction.risk_level in {"low", "medium", "high"}


def test_run_extraction_rejects_failed_parse() -> None:
    parse_record = dataclasses.replace(_make_complete_parse_record(), parse_status="failed")
    manifest = _minimal_manifest()
    extraction = run_extraction(
        document_id="doc001",
        manifest=manifest,
        parse_record=parse_record,
        parse_record_filename="parse_record.20240101T000000000000Z.json",
        run_id="test",
    )
    assert extraction.indexing_acceptance == "rejected_for_indexing"
    assert extraction.risk_level == "high"


# ---------------------------------------------------------------------------
# Parse validation errors gate indexing acceptance
# ---------------------------------------------------------------------------

def test_parse_validation_errors_block_index_ready() -> None:
    """parse_validation_errors passed to run_extraction must prevent index_ready."""
    parse_record = _make_complete_parse_record()
    manifest = _minimal_manifest()
    # Inject a parse-level validation error (e.g. modality/strategy mismatch found by validate_parse_record)
    extraction = run_extraction(
        document_id="doc001",
        manifest=manifest,
        parse_record=parse_record,
        parse_record_filename="parse_record.20240101T000000000000Z.json",
        run_id="test",
        parse_validation_errors=["modality/parse_strategy mismatch: modality='text_selectable' is not compatible with parse_strategy='ocr_fallback'"],
    )
    assert extraction.indexing_acceptance != "index_ready"
    assert any("mismatch" in e for e in extraction.validation_errors)


def test_parse_validation_errors_appear_in_extraction_record() -> None:
    """parse_validation_errors must be present in ExtractionRecord.validation_errors."""
    parse_record = _make_complete_parse_record()
    manifest = _minimal_manifest()
    extraction = run_extraction(
        document_id="doc001",
        manifest=manifest,
        parse_record=parse_record,
        parse_record_filename="parse_record.20240101T000000000000Z.json",
        run_id="test",
        parse_validation_errors=["quality_signals must be non-empty when parse_status is 'complete'"],
    )
    assert "quality_signals must be non-empty when parse_status is 'complete'" in extraction.validation_errors


def test_no_parse_validation_errors_preserves_index_ready() -> None:
    """When parse_validation_errors is empty (or None), a clean parse remains index_ready."""
    parse_record = _make_complete_parse_record()
    manifest = _minimal_manifest()
    # Add metadata_scan_status=complete to prevent scan-status validation error
    manifest["pipeline_status"]["metadata_scan_status"] = "complete"
    extraction_none = run_extraction(
        document_id="doc001",
        manifest=manifest,
        parse_record=parse_record,
        parse_record_filename="parse_record.20240101T000000000000Z.json",
        run_id="test",
        parse_validation_errors=None,
    )
    extraction_empty = run_extraction(
        document_id="doc001",
        manifest=manifest,
        parse_record=parse_record,
        parse_record_filename="parse_record.20240101T000000000000Z.json",
        run_id="test",
        parse_validation_errors=[],
    )
    assert extraction_none.indexing_acceptance == extraction_empty.indexing_acceptance
