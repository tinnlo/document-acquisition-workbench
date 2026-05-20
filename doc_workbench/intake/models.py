from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class ParseRecord:
    document_id: str
    schema_version: int           # always 1 for this schema release
    parser_name: str              # "pypdf" | "html_native" | "none"
    parser_version: str           # e.g. "pypdf==4.3.1"
    content_type: str             # mirrors manifest content_type
    modality: str                 # "text_selectable" | "image_or_unknown" | "html" | "unsupported"
    text_layer_present: bool
    parse_strategy: str           # "native_pdf_text" | "ocr_fallback" | "html_parse" | "skipped"
    parse_status: str             # "complete" | "partial" | "failed" | "skipped"
    page_count: int | None
    quality_signals: dict[str, Any]
    errors: list[str]
    created_at: str               # ISO 8601 UTC
    run_id: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "document_id": self.document_id,
            "schema_version": self.schema_version,
            "parser_name": self.parser_name,
            "parser_version": self.parser_version,
            "content_type": self.content_type,
            "modality": self.modality,
            "text_layer_present": self.text_layer_present,
            "parse_strategy": self.parse_strategy,
            "parse_status": self.parse_status,
            "page_count": self.page_count,
            "quality_signals": self.quality_signals,
            "errors": self.errors,
            "created_at": self.created_at,
            "run_id": self.run_id,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ParseRecord":
        return cls(
            document_id=data["document_id"],
            schema_version=data.get("schema_version", 1),
            parser_name=data.get("parser_name", ""),
            parser_version=data.get("parser_version", ""),
            content_type=data.get("content_type", ""),
            modality=data.get("modality", ""),
            text_layer_present=bool(data.get("text_layer_present", False)),
            parse_strategy=data.get("parse_strategy", ""),
            parse_status=data.get("parse_status", ""),
            page_count=data.get("page_count"),
            quality_signals=data.get("quality_signals") or {},
            errors=data.get("errors") or [],
            created_at=data.get("created_at", ""),
            run_id=data.get("run_id", ""),
        )
