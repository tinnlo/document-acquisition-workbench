"""LangGraph node functions for the document acquisition workflow.

Each node receives the full WorkbenchState, performs one stage, and returns
a dict of updated keys.  LangGraph merges the returned dict back into state.
"""

from __future__ import annotations

import asyncio
import copy
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse, urlunparse

from doc_workbench.acquisition.discovery import (
    _followup_allowed,
    _top_candidate_fields,
    discover_entity,
    score_candidate,
)
from doc_workbench.acquisition.followup.workflow import run_followup_for_candidates
from doc_workbench.models import DiscoveryCandidate, DiscoveryRecord
from doc_workbench.observability.langfuse_bridge import get_langfuse_client
from doc_workbench.orchestration.state import WorkbenchState
from doc_workbench.policy import ContextPolicy
from doc_workbench.review.workflow import build_review_rows_from_records
from doc_workbench.execution_policy import PolicyViolationError, enforce_followup_search
from doc_workbench.intake.guards import (
    check_artifact_path as _guard_artifact,
    check_sidecar_path as _guard_sidecar,
    validate_parse_sidecar_basename as _validate_parse_sidecar_basename_shared,
    validate_extraction_sidecar_basename as _validate_extraction_sidecar_basename_shared,
)


# ---------------------------------------------------------------------------
# Telemetry helpers
# ---------------------------------------------------------------------------

def _sanitize_url_for_telemetry(url: str) -> str:
    """Reduce a URL to scheme+hostname before sending to remote telemetry.

    Paths can carry opaque document IDs, signed tokens, or sensitive filenames.
    Credentials embedded in the authority component (``user:pass@host``) are
    explicitly stripped.  Only the scheme and hostname (with safe port, if
    present) are forwarded to Langfuse; everything else (credentials, path,
    query, fragment) stays in local ``workspace/traces/`` artifacts only.

    Examples
    --------
    >>> _sanitize_url_for_telemetry("https://example.com/ar/2024.pdf?token=x")
    'https://example.com'
    >>> _sanitize_url_for_telemetry("https://user:pass@example.com/report.pdf")
    'https://example.com'
    >>> _sanitize_url_for_telemetry("")
    ''
    """
    if not url:
        return url
    try:
        parsed = urlparse(url)
        # Build a clean netloc from hostname + optional port only.
        # parsed.netloc may contain "user:pass@host:port" — use parsed.hostname
        # and parsed.port instead to explicitly exclude credentials.
        hostname = parsed.hostname or ""
        if parsed.port:
            clean_netloc = f"{hostname}:{parsed.port}"
        else:
            clean_netloc = hostname
        return urlunparse((parsed.scheme, clean_netloc, "", "", "", ""))
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# Node: discover
# ---------------------------------------------------------------------------

def discover_node(state: WorkbenchState) -> dict[str, Any]:
    """Run per-entity discovery (official site + regulatory + search).

    Returns the full raw candidate pool without dedup/sort/cap so that
    followup_node and rank_node operate on the complete set.  The final
    truncation to 10 candidates is owned by rank_node.

    Does NOT run follow-up extraction — that is delegated to followup_node.
    """
    entities = state["entities"]
    policy: ContextPolicy = state["policy"]
    tracer = state.get("tracer")
    lf = get_langfuse_client(trace_id=tracer.trace_id if tracer else None)

    exec_policy = state.get("exec_policy")

    async def _run_one(entity: Any) -> tuple[DiscoveryRecord, float]:
        t0 = time.perf_counter()
        record = await discover_entity(
            entity, followup_search=False, policy=policy, tracer=tracer,
            _skip_ranking=True, _force_skip_followup=True,
            exec_policy=exec_policy,
        )
        return record, (time.perf_counter() - t0) * 1000.0

    async def _run_all() -> list[tuple[DiscoveryRecord, float]]:
        return list(await asyncio.gather(*[_run_one(e) for e in entities]))

    results: list[tuple[DiscoveryRecord, float]] = asyncio.run(_run_all())
    records = [r for r, _ in results]

    if tracer is not None:
        for record, entity_ms in results:
            top_candidate = max(record.candidates, key=lambda c: c.confidence, default=None)
            top_url_local = top_candidate.url if top_candidate else ""
            top_conf_local = top_candidate.confidence if top_candidate else 0.0
            tracer.add_span(
                entity_id=record.entity.entity_id,
                stage="discover_entity_pre_rank",
                provider="orchestrator",
                latency_ms=entity_ms,
                candidate_count_in=0,
                candidate_count_out=len(record.candidates),
                top_candidate_url=top_url_local,
                top_confidence=top_conf_local,
                details={"ranking_deferred": True},
                retry_count=0,
            )

    if lf is not None:
        for record, entity_ms in results:
            # candidates are unsorted (ranking is deferred to rank_node);
            # compute the top candidate explicitly rather than assuming sorted order.
            top_candidate = max(record.candidates, key=lambda c: c.confidence, default=None)
            top_url = top_candidate.url if top_candidate else ""
            top_conf = top_candidate.confidence if top_candidate else 0.0
            lf.flush_span(
                stage="discover_entity_pre_rank",
                entity_id=record.entity.entity_id,
                latency_ms=entity_ms,
                candidate_count_in=0,
                candidate_count_out=len(record.candidates),
                top_candidate_url=_sanitize_url_for_telemetry(top_url),
                top_confidence=top_conf,
                ranking_deferred=True,
            )

    return {"discovery_records": records}


# ---------------------------------------------------------------------------
# Node: followup
# ---------------------------------------------------------------------------

def followup_node(state: WorkbenchState) -> dict[str, Any]:
    """Run follow-up extraction over search-sourced candidates.

    Enriches each DiscoveryRecord's candidate list with promoted follow-up
    candidates.  Produces followup_records (copy of discovery_records with
    followup candidates appended).

    Respects execution policy: if ``exec_policy.followup_search.enabled`` is
    ``False``, the node raises ``PolicyViolationError`` before any extraction.
    """
    records: list[DiscoveryRecord] = state.get("discovery_records", [])
    policy: ContextPolicy = state["policy"]
    exec_policy = state.get("exec_policy")
    followup_search: bool = state.get("followup_search", False)
    tracer = state.get("tracer")
    lf = get_langfuse_client(trace_id=tracer.trace_id if tracer else None)

    # Execution-policy enforcement: only enforce followup_search.enabled when
    # follow-up was actually requested. Enforcing unconditionally would cause
    # "discover --no-followup-search" with a policy that disables follow-up to
    # fail, while the legacy path succeeds (behaviour mismatch).
    if exec_policy is not None and followup_search:
        enforce_followup_search(exec_policy)

    enriched: list[DiscoveryRecord] = []

    async def _run_followup_for_record(record: DiscoveryRecord) -> tuple[DiscoveryRecord, float, list[DiscoveryCandidate]]:
        t0 = time.perf_counter()
        search_candidates = [c for c in record.candidates if c.source_type == "search"]
        followup_enabled, _ = _followup_allowed(
            policy=policy,
            followup_search=followup_search,
            official_candidates=[c for c in record.candidates if c.source_tier == "official"],
            regulatory_candidates=[c for c in record.candidates if c.source_tier == "regulatory"],
        )
        if not followup_enabled or not search_candidates:
            # No enrichment — return a deep copy to preserve stage isolation.
            return DiscoveryRecord(
                entity=record.entity,
                status=record.status,
                candidates=[copy.copy(c) for c in record.candidates],
                errors=list(record.errors),
            ), (time.perf_counter() - t0) * 1000.0, []

        seeds = [
            c for c in search_candidates
            if c.source_tier in policy.followup_search.allowed_seed_source_tiers
        ]
        if not seeds:
            return DiscoveryRecord(
                entity=record.entity,
                status=record.status,
                candidates=[copy.copy(c) for c in record.candidates],
                errors=list(record.errors),
            ), (time.perf_counter() - t0) * 1000.0, []

        errors: list[str] = list(record.errors)
        try:
            _results, promoted = await run_followup_for_candidates(
                record.entity, seeds, materialize=False, registry=None, exec_policy=exec_policy
            )
        except PolicyViolationError:
            raise
        except Exception as exc:
            errors.append(f"followup_node:{type(exc).__name__}: {exc}")
            return DiscoveryRecord(
                entity=record.entity,
                status=record.status,
                candidates=[copy.copy(c) for c in record.candidates],
                errors=errors,
            ), (time.perf_counter() - t0) * 1000.0, []

        # Keep raw promoted list for telemetry BEFORE dedup.
        raw_promoted = list(promoted)

        all_candidates = [copy.copy(c) for c in record.candidates] + promoted
        deduped: dict[str, DiscoveryCandidate] = {}
        for c in all_candidates:
            existing = deduped.get(c.url)
            if existing is None or c.confidence > existing.confidence:
                deduped[c.url] = c
        new_candidates = sorted(deduped.values(), key=lambda c: c.confidence, reverse=True)
        return DiscoveryRecord(
            entity=record.entity,
            status=record.status,
            candidates=new_candidates,
            errors=errors,
        ), (time.perf_counter() - t0) * 1000.0, raw_promoted

    async def _run_all() -> list[tuple[DiscoveryRecord, float, list[DiscoveryCandidate]]]:
        return list(await asyncio.gather(*[_run_followup_for_record(r) for r in records]))

    followup_results = asyncio.run(_run_all())
    enriched = [r for r, _, _ in followup_results]

    if tracer is not None:
        for orig, (enriched_record, entity_ms, raw_promoted) in zip(records, followup_results):
            # Count all search candidates as input — matching the legacy
            # followup_extraction span which uses len(search_candidates).
            # Use raw promoted (before dedup) for output counts — matching
            # legacy which traces len(followup_candidates) before dedup.
            search_in = [c for c in orig.candidates if c.source_type == "search"]
            followup_enabled, followup_reason = _followup_allowed(
                policy=policy,
                followup_search=followup_search,
                official_candidates=[c for c in orig.candidates if c.source_tier == "official"],
                regulatory_candidates=[c for c in orig.candidates if c.source_tier == "regulatory"],
            )
            top_url_local, top_conf_local = _top_candidate_fields(raw_promoted)
            tracer.add_span(
                entity_id=enriched_record.entity.entity_id,
                stage="followup_extraction",
                provider="followup_search",
                latency_ms=entity_ms,
                candidate_count_in=len(search_in),
                candidate_count_out=len(raw_promoted),
                top_candidate_url=top_url_local,
                top_confidence=top_conf_local,
                details={"enabled": followup_enabled, "reason": followup_reason},
                retry_count=0,
            )

    if lf is not None:
        for orig, (enriched_record, entity_ms_lf, raw_promoted_lf) in zip(records, followup_results):
            # Count all search candidates as input — matching legacy parity.
            # Use raw promoted (before dedup) for output counts.
            search_in_lf = [c for c in orig.candidates if c.source_type == "search"]
            followup_enabled_lf, followup_reason_lf = _followup_allowed(
                policy=policy,
                followup_search=followup_search,
                official_candidates=[c for c in orig.candidates if c.source_tier == "official"],
                regulatory_candidates=[c for c in orig.candidates if c.source_tier == "regulatory"],
            )
            top_url_lf, top_conf_lf = _top_candidate_fields(raw_promoted_lf)
            lf.flush_span(
                stage="followup_extraction",
                entity_id=enriched_record.entity.entity_id,
                latency_ms=entity_ms_lf,
                candidate_count_in=len(search_in_lf),
                candidate_count_out=len(raw_promoted_lf),
                top_candidate_url=_sanitize_url_for_telemetry(top_url_lf),
                top_confidence=top_conf_lf,
                enabled=followup_enabled_lf,
                reason=followup_reason_lf,
            )

    return {"followup_records": enriched}


# ---------------------------------------------------------------------------
# Node: rank
# ---------------------------------------------------------------------------

def rank_node(state: WorkbenchState) -> dict[str, Any]:
    """Deduplicate, re-score, sort, and cap candidates at 10 per entity.

    Operates on followup_records (or falls back to discovery_records).
    Returns fresh DiscoveryRecord objects — does not mutate the input records.
    """
    records: list[DiscoveryRecord] = state.get("followup_records") or state.get("discovery_records", [])
    policy: ContextPolicy = state["policy"]
    tracer = state.get("tracer")
    lf = get_langfuse_client(trace_id=tracer.trace_id if tracer else None)

    ranked: list[DiscoveryRecord] = []
    per_record_ms: list[float] = []

    for record in records:
        record_start = time.perf_counter()
        # Re-score all candidates through the policy — work on copies so
        # the source records (followup_records / discovery_records) are not
        # mutated and the two state keys remain independent.
        rescored: list[DiscoveryCandidate] = []
        for candidate in record.candidates:
            scored, _ = score_candidate(record.entity, copy.copy(candidate), policy)
            rescored.append(scored)
        # Dedup by URL, keep highest-confidence copy
        deduped: dict[str, DiscoveryCandidate] = {}
        for c in rescored:
            existing = deduped.get(c.url)
            if existing is None or c.confidence > existing.confidence:
                deduped[c.url] = c
        new_candidates = sorted(deduped.values(), key=lambda c: c.confidence, reverse=True)[:10]
        # Return a fresh record rather than mutating the shared input object.
        ranked.append(DiscoveryRecord(
            entity=record.entity,
            status=record.status,
            candidates=new_candidates,
            errors=list(record.errors),
        ))
        per_record_ms.append((time.perf_counter() - record_start) * 1000.0)

    if tracer is not None:
        for orig_record, ranked_record, entity_ms in zip(records, ranked, per_record_ms):
            top_url_local, top_conf_local = _top_candidate_fields(ranked_record.candidates)
            tracer.add_span(
                entity_id=ranked_record.entity.entity_id,
                stage="candidate_ranking",
                provider="ranking_policy",
                latency_ms=entity_ms,
                candidate_count_in=len(orig_record.candidates),
                candidate_count_out=len(ranked_record.candidates),
                top_candidate_url=top_url_local,
                top_confidence=top_conf_local,
                details={"ranking_deferred": False},
                retry_count=0,
            )

    if lf is not None:
        for orig_record, ranked_record, entity_ms in zip(records, ranked, per_record_ms):
            top_url, top_conf = _top_candidate_fields(ranked_record.candidates)
            lf.flush_span(
                stage="candidate_ranking",
                entity_id=ranked_record.entity.entity_id,
                latency_ms=entity_ms,
                candidate_count_in=len(orig_record.candidates),
                candidate_count_out=len(ranked_record.candidates),
                top_candidate_url=_sanitize_url_for_telemetry(top_url),
                top_confidence=top_conf,
                ranking_deferred=False,
            )

    return {"ranked_records": ranked}


# ---------------------------------------------------------------------------
# Node: review_prep
# ---------------------------------------------------------------------------

def review_prep_node(state: WorkbenchState) -> dict[str, Any]:
    """Build review rows from ranked records.

    Delegates to build_review_rows_from_records (record-list variant).
    Emits a local tracer span so the review stage appears in workspace/traces/,
    and a remote Langfuse span when observability is enabled.
    """
    records: list[DiscoveryRecord] = state.get("ranked_records") or state.get("discovery_records", [])
    policy: ContextPolicy = state["policy"]
    tracer = state.get("tracer")
    lf = get_langfuse_client(trace_id=tracer.trace_id if tracer else None)

    start = time.perf_counter()
    rows, review_trace, recommendation_summary = build_review_rows_from_records(records, policy)
    latency_ms = (time.perf_counter() - start) * 1000.0

    if tracer is not None:
        tracer.add_span(
            entity_id="all",
            stage="review_queue_generation",
            provider="review_policy",
            latency_ms=latency_ms,
            candidate_count_in=sum(len(r.candidates) for r in records),
            candidate_count_out=len(rows),
            recommendation_summary=recommendation_summary,
            retry_count=0,
        )

    if lf is not None:
        lf.flush_span(
            stage="review_queue_generation",
            entity_id="all",
            latency_ms=latency_ms,
            candidate_count_in=sum(len(r.candidates) for r in records),
            candidate_count_out=len(rows),
            top_candidate_url="",
            top_confidence=0.0,
            recommendation_summary=recommendation_summary,
        )

    return {
        "review_rows": rows,
        "review_trace": review_trace,
        "recommendation_summary": recommendation_summary,
    }


# ---------------------------------------------------------------------------
# Intake node functions (parse / extract / chunk)
# ---------------------------------------------------------------------------

def _require_intake_target(state: WorkbenchState) -> None:
    """Raise ValueError if none of the three intake target selectors are set."""
    if (
        not state.get("intake_all")
        and not state.get("intake_entity_id")
        and not state.get("intake_document_ids")
    ):
        raise ValueError(
            "At least one of intake_document_ids, intake_entity_id, or intake_all=True "
            "must be set in state before calling intake nodes."
        )


_DEFAULT_MAX_FILE_BYTES = 52_428_800  # 50 MiB — mirrors execution policy default


def _enforce_registry_root_node(state: WorkbenchState, artifact_path: "Path") -> None:
    """Enforce exec_policy.registry.root_restriction parity with the CLI.

    When *exec_policy* is present in state, *intake_workspace_root* **must** also
    be provided — mirroring the CLI, which always passes the workspace root.
    Raises ``PolicyViolationError`` (not ``ValueError``) when *intake_workspace_root*
    is absent and an *exec_policy* is in effect, so callers cannot silently bypass
    the restriction check.

    When *exec_policy* is absent (e.g. integration tests without policy), the call
    is a no-op; the shared ``_guard_artifact`` / ``_guard_sidecar`` helpers still
    enforce basic registry-root containment.
    """
    exec_policy = state.get("exec_policy")
    if exec_policy is None:
        return
    workspace_root = state.get("intake_workspace_root")
    if workspace_root is None:
        from doc_workbench.execution_policy import PolicyViolationError
        raise PolicyViolationError(
            "intake_workspace_root must be set in state whenever exec_policy is present. "
            "This is required to enforce registry.root_restriction parity with the CLI."
        )
    from doc_workbench.execution_policy import enforce_registry_root as _err
    _err(exec_policy, artifact_path, Path(workspace_root))


def _max_file_bytes(state: WorkbenchState) -> int:
    """Return the file-size limit to apply in intake nodes.

    Reads from ``state["exec_policy"].download.max_file_size_bytes`` when an
    execution policy is present; otherwise falls back to the 50 MiB default.
    """
    exec_policy = state.get("exec_policy")
    try:
        if exec_policy is not None:
            return int(exec_policy.download.max_file_size_bytes)
    except Exception:
        pass
    return _DEFAULT_MAX_FILE_BYTES


def _validate_parse_sidecar_filename(filename: str) -> str:
    """Return a validated parse sidecar basename.

    Accepts only bare filenames matching
    ``parse_record.<timestamp>.json`` where timestamp is the project's
    microsecond-precision UTC form.
    """
    import re as _re

    pattern = _re.compile(r"^parse_record\.\d{8}T\d{6}\d+Z\.json$")
    basename = Path(filename).name
    if basename != filename or not pattern.match(basename):
        raise ValueError(
            f"Invalid parse sidecar filename {filename!r}. "
            "Must be a bare filename matching parse_record.<ts>.json"
        )
    return basename


def parse_node(state: WorkbenchState) -> dict:
    """LangGraph node: parse all targeted documents.

    Reads:  intake_document_ids | intake_entity_id | intake_all
            intake_force, intake_registry_root
    Writes: parse_records

    Each entry in parse_records contains:
      document_id, parse_status, parse_sidecar_filename, validation_errors,
      manifest (pass-through for extract_node), or error.
    """
    _require_intake_target(state)

    exec_policy = state.get("exec_policy")
    if exec_policy is not None:
        from doc_workbench.execution_policy import enforce_command_stage as _enforce
        _enforce(exec_policy, "analyze")

    import json as _json
    from doc_workbench.registry.document_registry import DocumentRegistry
    from doc_workbench.intake.parser import run_parse
    from doc_workbench.intake.validation import validate_parse_record

    registry = DocumentRegistry(state["intake_registry_root"])
    entity_id: str = state.get("intake_entity_id") or ""
    doc_ids: list[str] = state.get("intake_document_ids") or []
    force: bool = bool(state.get("intake_force"))
    registry_root = state["intake_registry_root"]

    manifests = registry.list_manifests(entity_id or None)
    if doc_ids:
        manifests = [m for m in manifests if str(m.get("document_id") or "") in doc_ids]

    results: list[dict] = []
    for manifest in manifests:
        document_id = str(manifest.get("document_id") or "")
        pipeline_status = manifest.get("pipeline_status") or {}
        if pipeline_status.get("parse_status") == "complete" and not force:
            continue
        if pipeline_status.get("download_status") != "complete":
            continue

        artifact_path = registry._normalize_manifest_path(str(manifest["local_path"]))
        try:
            # Enforce root_restriction parity with CLI, then containment/symlink/size guards.
            _enforce_registry_root_node(state, artifact_path)
            _guard_artifact(artifact_path, Path(registry_root).resolve(), _max_file_bytes(state))

            record = run_parse(
                document_id=document_id,
                local_path=artifact_path,
                manifest=manifest,
                run_id="langgraph",
            )
            validation_errs = validate_parse_record(record)
            parse_sidecar_path = registry.write_analysis_sidecar(
                document_id, "parse_record", record.to_dict()
            )
            registry.update_manifest(document_id, {
                "pipeline_status": {"parse_status": record.parse_status}
            })
            results.append({
                "document_id": document_id,
                "parse_status": record.parse_status,
                "parse_sidecar_filename": parse_sidecar_path.name,
                "validation_errors": validation_errs,
                "manifest": manifest,
            })
        except Exception as exc:
            from doc_workbench.execution_policy import PolicyViolationError as _PVE
            if isinstance(exc, (_PVE, ValueError)):
                raise
            results.append({
                "document_id": document_id,
                "error": f"{type(exc).__name__}: {exc}",
                "manifest": manifest,
            })

    return {"parse_records": results}


def extract_node(state: WorkbenchState) -> dict:
    """LangGraph node: run extraction over documents processed by parse_node.

    Reads:  parse_records (from state), intake_registry_root
    Writes: extraction_records

    Each successful entry in extraction_records includes an
    ``extraction_sidecar_filename`` key containing the exact timestamped
    filename written during this run.  chunk_node uses that filename directly
    so it never accidentally picks up a later or tampered sidecar.

    Iterates over parse_records entries that have a parse_sidecar_filename
    (i.e. successfully parsed).  Documents with errors in parse_records are
    passed through with an error entry in extraction_records.
    """
    exec_policy = state.get("exec_policy")
    if exec_policy is not None:
        from doc_workbench.execution_policy import enforce_command_stage as _enforce
        _enforce(exec_policy, "analyze")

    from doc_workbench.registry.document_registry import DocumentRegistry
    from doc_workbench.intake.extractor import run_extraction

    registry = DocumentRegistry(state["intake_registry_root"])
    parse_records: list[dict] = state.get("parse_records") or []

    results: list[dict] = []
    for entry in parse_records:
        document_id = str(entry.get("document_id") or "")
        if "error" in entry or not entry.get("parse_sidecar_filename"):
            results.append({
                "document_id": document_id,
                "error": entry.get("error", "no parse_sidecar_filename available"),
            })
            continue

        manifest = entry.get("manifest") or {}
        parse_sidecar_filename = _validate_parse_sidecar_basename_shared(
            str(entry["parse_sidecar_filename"])
        )
        analysis_dir = registry.ensure_analysis_dir(document_id)
        parse_path = (analysis_dir / parse_sidecar_filename).resolve()

        try:
            import json as _json
            # Sidecar containment + symlink + size guards via shared helper
            _guard_sidecar(parse_path, registry.registry_root.resolve(), _max_file_bytes(state))
            from doc_workbench.intake.models import ParseRecord
            parse_record = ParseRecord.from_dict(
                _json.loads(parse_path.read_text(encoding="utf-8"))
            )
            extraction = run_extraction(
                document_id=document_id,
                manifest=manifest,
                parse_record=parse_record,
                parse_record_filename=parse_sidecar_filename,
                run_id="langgraph",
                parse_validation_errors=entry.get("validation_errors") or [],
            )
            extraction_sidecar_path = registry.write_analysis_sidecar(
                document_id, "extraction_record", extraction.to_dict()
            )
            results.append({
                "document_id": document_id,
                "indexing_acceptance": extraction.indexing_acceptance,
                "risk_level": extraction.risk_level,
                "parse_sidecar_filename": parse_sidecar_filename,
                "extraction_sidecar_filename": extraction_sidecar_path.name,
                "manifest": manifest,
            })
        except Exception as exc:
            from doc_workbench.execution_policy import PolicyViolationError as _PVE
            if isinstance(exc, (_PVE, ValueError)):
                raise
            results.append({
                "document_id": document_id,
                "error": f"{type(exc).__name__}: {exc}",
            })

    return {"extraction_records": results}


def chunk_node(state: WorkbenchState) -> dict:
    """LangGraph node: chunk index-ready documents.

    Reads:  extraction_records (from state), intake_registry_root, intake_force
    Writes: chunk_records
    """
    exec_policy = state.get("exec_policy")
    if exec_policy is not None:
        from doc_workbench.execution_policy import enforce_command_stage as _enforce
        _enforce(exec_policy, "chunk")

    import json as _json
    import re as _re
    from datetime import datetime as _dt, timezone as _tz
    from doc_workbench.registry.document_registry import DocumentRegistry
    from doc_workbench.intake.models import ParseRecord
    from doc_workbench.intake.extractor import ExtractionRecord
    from doc_workbench.knowledge.chunker import chunk_document
    from doc_workbench.knowledge.packager import write_chunk_jsonl

    registry = DocumentRegistry(state["intake_registry_root"])
    registry_root = state["intake_registry_root"]
    force: bool = bool(state.get("intake_force"))
    extraction_records: list[dict] = state.get("extraction_records") or []

    _REF_RE = _re.compile(r"^parse_record\.\d{8}T\d{6}\d+Z\.json$")

    results: list[dict] = []
    for entry in extraction_records:
        document_id = str(entry.get("document_id") or "")
        if "error" in entry:
            results.append({"document_id": document_id, "error": entry["error"]})
            continue

        manifest = entry.get("manifest") or {}
        pipeline_status = manifest.get("pipeline_status") or {}
        if pipeline_status.get("chunking_status") == "complete" and not force:
            continue

        if entry.get("indexing_acceptance") != "index_ready":
            registry.update_manifest(document_id, {"pipeline_status": {"chunking_status": "skipped"}})
            continue

        artifact_path = registry._normalize_manifest_path(str(manifest.get("local_path", "")))
        try:
            # Enforce root_restriction parity with CLI, then containment/symlink/size guards.
            _enforce_registry_root_node(state, artifact_path)
            _guard_artifact(artifact_path, Path(registry_root).resolve(), _max_file_bytes(state))

            # Load the exact extraction sidecar written by extract_node in this
            # graph run.  Validate the filename as a strict bare basename first
            # (same pattern as parse sidecars) to block traversal values like
            # '../other_doc/analysis/extraction_record.<ts>.json'.  Then assert
            # the resolved path is owned by *this* document's analysis_dir, not
            # merely somewhere under registry_root.
            extraction_sidecar_filename = entry.get("extraction_sidecar_filename")
            if not extraction_sidecar_filename:
                raise FileNotFoundError(
                    f"extraction_sidecar_filename missing from state for {document_id}; "
                    "extract_node must run before chunk_node"
                )
            # Strict basename validation — raises ValueError on any traversal attempt.
            extraction_sidecar_filename = _validate_extraction_sidecar_basename_shared(
                str(extraction_sidecar_filename)
            )
            analysis_dir = registry.ensure_analysis_dir(document_id)
            extraction_sidecar_path = (analysis_dir / extraction_sidecar_filename).resolve()
            # Ownership check: path must be inside *this* document's analysis_dir,
            # not just anywhere under registry_root.
            try:
                extraction_sidecar_path.relative_to(analysis_dir.resolve())
            except ValueError:
                raise ValueError(
                    f"extraction_sidecar_path '{extraction_sidecar_path}' is outside "
                    f"the analysis dir for document '{document_id}'. Blocked."
                )
            _guard_sidecar(extraction_sidecar_path, registry.registry_root.resolve(), _max_file_bytes(state))
            extraction = ExtractionRecord.from_dict(
                _json.loads(extraction_sidecar_path.read_text(encoding="utf-8"))
            )

            basename = extraction.parse_record_ref
            if not _REF_RE.match(basename):
                raise ValueError(f"Invalid parse_record_ref: {basename!r}")
            # Also validate via shared helper for consistency
            _validate_parse_sidecar_basename_shared(basename)
            analysis_dir = registry.ensure_analysis_dir(document_id)
            parse_path = (analysis_dir / basename).resolve()
            _guard_sidecar(parse_path, registry.registry_root.resolve(), _max_file_bytes(state))
            parse_record = ParseRecord.from_dict(
                _json.loads(parse_path.read_text(encoding="utf-8"))
            )

            chunk_iter = chunk_document(
                local_path=artifact_path,
                manifest=manifest,
                parse_record=parse_record,
                extraction_record=extraction,
                run_id="langgraph",
            )
            if chunk_iter is None:
                registry.update_manifest(document_id, {"pipeline_status": {"chunking_status": "skipped"}})
                continue

            # Write JSONL with no-overwrite collision retry (up to 3 attempts).
            # Existence pre-check + exclusive-create ("x") together close the TOCTOU window.
            chunk_path = None
            chunk_count = 0
            for _attempt in range(3):
                ts = _dt.now(_tz.utc).strftime("%Y%m%dT%H%M%S%f") + "Z"
                candidate = analysis_dir / f"chunks.{ts}.jsonl"
                if candidate.exists() or candidate.is_symlink():
                    continue
                try:
                    chunk_count = write_chunk_jsonl(chunk_iter, candidate)
                    chunk_path = candidate
                    break
                except FileExistsError:
                    continue
            if chunk_path is None:
                raise RuntimeError(
                    f"Failed to write chunk JSONL after 3 attempts for {document_id}"
                )
            registry.update_manifest(document_id, {"pipeline_status": {"chunking_status": "complete"}})
            results.append({
                "document_id": document_id,
                "chunk_count": chunk_count,
                "chunk_file": chunk_path.name,
            })
        except Exception as exc:
            from doc_workbench.execution_policy import PolicyViolationError as _PVE
            if isinstance(exc, (_PVE, ValueError)):
                raise
            results.append({"document_id": document_id, "error": f"{type(exc).__name__}: {exc}"})
            try:
                registry.update_manifest(document_id, {"pipeline_status": {"chunking_status": "failed"}})
            except Exception:
                pass

    return {"chunk_records": results}
