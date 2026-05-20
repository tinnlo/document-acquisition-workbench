from __future__ import annotations

from io import BytesIO
from pathlib import Path
from typing import Any


# Threshold: average chars per page across sampled pages.
# Below this → image/scanned PDF; native text extraction is unreliable.
_TEXT_THRESHOLD = 100
# Maximum pages to sample for strategy detection (keep detection fast).
_SAMPLE_MAX_PAGES = 10


def detect_parse_strategy(
    content_type: str,
    local_path: Path,
) -> tuple[str, str, bool, dict[str, Any]]:
    """Return (modality, parse_strategy, text_layer_present, quality_signals).

    Decision table
    --------------
    content_type=application/pdf, sampled_avg_chars_per_page >= 100
        → text_selectable, native_pdf_text, True
    content_type=application/pdf, sampled_avg_chars_per_page < 100
        → image_or_unknown, ocr_fallback, False
    content_type=text/html (or .html extension)
        → html, html_parse, True
    anything else
        → unsupported, skipped, False
    """
    normalized_ct = (content_type or "").split(";")[0].strip().lower()
    path_lower = str(local_path).lower()

    if "html" in normalized_ct or path_lower.endswith(".html") or path_lower.endswith(".htm"):
        return "html", "html_parse", True, {"heading_count": 0, "paragraph_count": 0}

    if "pdf" not in normalized_ct and not path_lower.endswith(".pdf"):
        return "unsupported", "skipped", False, {}

    # PDF — sample up to _SAMPLE_MAX_PAGES pages.
    try:
        from pypdf import PdfReader
        reader = PdfReader(BytesIO(local_path.read_bytes()))
    except Exception:
        return "image_or_unknown", "ocr_fallback", False, {}

    pages = reader.pages
    sample_pages = pages[:_SAMPLE_MAX_PAGES]
    sampled_count = len(sample_pages)

    char_counts: list[int] = []
    empty_page_count = 0
    for page in sample_pages:
        try:
            text = (page.extract_text() or "").strip()
        except Exception:
            text = ""
        chars = len(text)
        char_counts.append(chars)
        if chars == 0:
            empty_page_count += 1

    sampled_nonempty = sampled_count - empty_page_count
    sampled_nonempty_ratio = sampled_nonempty / sampled_count if sampled_count > 0 else 0.0
    sampled_avg_chars = sum(char_counts) / sampled_count if sampled_count > 0 else 0.0

    quality_signals: dict[str, Any] = {
        "sampled_pages": sampled_count,
        "sampled_nonempty_page_ratio": round(sampled_nonempty_ratio, 4),
        "sampled_avg_chars_per_page": round(sampled_avg_chars, 2),
        "empty_page_count": empty_page_count,
    }

    if sampled_avg_chars >= _TEXT_THRESHOLD:
        return "text_selectable", "native_pdf_text", True, quality_signals
    else:
        return "image_or_unknown", "ocr_fallback", False, quality_signals
