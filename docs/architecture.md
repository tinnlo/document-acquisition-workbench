# Document Acquisition Workbench Architecture

`doc_workbench` is a public-safe reconstruction of a production document acquisition workflow.
The interesting part is not the scraping itself. The interesting part is the contract around it:
stable artifacts, policy-driven decisioning, explicit review, local-first persistence, optional
orchestration, and safe observability.

## Architectural Intent

This repository is designed to demonstrate five things:

- acquisition as a staged pipeline, not a one-shot crawler
- execution-model swapability without downstream artifact drift
- policy packaged as runtime data rather than hardcoded branch logic everywhere
- reviewable outputs and trace sidecars at every decision boundary
- safe opt-in remote telemetry without making local development fragile

## System View

```mermaid
graph TD

subgraph INPUTS["Inputs"]
  direction TB
  E["📥 Entity batch CSV"]:::bronze
  P["📘 Context policy"]:::external
end

subgraph ACQ["Acquisition and decisioning"]
  direction TB
  D["📥 Discover"]:::bronze
  F["🔄 Follow-up extraction"]:::silver
  R["🔄 Rank + dedupe + cap"]:::silver
  Q["🏆 Review queue generation"]:::golden
end

subgraph DELIVERY["Delivery and registry"]
  direction TB
  DL["🏆 Download approved PDFs"]:::golden
  SC["🏆 Scan metadata"]:::golden
  RG["📦 workspace/registry"]:::external
end

subgraph OUTPUTS["Artifacts and telemetry"]
  direction TB
  O1["📄 Discover artifacts"]:::artifact
  O2["📄 Review artifacts"]:::artifact
  O3["📄 Policy + traces"]:::artifact
  O4["📡 Langfuse (optional)"]:::external
  O5["📄 Eval report"]:::artifact
end

E --> D
P -. "rules" .-> D
P -. "rules" .-> R
P -. "rules" .-> Q
D --> F --> R --> Q --> DL --> SC --> RG
R --> O1
Q --> O2
R --> O3
Q --> O3
Q -. "opt-in" .-> O4
R -. "eval harness" .-> O5

classDef bronze fill:#ffe6e6,stroke:#b30000,stroke-width:1px
classDef silver fill:#e6f0ff,stroke:#003399,stroke-width:1px
classDef golden fill:#e6ffe6,stroke:#006400,stroke-width:1px
classDef external fill:#fce8ff,stroke:#7b2fa8,stroke-width:1.5px,stroke-dasharray:5 3
classDef artifact fill:#fff8e1,stroke:#e65100,stroke-width:1px,stroke-dasharray:5 5

style INPUTS fill:transparent,stroke:#7b2fa8,stroke-width:1px,stroke-dasharray:4 4
style ACQ fill:transparent,stroke:#003399,stroke-width:1px,stroke-dasharray:4 4
style DELIVERY fill:transparent,stroke:#006400,stroke-width:1px,stroke-dasharray:4 4
style OUTPUTS fill:transparent,stroke:#e65100,stroke-width:1px,stroke-dasharray:4 4
```

## Execution Models

The repo supports two execution models that intentionally converge on the same core output contract.

```mermaid
graph TD

subgraph LEGACY["Legacy path (default)"]
  direction TB
  LD["📥 discover command"]:::bronze --> LE["discover_entity()\nowns discovery + follow-up + ranking"]:::bronze
  LR["🏆 review command"]:::golden --> LQ["build_review_rows()"]:::golden
end

subgraph GRAPH["LangGraph path (opt-in)"]
  direction TB
  GD["📘 discover --engine langgraph"]:::external --> G1["🔄 discover_node"]:::silver --> G2["🔄 followup_node"]:::silver --> G3["🔄 rank_node"]:::silver
  GR["📘 review --engine langgraph"]:::external --> G4["🔄 rank_node"]:::silver --> G5["🏆 review_prep_node"]:::golden
end

CONTRACT["📄 Shared artifact contract"]:::artifact

LE --> CONTRACT
LQ --> CONTRACT
G3 --> CONTRACT
G5 --> CONTRACT

classDef bronze fill:#ffe6e6,stroke:#b30000,stroke-width:1px
classDef silver fill:#e6f0ff,stroke:#003399,stroke-width:1px
classDef golden fill:#e6ffe6,stroke:#006400,stroke-width:1px
classDef external fill:#fce8ff,stroke:#7b2fa8,stroke-width:1.5px,stroke-dasharray:5 3
classDef artifact fill:#fff8e1,stroke:#e65100,stroke-width:1px,stroke-dasharray:5 5

style LEGACY fill:transparent,stroke:#b30000,stroke-width:1px,stroke-dasharray:4 4
style GRAPH fill:transparent,stroke:#003399,stroke-width:1px,stroke-dasharray:4 4
```

### Path Comparison

| Concern | Legacy | LangGraph |
|---|---|---|
| Default path | Yes | No |
| `discover` implementation | `acquisition/discovery.py::discover_entity()` | `discover_node -> followup_node -> rank_node` |
| `review` implementation | `review/workflow.py::build_review_rows()` | `rank_node -> review_prep_node` |
| Compiled graph usage | No | Yes for `discover`; no for `review` |
| Optional dependency | none | `langgraph` only for compiled discover graph |
| Output filenames | same contract | same contract |

Important nuance:

- `discover --engine langgraph` stops at `rank -> END`; it does **not** run `review_prep_node`.
- `review --engine langgraph` calls node functions directly and does **not** execute a compiled `StateGraph`.
- Internal full-mode execution exists as `run_graph(mode="full")` for cases that need `review_prep` in the same stateful flow.

## Command-to-Module Map

| CLI command | Main module path | Notes |
|---|---|---|
| `discover` | `doc_workbench/acquisition/discovery.py` or `doc_workbench/orchestration/*` | Provider acquisition, follow-up, ranking |
| `followup-search` | `doc_workbench/followup/workflow.py` | Materializes promoted targets into the registry |
| `review` | `doc_workbench/review/workflow.py` or orchestration nodes | Produces review queue and review trace |
| `download` | `doc_workbench/storage/downloader.py` + `doc_workbench/registry/document_registry.py` | Fetches approved PDFs and records registry state |
| `scan` | `doc_workbench/registry/metadata_scanner.py` | Adds lightweight PDF metadata to manifests |
| `analyze` | `doc_workbench/intake/parser.py` + `doc_workbench/intake/extractor.py` | Parse and extract; writes versioned `parse_record` and `extraction_record` sidecars |
| `chunk` | `doc_workbench/knowledge/chunker.py` + `doc_workbench/knowledge/packager.py` | Chunk text PDFs; writes `chunks.<ts>.jsonl` sidecars |
| `eval` | `doc_workbench/evals/run_evals.py` | Reuses real ranking/review node logic |

## State Model

The LangGraph path threads a shared `WorkbenchState` through all nodes.

| Field | Set by | Meaning |
|---|---|---|
| `entities` | caller | input entity records |
| `policy` | caller | loaded `ContextPolicy` |
| `exec_policy` | caller | loaded `ExecutionPolicy` (optional; governs runtime permissions) |
| `tracer` | caller | local trace collector |
| `output_dir` | caller | run-scoped artifact directory |
| `followup_search` | caller | whether follow-up logic is enabled |
| `discovery_records` | `discover_node` | pre-follow-up / pre-rank candidate pool |
| `followup_records` | `followup_node` | candidate pool after follow-up enrichment |
| `ranked_records` | `rank_node` | rescored, deduped, capped candidates |
| `review_rows` | `review_prep_node` | final review queue |
| `review_trace` | `review_prep_node` | review explainability payload |
| `recommendation_summary` | `review_prep_node` | aggregate recommendation counts |
| `intake_document_ids` | caller | explicit document IDs for intake nodes (mutually exclusive with `intake_all`) |
| `intake_entity_id` | caller | entity filter for intake nodes |
| `intake_all` | caller | process all manifests |
| `intake_force` | caller | re-run even if already analyzed/chunked |
| `intake_registry_root` | caller | path to the registry root |
| `parse_records` | `parse_node` | one entry per document from parse phase |
| `extraction_records` | `extract_node` | one entry per document from extraction phase |
| `chunk_records` | `chunk_node` | one entry per document from chunking phase |

## Artifact Surfaces

Artifact stability is one of the strongest architectural choices in the repo.

| Stage | Primary artifacts | Why they exist |
|---|---|---|
| Discover | `discover.json`, `discover_summary.csv`, `ranking_trace.json`, `resolved_policy.json`, `resolved_execution_policy.json` | freeze candidate pool, ranking rationale, and runtime constraints |
| Review | `review_queue.csv`, `review_trace.json`, `resolved_policy.json`, `resolved_execution_policy.json` | hand off a reviewable queue with recommendation reasons and policy record |
| Download | `download_results.json`, `download_results.csv`, `resolved_execution_policy.json` | record of what was fetched and under which execution constraints |
| Trace | `workspace/traces/{run_id}.json` | local execution record independent of remote telemetry |
| Registry | `workspace/registry/*` | persistent downloaded-document state |
| Analyze | `registry/<entity>/<family>/<year>/<type>/<doc_id>/analysis/parse_record.<ts>.json`, `extraction_record.<ts>.json` | versioned, immutable per-run parse and extraction sidecars |
| Chunk | `registry/<entity>/<family>/<year>/<type>/<doc_id>/analysis/chunks.<ts>.jsonl` | retrieval-ready chunk records; one file per successful chunk run |
| Eval | `eval_report.json` (written to current directory) | machine-readable regression report |

The artifact filenames are intentionally stable across execution models so downstream consumers do not care whether the run was legacy or LangGraph-backed.

### Intake pipeline: analyze + chunk

The `analyze` command merges parse and extraction into a single step to avoid fragile two-step state. The `chunk` command is separate because chunking is only meaningful for `index_ready` documents, and splitting the commands allows selective retry without re-parsing.

**Manifest vs. versioned sidecars:**

The registry manifest (`metadata.json`) is the **canonical, mutable status record** for each document. It stores only lightweight pipeline status fields (`parse_status`, `chunking_status`). All richer, versioned artifacts live in **immutable timestamped sidecars** inside the document's `analysis/` subdirectory and are never overwritten. `indexing_acceptance` lives only in the `extraction_record` sidecar — it is **not** written to the manifest.

**`analyze` flow:**
1. Filter manifests by `download_status=complete`; skip if `parse_status=complete` (unless `--force`)
2. Validate `local_path` containment, symlink rejection, file-size guard
3. `detect_parse_strategy()` → `run_parse()` → `validate_parse_record()` → `run_extraction()`
4. Write `parse_record.<ts>.json` and `extraction_record.<ts>.json` sidecars (timestamp-versioned, never overwritten)
5. Update manifest `parse_status` only

**`chunk` flow:**
1. Skip if `parse_status != complete` (sets `chunking_status=skipped`)
2. Load latest `extraction_record` sidecar; skip if `indexing_acceptance != index_ready`
3. Validate `parse_record_ref` basename against `^parse_record\.\d{8}T\d{6}\d+Z\.json$`; resolve and containment-check
4. `chunk_document()` → `Iterator[ChunkRecord] | None`; `None` → `chunking_status=skipped`
5. `write_chunk_jsonl()` → `chunks.<ts>.jsonl` (no-overwrite collision retry); update manifest `chunking_status=complete`

**LangGraph parity:** `parse_node`, `extract_node`, and `chunk_node` are independent node functions in `doc_workbench/orchestration/nodes.py`. `build_intake_graph()` in `doc_workbench/orchestration/graph.py` composes: `parse → extract → chunk → END`. `extract_node` reads `state["parse_records"]` and writes `state["extraction_records"]`; `chunk_node` reads `state["extraction_records"]` and writes `state["chunk_records"]`.

## Observability Model

Two observability layers run in parallel:

| Layer | Behavior |
|---|---|
| Local JSON traces | Always on. Written under `workspace/traces/` for all runs. |
| Langfuse remote spans | Optional. Only active when explicit opt-in and credentials are both present. |

Both layers share a common `trace_id` (32-character lowercase hexadecimal UUID) generated at CLI entry, enabling correlation between local debugging artifacts and remote telemetry.

### Local traces

`RunTrace` records stage-level spans for the acquisition workflow and writes them to disk with the run ID. Each trace includes:

- `trace_id`: UUID for correlation with remote spans
- `run_id`: timestamp-based identifier for local file naming
- `spans`: stage-level execution records with latency and candidate counts

Key semantic detail:

- legacy `discover` emits the authoritative `discover_entity` span from `acquisition/discovery.py`
- LangGraph emits a distinct pre-rank span name, `discover_entity_pre_rank`, to avoid pretending that pre-rank discovery is the same thing as the legacy final discover stage
- follow-up, ranking, and review use canonical stage names such as `followup_extraction`, `candidate_ranking`, and `review_queue_generation`

### Remote telemetry guardrails

`doc_workbench/observability/langfuse_bridge.py` is intentionally conservative:

- requires `DOC_WORKBENCH_ENABLE_LANGFUSE=1`
- requires `LANGFUSE_SECRET_KEY` and `LANGFUSE_PUBLIC_KEY`
- falls back to a no-op client on any missing gate or SDK failure
- sanitizes URLs before remote emission so embedded credentials are never forwarded
- is suppressed entirely during `doc-workbench eval`
- accepts `trace_id` parameter to correlate with local traces
- invalidates cached client when `trace_id` changes to ensure proper trace boundaries

Remote Langfuse spans are produced only by the LangGraph path.

## Policy And Decisioning

### Context policy

The acquisition policy is bundled at `doc_workbench/context/context_policy.yaml` and loaded via `importlib.resources`, which means the runtime behavior is preserved after `pip install` without needing the source tree.

That policy governs *acquisition strategy*:

- source acquisition order
- ranking boosts and penalties
- recommendation thresholds
- follow-up eligibility rules

Every `discover`, `review`, and `followup-search` run writes a `resolved_policy.json` sidecar so the exact context policy used for a run is preserved next to the outputs.

### Execution policy

A separate policy layer — `doc_workbench/context/execution_policy.yaml` — governs *what the runtime is allowed to do*. These are two distinct concerns:

| Policy | File | Governs |
|---|---|---|
| Context policy | `context_policy.yaml` | where to search, in what order |
| Execution policy | `execution_policy.yaml` | what the runtime may fetch, write, and execute |

The execution policy controls:

| Dimension | Config key | Default |
|---|---|---|
| Allowed command stages | `allowed_command_stages` | discover, review, download, followup-search, scan |
| Allowed source / domain families | `allowed_source_families` | explicit allowlist of known public annual-report hosts (no wildcard) |
| Download enabled | `download.enabled` | `true` |
| Max downloads per run | `download.max_count` | 50 |
| Max file size before registry write | `download.max_file_size_bytes` | 50 MB |
| Allowed MIME types before registry write | `download.allowed_mime_types` | `["application/pdf", "text/html"]` |
| Follow-up extraction permitted | `followup_search.enabled` | `true` |
| Registry write location restriction | `registry.root_restriction` | `"registry"` |

Enforcement is **deterministic and pre-action**: a `PolicyViolationError` is raised *before* any network call, fetch, or registry write. A blocked domain is rejected before the HTTP request is issued. An oversize file is rejected before `register_document()` is called.

Three specific enforcement invariants worth noting:

- **Egress cap on failures** — the download egress cap (`download.max_count`) is enforced against `fetch_attempt_count`, which is incremented *before* the network call. Repeated failed downloads (MIME rejection, network error) cannot bypass the cap by never successfully registering.
- **Strict boolean policy parsing** — `download.enabled` and `followup_search.enabled` require real YAML booleans. Quoted strings like `"false"` are rejected with a clear error rather than silently becoming truthy, which would re-enable blocked features.
- **Entity-ID collision guard** — the registry dedupe loop verifies `payload["entity_id"] == entity_id` before treating a cached manifest as a duplicate. Two distinct entity IDs that sanitize to the same directory prefix cannot cross-wire each other's artifact cache.

Both the legacy path and the LangGraph path enforce the same execution policy. The `followup_node` in the LangGraph graph checks `exec_policy.followup_search.enabled` before any extraction runs, so neither engine can bypass what the other enforces.

Every `discover`, `review`, `download`, and `followup-search` run writes a `resolved_execution_policy.json` sidecar alongside `resolved_policy.json`, so a reviewer can reconstruct the exact constraints in force for any run.

## Provider Surface And Extension Points

The public provider abstraction is intentionally generic:

- `official_site`
- `regulatory_filings`
- `search`
- follow-up extraction over search-surfaced seeds

Current public implementation detail:

- the regulatory path is SEC-backed for the sample dataset
- the code imports the neutral abstraction rather than regulator-specific names in the high-level flow

Practical extension points for a production adaptation:

- replace the search provider
- add alternative regulatory providers
- change scoring rules through policy data
- add new orchestration modes without changing artifact names
- attach downstream consumers to the registry or review queue without coupling them to acquisition internals

## Packaging And Installability

This repo is intentionally installable, not just source-tree runnable.

| Concern | Approach |
|---|---|
| Context policy | packaged under `doc_workbench/context/` |
| Eval harness | packaged under `doc_workbench/evals/` |
| Orchestration dependency | optional extra: `[orchestration]` |
| Remote observability dependency | optional extra: `[observability]` |

This matters because a lot of portfolio demos look architectural only in a cloned repo. This one keeps the runtime contract intact after installation.

## File Map For Reviewers

| If you want to inspect... | Start here |
|---|---|
| legacy acquisition flow | `doc_workbench/acquisition/discovery.py` |
| LangGraph nodes | `doc_workbench/orchestration/nodes.py` |
| compiled graph | `doc_workbench/orchestration/graph.py` |
| CLI routing and command contract | `doc_workbench/cli.py` |
| context policy loading | `doc_workbench/policy.py` |
| execution policy loading and enforcement | `doc_workbench/execution_policy.py` |
| review logic | `doc_workbench/review/workflow.py` |
| local tracing | `doc_workbench/observability/tracer.py` |
| Langfuse bridge | `doc_workbench/observability/langfuse_bridge.py` |
| eval harness | `doc_workbench/evals/run_evals.py` |
| hardened local runtime | `docs/hardened-runtime.md` |

## Summary

The architecture is intentionally boring in the right places:

- explicit stages
- explicit artifacts
- explicit review boundary
- explicit policy governance at two levels (context + execution)
- explicit opt-in telemetry
- explicit packaging of runtime data

That is the point. The repo demonstrates an execution model that can evolve internally without creating downstream ambiguity, and where every runtime decision is governed by a policy that is itself preserved as a run artifact.
