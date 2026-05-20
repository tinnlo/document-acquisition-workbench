# HANDOVER

## Goal

Use this repo's existing acquisition, review, registry, and trace contract as the base for a next-phase implementation that demonstrates two stronger ideas:

1. **Part 1 adaptation: parse-aware document intake**
   The pipeline should not stop at "we downloaded a PDF." It should produce a validated, versioned record of how the document was parsed, what structured fields were extracted, and whether the artifact is safe for downstream use.
2. **Part 2 adaptation: retrieval-ready evidence packaging**
   Approved documents should become chunked, provenance-rich artifacts that are ready for grounded retrieval and citation-based Q&A. The repo does not need a full chatbot first; it needs retrieval-grade acquisition outputs.

This should be implemented **without breaking** the repo's current value proposition:

- stable CLI contract
- policy-driven acquisition and download enforcement
- local-first registry
- legacy and LangGraph parity
- deterministic artifacts and tests

## Current Repo Assessment

### What is already strong

- `discover -> followup -> review -> download -> scan` is a coherent staged pipeline already.
- Policy enforcement is explicit and deterministic before network or registry writes.
- The registry contract is clean and local-first.
- Trace sidecars and execution-policy sidecars are good portfolio signals.
- `scan_pdf()` already exposes a useful hook: it distinguishes `text_selectable` vs `image_or_unknown`.

### Current gaps relative to the stronger design

1. **The repo stops too early at acquisition**
   It downloads and lightly scans artifacts, but does not model a full post-download intake layer.
2. **No versioned extraction record**
   `metadata.json` is useful, but it is acting as both manifest and derived analysis surface. Those responsibilities should be separated.
3. **No parse-strategy routing**
   There is no explicit "text-layer first, OCR only when needed" intake contract.
4. **No chunk-level provenance**
   Follow-up extraction finds pointers, but downloaded documents do not become retrieval-ready chunk records with page/section provenance.
5. **No post-download acceptance policy**
   Current review logic is pre-download and source-confidence-driven. There is no second gate for "is this parsed document suitable for downstream knowledge use?"

## Design Direction

Treat the current repo as the **front half** of a document-intake and retrieval-preparation system.

Do **not** turn it into a mortgage product demo. Keep it repo-factual and acquisition-centric.

The right framing is:

- current repo strength = safe document acquisition
- next repo strength = safe document acquisition **plus** versioned parse/extraction artifacts **plus** retrieval-ready evidence packaging

## Important Constraint

Do **not** introduce a database in this public repo in the next phase.

For this repo, the correct storage model is still:

- raw artifact bytes in the local registry
- append-only JSON sidecars for parse/extraction/chunk artifacts
- stable manifest metadata in `metadata.json`

Production mapping should be documented, but not implemented:

- raw files -> object storage
- versioned extraction record -> PostgreSQL `JSONB`
- canonical approved facts -> normalized relational tables

In this public repo, the closest equivalent is:

- `metadata.json` = canonical operational manifest
- `analysis/*.json` sidecars = versioned extraction / parse records

## Why We Need Both Canonical Manifest And Versioned Extraction Record

This distinction should be made explicit in code and docs.

### Canonical manifest (`metadata.json`)

Use it for stable operational facts:

- `document_id`
- `entity_id`
- `entity_name`
- `source_url`
- `artifact_family`
- `artifact_type`
- `content_type`
- `local_path`
- pipeline status fields
- latest approved summary metadata

This is the file that current commands already depend on. Keep it stable.

### Versioned extraction / parse record

Use it for derived, revisable, parser-versioned analysis:

- parse strategy used
- text-layer availability
- parser version
- extraction version
- field-level outputs
- provenance
- validation errors
- risk score
- chunking outputs

This should be append-only or versioned. It must be possible to re-run parsing or extraction without overwriting the historical record.

## Proposed File/Artifact Contract

Keep existing artifacts unchanged. Add new ones.

### Existing artifacts that must remain

- `discover.json`
- `discover_summary.csv`
- `ranking_trace.json`
- `review_queue.csv`
- `review_trace.json`
- `scan_results.json`
- registry `metadata.json`

### New registry-side artifacts

For each downloaded document folder, an `analysis/` directory holds versioned
timestamped sidecars (never overwritten — each run appends a new file):

```text
registry/.../doc_xxxxx/
  artifact.pdf
  metadata.json
  analysis/
    parse_record.<ts>.json
    extraction_record.<ts>.json
    chunks.<ts>.jsonl
```

Timestamp format: `YYYYMMDDTHHMMSSffffffZ` (microsecond UTC precision).

### New run-level artifacts

The `analyze` command writes additive run outputs under `workspace/runs/analyze_<ts>/`:

- `analyze_results.json`
- `analyze_summary.csv`
- `resolved_execution_policy.json`
- local trace file

The `chunk` command writes under `workspace/runs/chunk_<ts>/`:

- `chunk_results.json`
- `resolved_execution_policy.json`
- local trace file

## Phase Plan

## Phase 1: Parse-Aware Intake Layer

### Objective

Add a real post-download intake stage that decides how a document should be parsed.

### Core rule

Use **text-layer parsing first**. Use OCR only for scanned or low-text artifacts.

### Implementation notes

- Build on `doc_workbench/registry/metadata_scanner.py`.
- The current `modality` output is the right seed but too shallow.
- Introduce a deeper parse-routing module that emits:
  - `text_layer_present`
  - `parse_strategy`
  - `parse_status`
  - `parse_errors`
  - `page_count`
  - `quality_signals`

### Suggested new modules

- `doc_workbench/intake/models.py`
- `doc_workbench/intake/detector.py`
- `doc_workbench/intake/parser.py`
- `doc_workbench/intake/validation.py`

### Suggested new dataclass

```python
@dataclass(slots=True)
class ParseRecord:
    document_id: str
    schema_version: int
    parser_version: str
    content_type: str
    modality: str
    text_layer_present: bool
    parse_strategy: str
    parse_status: str
    page_count: int | None
    quality_signals: dict[str, Any]
    errors: list[str]
```

### Parse strategy rules

- text-based PDF -> native PDF text extraction + layout-aware parsing
- scanned/image-heavy PDF -> OCR fallback interface
- HTML -> HTML parse path
- non-supported binaries -> `parse_status=skipped`

### Dependency guidance

Keep the base install light.

Use optional extras for heavier parsing:

- `[parsing]` for `docling` or `unstructured`
- `[ocr]` only if an OCR path is added later

Do not make OCR a mandatory dependency.

## Phase 2: Versioned Extraction Record

### Objective

Turn downloaded artifacts into typed, versioned acquisition records rather than leaving analysis implicit in `metadata.json`.

### Scope

This is **not** business-domain extraction. It is acquisition-domain extraction.

The typed schema should focus on document identity and downstream usability:

- title
- issuer name
- reporting period
- publication date
- page count
- modality
- text-layer availability
- likely document family
- parse confidence / risk
- provenance

### Suggested new dataclass

```python
@dataclass(slots=True)
class ExtractionRecord:
    document_id: str
    schema_version: int
    extractor_version: str
    parse_record_version: str
    review_status: str
    risk_level: str
    fields: dict[str, Any]
    validation_errors: list[str]
    provenance: dict[str, Any]
```

### Validation rules

At minimum validate:

- title is non-empty for parseable documents
- year/publication period consistency where available
- page count is positive for PDFs
- content type and chosen parse strategy agree
- source URL domain and artifact classification remain coherent

### Risk policy

Do not use a single raw confidence score as truth.

Create a composite risk signal from:

- parse success or failure
- text-layer quality
- title/year consistency
- source tier
- whether a document is duplicate-like or malformed
- whether extraction fields are sparse or contradictory

### Review model

Keep current pre-download review logic.

Add a **second post-download acceptance layer** for downstream indexing:

- `index_ready`
- `needs_document_review`
- `rejected_for_indexing`

This is the public-repo equivalent of "validated extraction before canonical acceptance."

## Phase 3: Retrieval-Ready Chunking

### Objective

Prepare approved documents for grounded retrieval with provenance.

### Why this matters

This repo is already strong on acquisition. The next strong signal is that acquired documents are not just downloaded; they are packaged for evidence-based downstream use.

### Suggested new modules

- `doc_workbench/knowledge/models.py`
- `doc_workbench/knowledge/chunker.py`
- `doc_workbench/knowledge/packager.py`

### Chunking rules

- text PDFs -> section-aware chunks where possible
- scanned PDFs -> OCR text chunks only if OCR exists; otherwise mark not chunkable
- HTML -> section-aware HTML chunks

### Chunk metadata should include

- `document_id`
- `chunk_id`
- `entity_id`
- `artifact_family`
- `source_url`
- `page_start`
- `page_end`
- `section_title`
- `text`
- `parser_version`
- `extraction_version`
- `audience`
- `effective_from`
- `effective_to`

For this repo, `audience` can default to `public`.

### Output format

Use `JSONL` for chunk records:

- one chunk per line
- easy to diff, stream, and feed into retrieval pipelines

## Phase 4: Optional Grounded Retrieval Demo

### Objective

Only after chunking exists, consider a lightweight retrieval demo.

### Recommendation

Do not start with a full agent or chat UX.

Start with one of:

- `doc-workbench chunk`
- `doc-workbench retrieve --query "..."`
- `doc-workbench evidence-pack --entity-id ...`

That is enough to prove:

- chunk provenance exists
- retrieval is possible
- citations can be attached back to the registry artifact

### Stretch goal

An optional `ask` command can come later, but it should only answer from retrieved chunks and should return citations.

## Specific Repo Changes

## Files to add

- `doc_workbench/intake/__init__.py`
- `doc_workbench/intake/models.py`
- `doc_workbench/intake/detector.py`
- `doc_workbench/intake/parser.py`
- `doc_workbench/intake/validation.py`
- `doc_workbench/knowledge/__init__.py`
- `doc_workbench/knowledge/models.py`
- `doc_workbench/knowledge/chunker.py`
- `doc_workbench/knowledge/packager.py`

## Files to modify

- `doc_workbench/cli.py`
- `doc_workbench/models.py`
- `doc_workbench/registry/document_registry.py`
- `doc_workbench/registry/metadata_scanner.py`
- `doc_workbench/review/classifier.py`
- `README.md`
- `docs/architecture.md`
- `pyproject.toml`

## Existing modules to preserve

- `doc_workbench/acquisition/discovery.py`
- `doc_workbench/acquisition/followup/*`
- `doc_workbench/orchestration/*`
- execution policy logic
- current review queue logic

The next phase should extend these, not replace them.

## CLI Recommendation

Add new commands instead of overloading current ones:

- `doc-workbench parse`
- `doc-workbench extract`
- `doc-workbench chunk`

If command proliferation feels too heavy, merge `parse` and `extract` into one command:

- `doc-workbench analyze`

But avoid turning `scan` into an overloaded kitchen-sink command. `scan` is currently small and understandable.

## LangGraph Recommendation

Keep legacy and LangGraph parity.

If new stages are added, they should map cleanly into both paths:

- legacy: direct CLI orchestration
- graph: `parse_node -> extract_node -> chunk_node`

Do not let the graph gain exclusive functionality that the legacy path cannot reproduce.

## Tests To Add

### Unit tests

- text-layer detection routes text PDFs to parse-first path
- scanned/image-heavy PDFs route to OCR fallback interface
- parse sidecar is written under `analysis/`
- extraction sidecar is versioned and append-only
- chunk JSONL includes provenance fields
- post-download review/risk classification behaves deterministically

### CLI flow tests

- `review -> download -> parse`
- `review -> download -> parse -> chunk`
- non-PDF artifact is skipped cleanly
- missing text layer does not crash chunking

### Orchestration tests

- legacy and LangGraph produce the same additive artifact set
- new nodes preserve existing output contract

## Acceptance Criteria

The next agent should treat the work as complete only when all of these are true:

1. Existing commands still work and current tests still pass.
2. A downloaded document can produce a parse sidecar without mutating the existing registry manifest contract.
3. Extraction records are versioned and separate from `metadata.json`.
4. Parse routing is explicit: text-layer first, OCR only as fallback.
5. Retrieval-ready chunk records can be emitted with document provenance.
6. The repo docs explain why canonical manifest and versioned extraction records are separate.
7. New behavior is covered by deterministic tests with local fixtures only.

## Non-Goals For This Phase

- do not add a production database
- do not add a remote vector database
- do not require live OCR services
- do not build a full chat product
- do not rewrite the existing acquisition/review pipeline from scratch

## Recommended Implementation Order

1. Add intake models and parse sidecars.
2. Add extraction record and validation contract.
3. Add post-download acceptance/risk classification.
4. Add chunk JSONL packaging.
5. Update README and architecture docs.
6. Only then consider a small retrieval demo.

## Short Rationale

The strongest next move for this repo is not "more scraping."

It is showing that:

- acquisition artifacts are parse-aware
- derived document understanding is versioned and auditable
- downstream retrieval can cite acquired evidence precisely

That bridges the repo from "safe downloader and review queue" to "safe document intake workbench" without losing the current strengths.
