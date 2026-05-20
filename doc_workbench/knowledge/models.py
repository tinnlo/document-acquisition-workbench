from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(slots=True)
class ChunkRecord:
    document_id: str
    chunk_id: str              # f"{document_id}_chunk_{index:04d}"
    chunk_index: int           # 0-based
    entity_id: str
    entity_name: str
    artifact_family: str
    source_url: str
    page_start: int | None     # 1-based page number
    page_end: int | None       # inclusive; same as page_start for single-page chunks
    section_title: str | None  # None for PDF; reserved for future HTML chunking
    text: str
    char_count: int
    parser_version: str        # from ParseRecord.parser_version
    extraction_version: str    # from ExtractionRecord.extractor_version
    audience: str              # "public" (default for this repo)
    effective_from: str | None # from ExtractionRecord.fields.reporting_period if available
    effective_to: str | None
    schema_version: int        # 1
    created_at: str
    run_id: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "document_id": self.document_id,
            "chunk_id": self.chunk_id,
            "chunk_index": self.chunk_index,
            "entity_id": self.entity_id,
            "entity_name": self.entity_name,
            "artifact_family": self.artifact_family,
            "source_url": self.source_url,
            "page_start": self.page_start,
            "page_end": self.page_end,
            "section_title": self.section_title,
            "text": self.text,
            "char_count": self.char_count,
            "parser_version": self.parser_version,
            "extraction_version": self.extraction_version,
            "audience": self.audience,
            "effective_from": self.effective_from,
            "effective_to": self.effective_to,
            "schema_version": self.schema_version,
            "created_at": self.created_at,
            "run_id": self.run_id,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ChunkRecord":
        return cls(
            document_id=data["document_id"],
            chunk_id=data["chunk_id"],
            chunk_index=data["chunk_index"],
            entity_id=data.get("entity_id", ""),
            entity_name=data.get("entity_name", ""),
            artifact_family=data.get("artifact_family", ""),
            source_url=data.get("source_url", ""),
            page_start=data.get("page_start"),
            page_end=data.get("page_end"),
            section_title=data.get("section_title"),
            text=data.get("text", ""),
            char_count=data.get("char_count", 0),
            parser_version=data.get("parser_version", ""),
            extraction_version=data.get("extraction_version", ""),
            audience=data.get("audience", "public"),
            effective_from=data.get("effective_from"),
            effective_to=data.get("effective_to"),
            schema_version=data.get("schema_version", 1),
            created_at=data.get("created_at", ""),
            run_id=data.get("run_id", ""),
        )
