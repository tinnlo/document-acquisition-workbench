from __future__ import annotations

from doc_workbench.intake.models import ParseRecord

# Map of (modality, parse_strategy) pairs that are valid together.
_VALID_PAIRS: set[tuple[str, str]] = {
    ("text_selectable", "native_pdf_text"),
    ("image_or_unknown", "ocr_fallback"),
    ("html", "html_parse"),
    ("unsupported", "skipped"),
}


def validate_parse_record(record: ParseRecord) -> list[str]:
    """Return a list of validation error strings. Empty list means valid."""
    errors: list[str] = []

    # 1. strategy/modality agreement
    pair = (record.modality, record.parse_strategy)
    if pair not in _VALID_PAIRS:
        errors.append(
            f"modality/parse_strategy mismatch: modality={record.modality!r} "
            f"is not compatible with parse_strategy={record.parse_strategy!r}"
        )

    # 2. page_count > 0 for PDF content when not skipped
    if record.parse_status != "skipped":
        is_pdf = "pdf" in (record.content_type or "").lower() or record.parse_strategy in (
            "native_pdf_text", "ocr_fallback"
        )
        if is_pdf:
            if record.page_count is None or record.page_count <= 0:
                errors.append(
                    f"page_count must be > 0 for PDF content when parse_status is not 'skipped'; "
                    f"got page_count={record.page_count!r}"
                )

    # 3. quality_signals non-empty when complete
    if record.parse_status == "complete" and not record.quality_signals:
        errors.append(
            "quality_signals must be non-empty when parse_status is 'complete'"
        )

    return errors
