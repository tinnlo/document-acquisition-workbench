# IMPLEMENTATION_PLAN.md

## Overview

Extend `document-acquisition-workbench` from a **safe document downloader and review queue** into
a **safe document intake workbench** — one that produces versioned parse/extraction artifacts and
retrieval-ready chunk records.

The existing acquisition, review, registry, and trace contracts are unchanged. All new behaviour
is additive.

---

## Confirmed Decisions

| # | Decision |
|---|---|
| 1 | CLI: `doc-workbench analyze` (parse + extract merged) + `doc-workbench chunk` (two new commands) |
| 2 | Chunking: page-based only for text PDFs in this phase; HTML chunking deferred (see Non-Goals) |
| 3 | Sidecar versioning: timestamp-based filenames with microsecond precision — never overwritten |
| 4 | Both `README.md` and `docs/architecture.md` exist and must be kept in sync after implementation |
| 5 | LangGraph parity: new node functions (`parse_node`, `extract_node`, `chunk_node`) exist as modules; `--engine` flag not added to `analyze`/`chunk` CLI in this phase (see Stage 6) |

---

## Repo Context (as of this plan)

### Existing strengths this plan builds on

| Existing capability | How this plan extends it |
|---|---|
| `scan_pdf()` in `metadata_scanner.py` distinguishes `text_selectable` vs `image_or_unknown` | `detector.py` deepens the modality signal into a full parse strategy decision |
| `metadata.json` manifest has a stable `pipeline_status` dict | Two new keys added: `parse_status`, `chunking_status`; acceptance lives in the sidecar only |
| `pypdf` and `beautifulsoup4` are already runtime dependencies | Used as the sole base implementations; no new mandatory deps |
| `DocumentRegistry` has `update_manifest()` and hardened path containment | Extended with `ensure_analysis_dir()`, `write_analysis_sidecar()`, `list_analysis_sidecars()` using the same security model |
| `RunTrace` sidecar per run | `analyze` and `chunk` commands emit the same trace sidecar pattern |
| Two-engine parity (legacy + LangGraph) | Three new intake node functions defined; CLI uses legacy path only; parity is at function level |

### Current gaps addressed

1. Repo stops at acquisition — no post-download intake layer
2. No versioned extraction record separate from `metadata.json`
3. No parse-strategy routing (`text_layer_present` → native; `image_or_unknown` → OCR interface)
4. No chunk-level provenance for retrieval
5. No second acceptance gate ("is this document safe for downstream indexing?")

---

## New Artifact Contract

### Registry layout (additive)

```
workspace/registry/
  <entity_dir>/
    <family>/<year>/<type>/
      <document_id>/
        artifact.pdf              ← unchanged
        metadata.json             ← unchanged; two new pipeline_status keys added
        analysis/                 ← new directory
          parse_record.<ts>.json
          extraction_record.<ts>.json
          chunks.<ts>.jsonl
```

Timestamp format: `YYYYMMDDTHHMMSSffffffZ` (UTC, microsecond precision — e.g.
`20260519T143022123456Z`). Microseconds make collisions negligible; the sidecar writer
additionally retries with a fresh timestamp if the generated filename already exists.
Files are never overwritten.

Re-running `analyze` produces a new timestamped file alongside the previous one.

### Run-level artifacts (new)

```
workspace/runs/analyze_<timestamp>/
  analyze_results.json
  analyze_summary.csv
  resolved_execution_policy.json
  <run_id>.json              ← RunTrace sidecar

workspace/runs/chunk_<timestamp>/
  chunk_results.json
  resolved_execution_policy.json
  <run_id>.json              ← RunTrace sidecar
```

### `metadata.json` additions (backwards-compatible)

Two new keys are added to `pipeline_status`. Existing keys are unchanged.
Consumers reading old manifests that lack these keys must treat absent keys as `"pending"`.

```json
"pipeline_status": {
  "download_status": "complete",
  "metadata_scan_status": "complete",
  "parse_status": "pending",
  "chunking_status": "pending"
}
```

`parse_status` values: `"pending"` | `"complete"` | `"partial"` | `"failed"` | `"skipped"`
`chunking_status` values: `"pending"` | `"complete"` | `"failed"` | `"skipped"`

Note: indexing acceptance (`index_ready` / `needs_document_review` / `rejected_for_indexing`)
lives in the `ExtractionRecord` sidecar — not in `metadata.json`. That is an intentional
separation: the manifest holds stable operational facts; derived risk classification is
versioned separately.

---

## Why Canonical Manifest And Versioned Extraction Records Are Separate

### Canonical manifest (`metadata.json`)

The manifest holds stable operational facts that the current pipeline already depends on:

- `document_id`, `entity_id`, `entity_name`, `source_url`
- `artifact_family`, `artifact_type`, `content_type`, `local_path`
- pipeline status fields
- latest approved summary metadata (title, issuer, page count, etc.)

Commands like `scan`, `download`, and `registry.find_by_source_url()` read this file.
Its keys must remain stable across pipeline versions.

### Versioned extraction records (`analysis/*.json`)

Derived analysis lives separately because:

- it is **revisable** — re-running a parser with a newer version should not overwrite history
- it is **parser-versioned** — two records for the same document can differ if parser logic changes
- it is **richer** — risk signals, validation errors, acceptance classification, and quality signals
  do not belong in the operational manifest
- it is **append-only** — every new run adds a timestamped file; no existing record is mutated

### Production analogy (documentation only — not implemented here)

This public repo uses local JSON sidecars. A production system would map these artifacts as:

| Local (this repo) | Production analogy |
|---|---|
| `metadata.json` | canonical row in a documents table |
| `analysis/parse_record.<ts>.json` | append-only row in a parse events log |
| `analysis/extraction_record.<ts>.json` | versioned record in an extractions store |
| `analysis/chunks.<ts>.jsonl` | rows in a chunks + retrieval index |
| raw artifact file | object storage keyed by content hash |

This mapping is documented to explain the design intent. No database, no vector store, and no
remote backend is added to this repo.

---

## Stage 1 — Parse-Aware Intake Layer

**Status:** Complete
**Goal:** Post-download parse routing that produces `parse_record.<ts>.json` under `analysis/`.

**Success criteria:**
- [x] Text PDFs route to `native_pdf_text` strategy
- [x] Image/scanned PDFs route to `ocr_fallback` with `parse_status=failed` (no crash, informative error)
- [x] HTML routes to `html_parse`; HTML artifacts are parsed but not chunked in this phase
- [x] Non-PDF/HTML produce `parse_status=skipped`
- [x] `analysis/parse_record.<ts>.json` created; second run creates a second file (first unchanged)
- [x] `metadata.json` gains `pipeline_status.parse_status`
- [x] Artifact path and sidecar paths are validated inside registry root before any read/write

### New files

#### `doc_workbench/intake/__init__.py`
Empty module marker.

#### `doc_workbench/intake/models.py`

```python
from __future__ import annotations
from dataclasses import dataclass
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
```

`quality_signals` keys for PDFs: `sampled_pages`, `sampled_nonempty_page_ratio`,
`sampled_avg_chars_per_page`, `empty_page_count`.

For HTML: `heading_count`, `paragraph_count`.

Note: `text_coverage_ratio` is intentionally dropped. Sampling over a few pages produces an
unreliable denominator. Prefer the explicit sampled metrics above for risk decisions.

#### `doc_workbench/intake/detector.py`

Key public function:

```python
def detect_parse_strategy(
    content_type: str,
    local_path: Path,
) -> tuple[str, str, bool, dict[str, Any]]:
    """Returns (modality, parse_strategy, text_layer_present, quality_signals)."""
```

Decision rules:

| `content_type` | Condition | `modality` | `parse_strategy` | `text_layer_present` |
|---|---|---|---|---|
| `application/pdf` | `sampled_avg_chars_per_page >= 100` | `text_selectable` | `native_pdf_text` | `True` |
| `application/pdf` | `sampled_avg_chars_per_page < 100` | `image_or_unknown` | `ocr_fallback` | `False` |
| `text/html` | always | `html` | `html_parse` | `True` |
| anything else | always | `unsupported` | `skipped` | `False` |

Quality signals populated: `sampled_pages`, `sampled_nonempty_page_ratio`,
`sampled_avg_chars_per_page`, `empty_page_count`.

For PDFs: sample up to the first 10 pages to keep detection fast.

#### `doc_workbench/intake/parser.py`

```python
class OcrBackend(Protocol):
    """Contract for a future OCR implementation. Not bundled in this phase."""
    def extract_text(self, path: Path, page_number: int) -> str: ...

def run_parse(
    document_id: str,
    local_path: Path,
    manifest: dict,
    run_id: str,
    ocr_backend: OcrBackend | None = None,
) -> ParseRecord:
    """Route artifact to correct parse path and return a ParseRecord."""
```

Routing:
- `native_pdf_text`: iterate `pypdf.PdfReader` pages; collect per-page text; compute quality signals
- `html_parse`: open with `BeautifulSoup`; extract headings + `<p>` text; record heading count
- `ocr_fallback`: if `ocr_backend` provided — delegate; else — `parse_status="failed"`,
  `errors=["OCR backend not configured; OCR is not included in this phase"]`
- `skipped`: immediate `ParseRecord` with `parse_status="skipped"`, empty quality signals

#### `doc_workbench/intake/validation.py`

```python
def validate_parse_record(record: ParseRecord) -> list[str]:
    """Returns list of validation error strings. Empty list = valid."""
```

Checks:
- `page_count > 0` for PDF content types when `parse_status != "skipped"`
- `parse_strategy` and `modality` agree (per decision table above)
- `quality_signals` is non-empty when `parse_status == "complete"`

### Modified files

#### `doc_workbench/registry/document_registry.py`

Add three methods (implementation detail in Stage 5):

```python
def ensure_analysis_dir(self, document_id: str) -> Path
def write_analysis_sidecar(self, document_id: str, basename: str, data: dict) -> Path
def list_analysis_sidecars(self, document_id: str, basename: str) -> list[Path]
```

Full security contract specified in Stage 5.

---

## Stage 2 — Versioned Extraction Record

**Status:** Complete
**Goal:** Typed, versioned `extraction_record.<ts>.json` with composite risk signal and
post-download acceptance classification.

**Success criteria:**
- [x] `ExtractionRecord.fields` captures all acquisition-domain fields from manifest + parse record
- [x] Risk level derived from composite signals, not a single confidence score
- [x] `indexing_acceptance` is one of: `index_ready` | `needs_document_review` | `rejected_for_indexing`
- [x] `analysis/extraction_record.<ts>.json` created; re-run creates new timestamped file
- [x] `metadata.json` is NOT modified by extraction (acceptance lives in sidecar only)

### Prerequisite: `analyze` and `scan` ordering

`run_extraction()` reads fields from `manifest["metadata"]` (title, issuer_name, page_count,
reporting_period, publication_date). These fields are populated by `scan`. If `scan` has not
run, `manifest["metadata"]` values will be absent or empty.

**Rule:** `analyze` does not require `metadata_scan_status=complete`. When metadata fields are
absent (scan has not run), `run_extraction()` fills them as empty strings / `None` and records a
`validation_error` noting that scan has not been run. Risk scoring treats a missing title as a
medium-risk signal, so a successful parse on an un-scanned document will produce
`needs_document_review`. A failed parse always produces `rejected_for_indexing` regardless of
scan status — the scan ordering rule applies only when `parse_status=complete`.

### New file

#### `doc_workbench/intake/extractor.py`

```python
@dataclass(slots=True)
class ExtractionRecord:
    document_id: str
    schema_version: int           # 1
    extractor_version: str        # semantic version string for the extractor logic
    parse_record_ref: str         # exact filename of the parse sidecar this was derived from
    indexing_acceptance: str      # "index_ready" | "needs_document_review" | "rejected_for_indexing"
    risk_level: str               # "low" | "medium" | "high"
    fields: dict[str, Any]        # title, issuer_name, reporting_period, publication_date, page_count, modality
    validation_errors: list[str]
    provenance: dict[str, Any]    # source_url, entity_id, artifact_family, artifact_type, content_hash
    created_at: str
    run_id: str

def run_extraction(
    document_id: str,
    manifest: dict,
    parse_record: ParseRecord,
    parse_record_filename: str,   # passed explicitly so ExtractionRecord.parse_record_ref is exact
    run_id: str,
) -> ExtractionRecord:
```

`fields` is populated from:
- `manifest["metadata"]` keys: title, issuer_name, reporting_period, publication_date, page_count
- `parse_record`: modality, text_layer_present, parse_strategy, quality_signals summary
- Absent/None manifest metadata fields are recorded as empty string / `None`; a validation error
  is added when a required field is absent

### Risk scoring (composite, explicit rules)

| Signal | Risk contribution |
|---|---|
| `parse_status == "failed"` | → `high` (immediate override) |
| `parse_status == "partial"` | +`medium` |
| `text_layer_present == False` | +`medium` |
| `title` empty or missing | +`medium` |
| `page_count` is `None` or `<= 0` | +`low` |
| `sampled_nonempty_page_ratio < 0.5` | +`low` |
| any `validation_errors` | +`low` per error |

Final: any `high` signal → `risk_level="high"`; one or more `medium` signals → `"medium"`;
else → `"low"`.

Note: `sampled_nonempty_page_ratio` replaces the previous `text_coverage_ratio` signal for
risk decisions. It is a direct count of sampled non-empty pages, not a derived ratio with a
weak denominator.

### Acceptance classification

| Condition | `indexing_acceptance` |
|---|---|
| `parse_status == "failed"` OR `risk_level == "high"` | `rejected_for_indexing` |
| `parse_status == "complete"` AND `risk_level == "low"` AND `validation_errors == []` | `index_ready` |
| everything else | `needs_document_review` |

---

## Stage 3 — Retrieval-Ready Chunking

**Status:** Complete
**Goal:** `chunks.<ts>.jsonl` with per-chunk provenance for text PDFs accepted as `index_ready`.

**Scope in this phase:** Text PDF chunking only (page-based). HTML parsing runs in `analyze`
but HTML chunking is deferred. See Non-Goals.

**Success criteria:**
- [x] Each chunk has all provenance fields (document_id, entity_id, source_url, page_start, page_end)
- [x] Text PDFs: one `ChunkRecord` per non-empty page
- [x] HTML artifacts: `chunking_status=skipped`, **no `chunks.<ts>.jsonl` written**
- [x] Scanned/unsupported: `chunking_status=skipped`, **no `chunks.<ts>.jsonl` written**
- [x] Parse-failed / parse-skipped / parse-partial documents: `chunking_status=skipped`; no document
      visited by `chunk` remains at `chunking_status=pending` after the run
- [x] `metadata.json` gains `pipeline_status.chunking_status`
- [x] JSONL written line-by-line (does not accumulate full list in memory before writing)
- [x] `chunk` resolves latest extraction record via `parse_record_ref` chain, not by filename sort alone

### How `chunk` identifies which sidecars to use

The `chunk` command loads the latest `extraction_record.<ts>.json` (most recent by timestamp in
filename) for each document. The `ExtractionRecord.parse_record_ref` field names the exact parse
sidecar that extraction was derived from. The chunker reads that specific parse sidecar. This
avoids ambiguity when multiple parse sidecars exist for the same document.

**`parse_record_ref` validation contract**

`parse_record_ref` is a value loaded from a sidecar file and must not be trusted as a safe path
without validation. Before resolving it to a filesystem path, the `chunk` command must:

1. Validate `parse_record_ref` matches the strict sidecar filename regex:
   `^parse_record\.\d{8}T\d{6}\d+Z\.json$` — raise `ValueError` on mismatch
2. Strip any directory components (basename only); the ref must be a filename, not a path
3. Join the validated basename to `<analysis_dir>` and resolve via `Path.resolve()`
4. Re-check that the resolved path is inside the registry root (containment assertion)
5. Verify the file exists; if missing or invalid, record an error in `chunk_results.json`
   and set `chunking_status=failed` for that document — do not crash

If any validation step fails, skip that document with an error entry; do not propagate
the exception to other documents in the same run.

### New files

#### `doc_workbench/knowledge/__init__.py`
Empty module marker.

#### `doc_workbench/knowledge/models.py`

```python
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
```

#### `doc_workbench/knowledge/chunker.py`

```python
def chunk_document(
    local_path: Path,
    manifest: dict,
    parse_record: ParseRecord,
    extraction_record: ExtractionRecord,
    run_id: str,
) -> Iterator[ChunkRecord] | None:
    """
    Returns an Iterator[ChunkRecord] for text PDFs (stream to JSONL).
    Returns None for non-chunkable artifacts (HTML, scanned, unsupported).
    Never returns an empty iterator — callers must treat None as chunking_status=skipped.
    """
```

Returns an iterator (not a list) for text PDFs so the packager can stream-write JSONL without
buffering all chunks. Returns `None` — never an empty iterator — for non-chunkable strategies;
this makes the caller's branching explicit and avoids accidentally writing an empty JSONL file.
Callers must consume the iterator exactly once.

Routing:
- `parse_strategy == "native_pdf_text"`: use `pypdf.PdfReader`; yield one `ChunkRecord` per
  page with non-empty text; `page_start == page_end == (page_index + 1)` (1-based)
- anything else (including `html_parse`, `ocr_fallback`, `skipped`): return `None` (sentinel
  meaning "not chunkable"); the CLI caller must **not** write any `chunks.<ts>.jsonl` for this
  document; it sets `chunking_status=skipped` and records the reason in `chunk_results.json`

`chunking_status=skipped` means no chunk sidecar file was created. An empty JSONL is never
written. Downstream consumers must treat an absent `chunks.<ts>.jsonl` as equivalent to
`chunking_status=skipped` in the manifest.

#### `doc_workbench/knowledge/packager.py`

```python
def write_chunk_jsonl(
    chunks: Iterator[ChunkRecord],
    output_path: Path,
) -> int:
    """Writes JSONL line-by-line from iterator. Returns chunk count written."""

def read_chunk_jsonl(path: Path) -> Iterator[dict]:
    """Yields one parsed dict per line without loading the full file."""
```

---

## Stage 4 — CLI Commands

**Status:** Complete

### `doc-workbench analyze`

```
doc-workbench analyze
  --entity-id TEXT         Target a single entity by ID
  --all                    Process all registry entries with download_status=complete
  --workspace-root PATH    [default: workspace]
  --force                  Re-run even if parse_status=complete (writes new timestamped sidecar)
  --execution-policy-path PATH
```

Behaviour:
1. Enforce execution policy (`analyze` must be in `allowed_command_stages`)
2. **Fail-closed target selection:** require exactly one of `--all` or `--entity-id`; raise
   `typer.BadParameter` if neither (or both) are given — same rule as `scan`
3. Load manifests for target documents
4. **Before reading any artifact:** resolve and validate artifact path is inside registry root;
   check file size against `download.max_file_size_bytes`; reject symlinks
5. Skip documents where `download_status != "complete"` (warn in output)
6. Skip documents where `parse_status == "complete"` and `--force` is not set (info in output)
7. For each document: run `detector` → `run_parse` → `validate_parse_record` → `run_extraction`
8. **Before writing any sidecar:** validate sidecar target path is inside registry root
9. Write `analysis/parse_record.<ts>.json` and `analysis/extraction_record.<ts>.json`
10. Update `metadata.json` `pipeline_status.parse_status`
11. Write `analyze_results.json`, `analyze_summary.csv`, trace sidecar

`analyze_results.json` schema:
```json
{
  "run_id": "...",
  "command": "analyze",
  "timestamp": "...",
  "processed": [
    {
      "document_id": "...",
      "entity_id": "...",
      "parse_status": "complete",
      "indexing_acceptance": "index_ready",
      "risk_level": "low",
      "parse_record_file": "parse_record.20260519T143022123456Z.json",
      "extraction_record_file": "extraction_record.20260519T143022123456Z.json"
    }
  ],
  "skipped": [...],
  "errors": [...]
}
```

### `doc-workbench chunk`

```
doc-workbench chunk
  --entity-id TEXT
  --all
  --workspace-root PATH
  --force
  --execution-policy-path PATH
```

Behaviour:
1. Enforce execution policy (`chunk` must be in `allowed_command_stages`)
2. **Fail-closed target selection:** require exactly one of `--all` or `--entity-id`; raise
   `typer.BadParameter` if neither (or both) are given — same rule as `scan`
3. Load manifests for the target scope (all, or filtered by entity-id)
4. **Before reading any sidecar or artifact:** resolve and validate artifact `local_path` is
   inside registry root; check file size against `download.max_file_size_bytes`; reject symlinks
   on both the artifact file and any sidecar files read. This mirrors the same guards in `scan`
   and `download` and must not be skipped even when the artifact was already validated at
   download time.
5. **Triage documents by `parse_status`:**
   - `parse_status` not `"complete"` (e.g. `"failed"`, `"partial"`, `"skipped"`, `"pending"`):
     set `chunking_status=skipped`; update manifest; record in results; skip to next document.
     These documents are not chunkable and must not remain at `chunking_status=pending`.
   - `parse_status == "complete"`: continue to step 6
6. Load latest `extraction_record.<ts>.json` (highest timestamp) for each document
7. Resolve `parse_record_ref` to the specific parse sidecar; validate that file exists
8. Skip documents where `indexing_acceptance != "index_ready"` (warn in output);
   set `chunking_status=skipped` for these documents; update manifest
9. Skip documents where `chunking_status == "complete"` and `--force` is not set (info in output)
10. For `index_ready` documents: call `chunk_document`
    - If it returns an iterator (text PDF): stream via `write_chunk_jsonl` to `analysis/chunks.<ts>.jsonl`
    - If it returns `None` (HTML, scanned, unsupported): **do not write a chunk file**;
      set `chunking_status=skipped`
11. Update `metadata.json` `pipeline_status.chunking_status` (`"complete"` or `"skipped"` or `"failed"`)
12. Write `chunk_results.json`, trace sidecar

**Final `chunking_status` mapping:**

| `parse_status` | `indexing_acceptance` | `chunk_document` result | `chunking_status` |
|---|---|---|---|
| not `complete` | n/a | n/a | `skipped` |
| `complete` | not `index_ready` | n/a | `skipped` |
| `complete` | `index_ready` | iterator | `complete` |
| `complete` | `index_ready` | `None` | `skipped` |
| `complete` | `index_ready` | write error | `failed` |

No document that has been visited by `chunk` should ever remain at `chunking_status=pending`.

---

## Stage 5 — DocumentRegistry Extensions

**Status:** Complete

### `ensure_analysis_dir(document_id) -> Path`

Creates `<doc_folder>/analysis/` if it does not exist. Returns the resolved path.

Security contract:
- Resolve the analysis dir path via `Path.resolve()`
- Assert the resolved path is within `self._registry_root.resolve()` (same containment check as
  existing `register_document`)
- Never follow symlinks: raise `ValueError` if any component of the path is a symlink

### `write_analysis_sidecar(document_id, basename, data) -> Path`

- Used for **JSON sidecars only** (`parse_record`, `extraction_record`); takes a `dict` payload
- **Not used for chunk JSONL** — `write_chunk_jsonl` in `packager.py` writes directly to a path
  returned by `ensure_analysis_dir()` to support streaming; `write_analysis_sidecar` does not
  need to handle `list` or iterator types
- `basename` must match `^[a-zA-Z0-9_]+$`; raise `ValueError` on invalid basename
- Generates timestamp suffix: `datetime.utcnow().strftime("%Y%m%dT%H%M%S%fZ")` (microsecond precision)
- Extension: always `.json`
- Writes to `<analysis_dir>/<basename>.<ts>.json`
- If the generated filename already exists (same-microsecond collision), retry with a fresh
  timestamp up to 3 times; raise `RuntimeError` if all retries collide
- Validate target path is inside registry root before opening for write
- Returns the path that was written

### `list_analysis_sidecars(document_id, basename) -> list[Path]`

- `basename` must match `^[a-zA-Z0-9_]+$`
- Iterates `analysis/` directory entries; matches only files matching
  `^<basename>\.\d{8}T\d{6}\d+Z\.(json|jsonl)$` (strict regex — no glob blobs)
- Resolves each path and validates containment before returning
- Rejects (skips with a warning) any symlinks in the listing
- Returns sorted list (oldest first by filename)
- Returns empty list if `analysis/` dir does not exist

---

## Stage 6 — LangGraph Parity

**Status:** Complete

### Scope and constraints

`analyze` and `chunk` CLI commands do not accept an `--engine` flag in this phase.
The legacy path is the only execution path for the two new commands.

LangGraph parity is implemented at the **node function level only**: the three new node
functions are added to `orchestration/nodes.py` and a new intake graph is defined in
`orchestration/graph.py`. This ensures the intake logic can be wired into a LangGraph flow
in a future phase without requiring CLI changes now.

No LangGraph-exclusive behaviour is permitted: each node delegates entirely to the same
`intake/` and `knowledge/` module functions used by the CLI.

### `orchestration/state.py` additions

The three new node functions need document selection inputs in state. Add the following
alongside the existing keys:

```python
class WorkbenchState(TypedDict, total=False):
    # ... all existing keys unchanged ...

    # --- intake document selection inputs (set before intake graph invocation) ---
    intake_document_ids: list[str]      # explicit list of document_ids to process; if set,
                                        # intake nodes operate on this list only
    intake_entity_id: str               # alternative: all docs for this entity_id
    intake_all: bool                    # alternative: all registry docs with download_status=complete
    intake_force: bool                  # re-run even if parse/chunking already complete
    intake_registry_root: Path          # resolved registry root (required for all intake nodes)
    intake_workspace_root: Path         # resolved workspace root (for exec_policy path checks)

    # --- intake node outputs ---
    parse_records: list[ParseRecord]            # new optional
    extraction_records: list[ExtractionRecord]  # new optional
    chunk_records: list[ChunkRecord]            # new optional
```

**Rule:** At least one of `intake_document_ids`, `intake_entity_id`, or `intake_all=True` must
be present in state before `parse_node` is called. Nodes raise `ValueError` if none are set.
This keeps the same module functions used by the CLI — no hidden side channels.

### `orchestration/nodes.py` additions

```python
def parse_node(state: WorkbenchState) -> dict:
    """
    Resolves target documents from state (intake_document_ids / intake_entity_id / intake_all).
    Delegates to run_parse() for each document.
    Applies the same artifact path + file-size guards as the CLI analyze command.
    Returns {'parse_records': list[ParseRecord]}.
    """

def extract_node(state: WorkbenchState) -> dict:
    """
    Runs run_extraction() over each ParseRecord in state['parse_records'].
    Returns {'extraction_records': list[ExtractionRecord]}.
    """

def chunk_node(state: WorkbenchState) -> dict:
    """
    Runs chunk_document() over index_ready ExtractionRecords in state['extraction_records'].
    Applies the same parse_record_ref validation and artifact path guards as the CLI chunk command.
    Returns {'chunk_records': list[ChunkRecord]}.
    """
```

### `orchestration/graph.py` additions

```python
def build_intake_graph() -> CompiledGraph:
    """parse_node → extract_node → chunk_node → END"""
```

---

## Stage 7 — Tests

**Status:** Complete

All tests follow existing repo patterns:
- `tmp_path` (pytest) for isolated filesystem
- Inline `_make_*()` factory helpers
- `typer.testing.CliRunner` for CLI tests
- No `@pytest.mark.asyncio` (async internals monkeypatched)
- `pytest.importorskip("langgraph")` for graph tests
- Permissive execution policy via `_write_permissive_exec_policy(tmp_path)` helper

### `tests/test_intake.py`

- `test_detect_text_pdf_routes_native_pdf_text`
- `test_detect_sparse_text_pdf_routes_ocr_fallback`
- `test_detect_html_routes_html_parse`
- `test_detect_unsupported_content_type_routes_skipped`
- `test_quality_signals_use_sampled_not_coverage_ratio`
- `test_parse_sidecar_written_under_analysis_dir`
- `test_parse_sidecar_timestamp_versioned_on_rerun` — two rapid reruns → two distinct files, first unchanged
- `test_ocr_fallback_without_backend_returns_failed_not_crash`
- `test_validate_parse_record_catches_strategy_modality_mismatch`
- `test_validate_parse_record_catches_zero_page_count`
- `test_sidecar_write_blocked_outside_registry_root` — traversal attempt raises ValueError
- `test_sidecar_symlink_rejected_on_write`
- `test_sidecar_symlink_rejected_on_list`

### `tests/test_extraction.py`

- `test_extraction_fields_populated_from_manifest_and_parse_record`
- `test_extraction_fields_empty_when_scan_not_run` — absent manifest metadata → empty fields + validation_error
- `test_analyze_before_scan_produces_needs_review_not_index_ready`
- `test_risk_high_on_parse_failed_status`
- `test_risk_medium_on_image_only_pdf`
- `test_risk_low_on_clean_complete_parse`
- `test_acceptance_index_ready_on_low_risk_complete_parse`
- `test_acceptance_rejected_on_failed_parse`
- `test_acceptance_needs_review_on_partial_parse`
- `test_extraction_sidecar_versioned_independently_of_parse`
- `test_provenance_fields_present`
- `test_parse_record_ref_is_exact_filename`

### `tests/test_chunking.py`

- `test_chunk_text_pdf_one_chunk_per_non_empty_page`
- `test_chunk_record_has_all_provenance_fields`
- `test_chunk_id_format_stable`
- `test_chunk_page_start_page_end_set_correctly`
- `test_html_parse_strategy_returns_none` — HTML parse_strategy → chunk_document returns None → chunking_status=skipped, no JSONL written
- `test_scanned_pdf_yields_empty_iterator`
- `test_chunk_jsonl_roundtrip`
- `test_write_chunk_jsonl_is_line_by_line` — verifies streaming write, not list accumulation
- `test_read_chunk_jsonl_streams_without_full_load`
- `test_chunk_command_resolves_parse_record_via_ref_not_sort`

### `tests/test_cli_analyze.py`

- `test_analyze_command_processes_downloaded_doc`
- `test_analyze_produces_parse_and_extraction_sidecars`
- `test_analyze_updates_pipeline_status_parse_status`
- `test_analyze_skips_doc_without_complete_download`
- `test_analyze_skips_already_analyzed_without_force`
- `test_analyze_force_flag_produces_second_timestamped_sidecar`
- `test_analyze_nonpdf_artifact_skips_cleanly`
- `test_analyze_validates_artifact_path_before_read`
- `test_analyze_rejects_symlinked_artifact` — symlinked artifact file → ValueError; document
  skipped with error entry; run continues
- `test_analyze_enforces_file_size_limit_on_artifact` — artifact exceeds `max_file_size_bytes`
  → PolicyViolationError before any read; consistent with scan/download behaviour
- `test_analyze_requires_all_or_entity_id` — neither `--all` nor `--entity-id` → BadParameter,
  exit code 2; same fail-closed rule as scan
- `test_analyze_old_manifest_missing_parse_status_treated_as_pending` — manifest without
  `parse_status` key → treated as `"pending"` (not KeyError); analyze runs normally
- `test_analyze_command_enforces_execution_policy_stage` — `analyze` not in
  `allowed_command_stages` → PolicyViolationError, exit code 2

### `tests/test_cli_chunk.py`

- `test_chunk_command_produces_jsonl_for_index_ready_doc`
- `test_chunk_command_skips_non_index_ready_docs_with_warning`
- `test_chunk_command_updates_chunking_status_in_manifest`
- `test_chunk_command_sets_chunking_status_failed_on_write_error`
- `test_chunk_command_skips_already_chunked_without_force`
- `test_chunk_command_rejects_traversal_parse_record_ref` — `parse_record_ref` with `../` or
  absolute path raises ValueError; document is skipped with error, run continues
- `test_chunk_command_error_when_parse_record_ref_file_missing` — referenced parse sidecar
  absent on disk → `chunking_status=failed` in manifest, error entry in `chunk_results.json`
- `test_chunk_command_rejects_symlinked_parse_sidecar` — symlink in analysis dir pointing to
  valid-looking filename → skipped with error; run continues
- `test_chunk_command_rejects_symlinked_artifact_local_path` — symlinked artifact file →
  ValueError raised; document skipped with error entry
- `test_chunk_command_enforces_file_size_limit_on_artifact` — oversized artifact → PolicyViolationError
  before any read; consistent with scan/download behaviour
- `test_chunk_sets_skipped_for_parse_failed_docs` — doc with `parse_status=failed` → `chunking_status=skipped`
  in manifest after chunk run; does not remain `pending`
- `test_chunk_sets_skipped_for_parse_skipped_docs` — doc with `parse_status=skipped` (non-PDF) →
  same; does not remain `pending`
- `test_chunk_rejects_symlinked_extraction_record` — symlink at `extraction_record.<ts>.json`
  location → skipped with error; run continues
- `test_chunk_old_manifest_missing_chunking_status_treated_as_pending` — manifest without
  `chunking_status` key → treated as `"pending"` (not KeyError); chunk runs normally
- `test_chunk_requires_all_or_entity_id` — neither `--all` nor `--entity-id` → BadParameter,
  exit code 2
- `test_chunk_command_enforces_execution_policy_stage` — `chunk` not in
  `allowed_command_stages` → PolicyViolationError, exit code 2

### Extend `tests/test_cli_flow.py`

- `test_review_download_analyze_chunk_end_to_end_flow`
- `test_missing_text_layer_does_not_crash_chunk_command`
- `test_analyze_before_scan_is_allowed_but_produces_needs_review`

### `tests/test_orchestration.py` additions (extend existing)

- `test_intake_graph_compiles`
- `test_parse_node_returns_parse_records_key`
- `test_parse_node_raises_without_document_selection_input` — no intake_document_ids /
  intake_entity_id / intake_all → ValueError before any I/O
- `test_chunk_node_skips_non_index_ready`
- `test_legacy_and_langgraph_intake_produce_same_sidecars`

---

## Stage 8 — Documentation

**Status:** Complete

Both files must be updated and kept in sync. Changes are additive.

### `README.md` additions

- Update CLI table with `analyze` and `chunk` commands
- Update artifact contract table with new `analysis/` sidecars
- New "Parse-Aware Intake" section (explains `analyze`, parse routing, `analysis/` dir)
- New "Retrieval-Ready Chunking" section (explains `chunk`, JSONL output, provenance fields)
- New "Why canonical manifest and extraction records are separate" callout
- Update "Where To Look First" table

### `docs/architecture.md` additions

- Extend "Command-to-Module Map" table with `analyze` and `chunk`
- New "Post-Download Intake" section: parse routing table, quality signals, OCR interface
- New "Analysis Sidecar Contract" section: timestamp format, no-overwrite rule, security model
- New "Canonical Manifest vs Versioned Extraction Records" section (the core distinction)
- New "Post-Download Acceptance Policy" section: three acceptance states, risk scoring rules,
  interaction with scan ordering
- New "Retrieval-Ready Chunking" section: routing rules, provenance fields, streaming write
- Extend LangGraph state model table with new optional keys
- Extend artifact surfaces table with `analyze` and `chunk` stages
- Note: HTML chunking is deferred to a future phase

---

## Full File Inventory

### New files

| File | Stage |
|---|---|
| `doc_workbench/intake/__init__.py` | 1 |
| `doc_workbench/intake/models.py` | 1 |
| `doc_workbench/intake/detector.py` | 1 |
| `doc_workbench/intake/parser.py` | 1 |
| `doc_workbench/intake/validation.py` | 1 |
| `doc_workbench/intake/extractor.py` | 2 |
| `doc_workbench/knowledge/__init__.py` | 3 |
| `doc_workbench/knowledge/models.py` | 3 |
| `doc_workbench/knowledge/chunker.py` | 3 |
| `doc_workbench/knowledge/packager.py` | 3 |
| `tests/test_intake.py` | 7 |
| `tests/test_extraction.py` | 7 |
| `tests/test_chunking.py` | 7 |
| `tests/test_cli_analyze.py` | 7 |
| `tests/test_cli_chunk.py` | 7 |

### Modified files

| File | Change |
|---|---|
| `doc_workbench/registry/document_registry.py` | Add `ensure_analysis_dir`, `write_analysis_sidecar`, `list_analysis_sidecars` |
| `doc_workbench/cli.py` | Add `analyze` and `chunk` commands |
| `doc_workbench/orchestration/state.py` | Add optional parse/extract/chunk state keys |
| `doc_workbench/orchestration/nodes.py` | Add `parse_node`, `extract_node`, `chunk_node` |
| `doc_workbench/orchestration/graph.py` | Add `build_intake_graph()` |
| `doc_workbench/context/execution_policy.yaml` | Add `analyze` and `chunk` to `allowed_command_stages` |
| `tests/test_cli_flow.py` | Extend with `analyze`/`chunk` integration tests |
| `pyproject.toml` | Add `[parsing]` optional extra (docling hook — future) |
| `README.md` | New sections |
| `docs/architecture.md` | New sections |

### Untouched files

`acquisition/`, `review/`, `storage/`, `providers/`, `observability/`, `evals/`,
`config.py`, `policy.py`, `execution_policy.py`, `http_utils.py`, all existing tests
(except `test_cli_flow.py` which is extended, not replaced).

---

## Non-Goals For This Phase

- No production database (no SQLAlchemy, no PostgreSQL client)
- No remote vector store
- No live OCR service dependency in default install
- No LLM calls in intake or chunking path
- No chat/ask product interface
- No HTML chunking (HTML is parsed in `analyze`; chunking of HTML deferred to a future phase)
- No rewrite of the existing acquisition/review pipeline
- No `--engine` flag on `analyze` or `chunk` (LangGraph parity is node-level only in this phase)

---

## Acceptance Criteria

Implementation is complete only when all of these are true:

- [x] All existing tests still pass
- [x] `doc-workbench analyze` produces `analysis/parse_record.<ts>.json` and
      `analysis/extraction_record.<ts>.json` without mutating existing manifest keys (other than
      the two new `pipeline_status` additions)
- [x] Re-running `analyze` writes a new timestamped file; previous file is unchanged
- [x] Parse routing is explicit: text PDFs → `native_pdf_text`; image PDFs → `ocr_fallback`
      (graceful failure without crash); HTML → `html_parse`
- [x] Artifact and sidecar paths are validated inside registry root before every read/write;
      symlinks are rejected; traversal attempts raise `ValueError`
- [x] `chunk` enforces file-size limit (`download.max_file_size_bytes`) on artifact `local_path`
      before reading — same guard as `scan` and `download`
- [x] Timestamp collision is handled: microsecond precision + retry on collision
- [x] `doc-workbench chunk` produces `analysis/chunks.<ts>.jsonl` with provenance fields for
      `index_ready` text-PDF documents
- [x] HTML and scanned/unsupported artifacts set `chunking_status=skipped` cleanly (no crash)
- [x] `chunk` resolves the correct parse record via `ExtractionRecord.parse_record_ref`
- [x] Quality signals use `sampled_nonempty_page_ratio` and `sampled_avg_chars_per_page`;
      `text_coverage_ratio` is not present
- [x] Risk scoring uses `sampled_nonempty_page_ratio` as the quality signal for risk decisions
- [x] Analyze-before-scan: a successful parse (`parse_status=complete`) on a document where
      `metadata_scan_status` is not `complete` produces `needs_document_review` (missing title
      is a medium-risk signal); a failed parse (`parse_status=failed`) still produces
      `rejected_for_indexing` regardless of scan status
- [x] Old manifests without `parse_status`/`chunking_status` are treated as `"pending"` without error
- [x] `README.md` and `docs/architecture.md` both explain the canonical manifest / versioned
      extraction record distinction and are kept in sync
- [x] All new behaviour is covered by deterministic tests using only local fixtures
- [x] No document visited by `chunk` remains at `chunking_status=pending` after the run; every
      document receives `complete`, `skipped`, or `failed`
- [x] LangGraph node functions (`parse_node`, `extract_node`, `chunk_node`) exist and delegate
      to the same modules as the CLI; nodes raise `ValueError` when no document selection input
      is present in state; intake graph compiles
