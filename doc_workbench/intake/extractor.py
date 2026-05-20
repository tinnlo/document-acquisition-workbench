from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from doc_workbench.intake.models import ParseRecord

_EXTRACTOR_VERSION = "1.0.0"


@dataclass(slots=True)
class ExtractionRecord:
    document_id: str
    schema_version: int           # 1
    extractor_version: str        # semantic version string for the extractor logic
    parse_record_ref: str         # exact filename of the parse sidecar this was derived from
    indexing_acceptance: str      # "index_ready" | "needs_document_review" | "rejected_for_indexing"
    risk_level: str               # "low" | "medium" | "high"
    fields: dict[str, Any]        # title, issuer_name, reporting_period, publication_date,
                                  # page_count, modality, text_layer_present, parse_strategy,
                                  # quality_signals_summary
    validation_errors: list[str]
    provenance: dict[str, Any]    # source_url, entity_id, artifact_family, artifact_type,
                                  # content_hash
    created_at: str
    run_id: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "document_id": self.document_id,
            "schema_version": self.schema_version,
            "extractor_version": self.extractor_version,
            "parse_record_ref": self.parse_record_ref,
            "indexing_acceptance": self.indexing_acceptance,
            "risk_level": self.risk_level,
            "fields": self.fields,
            "validation_errors": self.validation_errors,
            "provenance": self.provenance,
            "created_at": self.created_at,
            "run_id": self.run_id,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ExtractionRecord":
        return cls(
            document_id=data["document_id"],
            schema_version=data.get("schema_version", 1),
            extractor_version=data.get("extractor_version", ""),
            parse_record_ref=data.get("parse_record_ref", ""),
            indexing_acceptance=data.get("indexing_acceptance", ""),
            risk_level=data.get("risk_level", ""),
            fields=data.get("fields") or {},
            validation_errors=data.get("validation_errors") or [],
            provenance=data.get("provenance") or {},
            created_at=data.get("created_at", ""),
            run_id=data.get("run_id", ""),
        )


def run_extraction(
    document_id: str,
    manifest: dict[str, Any],
    parse_record: ParseRecord,
    parse_record_filename: str,
    run_id: str,
    parse_validation_errors: list[str] | None = None,
) -> ExtractionRecord:
    """Derive an ExtractionRecord from a manifest + ParseRecord.

    parse_record_filename is passed explicitly so ExtractionRecord.parse_record_ref
    is always the exact filename written to disk — not re-derived.
    """
    now = datetime.now(timezone.utc).isoformat()
    meta = manifest.get("metadata") or {}
    # Seed validation_errors with any errors raised by validate_parse_record(),
    # so structural parse issues immediately block index_ready acceptance.
    validation_errors: list[str] = list(parse_validation_errors or [])

    # --- populate fields ---
    title = str(meta.get("title") or "").strip()
    issuer_name = str(meta.get("issuer_name") or "").strip()
    reporting_period = str(meta.get("reporting_period") or "").strip()
    publication_date = str(meta.get("publication_date") or "").strip()
    page_count = meta.get("page_count") or parse_record.page_count

    scan_status = (manifest.get("pipeline_status") or {}).get("metadata_scan_status", "")
    if scan_status != "complete":
        validation_errors.append(
            f"metadata_scan_status={scan_status!r}; scan has not run — "
            "title, issuer_name, reporting_period, publication_date may be absent"
        )

    if not title:
        validation_errors.append("title is empty or missing")
    if page_count is None or page_count <= 0:
        validation_errors.append(f"page_count is {page_count!r}")

    fields: dict[str, Any] = {
        "title": title,
        "issuer_name": issuer_name,
        "reporting_period": reporting_period,
        "publication_date": publication_date,
        "page_count": page_count,
        "modality": parse_record.modality,
        "text_layer_present": parse_record.text_layer_present,
        "parse_strategy": parse_record.parse_strategy,
        "quality_signals_summary": parse_record.quality_signals,
    }

    provenance: dict[str, Any] = {
        "source_url": str(manifest.get("source_url") or ""),
        "entity_id": str(manifest.get("entity_id") or ""),
        "artifact_family": str(manifest.get("artifact_family") or ""),
        "artifact_type": str(manifest.get("artifact_type") or ""),
        "content_hash": str(manifest.get("content_hash") or ""),
    }

    # --- risk scoring ---
    risk_level = _score_risk(parse_record, title, page_count, validation_errors)

    # --- acceptance classification ---
    indexing_acceptance = _classify_acceptance(parse_record, risk_level, validation_errors)

    return ExtractionRecord(
        document_id=document_id,
        schema_version=1,
        extractor_version=_EXTRACTOR_VERSION,
        parse_record_ref=parse_record_filename,
        indexing_acceptance=indexing_acceptance,
        risk_level=risk_level,
        fields=fields,
        validation_errors=validation_errors,
        provenance=provenance,
        created_at=now,
        run_id=run_id,
    )


def _score_risk(
    parse_record: ParseRecord,
    title: str,
    page_count: int | None,
    validation_errors: list[str],
) -> str:
    """Composite risk scoring.

    Signal table
    ------------
    parse_status == "failed"                        → high (immediate override)
    parse_status == "partial"                       → +medium
    text_layer_present == False                     → +medium
    title empty or missing                          → +medium
    page_count is None or <= 0                      → +low
    sampled_nonempty_page_ratio < 0.5               → +low
    any validation_errors                           → +low per error

    Final: any high → "high"; any medium → "medium"; else → "low"
    """
    if parse_record.parse_status == "failed":
        return "high"

    medium_signals: list[str] = []
    low_signals: list[str] = []

    if parse_record.parse_status == "partial":
        medium_signals.append("parse_status=partial")
    if not parse_record.text_layer_present:
        medium_signals.append("text_layer_present=False")
    if not title:
        medium_signals.append("title empty or missing")

    if page_count is None or page_count <= 0:
        low_signals.append(f"page_count={page_count!r}")

    ratio = parse_record.quality_signals.get("sampled_nonempty_page_ratio")
    if ratio is not None and ratio < 0.5:
        low_signals.append(f"sampled_nonempty_page_ratio={ratio}")

    for _ in validation_errors:
        low_signals.append("validation_error")

    if medium_signals:
        return "medium"
    if low_signals:
        return "low"
    return "low"


def _classify_acceptance(
    parse_record: ParseRecord,
    risk_level: str,
    validation_errors: list[str],
) -> str:
    """Three-state acceptance classification.

    rejected_for_indexing: parse_status=failed OR risk_level=high
    index_ready:           parse_status=complete AND risk_level=low AND no validation_errors
    needs_document_review: everything else
    """
    if parse_record.parse_status == "failed" or risk_level == "high":
        return "rejected_for_indexing"
    if (
        parse_record.parse_status == "complete"
        and risk_level == "low"
        and not validation_errors
    ):
        return "index_ready"
    return "needs_document_review"
