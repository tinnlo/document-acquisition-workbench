from __future__ import annotations

import asyncio
import contextlib
import csv
import json
import uuid
from datetime import datetime, timezone
from typing import Any
import click
import time
from pathlib import Path
import typer
from rich.console import Console
from rich.table import Table

from doc_workbench.acquisition.discovery import (
    build_ranking_trace,
    discover_entity,
    load_entities,
    write_discovery_artifacts,
)
from doc_workbench.acquisition.followup.workflow import (
    load_discovery_records,
    run_followup_for_candidates,
    write_followup_artifacts,
)
from doc_workbench.config import VALID_ENGINES, WorkspacePaths, resolve_engine
from doc_workbench.intake.guards import (
    check_artifact_path as _guard_artifact,
    check_sidecar_path as _guard_sidecar,
    validate_parse_sidecar_basename as _validate_parse_sidecar_basename,
)
from doc_workbench.models import DownloadRow, MetadataScanRow
from doc_workbench.observability.tracer import RunTrace, summarize_trace
from doc_workbench.execution_policy import (
    PolicyViolationError,
    load_execution_policy,
    write_resolved_execution_policy,
    enforce_command_stage,
    enforce_file_size,
    enforce_followup_search,
    enforce_registry_root,
)
from doc_workbench.policy import load_context_policy, write_resolved_policy
from doc_workbench.registry.document_registry import DocumentRegistry
from doc_workbench.registry.metadata_scanner import scan_pdf
from doc_workbench.review.workflow import build_review_rows, write_review_csv
from doc_workbench.storage.downloader import download_bytes

app = typer.Typer(help="Public document acquisition workbench.", no_args_is_help=True)
console = Console()


def _policy_guard() -> "contextlib.AbstractContextManager[None]":
    """Context manager that catches PolicyViolationError and exits with code 2.

    Wrap every command body with this so all commands emit a consistent,
    machine-readable signal on policy denial instead of an unhandled traceback.
    """
    import contextlib

    @contextlib.contextmanager
    def _cm():  # type: ignore[return]
        try:
            yield
        except PolicyViolationError as exc:
            console.print(f"[red]Policy violation: {exc}[/red]")
            raise typer.Exit(code=2) from exc

    return _cm()


@app.command("paths")
def show_paths(workspace_root: str | None = typer.Option(None, "--workspace-root")) -> None:
    paths = WorkspacePaths.resolve(workspace_root)
    paths.ensure()
    table = Table(title="Workspace Paths")
    table.add_column("Name")
    table.add_column("Path")
    table.add_row("root", str(paths.root))
    table.add_row("registry_root", str(paths.registry_root))
    table.add_row("runs_root", str(paths.runs_root))
    table.add_row("cache_root", str(paths.cache_root))
    table.add_row("traces_root", str(paths.traces_root))
    console.print(table)


@app.command("policy")
def show_policy(policy_path: str | None = typer.Option(None, "--policy-path")) -> None:
    policy = load_context_policy(policy_path)
    console.print_json(json.dumps({"policy_digest": policy.digest, **policy.to_dict()}, indent=2))


@app.command("trace-summary")
def trace_summary(
    input_path: Path = typer.Option(..., "--input"),
) -> None:
    summary = summarize_trace(input_path)
    console.print_json(json.dumps(summary, indent=2))


@app.command("discover")
def discover(
    entities: Path = typer.Option(Path("examples/public_companies.csv"), "--entities"),
    workspace_root: str | None = typer.Option(None, "--workspace-root"),
    followup_search: bool = typer.Option(False, "--followup-search/--no-followup-search"),
    policy_path: str | None = typer.Option(None, "--policy-path"),
    execution_policy_path: str | None = typer.Option(None, "--execution-policy-path"),
    engine: str | None = typer.Option(None, "--engine", help="Engine to use.", click_type=click.Choice(list(VALID_ENGINES))),
) -> None:
    with _policy_guard():
        paths = WorkspacePaths.resolve(workspace_root)
        paths.ensure()
        policy = load_context_policy(policy_path)
        exec_policy = load_execution_policy(execution_policy_path)
        enforce_command_stage(exec_policy, "discover")
        # If follow-up is requested, enforce execution policy before any work
        # begins. This applies to both the legacy and LangGraph paths so neither
        # engine can bypass what the other enforces.
        # followup-search also fetches and materializes seed documents, so
        # download.enabled must be checked here as well as in followup_search cmd.
        if followup_search:
            enforce_followup_search(exec_policy)
            from doc_workbench.execution_policy import enforce_download_enabled
            enforce_download_enabled(exec_policy)
        try:
            selected_engine = resolve_engine(engine)
        except ValueError as exc:
            raise click.BadParameter(str(exc), param_hint="'--engine' / DOC_WORKBENCH_ENGINE") from exc
        output_dir, run_id = paths.new_run_dir("discover")
        trace_id = uuid.uuid4().hex
        tracer = RunTrace(trace_id=trace_id, run_id=run_id, command="discover", policy_digest=policy.digest, exec_policy_digest=exec_policy.digest)

        if selected_engine == "langgraph":
            try:
                from doc_workbench.orchestration.graph import run_graph
            except ImportError as exc:
                raise click.UsageError(
                    "The langgraph engine requires the '[orchestration]' optional extra.\n"
                    "Install it with:  pip install -e '.[orchestration]'"
                ) from exc

            entity_list = load_entities(entities)
            final_state = run_graph(
                entities=entity_list,
                policy=policy,
                tracer=tracer,
                output_dir=output_dir,
                followup_search=followup_search,
                exec_policy=exec_policy,
            )
            records = final_state.get("ranked_records") or final_state.get("discovery_records", [])
        else:
            records = asyncio.run(_discover_all(load_entities(entities), followup_search=followup_search, policy=policy, tracer=tracer, exec_policy=exec_policy))

        json_path, csv_path = write_discovery_artifacts(output_dir, records)
        ranking_trace_path = output_dir / "ranking_trace.json"
        ranking_trace_path.write_text(json.dumps(build_ranking_trace(records, policy), indent=2), encoding="utf-8")
        write_resolved_policy(output_dir / "resolved_policy.json", policy)
        write_resolved_execution_policy(output_dir / "resolved_execution_policy.json", exec_policy)
        trace_path = tracer.write(paths.traces_root / f"{run_id}.json")
        console.print(f"Engine: {selected_engine}")
        console.print(f"Discovery JSON: {json_path}")
        console.print(f"Discovery summary: {csv_path}")
        console.print(f"Resolved policy: {output_dir / 'resolved_policy.json'}")
        console.print(f"Resolved execution policy: {output_dir / 'resolved_execution_policy.json'}")
        console.print(f"Ranking trace: {ranking_trace_path}")
        console.print(f"Trace file: {trace_path}")


async def _discover_all(entities: list, *, followup_search: bool, policy, tracer: RunTrace, exec_policy=None) -> list:
    records = []
    for entity in entities:
        records.append(
            await discover_entity(
                entity,
                followup_search=followup_search,
                policy=policy,
                tracer=tracer,
                exec_policy=exec_policy,
            )
        )
    return records


@app.command("review")
def review(
    input_path: Path = typer.Option(..., "--input"),
    workspace_root: str | None = typer.Option(None, "--workspace-root"),
    policy_path: str | None = typer.Option(None, "--policy-path"),
    execution_policy_path: str | None = typer.Option(None, "--execution-policy-path"),
    engine: str | None = typer.Option(None, "--engine", help="Engine to use.", click_type=click.Choice(list(VALID_ENGINES))),
) -> None:
    with _policy_guard():
        paths = WorkspacePaths.resolve(workspace_root)
        paths.ensure()
        policy = load_context_policy(policy_path)
        exec_policy = load_execution_policy(execution_policy_path)
        enforce_command_stage(exec_policy, "review")
        try:
            selected_engine = resolve_engine(engine)
        except ValueError as exc:
            raise click.BadParameter(str(exc), param_hint="'--engine' / DOC_WORKBENCH_ENGINE") from exc
        output_dir, run_id = paths.new_run_dir("review")
        trace_id = uuid.uuid4().hex
        tracer = RunTrace(trace_id=trace_id, run_id=run_id, command="review", policy_digest=policy.digest, exec_policy_digest=exec_policy.digest)

        if selected_engine == "langgraph":
            from doc_workbench.acquisition.followup.workflow import load_discovery_records as _load
            from doc_workbench.orchestration.nodes import rank_node, review_prep_node
            from doc_workbench.orchestration.state import WorkbenchState

            records = _load(input_path)
            # NOTE: review --engine langgraph calls rank_node + review_prep_node directly.
            # It does NOT execute a compiled StateGraph — review operates on an existing
            # discovery file, so running the full graph would redundantly re-run discover
            # and followup stages.  The [orchestration] extra (langgraph package) is NOT
            # required for this path; only doc_workbench.orchestration.nodes is imported.
            rank_state: WorkbenchState = {
                "entities": [],
                "policy": policy,
                "exec_policy": exec_policy,
                "tracer": tracer,
                "output_dir": output_dir,
                "followup_search": False,
                "followup_records": records,
            }
            rank_result = rank_node(rank_state)
            review_state: WorkbenchState = {**rank_state, **rank_result}
            result = review_prep_node(review_state)
            rows = result["review_rows"]
            review_trace = result["review_trace"]
            recommendation_summary = result["recommendation_summary"]
        else:
            rows, review_trace, recommendation_summary = build_review_rows(input_path, policy)

        csv_path = write_review_csv(output_dir / "review_queue.csv", rows)
        review_trace_path = output_dir / "review_trace.json"
        review_trace_path.write_text(json.dumps(review_trace, indent=2), encoding="utf-8")
        write_resolved_policy(output_dir / "resolved_policy.json", policy)
        write_resolved_execution_policy(output_dir / "resolved_execution_policy.json", exec_policy)
        # review_prep_node already emits this span (with real latency) in the
        # langgraph path — only add it here for the legacy path to avoid doubling.
        if selected_engine != "langgraph":
            tracer.add_span(
                entity_id="all",
                stage="review_queue_generation",
                provider="review_policy",
                latency_ms=0.0,
                candidate_count_in=len(review_trace),
                candidate_count_out=len(rows),
                recommendation_summary=recommendation_summary,
            )
        trace_path = tracer.write(paths.traces_root / f"{run_id}.json")
        console.print(f"Engine: {selected_engine}")
        console.print(f"Review queue: {csv_path}")
        console.print(f"Review trace: {review_trace_path}")
        console.print(f"Resolved policy: {output_dir / 'resolved_policy.json'}")
        console.print(f"Resolved execution policy: {output_dir / 'resolved_execution_policy.json'}")
        console.print(f"Trace file: {trace_path}")


@app.command("download")
def download(
    input_path: Path = typer.Option(..., "--input"),
    workspace_root: str | None = typer.Option(None, "--workspace-root"),
    execution_policy_path: str | None = typer.Option(None, "--execution-policy-path"),
) -> None:
    with _policy_guard():
        paths = WorkspacePaths.resolve(workspace_root)
        paths.ensure()
        exec_policy = load_execution_policy(execution_policy_path)
        enforce_command_stage(exec_policy, "download")
        output_dir, run_id = paths.new_run_dir("download")
        trace_id = uuid.uuid4().hex
        tracer = RunTrace(trace_id=trace_id, run_id=run_id, command="download", policy_digest="", exec_policy_digest=exec_policy.digest)
        registry = DocumentRegistry(paths.registry_root, exec_policy=exec_policy)
        start = time.perf_counter()
        rows = asyncio.run(_download_from_review(input_path, registry, exec_policy, paths.registry_root))
        json_path = output_dir / "download_results.json"
        csv_path = output_dir / "download_results.csv"
        json_path.write_text(json.dumps([row.to_dict() for row in rows], indent=2), encoding="utf-8")
        with csv_path.open("w", encoding="utf-8", newline="") as handle:
            fieldnames = ["document_id", "entity_id", "entity_name", "url", "local_path", "byte_size", "is_duplicate", "status", "error"]
            writer = csv.DictWriter(handle, fieldnames=fieldnames)
            writer.writeheader()
            for row in rows:
                writer.writerow(row.to_dict())
        tracer.add_span(
            entity_id="all",
            stage="download_documents",
            provider="registry_downloader",
            latency_ms=(time.perf_counter() - start) * 1000.0,
            candidate_count_in=len(rows),
            candidate_count_out=sum(1 for row in rows if row.status == "complete"),
        )
        trace_path = tracer.write(paths.traces_root / f"{run_id}.json")
        write_resolved_execution_policy(output_dir / "resolved_execution_policy.json", exec_policy)
        console.print(f"Download results: {json_path}")
        console.print(f"Registry root: {paths.registry_root}")
        console.print(f"Resolved execution policy: {output_dir / 'resolved_execution_policy.json'}")
        console.print(f"Trace file: {trace_path}")
        failed = [r for r in rows if r.status == "failed"]
        if failed:
            console.print(
                f"[red]Error: {len(failed)} row(s) failed during download.[/red]"
            )
            raise typer.Exit(code=1)


async def _download_from_review(
    input_path: Path,
    registry: DocumentRegistry,
    exec_policy: Any = None,
    registry_root: "Path | None" = None,
) -> list[DownloadRow]:
    from doc_workbench.execution_policy import (
        enforce_domain,
        enforce_download_enabled,
        enforce_download_count,
        enforce_file_size,
        enforce_mime_type,
        enforce_registry_root,
    )

    rows: list[DownloadRow] = []
    download_count = 0
    # fetch_attempt_count tracks every outbound network request attempted,
    # including those that ultimately fail (MIME reject, size limit, I/O error).
    # Using download_count (incremented only on success) would allow unbounded
    # outbound requests on repeated failures, bypassing the egress cap.
    fetch_attempt_count = 0

    if exec_policy is not None:
        enforce_download_enabled(exec_policy)

    with input_path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            if str(row.get("recommendation") or "").strip() != "approved":
                continue
            url = str(row.get("url") or "")
            try:
                if exec_policy is not None:
                    enforce_domain(exec_policy, url)

                existing_followup_id = str(row.get("followup_target_document_id") or "").strip()
                existing_manifest = registry.get_manifest(existing_followup_id) if existing_followup_id else None
                if existing_manifest is not None:
                    # Guard: verify the cached manifest actually belongs to the
                    # approved row before reusing its bytes.  A tampered or stale
                    # review CSV must not be able to promote an unrelated artifact.
                    row_entity_id = str(row.get("entity_id") or "").strip()
                    row_url = url.strip()
                    manifest_entity = str(existing_manifest.get("entity_id") or "").strip()
                    manifest_source = str(existing_manifest.get("source_url") or "").strip()
                    manifest_parent = str(existing_manifest.get("source_parent_document_id") or "").strip()
                    if manifest_entity != row_entity_id or (
                        manifest_source != row_url and manifest_parent not in (row_url, existing_followup_id)
                    ):
                        # Provenance mismatch — fall through to a direct fetch of
                        # the approved URL instead of reusing the cached artifact.
                        existing_manifest = None
                if existing_manifest is not None:
                    local_path = registry._normalize_manifest_path(str(existing_manifest["local_path"]))
                    content_type = str(existing_manifest.get("content_type") or "application/pdf")
                    # 1. Validate path is inside registry root BEFORE any I/O.
                    if exec_policy is not None and registry_root is not None:
                        enforce_registry_root(exec_policy, local_path, registry_root.parent)
                    # 2. Check file size via stat() BEFORE loading into memory to
                    #    prevent an unbounded read from a large in-registry file.
                    if exec_policy is not None:
                        enforce_file_size(exec_policy, local_path.stat().st_size, url)
                        enforce_mime_type(exec_policy, content_type, url)
                    pdf_bytes = local_path.read_bytes()
                    # Only promote to "final" for confirmed PDF content — same rule
                    # as register_document() — so reused HTML/binary follow-up
                    # artifacts cannot bypass the promotion guard.
                    is_pdf = "pdf" in content_type.lower()
                    reuse_stage = "final" if is_pdf else "pre_review"
                    registration = registry.register_artifact(
                        entity_id=str(row.get("entity_id") or ""),
                        entity_name=str(row.get("entity_name") or ""),
                        source_url=url,
                        artifact_family="annual_reports",
                        artifact_type=str(row.get("candidate_kind") or "document"),
                        year=str(row.get("year") or "unknown"),
                        content_bytes=pdf_bytes,
                        extension=local_path.suffix or ".pdf",
                        content_type=content_type,
                        stage=reuse_stage,
                        source_parent_document_id=str(existing_manifest.get("document_id") or ""),
                        parsed=dict(existing_manifest.get("parsed") or {}),
                        metadata=dict(existing_manifest.get("metadata") or {}),
                        dedupe_scope="family",
                    )
                else:
                    # Enforce egress cap BEFORE the network call and count the
                    # attempt regardless of outcome, so repeated failures cannot
                    # bypass download.max_count by never successfully registering.
                    if exec_policy is not None:
                        enforce_download_count(exec_policy, fetch_attempt_count)
                    fetch_attempt_count += 1
                    # Pass exec_policy so download_bytes → safe_get enforces
                    # domain at every redirect hop before data is transferred.
                    pdf_bytes, content_type, final_url = await download_bytes(url, exec_policy=exec_policy)
                    if exec_policy is not None:
                        enforce_file_size(exec_policy, len(pdf_bytes), final_url)
                        enforce_mime_type(exec_policy, content_type, final_url)
                    registration = registry.register_document(
                        entity_id=str(row.get("entity_id") or ""),
                        entity_name=str(row.get("entity_name") or ""),
                        source_url=url,
                        family="annual_reports",
                        doc_type=str(row.get("candidate_kind") or "document"),
                        year=str(row.get("year") or "unknown"),
                        pdf_bytes=pdf_bytes,
                        content_type=content_type,
                    )
                download_count += 1
                rows.append(
                    DownloadRow(
                        document_id=registration.document_id,
                        entity_id=str(row.get("entity_id") or ""),
                        entity_name=str(row.get("entity_name") or ""),
                        url=url,
                        local_path=str(registration.local_path),
                        byte_size=len(pdf_bytes),
                        is_duplicate=registration.is_duplicate,
                        status="complete",
                    )
                )
            except PolicyViolationError:
                # Abort the entire download run immediately — do not accumulate
                # more registry writes after a policy denial.
                raise
            except Exception as exc:
                rows.append(
                    DownloadRow(
                        document_id="",
                        entity_id=str(row.get("entity_id") or ""),
                        entity_name=str(row.get("entity_name") or ""),
                        url=url,
                        local_path="",
                        byte_size=0,
                        is_duplicate=False,
                        status="failed",
                        error=f"{type(exc).__name__}: {exc}",
                    )
                )
    return rows


@app.command("followup-search")
def followup_search(
    input_path: Path = typer.Option(..., "--input"),
    workspace_root: str | None = typer.Option(None, "--workspace-root"),
    policy_path: str | None = typer.Option(None, "--policy-path"),
    execution_policy_path: str | None = typer.Option(None, "--execution-policy-path"),
) -> None:
    with _policy_guard():
        paths = WorkspacePaths.resolve(workspace_root)
        paths.ensure()
        policy = load_context_policy(policy_path)
        exec_policy = load_execution_policy(execution_policy_path)
        enforce_command_stage(exec_policy, "followup-search")
        from doc_workbench.execution_policy import enforce_followup_search, enforce_download_enabled
        enforce_followup_search(exec_policy)
        # followup-search materializes artifacts (fetch + registry write) so it must
        # also respect the download.enabled flag, not only followup_search.enabled.
        enforce_download_enabled(exec_policy)
        registry = DocumentRegistry(paths.registry_root, exec_policy=exec_policy)
        output_dir, run_id = paths.new_run_dir("followup_search")
        trace_id = uuid.uuid4().hex
        tracer = RunTrace(trace_id=trace_id, run_id=run_id, command="followup-search", policy_digest=policy.digest, exec_policy_digest=exec_policy.digest)
        records = load_discovery_records(input_path)
        results_by_entity: dict[str, list] = {}
        promoted_candidates = []
        enriched_records = []
        for record in asyncio.run(_followup_all(records, registry, policy, tracer, exec_policy=exec_policy)):
            enriched_records.append(record["record"])
            results_by_entity[record["entity_id"]] = record["results"]
            promoted_candidates.extend(record["promoted"])
        results_path, promoted_json_path, enriched_path = write_followup_artifacts(
            output_dir,
            results_by_entity=results_by_entity,
            promoted_candidates=promoted_candidates,
            enriched_records=enriched_records,
        )
        write_resolved_policy(output_dir / "resolved_policy.json", policy)
        write_resolved_execution_policy(output_dir / "resolved_execution_policy.json", exec_policy)
        trace_path = tracer.write(paths.traces_root / f"{run_id}.json")
        console.print(f"Follow-up results: {results_path}")
        console.print(f"Promoted candidates: {promoted_json_path}")
        console.print(f"Enriched discovery: {enriched_path}")
        console.print(f"Resolved policy: {output_dir / 'resolved_policy.json'}")
        console.print(f"Resolved execution policy: {output_dir / 'resolved_execution_policy.json'}")
        console.print(f"Trace file: {trace_path}")


async def _followup_all(records: list, registry: DocumentRegistry, policy, tracer: RunTrace, exec_policy=None) -> list[dict]:
    output: list[dict] = []
    approval_cutoff = policy.review_thresholds.approved_min_confidence
    for record in records:
        seed_candidates = [
            candidate
            for candidate in record.candidates
            if (candidate.source_type == "search" or candidate.source_tier.startswith("search_"))
            and candidate.source_tier in policy.followup_search.allowed_seed_source_tiers
        ]
        has_higher_priority_candidate = any(
            candidate.source_tier in {"official", "regulatory"} and candidate.confidence >= approval_cutoff
            for candidate in record.candidates
        )
        enabled = not (policy.followup_search.skip_if_higher_priority_approved and has_higher_priority_candidate)
        start = time.perf_counter()
        if enabled:
            results, promoted = await run_followup_for_candidates(
                record.entity,
                seed_candidates,
                materialize=True,
                registry=registry,
                exec_policy=exec_policy,
            )
        else:
            results, promoted = [], []
        deduped: dict[str, object] = {}
        for candidate in [*record.candidates, *promoted]:
            existing = deduped.get(candidate.url)
            if existing is None or candidate.confidence > existing.confidence:
                deduped[candidate.url] = candidate
        record.candidates = sorted(deduped.values(), key=lambda item: item.confidence, reverse=True)
        top = record.candidates[0] if record.candidates else None
        tracer.add_span(
            entity_id=record.entity.entity_id,
            stage="followup_extraction",
            provider="followup_search",
            latency_ms=(time.perf_counter() - start) * 1000.0,
            candidate_count_in=len(seed_candidates),
            candidate_count_out=len(promoted),
            top_candidate_url=top.url if top else "",
            top_confidence=float(top.confidence) if top else 0.0,
            details={"enabled": enabled, "skip_due_to_policy": has_higher_priority_candidate},
        )
        output.append(
            {
                "entity_id": record.entity.entity_id,
                "record": record,
                "results": results,
                "promoted": promoted,
            }
        )
    return output


@app.command("eval")
def run_eval(
    fixtures_dir: Path = typer.Option(None, "--fixtures-dir", help="Override fixture directory (default: bundled package fixtures)"),
    report_path: Path = typer.Option(Path("evals/latest_report.json"), "--report-path"),
) -> None:
    """Run the eval harness against fixture cases and write a machine-readable report."""
    from doc_workbench.evals.run_evals import FIXTURES_DIR as _DEFAULT_FIXTURES, run_evals

    effective_fixtures = fixtures_dir if fixtures_dir is not None else _DEFAULT_FIXTURES
    try:
        report = run_evals(fixtures_dir=effective_fixtures, report_path=report_path)
    except FileNotFoundError as exc:
        raise typer.BadParameter(str(exc), param_hint="--fixtures-dir") from exc
    agg = report["aggregate"]
    console.print(f"Evals: {agg['passed']}/{agg['total_cases']} passed  (pass_rate={agg['pass_rate']})")
    console.print(f"Report written to: {report_path}")
    if not agg["overall_passed"]:
        for case in report["cases"]:
            if not case["passed"]:
                console.print(f"  FAIL  {case['entity_id']}  actual={case['actual']}  expected={case['expected']}")
        raise typer.Exit(code=1)


@app.command("scan")
def scan(
    entity_id: str = typer.Option("", "--entity-id"),
    all_: bool = typer.Option(False, "--all"),
    workspace_root: str | None = typer.Option(None, "--workspace-root"),
    force: bool = typer.Option(False, "--force"),
    execution_policy_path: str | None = typer.Option(None, "--execution-policy-path"),
) -> None:
    with _policy_guard():
        if not entity_id and not all_:
            raise typer.BadParameter("Pass --all or --entity-id.")
        paths = WorkspacePaths.resolve(workspace_root)
        paths.ensure()
        exec_policy = load_execution_policy(execution_policy_path)
        enforce_command_stage(exec_policy, "scan")
        registry = DocumentRegistry(paths.registry_root)
        manifests = registry.list_manifests(entity_id or None, artifact_family="annual_reports")
        output_dir, run_id = paths.new_run_dir("scan")
        trace_id = uuid.uuid4().hex
        tracer = RunTrace(trace_id=trace_id, run_id=run_id, command="scan", policy_digest="", exec_policy_digest=exec_policy.digest)
        rows: list[MetadataScanRow] = []
        start = time.perf_counter()
        for manifest in manifests:
            if not force and ((manifest.get("pipeline_status") or {}).get("metadata_scan_status") == "complete"):
                continue
            artifact_path = registry._normalize_manifest_path(str(manifest["local_path"]))
            # Enforce that the artifact is inside the allowed registry root before
            # reading it — prevents a tampered manifest from pointing outside the
            # workspace.
            enforce_registry_root(exec_policy, artifact_path, paths.root)
            # Enforce file size before read_bytes() to avoid OOM on huge files.
            if artifact_path.exists():
                enforce_file_size(exec_policy, artifact_path.stat().st_size, str(artifact_path))
            result = scan_pdf(artifact_path, content_type=str(manifest.get("content_type") or "application/pdf"))
            updated = registry.update_manifest(
                str(manifest["document_id"]),
                {
                    "metadata": result,
                    "pipeline_status": {"metadata_scan_status": result["status"]},
                },
            )
            rows.append(
                MetadataScanRow(
                    document_id=str(updated["document_id"]),
                    entity_id=str(updated["entity_id"]),
                    entity_name=str(updated["entity_name"]),
                    title=str((updated.get("metadata") or {}).get("title") or ""),
                    issuer_name=str((updated.get("metadata") or {}).get("issuer_name") or ""),
                    reporting_period=str((updated.get("metadata") or {}).get("reporting_period") or ""),
                    publication_date=str((updated.get("metadata") or {}).get("publication_date") or ""),
                    page_count=(updated.get("metadata") or {}).get("page_count"),
                    modality=str((updated.get("metadata") or {}).get("modality") or ""),
                    status=str((updated.get("pipeline_status") or {}).get("metadata_scan_status") or ""),
                    error=str((updated.get("metadata") or {}).get("error") or ""),
                )
            )
        json_path = output_dir / "scan_results.json"
        json_path.write_text(json.dumps([row.to_dict() for row in rows], indent=2), encoding="utf-8")
        tracer.add_span(
            entity_id=entity_id or "all",
            stage="metadata_scan",
            provider="metadata_scanner",
            latency_ms=(time.perf_counter() - start) * 1000.0,
            candidate_count_in=len(manifests),
            candidate_count_out=len(rows),
        )
        trace_path = tracer.write(paths.traces_root / f"{run_id}.json")
        write_resolved_execution_policy(output_dir / "resolved_execution_policy.json", exec_policy)
        console.print(f"Metadata scan results: {json_path}")
        console.print(f"Resolved execution policy: {output_dir / 'resolved_execution_policy.json'}")
        console.print(f"Trace file: {trace_path}")


# ---------------------------------------------------------------------------
# Shared helpers for analyze + chunk
# ---------------------------------------------------------------------------

def _reject_symlink_artifact(artifact_path: Path) -> None:
    """Raise ValueError if *artifact_path* is a symlink."""
    if artifact_path.is_symlink():
        raise ValueError(
            f"Symlink detected at artifact path {artifact_path}. Read blocked."
        )


def _validate_parse_record_ref(ref: str) -> str:
    """Validate and return the basename from a parse_record_ref value."""
    return _validate_parse_sidecar_basename(ref)


# ---------------------------------------------------------------------------
# analyze command
# ---------------------------------------------------------------------------

@app.command("analyze")
def analyze(
    entity_id: str = typer.Option("", "--entity-id"),
    all_: bool = typer.Option(False, "--all"),
    workspace_root: str | None = typer.Option(None, "--workspace-root"),
    force: bool = typer.Option(False, "--force"),
    execution_policy_path: str | None = typer.Option(None, "--execution-policy-path"),
) -> None:
    """Parse and extract metadata from downloaded artifacts."""
    with _policy_guard():
        if not entity_id and not all_:
            raise typer.BadParameter("Pass --all or --entity-id.")
        if entity_id and all_:
            raise typer.BadParameter("Pass exactly one of --all or --entity-id, not both.")
        paths = WorkspacePaths.resolve(workspace_root)
        paths.ensure()
        exec_policy = load_execution_policy(execution_policy_path)
        enforce_command_stage(exec_policy, "analyze")
        registry = DocumentRegistry(paths.registry_root, exec_policy=exec_policy)
        manifests = registry.list_manifests(entity_id or None)
        output_dir, run_id = paths.new_run_dir("analyze")
        trace_id = uuid.uuid4().hex
        tracer = RunTrace(
            trace_id=trace_id,
            run_id=run_id,
            command="analyze",
            policy_digest="",
            exec_policy_digest=exec_policy.digest,
        )

        from doc_workbench.intake.parser import run_parse
        from doc_workbench.intake.validation import validate_parse_record
        from doc_workbench.intake.extractor import run_extraction

        processed = []
        skipped = []
        errors = []
        start = time.perf_counter()

        for manifest in manifests:
            document_id = str(manifest.get("document_id") or "")
            pipeline_status = manifest.get("pipeline_status") or {}
            download_status = pipeline_status.get("download_status", "")
            parse_status = pipeline_status.get("parse_status", "pending")

            if download_status != "complete":
                skipped.append({"document_id": document_id, "reason": f"download_status={download_status!r}"})
                console.print(f"[yellow]skip[/yellow] {document_id}: download_status={download_status!r}")
                continue

            if parse_status == "complete" and not force:
                skipped.append({"document_id": document_id, "reason": "parse_status=complete (use --force to re-run)"})
                console.print(f"[dim]skip[/dim] {document_id}: already analyzed")
                continue

            artifact_path = registry._normalize_manifest_path(str(manifest["local_path"]))
            try:
                # --- path security guards (mirrors scan/download) ---
                enforce_registry_root(exec_policy, artifact_path, paths.root)
                _guard_artifact(artifact_path, paths.registry_root, exec_policy.download.max_file_size_bytes)

                # --- parse ---
                record = run_parse(
                    document_id=document_id,
                    local_path=artifact_path,
                    manifest=manifest,
                    run_id=run_id,
                )
                validation_errs = validate_parse_record(record)

                # --- write parse sidecar ---
                parse_sidecar_path = registry.write_analysis_sidecar(
                    document_id, "parse_record", record.to_dict()
                )
                parse_sidecar_filename = parse_sidecar_path.name

                # --- extract ---
                extraction = run_extraction(
                    document_id=document_id,
                    manifest=manifest,
                    parse_record=record,
                    parse_record_filename=parse_sidecar_filename,
                    run_id=run_id,
                    parse_validation_errors=validation_errs,
                )

                # --- write extraction sidecar ---
                registry.write_analysis_sidecar(
                    document_id, "extraction_record", extraction.to_dict()
                )

                # --- update manifest parse_status ---
                registry.update_manifest(document_id, {
                    "pipeline_status": {"parse_status": record.parse_status}
                })

                processed.append({
                    "document_id": document_id,
                    "entity_id": str(manifest.get("entity_id") or ""),
                    "parse_status": record.parse_status,
                    "indexing_acceptance": extraction.indexing_acceptance,
                    "risk_level": extraction.risk_level,
                    "parse_record_file": parse_sidecar_filename,
                    "extraction_record_file": registry.list_analysis_sidecars(
                        document_id, "extraction_record"
                    )[-1].name,
                    "validation_errors": validation_errs,
                })
                console.print(
                    f"[green]ok[/green] {document_id}: "
                    f"parse_status={record.parse_status} "
                    f"acceptance={extraction.indexing_acceptance}"
                )

            except PolicyViolationError:
                raise
            except Exception as exc:
                errors.append({"document_id": document_id, "error": f"{type(exc).__name__}: {exc}"})
                console.print(f"[red]error[/red] {document_id}: {exc}")

        results = {
            "run_id": run_id,
            "command": "analyze",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "processed": processed,
            "skipped": skipped,
            "errors": errors,
        }
        results_path = output_dir / "analyze_results.json"
        results_path.write_text(json.dumps(results, indent=2, ensure_ascii=False), encoding="utf-8")

        # summary CSV
        csv_path = output_dir / "analyze_summary.csv"
        with csv_path.open("w", encoding="utf-8", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=[
                "document_id", "entity_id", "parse_status",
                "indexing_acceptance", "risk_level",
                "parse_record_file", "extraction_record_file",
            ])
            writer.writeheader()
            for row in processed:
                writer.writerow({k: row.get(k, "") for k in writer.fieldnames})

        tracer.add_span(
            entity_id=entity_id or "all",
            stage="analyze_documents",
            provider="intake_parser",
            latency_ms=(time.perf_counter() - start) * 1000.0,
            candidate_count_in=len(manifests),
            candidate_count_out=len(processed),
        )
        trace_path = tracer.write(paths.traces_root / f"{run_id}.json")
        write_resolved_execution_policy(output_dir / "resolved_execution_policy.json", exec_policy)
        console.print(f"Analyze results: {results_path}")
        console.print(f"Analyze summary: {csv_path}")
        console.print(f"Resolved execution policy: {output_dir / 'resolved_execution_policy.json'}")
        console.print(f"Trace file: {trace_path}")
        if errors:
            console.print(f"[red]Error: {len(errors)} document(s) failed during analyze.[/red]")
            raise typer.Exit(code=1)


# ---------------------------------------------------------------------------
# chunk command
# ---------------------------------------------------------------------------

@app.command("chunk")
def chunk(
    entity_id: str = typer.Option("", "--entity-id"),
    all_: bool = typer.Option(False, "--all"),
    workspace_root: str | None = typer.Option(None, "--workspace-root"),
    force: bool = typer.Option(False, "--force"),
    execution_policy_path: str | None = typer.Option(None, "--execution-policy-path"),
) -> None:
    """Chunk index-ready documents into retrieval-ready JSONL records."""
    with _policy_guard():
        if not entity_id and not all_:
            raise typer.BadParameter("Pass --all or --entity-id.")
        if entity_id and all_:
            raise typer.BadParameter("Pass exactly one of --all or --entity-id, not both.")
        paths = WorkspacePaths.resolve(workspace_root)
        paths.ensure()
        exec_policy = load_execution_policy(execution_policy_path)
        enforce_command_stage(exec_policy, "chunk")
        registry = DocumentRegistry(paths.registry_root, exec_policy=exec_policy)
        manifests = registry.list_manifests(entity_id or None)
        output_dir, run_id = paths.new_run_dir("chunk")
        trace_id = uuid.uuid4().hex
        tracer = RunTrace(
            trace_id=trace_id,
            run_id=run_id,
            command="chunk",
            policy_digest="",
            exec_policy_digest=exec_policy.digest,
        )

        from doc_workbench.intake.models import ParseRecord
        from doc_workbench.intake.extractor import ExtractionRecord
        from doc_workbench.knowledge.chunker import chunk_document
        from doc_workbench.knowledge.packager import write_chunk_jsonl

        processed = []
        skipped = []
        errors = []
        start = time.perf_counter()

        for manifest in manifests:
            document_id = str(manifest.get("document_id") or "")
            pipeline_status = manifest.get("pipeline_status") or {}
            parse_status = pipeline_status.get("parse_status", "pending")
            chunking_status = pipeline_status.get("chunking_status", "pending")

            artifact_path = registry._normalize_manifest_path(str(manifest["local_path"]))

            # --- path/size/symlink guards on artifact (same as scan/download) ---
            try:
                enforce_registry_root(exec_policy, artifact_path, paths.root)
                _guard_artifact(artifact_path, paths.registry_root, exec_policy.download.max_file_size_bytes)
            except PolicyViolationError:
                raise
            except Exception as exc:
                errors.append({"document_id": document_id, "error": f"{type(exc).__name__}: {exc}"})
                console.print(f"[red]error[/red] {document_id}: {exc}")
                try:
                    registry.update_manifest(document_id, {"pipeline_status": {"chunking_status": "failed"}})
                except Exception:
                    pass
                continue

            # --- triage: non-complete parse → skipped ---
            if parse_status != "complete":
                registry.update_manifest(document_id, {"pipeline_status": {"chunking_status": "skipped"}})
                skipped.append({"document_id": document_id, "reason": f"parse_status={parse_status!r}"})
                console.print(f"[yellow]skip[/yellow] {document_id}: parse_status={parse_status!r}")
                continue

            # --- skip if already chunked (unless --force) ---
            if chunking_status == "complete" and not force:
                skipped.append({"document_id": document_id, "reason": "chunking_status=complete (use --force to re-run)"})
                console.print(f"[dim]skip[/dim] {document_id}: already chunked")
                continue

            try:
                # --- load latest extraction record ---
                extraction_sidecar_paths = registry.list_analysis_sidecars(document_id, "extraction_record")
                if not extraction_sidecar_paths:
                    raise FileNotFoundError(f"No extraction_record sidecar found for {document_id}")
                extraction_sidecar_path = extraction_sidecar_paths[-1]

                if extraction_sidecar_path.is_symlink():
                    raise ValueError(
                        f"Symlink at extraction_record sidecar {extraction_sidecar_path}. Read blocked."
                    )
                _guard_sidecar(extraction_sidecar_path, paths.registry_root, exec_policy.download.max_file_size_bytes)
                extraction_data = json.loads(extraction_sidecar_path.read_text(encoding="utf-8"))
                extraction = ExtractionRecord.from_dict(extraction_data)

                # --- skip non-index-ready ---
                if extraction.indexing_acceptance != "index_ready":
                    registry.update_manifest(document_id, {"pipeline_status": {"chunking_status": "skipped"}})
                    skipped.append({
                        "document_id": document_id,
                        "reason": f"indexing_acceptance={extraction.indexing_acceptance!r}",
                    })
                    console.print(
                        f"[yellow]skip[/yellow] {document_id}: "
                        f"indexing_acceptance={extraction.indexing_acceptance!r}"
                    )
                    continue

                # --- validate + resolve parse_record_ref ---
                ref = extraction.parse_record_ref
                safe_basename = _validate_parse_record_ref(ref)
                analysis_dir = registry.ensure_analysis_dir(document_id)
                parse_sidecar_path = (analysis_dir / safe_basename).resolve()
                registry_resolved = registry.registry_root.resolve()
                try:
                    parse_sidecar_path.relative_to(registry_resolved)
                except ValueError:
                    raise ValueError(
                        f"parse_record_ref resolves outside registry root: {parse_sidecar_path}"
                    )
                if not parse_sidecar_path.exists():
                    raise FileNotFoundError(
                        f"parse_record_ref {ref!r} not found at {parse_sidecar_path}"
                    )
                if parse_sidecar_path.is_symlink():
                    raise ValueError(
                        f"Symlink at parse_record sidecar {parse_sidecar_path}. Read blocked."
                    )

                _guard_sidecar(parse_sidecar_path, paths.registry_root, exec_policy.download.max_file_size_bytes)
                parse_data = json.loads(parse_sidecar_path.read_text(encoding="utf-8"))
                parse_record = ParseRecord.from_dict(parse_data)

                # --- chunk ---
                chunk_iter = chunk_document(
                    local_path=artifact_path,
                    manifest=manifest,
                    parse_record=parse_record,
                    extraction_record=extraction,
                    run_id=run_id,
                )

                if chunk_iter is None:
                    # non-chunkable (HTML, scanned, etc.)
                    registry.update_manifest(document_id, {"pipeline_status": {"chunking_status": "skipped"}})
                    skipped.append({"document_id": document_id, "reason": "chunk_document returned None (non-chunkable)"})
                    console.print(f"[yellow]skip[/yellow] {document_id}: non-chunkable strategy")
                    continue

                # --- stream-write JSONL with no-overwrite collision retry ---
                # Existence pre-check + exclusive-create ("x") in write_chunk_jsonl
                # together close the TOCTOU window: pre-check skips obvious collisions,
                # exclusive-create raises FileExistsError on any late collision.
                from datetime import datetime as _dt, timezone as _tz
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
                chunk_filename = chunk_path.name

                registry.update_manifest(document_id, {"pipeline_status": {"chunking_status": "complete"}})
                processed.append({
                    "document_id": document_id,
                    "entity_id": str(manifest.get("entity_id") or ""),
                    "chunk_count": chunk_count,
                    "chunk_file": chunk_filename,
                })
                console.print(f"[green]ok[/green] {document_id}: {chunk_count} chunks → {chunk_filename}")

            except PolicyViolationError:
                raise
            except Exception as exc:
                errors.append({"document_id": document_id, "error": f"{type(exc).__name__}: {exc}"})
                console.print(f"[red]error[/red] {document_id}: {exc}")
                try:
                    registry.update_manifest(document_id, {"pipeline_status": {"chunking_status": "failed"}})
                except Exception:
                    pass

        results = {
            "run_id": run_id,
            "command": "chunk",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "processed": processed,
            "skipped": skipped,
            "errors": errors,
        }
        results_path = output_dir / "chunk_results.json"
        results_path.write_text(json.dumps(results, indent=2, ensure_ascii=False), encoding="utf-8")

        tracer.add_span(
            entity_id=entity_id or "all",
            stage="chunk_documents",
            provider="knowledge_chunker",
            latency_ms=(time.perf_counter() - start) * 1000.0,
            candidate_count_in=len(manifests),
            candidate_count_out=len(processed),
        )
        trace_path = tracer.write(paths.traces_root / f"{run_id}.json")
        write_resolved_execution_policy(output_dir / "resolved_execution_policy.json", exec_policy)
        console.print(f"Chunk results: {results_path}")
        console.print(f"Resolved execution policy: {output_dir / 'resolved_execution_policy.json'}")
        console.print(f"Trace file: {trace_path}")
        if errors:
            console.print(f"[red]Error: {len(errors)} document(s) failed during chunk.[/red]")
            raise typer.Exit(code=1)
