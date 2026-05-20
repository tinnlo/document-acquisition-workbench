# Document Acquisition Workbench

`doc_workbench` is a public-safe reconstruction of a production document acquisition pipeline.
It is designed for architectural review more than feature demo: deterministic artifact contracts,
policy-driven decisioning, registry-backed downloads, installable evals, and an optional
LangGraph execution path with safe opt-in observability.

## Why This Repo Is Worth Reviewing

| Signal | Why it matters to a technical lead |
|---|---|
| Permissioned document acquisition | Every run is governed by both a context policy (acquisition strategy) and an execution policy (what the runtime may do). Both are emitted as per-run JSON sidecars. |
| Two execution models, one contract | The default legacy path and the LangGraph path produce the same core output filenames, and both enforce the same execution policy, so orchestration can evolve without breaking downstream consumers or bypassing safety rules. |
 | Execution-policy sidecars | The `discover`, `review`, `download`, and `followup-search` commands each write `resolved_execution_policy.json` alongside `resolved_policy.json`. A reviewer can reconstruct the exact constraints in force for any acquisition run. |
| Safe-by-default download workflow | Blocked domains, disallowed MIME types, and oversize files are rejected *before* any registry write. The download stage enforces all execution-policy rules deterministically. |
| Hardened local runtime | The container runs as a non-root user with a read-only code area and a separate writable workspace mount. See `docs/hardened-runtime.md`. |
| Local-first traceability | Every run writes local JSON traces plus decision sidecars such as `ranking_trace.json` and `review_trace.json`. |
| Safe remote telemetry | Langfuse is explicit opt-in, credentials-gated, URL-sanitized, and suppressed during eval runs. |
| Packaging discipline | Policy files and eval fixtures ship inside the installed package, so the CLI works after `pip install` without depending on the source tree. |

## Architecture At A Glance

```mermaid
graph TD

subgraph INPUTS["Inputs"]
  E["📥 Entity batch CSV"]:::bronze
  P["📘 Context policy"]:::external
end

subgraph PIPE["Pipeline stages"]
  D["📥 Discover"]:::bronze
  F["🔄 Follow-up extraction"]:::silver
  R["🔄 Rank + dedupe + cap"]:::silver
  Q["🏆 Review queue generation"]:::golden
  DL["🏆 Download approved PDFs"]:::golden
  SC["🏆 Scan metadata"]:::golden
  RG["📦 workspace/registry"]:::external
end

subgraph OUTPUTS["Artifacts and telemetry"]
  O1["📄 Run outputs<br/>discover.json | review_queue.csv"]:::artifact
  O2["📄 Trace + policy<br/>ranking_trace.json | review_trace.json<br/>resolved_policy.json | workspace/traces/*.json"]:::artifact
  O3["📡 Optional telemetry + evals<br/>Langfuse | eval report"]:::external
end

E --> P
O1 --> O2
O2 --> O3
D --> F --> R --> Q --> DL --> SC --> RG
E --> D
P -. "rules" .-> D
P -. "rules" .-> R
P -. "rules" .-> Q
Q --> O1
Q --> O2
Q -. "opt-in / eval harness" .-> O3

classDef bronze fill:#ffe6e6,stroke:#b30000,stroke-width:1px
classDef silver fill:#e6f0ff,stroke:#003399,stroke-width:1px
classDef golden fill:#e6ffe6,stroke:#006400,stroke-width:1px
classDef external fill:#fce8ff,stroke:#7b2fa8,stroke-width:1.5px,stroke-dasharray:5 3
classDef artifact fill:#fff8e1,stroke:#e65100,stroke-width:1px,stroke-dasharray:5 5

style E fill:#ffe6e6,stroke:#b30000,stroke-width:1px
style P fill:#fce8ff,stroke:#7b2fa8,stroke-width:1.5px,stroke-dasharray:5 3
style D fill:#ffe6e6,stroke:#b30000,stroke-width:1px
style F fill:#e6f0ff,stroke:#003399,stroke-width:1px
style R fill:#e6f0ff,stroke:#003399,stroke-width:1px
style Q fill:#e6ffe6,stroke:#006400,stroke-width:1px
style DL fill:#e6ffe6,stroke:#006400,stroke-width:1px
style SC fill:#e6ffe6,stroke:#006400,stroke-width:1px
style RG fill:#fce8ff,stroke:#7b2fa8,stroke-width:1.5px,stroke-dasharray:5 3
style O1 fill:#fff8e1,stroke:#e65100,stroke-width:1px,stroke-dasharray:5 5
style O2 fill:#fff8e1,stroke:#e65100,stroke-width:1px,stroke-dasharray:5 5
style O3 fill:#fce8ff,stroke:#7b2fa8,stroke-width:1.5px,stroke-dasharray:5 3
style INPUTS fill:transparent,stroke:#7b2fa8,stroke-width:1px,stroke-dasharray:4 4
style PIPE fill:transparent,stroke:#006400,stroke-width:1px,stroke-dasharray:4 4
style OUTPUTS fill:transparent,stroke:#e65100,stroke-width:1px,stroke-dasharray:4 4
linkStyle 0,1,2 stroke:transparent,fill:none
```

This is the core idea the repo is trying to show: acquisition is not just scraping. It is a staged pipeline with explicit policy, reviewable outputs, persistent state, and observable execution.

> Architecture deep dive: [`docs/architecture.md`](docs/architecture.md) documents the system view, execution-path differences, runtime artifacts, observability model, and extension points.

## Execution Modes

```mermaid
graph TD

subgraph DISC_LANE["discover command"]
  direction LR
  DCLI["📘 CLI discover"]:::external
  DLEG["📥 legacy<br/>discover_entity() owns<br/>discovery + follow-up + ranking"]:::bronze
  DGRAPH["🔄 langgraph<br/>discover_node -> followup_node -> rank_node"]:::silver
  DCLI --> DLEG
  DCLI --> DGRAPH
end

subgraph REV_LANE["review command"]
  direction LR
  RCLI["📘 CLI review"]:::external
  RLEG["🏆 legacy<br/>build_review_rows()"]:::golden
  RGRAPH["🔄 langgraph<br/>rank_node -> review_prep_node"]:::silver
  RCLI --> RLEG
  RCLI --> RGRAPH
end

CONTRACT["📄 Shared artifact contract"]:::artifact

DLEG --> CONTRACT
DGRAPH --> CONTRACT
RLEG --> CONTRACT
RGRAPH --> CONTRACT

classDef bronze fill:#ffe6e6,stroke:#b30000,stroke-width:1px
classDef silver fill:#e6f0ff,stroke:#003399,stroke-width:1px
classDef golden fill:#e6ffe6,stroke:#006400,stroke-width:1px
classDef external fill:#fce8ff,stroke:#7b2fa8,stroke-width:1.5px,stroke-dasharray:5 3
classDef artifact fill:#fff8e1,stroke:#e65100,stroke-width:1px,stroke-dasharray:5 5

style DCLI fill:#fce8ff,stroke:#7b2fa8,stroke-width:1.5px,stroke-dasharray:5 3
style RCLI fill:#fce8ff,stroke:#7b2fa8,stroke-width:1.5px,stroke-dasharray:5 3
style DLEG fill:#ffe6e6,stroke:#b30000,stroke-width:1px
style RLEG fill:#e6ffe6,stroke:#006400,stroke-width:1px
style DGRAPH fill:#e6f0ff,stroke:#003399,stroke-width:1px
style RGRAPH fill:#e6f0ff,stroke:#003399,stroke-width:1px
style CONTRACT fill:#fff8e1,stroke:#e65100,stroke-width:1px,stroke-dasharray:5 5
style DISC_LANE fill:transparent,stroke:#b30000,stroke-width:1px,stroke-dasharray:4 4
style REV_LANE fill:transparent,stroke:#006400,stroke-width:1px,stroke-dasharray:4 4
```

| Path | Default | What it does | Notes |
|---|---|---|---|
| `legacy` | Yes | Direct async CLI orchestration | Stable baseline path |
| `langgraph` for `discover` | No | Compiled graph: `discover_node -> followup_node -> rank_node` | Requires `.[orchestration]` |
| `langgraph` for `review` | No | Direct node calls: `rank_node -> review_prep_node` | Does not import `graph.py`; can run without the orchestration extra |

Set the engine explicitly with `--engine langgraph`, or use `DOC_WORKBENCH_ENGINE=langgraph`. The explicit CLI flag wins.

## Quick Start

```bash
python -m venv .venv
. .venv/bin/activate

# Core install
python -m pip install -e ".[dev]"

# Add LangGraph orchestration
python -m pip install -e ".[dev,orchestration]"

# Add optional Langfuse remote tracing
python -m pip install -e ".[dev,orchestration,observability]"
```

### Docker (hardened)

```bash
mkdir -p workspace
docker build -t doc-workbench:local .

# Hardened run: non-root, read-only code area, writable workspace mount
docker run --rm \
  --user 65534:65534 \
  --read-only \
  --tmpfs /tmp \
  -v "$PWD/workspace:/workspace" \
  -v "$PWD/examples:/app/examples:ro" \
  -e DOC_WORKBENCH_HOME=/workspace \
  doc-workbench:local \
  discover --entities examples/public_companies.csv

# Or with docker compose
docker compose run --rm workbench paths
```

See [`docs/hardened-runtime.md`](docs/hardened-runtime.md) for the full hardening story.

## Deployment Options

### Local Development
Use the Docker setup above for local development and testing.

### Google Cloud Platform (GCP)
Production deployment to GCP using Cloud Run Jobs, Cloud Storage, and Cloud Scheduler:

```bash
# Deploy infrastructure with Terraform
cd terraform/gcp
terraform init -backend-config=backend-prod.hcl
terraform apply -var-file=../environments/prod.tfvars

# Or use GitHub Actions CI/CD
# Push to main branch triggers automatic deployment
```

**Architecture**:
- **Cloud Run Job** - Batch processing with automatic scaling
- **Cloud Storage** - Registry storage via volume mounts at `/mnt/gcs`
- **Cloud Scheduler** - Periodic job triggers (optional)
- **Secret Manager** - API key storage (SERPER_API_KEY or BRAVE_API_KEY)

See [`docs/deployment_gcp.md`](docs/deployment_gcp.md) for complete deployment guide.

⚠️ **Note**: GCS volume mount write permissions need testing in dev environment. See `terraform/GCS_MOUNT_PERMISSIONS_ISSUE.md` if issues occur.

## CLI Surface

```bash
doc-workbench paths
doc-workbench discover --entities examples/public_companies.csv --followup-search
doc-workbench followup-search --input workspace/runs/discover_*/discover.json
doc-workbench review --input workspace/runs/discover_*/discover.json
doc-workbench download --input workspace/runs/review_*/review_queue.csv
doc-workbench scan --all
doc-workbench analyze --all
doc-workbench chunk --all
doc-workbench eval
```

### Typical end-to-end run

```bash
doc-workbench discover --entities examples/public_companies.csv --followup-search --engine langgraph
doc-workbench review --input workspace/runs/discover_*/discover.json --engine langgraph
doc-workbench download --input workspace/runs/review_*/review_queue.csv
doc-workbench scan --all
doc-workbench analyze --all
doc-workbench chunk --all
```

The bundled sample dataset lives in [`examples/public_companies.csv`](examples/public_companies.csv).

## Artifact Contract

One of the strongest design signals in this repo is that downstream artifacts stay stable even when the execution model changes.

| Command | Core outputs | Why they exist |
|---|---|---|
| `discover` | `discover.json`, `discover_summary.csv`, `ranking_trace.json`, `resolved_policy.json`, `resolved_execution_policy.json`, trace file | Candidate pool snapshot plus ranking explainability and runtime constraint record |
| `review` | `review_queue.csv`, `review_trace.json`, `resolved_policy.json`, `resolved_execution_policy.json`, trace file | Human-review handoff with recommendation rationale and policy record |
| `download` | downloaded files plus registry manifests, `resolved_execution_policy.json` | Persistent local registry for approved artifacts, under documented execution constraints |
| `scan` | updated manifest metadata | Lightweight PDF metadata enrichment |
| `analyze` | `parse_record.<ts>.json`, `extraction_record.<ts>.json` per document (versioned sidecars in registry `analysis/`); `analyze_results.json`, `analyze_summary.csv`, trace file | Versioned parse and extraction artifacts; sets `parse_status` on the manifest; `indexing_acceptance` lives only in the extraction sidecar |
| `chunk` | `chunks.<ts>.jsonl` per document (sidecar in registry); `chunk_results.json`, trace file | Retrieval-ready chunk records for index-ready documents; sets `chunking_status` |
| `eval` | `eval_report.json` (written to current directory) | Regression signal for ranking and recommendation behavior |

### Manifest vs. versioned sidecars

The registry manifest (`metadata.json`) for each document is the **canonical, mutable status record**. It tracks only lightweight pipeline status fields:

| Manifest field | Written by | Meaning |
|---|---|---|
| `pipeline_status.download_status` | `download` | Whether the artifact has been fetched |
| `pipeline_status.parse_status` | `analyze` | Whether parse completed |
| `pipeline_status.chunking_status` | `chunk` | Whether chunking completed |

All richer, versioned artifacts live in **immutable timestamped sidecars** inside each document's `analysis/` subdirectory and are never overwritten:

| Sidecar | Written by | Contains |
|---|---|---|
| `parse_record.<ts>.json` | `analyze` | page count, parse strategy, quality signals (`sampled_nonempty_page_ratio`, `sampled_avg_chars_per_page`) |
| `extraction_record.<ts>.json` | `analyze` | `indexing_acceptance`, `risk_level`, `fields`, `provenance` |
| `chunks.<ts>.jsonl` | `chunk` | one `ChunkRecord` per line (text + metadata per page chunk) |

`indexing_acceptance` is set only inside the `extraction_record` sidecar — it is **not** written to the manifest.


### Context policy

The acquisition policy is bundled inside the package at `doc_workbench/context/context_policy.yaml`. The runtime loads it via `importlib.resources`, so installed users get the same policy behavior without needing the source tree.

```yaml
acquisition_order:
  - official_site
  - regulatory_filings
  - search_expansion
  - followup_extraction
```

Every `discover` and `review` run writes a `resolved_policy.json` sidecar so the exact policy used for that run is preserved next to the outputs.

### Execution policy

A separate execution policy governs what the runtime is *allowed to do*. The two policies are distinct:

| Policy | File | Governs |
|---|---|---|
| Context policy | `context_policy.yaml` | where to search, in what order |
| Execution policy | `execution_policy.yaml` | what the runtime may fetch, download, or write |

The execution policy (`doc_workbench/context/execution_policy.yaml`) controls:

- which command stages are permitted to run
- which source domains may be fetched (explicit allowlist — no wildcard default)
- whether downloads are enabled, and at what count / size / MIME-type limits (`application/pdf` and `text/html` by default)
- whether follow-up extraction is permitted
- which path the registry may write to

Enforcement is **deterministic and pre-action**: a `PolicyViolationError` is raised before any network call or registry write. Both the legacy path and the LangGraph path enforce the same execution policy — neither engine can bypass what the other enforces.

Three enforcement invariants are worth calling out:

- The download egress cap (`download.max_count`) is enforced against `fetch_attempt_count`, incremented *before* the network call, so repeated failed downloads cannot bypass the cap.
- Boolean policy fields (`download.enabled`, `followup_search.enabled`) require real YAML booleans — quoted strings like `"false"` are rejected with a clear error rather than silently becoming truthy.
- The registry dedupe loop verifies `payload["entity_id"] == entity_id` before reusing a cached artifact, so two entity IDs that sanitize to the same directory prefix cannot cross-wire each other's cache.

The `discover`, `review`, `download`, and `followup-search` commands each write a `resolved_execution_policy.json` sidecar, so a reviewer can reconstruct the exact runtime constraints in force for that acquisition run. The `scan` and `eval` commands do not perform network acquisition and do not emit this sidecar.

To use a custom execution policy:

```bash
doc-workbench discover \
  --entities examples/public_companies.csv \
  --execution-policy-path /path/to/custom_execution_policy.yaml
```

### Decision traces

Discovery and review both emit explainability sidecars:

| File | Purpose |
|---|---|
| `ranking_trace.json` | Shows candidate scoring inputs and final confidence |
| `review_trace.json` | Shows confidence band, reason codes, and recommendation |

### Observability model

Two observability layers run in parallel:

| Layer | Behavior |
|---|---|
| Local traces | Always on. JSON spans are written under `workspace/traces/` for discovery, follow-up, ranking, and review stages. |
| Langfuse remote traces | Optional. Enabled only when the explicit opt-in gate and credentials are both present. |

Both layers share a common `trace_id` (UUID) generated at CLI entry, enabling correlation between local debugging artifacts and remote telemetry.

```bash
export DOC_WORKBENCH_ENABLE_LANGFUSE=1
export LANGFUSE_SECRET_KEY=sk-...
export LANGFUSE_PUBLIC_KEY=pk-...
export LANGFUSE_HOST=https://cloud.langfuse.com
```

If those variables are absent, the workbench keeps running with local traces only. No crash. No warning spam.

Important observability guarantees:

- Remote Langfuse spans are emitted only by the LangGraph path.
- Eval runs suppress Langfuse even if the environment is configured for remote tracing.
- Telemetry URLs are sanitized before remote emission so embedded credentials are never forwarded.
- Local JSON traces and remote Langfuse spans share the same `trace_id` for cross-system correlation.

## Evals

The eval harness ships inside the package at `doc_workbench/evals/`, so it works after installation without the source tree.

```bash
doc-workbench eval
python -m doc_workbench.evals.run_evals
```

Output: `eval_report.json` (written to current directory)

Per-fixture metrics:

- `candidate_recall`
- `top_ranked_correct`
- `top_recommendation_correct`

## Where To Look First

| If you care about... | Start here |
|---|---|
| Provider orchestration and public acquisition flow | `doc_workbench/acquisition/discovery.py` |
| LangGraph node boundaries and state handoff | `doc_workbench/orchestration/nodes.py` and `doc_workbench/orchestration/graph.py` |
| Context policy packaging and runtime loading | `doc_workbench/policy.py` and `doc_workbench/context/` |
| Execution policy enforcement | `doc_workbench/execution_policy.py` |
| Safe remote observability | `doc_workbench/observability/langfuse_bridge.py` |
| Review classification and queue building | `doc_workbench/review/workflow.py` |
| Post-download parse routing and quality signals | `doc_workbench/intake/detector.py` and `doc_workbench/intake/parser.py` |
| Versioned extraction records and risk scoring | `doc_workbench/intake/extractor.py` |
| Retrieval-ready chunk packaging | `doc_workbench/knowledge/chunker.py` and `doc_workbench/knowledge/packager.py` |
| Installable regression harness | `doc_workbench/evals/run_evals.py` |
| Hardened local runtime | `docs/hardened-runtime.md` |

## Public-Safe Boundaries

What this repo preserves from the production pattern:

- module boundaries and staged execution flow
- provider abstraction and fallback acquisition strategy
- policy-driven ranking and review
- registry-backed persistence
- trace and artifact lifecycle

What is intentionally generalized or removed:

- proprietary business rules
- internal datasets
- rollout logic and operational controls tied to private infrastructure
- company-specific storage conventions

## More Detail

- Architecture reference: [`docs/architecture.md`](docs/architecture.md)
- Hardened local runtime: [`docs/hardened-runtime.md`](docs/hardened-runtime.md)
- GCP deployment guide: [`docs/deployment_gcp.md`](docs/deployment_gcp.md)
