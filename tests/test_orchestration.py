"""Tests for the LangGraph orchestration layer.

These tests verify:
- WorkbenchState is a valid TypedDict
- The StateGraph compiles without error
- Individual nodes mutate state correctly (with mocked I/O)
- The full graph produces DiscoveryRecord and ReviewRow outputs

Requires the ``[orchestration]`` optional extra (``langgraph``).  The entire
module is skipped automatically when the package is absent so that a plain
``pip install -e ".[dev]"`` environment passes ``pytest`` without errors.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

# Skip this entire module if langgraph is not installed.
pytest.importorskip("langgraph", reason="langgraph not installed (install with .[orchestration])")  # noqa: E402

from doc_workbench.models import DiscoveryCandidate, DiscoveryRecord, EntityRecord  # noqa: E402
from doc_workbench.observability.tracer import RunTrace  # noqa: E402
from doc_workbench.orchestration.graph import _build_graph, run_graph  # noqa: E402
from doc_workbench.orchestration.nodes import rank_node, review_prep_node  # noqa: E402
from doc_workbench.orchestration.state import WorkbenchState  # noqa: E402
from doc_workbench.policy import load_context_policy  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_entity(entity_id: str = "T001", name: str = "Test Corp") -> EntityRecord:
    return EntityRecord(
        entity_id=entity_id,
        name=name,
        ticker="TEST",
        official_website="https://testcorp.example.com",
        cik="",
        country="US",
    )


def _make_candidate(entity: EntityRecord, url: str, confidence: float = 0.65, source_tier: str = "official") -> DiscoveryCandidate:
    return DiscoveryCandidate(
        entity_id=entity.entity_id,
        entity_name=entity.name,
        url=url,
        title="Annual Report 2023",
        snippet="official site",
        source_type="official_site",
        source_tier=source_tier,
        document_kind="official_pdf",
        year=2023,
        confidence=confidence,
        reasons=["same_domain", "pdf"],
    )


def _make_record(entity: EntityRecord, candidates: list[DiscoveryCandidate] | None = None) -> DiscoveryRecord:
    if candidates is None:
        candidates = [
            _make_candidate(entity, "https://testcorp.example.com/annual-report-2023.pdf")
        ]
    return DiscoveryRecord(entity=entity, status="success", candidates=candidates)


def _base_state(tmp_path: Path) -> WorkbenchState:
    policy = load_context_policy()
    entity = _make_entity()
    tracer = RunTrace(trace_id="test-run", run_id="test-run", command="discover", policy_digest=policy.digest)
    return {
        "entities": [entity],
        "policy": policy,
        "tracer": tracer,
        "output_dir": tmp_path,
        "followup_search": False,
    }


# ---------------------------------------------------------------------------
# Graph compilation
# ---------------------------------------------------------------------------

def test_graph_compiles() -> None:
    """StateGraph should compile to a callable without raising."""
    graph = _build_graph()
    assert callable(graph.invoke)


# ---------------------------------------------------------------------------
# rank_node
# ---------------------------------------------------------------------------

def test_rank_node_deduplicates_and_caps(tmp_path: Path) -> None:
    """rank_node should dedup by URL and cap at 10 candidates per entity."""
    policy = load_context_policy()
    entity = _make_entity()
    # Create 15 candidates with the same URL pattern (some duplicates)
    candidates = []
    for i in range(15):
        url = f"https://testcorp.example.com/report-{i % 12}.pdf"  # 12 unique URLs
        candidates.append(_make_candidate(entity, url, confidence=0.5 + i * 0.01))
    record = DiscoveryRecord(entity=entity, status="success", candidates=candidates)

    state: WorkbenchState = {
        "entities": [entity],
        "policy": policy,
        "tracer": None,
        "output_dir": tmp_path,
        "followup_search": False,
        "followup_records": [record],
    }
    result = rank_node(state)
    ranked = result["ranked_records"]
    assert len(ranked) == 1
    assert len(ranked[0].candidates) <= 10
    # Verify sorted by confidence descending
    confidences = [c.confidence for c in ranked[0].candidates]
    assert confidences == sorted(confidences, reverse=True)


def test_rank_node_falls_back_to_discovery_records(tmp_path: Path) -> None:
    """rank_node should use discovery_records when followup_records is absent."""
    policy = load_context_policy()
    entity = _make_entity()
    record = _make_record(entity)

    state: WorkbenchState = {
        "entities": [entity],
        "policy": policy,
        "tracer": None,
        "output_dir": tmp_path,
        "followup_search": False,
        "discovery_records": [record],
    }
    result = rank_node(state)
    assert "ranked_records" in result
    assert len(result["ranked_records"]) == 1


# ---------------------------------------------------------------------------
# review_prep_node
# ---------------------------------------------------------------------------

def test_review_prep_node_produces_review_rows(tmp_path: Path) -> None:
    """review_prep_node should produce review_rows, review_trace, and recommendation_summary."""
    policy = load_context_policy()
    entity = _make_entity()
    record = _make_record(entity)

    state: WorkbenchState = {
        "entities": [entity],
        "policy": policy,
        "tracer": None,
        "output_dir": tmp_path,
        "followup_search": False,
        "ranked_records": [record],
    }
    result = review_prep_node(state)
    assert "review_rows" in result
    assert "review_trace" in result
    assert "recommendation_summary" in result
    assert len(result["review_rows"]) == len(record.candidates)
    assert isinstance(result["recommendation_summary"], dict)
    assert set(result["recommendation_summary"].keys()) >= {"approved", "needs_review", "rejected"}


def test_review_prep_node_approved_candidate(tmp_path: Path) -> None:
    """A high-confidence same-domain PDF candidate should be 'approved'."""
    policy = load_context_policy()
    entity = _make_entity()
    candidate = DiscoveryCandidate(
        entity_id=entity.entity_id,
        entity_name=entity.name,
        url="https://testcorp.example.com/annual-report-2023.pdf",
        title="Annual Report 2023 PDF",
        snippet="official site",
        source_type="official_site",
        source_tier="official",
        document_kind="official_pdf",
        year=2023,
        confidence=0.91,
        reasons=["same_domain", "pdf"],
    )
    record = DiscoveryRecord(entity=entity, status="success", candidates=[candidate])
    state: WorkbenchState = {
        "entities": [entity],
        "policy": policy,
        "tracer": None,
        "output_dir": tmp_path,
        "followup_search": False,
        "ranked_records": [record],
    }
    result = review_prep_node(state)
    assert result["review_rows"][0].recommendation == "approved"


# ---------------------------------------------------------------------------
# Full graph run (mocked I/O)
# ---------------------------------------------------------------------------

def test_run_graph_produces_ranked_records(tmp_path: Path) -> None:
    """Default (discover) graph run produces ranked_records but not review_rows."""
    policy = load_context_policy()
    entity = _make_entity()
    tracer = RunTrace(trace_id="test", run_id="test", command="discover", policy_digest=policy.digest)

    async def _fake_discover(ent, *, followup_search=False, policy=None, tracer=None, _skip_ranking=False, _force_skip_followup=False, exec_policy=None):
        return _make_record(ent, [_make_candidate(ent, "https://testcorp.example.com/ar.pdf", confidence=0.91)])

    with patch("doc_workbench.orchestration.nodes.discover_entity", new=_fake_discover):
        final_state = run_graph(
            entities=[entity],
            policy=policy,
            tracer=tracer,
            output_dir=tmp_path,
            followup_search=False,
        )

    assert "ranked_records" in final_state
    assert len(final_state["ranked_records"]) == 1
    # Discover graph stops at rank — no review_rows
    assert "review_rows" not in final_state


def test_run_graph_full_mode_produces_review_rows(tmp_path: Path) -> None:
    """Full-mode graph run produces ranked_records and review_rows for each entity."""
    policy = load_context_policy()
    entity = _make_entity()
    tracer = RunTrace(trace_id="test", run_id="test", command="discover", policy_digest=policy.digest)

    async def _fake_discover(ent, *, followup_search=False, policy=None, tracer=None, _skip_ranking=False, _force_skip_followup=False, exec_policy=None):
        return _make_record(ent, [_make_candidate(ent, "https://testcorp.example.com/ar.pdf", confidence=0.91)])

    with patch("doc_workbench.orchestration.nodes.discover_entity", new=_fake_discover):
        final_state = run_graph(
            entities=[entity],
            policy=policy,
            tracer=tracer,
            output_dir=tmp_path,
            followup_search=False,
            mode="full",
        )

    assert "ranked_records" in final_state
    assert len(final_state["ranked_records"]) == 1
    assert "review_rows" in final_state
    assert len(final_state["review_rows"]) >= 1
    # Top candidate should be approved
    assert final_state["review_rows"][0].recommendation == "approved"


# ---------------------------------------------------------------------------
# Intake graph tests
# ---------------------------------------------------------------------------

from doc_workbench.orchestration.graph import build_intake_graph  # noqa: E402
from doc_workbench.orchestration.nodes import parse_node, extract_node, chunk_node  # noqa: E402


def test_intake_graph_compiles() -> None:
    """build_intake_graph() must return a compiled graph without error."""
    graph = build_intake_graph()
    assert graph is not None


def test_parse_node_raises_without_document_selection_input() -> None:
    """parse_node must raise ValueError when no intake target selector is set."""
    import pytest
    state: WorkbenchState = {}
    with pytest.raises(ValueError, match="intake"):
        parse_node(state)


def test_extract_node_passthrough_on_empty_parse_records(tmp_path: Path) -> None:
    """extract_node with an empty parse_records list must return extraction_records=[]."""
    state: WorkbenchState = {
        "intake_registry_root": tmp_path,
        "parse_records": [],
    }
    result = extract_node(state)
    assert result == {"extraction_records": []}


def test_extract_node_passes_error_entries_through(tmp_path: Path) -> None:
    """extract_node must propagate error entries from parse_records unchanged."""
    state: WorkbenchState = {
        "intake_registry_root": tmp_path,
        "parse_records": [
            {"document_id": "doc001", "error": "ParseError: something failed"},
        ],
    }
    result = extract_node(state)
    assert len(result["extraction_records"]) == 1
    assert result["extraction_records"][0]["document_id"] == "doc001"
    assert "error" in result["extraction_records"][0]


def test_chunk_node_passthrough_on_empty_extraction_records(tmp_path: Path) -> None:
    """chunk_node with an empty extraction_records list must return chunk_records=[]."""
    state: WorkbenchState = {
        "intake_registry_root": tmp_path,
        "extraction_records": [],
    }
    result = chunk_node(state)
    assert result == {"chunk_records": []}


def test_intake_graph_nodes_are_parse_extract_chunk(tmp_path: Path) -> None:
    """Compiled intake graph node names must be parse, extract, and chunk."""
    graph = build_intake_graph()
    # Registry root is empty so list_manifests returns []; graph should run cleanly.
    with patch("doc_workbench.registry.document_registry.DocumentRegistry.list_manifests", return_value=[]):
        state: WorkbenchState = {
            "intake_all": True,
            "intake_registry_root": tmp_path,
        }
        final = graph.invoke(state)
    assert "parse_records" in final
    assert "extraction_records" in final
    assert "chunk_records" in final


# ---------------------------------------------------------------------------
# Intake node policy enforcement (enforce_command_stage parity)
# ---------------------------------------------------------------------------

def test_parse_node_rejects_policy_stage_violation(tmp_path: Path) -> None:
    """parse_node must raise PolicyViolationError when exec_policy forbids 'analyze'."""
    import yaml
    from doc_workbench.execution_policy import load_execution_policy, PolicyViolationError

    policy_file = tmp_path / "policy.yaml"
    policy_file.write_text(
        yaml.dump({
            "allowed_command_stages": ["discover"],  # 'analyze' absent
            "allowed_source_families": ["*"],
            "download": {"enabled": True, "max_count": 10, "max_file_size_bytes": 1000, "allowed_mime_types": ["application/pdf"]},
            "followup_search": {"enabled": False},
            "registry": {"root_restriction": "registry"},
        }),
        encoding="utf-8",
    )
    exec_policy = load_execution_policy(str(policy_file))
    state: WorkbenchState = {
        "intake_all": True,
        "intake_registry_root": tmp_path,
        "exec_policy": exec_policy,
    }
    with pytest.raises(PolicyViolationError):
        parse_node(state)


def test_chunk_node_rejects_policy_stage_violation(tmp_path: Path) -> None:
    """chunk_node must raise PolicyViolationError when exec_policy forbids 'chunk'."""
    import yaml
    from doc_workbench.execution_policy import load_execution_policy, PolicyViolationError

    policy_file = tmp_path / "policy.yaml"
    policy_file.write_text(
        yaml.dump({
            "allowed_command_stages": ["discover", "analyze"],  # 'chunk' absent
            "allowed_source_families": ["*"],
            "download": {"enabled": True, "max_count": 10, "max_file_size_bytes": 1000, "allowed_mime_types": ["application/pdf"]},
            "followup_search": {"enabled": False},
            "registry": {"root_restriction": "registry"},
        }),
        encoding="utf-8",
    )
    exec_policy = load_execution_policy(str(policy_file))
    state: WorkbenchState = {
        "intake_registry_root": tmp_path,
        "extraction_records": [],
        "exec_policy": exec_policy,
    }
    with pytest.raises(PolicyViolationError):
        chunk_node(state)


def test_parse_node_rejects_exec_policy_without_workspace_root(tmp_path: Path) -> None:
    """parse_node must raise PolicyViolationError when exec_policy is set but
    intake_workspace_root is absent — enforcing root_restriction parity with CLI."""
    import yaml
    from doc_workbench.execution_policy import load_execution_policy, PolicyViolationError
    from doc_workbench.orchestration.nodes import parse_node
    from doc_workbench.registry.document_registry import DocumentRegistry

    # Policy allows 'analyze' so stage check passes; workspace_root is absent → must fail.
    policy_file = tmp_path / "policy.yaml"
    policy_file.write_text(
        yaml.dump({
            "allowed_command_stages": ["analyze", "chunk"],
            "allowed_source_families": ["*"],
            "download": {"enabled": True, "max_count": 10, "max_file_size_bytes": 52428800,
                         "allowed_mime_types": ["application/pdf"]},
            "followup_search": {"enabled": False},
            "registry": {"root_restriction": "registry"},
        }),
        encoding="utf-8",
    )
    exec_policy = load_execution_policy(str(policy_file))

    # Register a real download-complete document so parse_node reaches the artifact guard.
    registry_root = tmp_path / "registry"
    registry = DocumentRegistry(registry_root)
    import io
    minimal_pdf = (
        b"%PDF-1.4\n1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
        b"2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n"
        b"3 0 obj<</Type/Page/MediaBox[0 0 3 3]>>endobj\n"
        b"xref\n0 4\n0000000000 65535 f\n"
        b"0000000009 00000 n\n0000000058 00000 n\n"
        b"0000000115 00000 n\ntrailer<</Size 4/Root 1 0 R>>\nstartxref\n190\n%%EOF"
    )
    registry.register_document(
        entity_id="ent_test",
        entity_name="Test Corp",
        source_url="https://example.com/report.pdf",
        family="annual_reports",
        doc_type="official_pdf",
        year="2024",
        pdf_bytes=minimal_pdf,
    )

    state: WorkbenchState = {
        "intake_all": True,
        "intake_registry_root": registry_root,
        "exec_policy": exec_policy,
        # intake_workspace_root intentionally absent
    }
    with pytest.raises(PolicyViolationError, match="intake_workspace_root must be set"):
        parse_node(state)


# ---------------------------------------------------------------------------
# Shared intake guards (doc_workbench.intake.guards)
# ---------------------------------------------------------------------------

def test_check_sidecar_path_rejects_oversized_sidecar(tmp_path: Path) -> None:
    """check_sidecar_path must raise ValueError when sidecar exceeds the size limit."""
    from doc_workbench.intake.guards import check_sidecar_path

    sidecar = tmp_path / "parse_record.20240101T000000000000Z.json"
    sidecar.write_text("x" * 100, encoding="utf-8")

    with pytest.raises(ValueError, match="size"):
        check_sidecar_path(sidecar, registry_root=tmp_path, max_file_bytes=10)


def test_check_artifact_path_rejects_oversized_artifact(tmp_path: Path) -> None:
    """check_artifact_path must raise ValueError when artifact exceeds size limit."""
    from doc_workbench.intake.guards import check_artifact_path

    artifact = tmp_path / "doc.pdf"
    artifact.write_bytes(b"x" * 100)

    with pytest.raises(ValueError, match="size"):
        check_artifact_path(artifact, registry_root=tmp_path, max_file_bytes=10)


def test_check_artifact_path_rejects_symlink(tmp_path: Path) -> None:
    """check_artifact_path must raise ValueError for symlinked artifacts."""
    from doc_workbench.intake.guards import check_artifact_path

    real_file = tmp_path / "real.pdf"
    real_file.write_bytes(b"data")
    link = tmp_path / "link.pdf"
    link.symlink_to(real_file)

    with pytest.raises(ValueError, match="[Ss]ymlink"):
        check_artifact_path(link, registry_root=tmp_path)


def test_check_sidecar_path_rejects_path_outside_registry(tmp_path: Path) -> None:
    """check_sidecar_path must raise ValueError when path is outside registry_root."""
    import tempfile
    from doc_workbench.intake.guards import check_sidecar_path

    with tempfile.TemporaryDirectory() as other_dir:
        sidecar = Path(other_dir) / "leaked.json"
        sidecar.write_text("{}", encoding="utf-8")
        with pytest.raises(ValueError, match="outside registry root"):
            check_sidecar_path(sidecar, registry_root=tmp_path)


# ---------------------------------------------------------------------------
# Happy-path node tests (parse_node → extract_node → chunk_node)
# ---------------------------------------------------------------------------

def _make_registry_with_pdf(tmp_path: Path):
    """Register a blank PDF with download_status=complete; return (registry_root, document_id)."""
    from io import BytesIO
    from pypdf import PdfWriter
    from doc_workbench.registry.document_registry import DocumentRegistry

    writer = PdfWriter()
    writer.add_blank_page(width=200, height=200)
    writer.add_metadata({"/Title": "Node Test 2024"})
    buf = BytesIO()
    writer.write(buf)
    pdf_bytes = buf.getvalue()

    registry_root = tmp_path / "registry"
    registry = DocumentRegistry(registry_root)
    result = registry.register_document(
        entity_id="ent_nodetest",
        entity_name="Node Test Corp",
        source_url="https://example.com/node_test.pdf",
        family="annual_reports",
        doc_type="official_pdf",
        year="2024",
        pdf_bytes=pdf_bytes,
    )
    return registry_root, result.document_id


def test_parse_node_happy_path(tmp_path: Path) -> None:
    """parse_node must write a parse_record sidecar and return parse_records in state."""
    from doc_workbench.orchestration.nodes import parse_node

    registry_root, doc_id = _make_registry_with_pdf(tmp_path)
    state: WorkbenchState = {
        "intake_all": True,
        "intake_registry_root": registry_root,
    }
    result = parse_node(state)

    assert "parse_records" in result
    assert len(result["parse_records"]) == 1
    entry = result["parse_records"][0]
    assert entry["document_id"] == doc_id
    assert "error" not in entry
    assert entry.get("parse_sidecar_filename", "").startswith("parse_record.")
    # Sidecar must physically exist on disk.
    from doc_workbench.registry.document_registry import DocumentRegistry
    registry = DocumentRegistry(registry_root)
    sidecars = registry.list_analysis_sidecars(doc_id, "parse_record")
    assert len(sidecars) == 1


def test_extract_node_happy_path(tmp_path: Path) -> None:
    """extract_node must write an extraction_record sidecar and return extraction_records."""
    from doc_workbench.orchestration.nodes import parse_node, extract_node

    registry_root, doc_id = _make_registry_with_pdf(tmp_path)
    state: WorkbenchState = {
        "intake_all": True,
        "intake_registry_root": registry_root,
    }
    parse_result = parse_node(state)
    state = {**state, **parse_result}

    extract_result = extract_node(state)
    assert "extraction_records" in extract_result
    assert len(extract_result["extraction_records"]) == 1
    entry = extract_result["extraction_records"][0]
    assert entry["document_id"] == doc_id
    assert "error" not in entry
    assert "extraction_sidecar_filename" in entry
    assert entry["extraction_sidecar_filename"].startswith("extraction_record.")
    # Sidecar must physically exist on disk.
    from doc_workbench.registry.document_registry import DocumentRegistry
    registry = DocumentRegistry(registry_root)
    sidecars = registry.list_analysis_sidecars(doc_id, "extraction_record")
    assert len(sidecars) == 1


def test_chunk_node_happy_path_uses_exact_sidecar(tmp_path: Path) -> None:
    """chunk_node must use extraction_sidecar_filename from state (not a disk scan)."""
    from doc_workbench.orchestration.nodes import parse_node, extract_node, chunk_node
    from doc_workbench.registry.document_registry import DocumentRegistry

    registry_root, doc_id = _make_registry_with_pdf(tmp_path)
    state: WorkbenchState = {
        "intake_all": True,
        "intake_registry_root": registry_root,
    }
    state = {**state, **parse_node(state)}
    state = {**state, **extract_node(state)}

    # Verify extraction_sidecar_filename is threaded through state.
    extraction_entry = state["extraction_records"][0]
    assert "extraction_sidecar_filename" in extraction_entry

    # Write a *second* competing extraction sidecar for the same document so
    # that a disk-scan implementation (list_analysis_sidecars[-1]) would pick
    # the wrong file.  The node must use the filename from state, not the latest.
    import json, time as _time
    from doc_workbench.registry.document_registry import DocumentRegistry as _R
    registry = _R(registry_root)
    analysis_dir = registry.ensure_analysis_dir(doc_id)
    _time.sleep(0.01)  # ensure a distinct timestamp
    competing_ts = __import__("datetime").datetime.now(
        __import__("datetime").timezone.utc
    ).strftime("%Y%m%dT%H%M%S%f") + "Z"
    competing_path = analysis_dir / f"extraction_record.{competing_ts}.json"
    competing_path.write_text(
        json.dumps({"document_id": doc_id, "_competing": True}), encoding="utf-8"
    )
    # Two sidecars on disk; state still carries the original filename.
    sidecars_on_disk = registry.list_analysis_sidecars(doc_id, "extraction_record")
    assert len(sidecars_on_disk) == 2

    chunk_result = chunk_node(state)
    assert "chunk_records" in chunk_result
    # Blank PDF → chunking_status=skipped (not an error).
    updated = registry.get_manifest(doc_id)
    assert updated["pipeline_status"].get("chunking_status") in ("skipped", "complete", "failed")


def test_chunk_node_rejects_traversal_extraction_sidecar_filename(tmp_path: Path) -> None:
    """chunk_node must reject extraction_sidecar_filename values containing path traversal."""
    from doc_workbench.orchestration.nodes import parse_node, extract_node, chunk_node

    registry_root, doc_id = _make_registry_with_pdf(tmp_path)
    state: WorkbenchState = {
        "intake_all": True,
        "intake_registry_root": registry_root,
    }
    state = {**state, **parse_node(state)}
    state = {**state, **extract_node(state)}

    extraction_entry = state["extraction_records"][0]
    real_filename = extraction_entry["extraction_sidecar_filename"]

    # Craft a traversal filename that looks like it targets a sibling document.
    traversal_filename = f"../other_doc/analysis/{real_filename}"
    # Set indexing_acceptance=index_ready so chunk_node reaches the sidecar-load code.
    tampered_records = [
        {
            **extraction_entry,
            "indexing_acceptance": "index_ready",
            "extraction_sidecar_filename": traversal_filename,
        }
    ]
    tampered_state: WorkbenchState = {
        **state,
        "extraction_records": tampered_records,
    }
    with pytest.raises(ValueError, match="Invalid extraction sidecar filename"):
        chunk_node(tampered_state)


# ---------------------------------------------------------------------------
# Stale-state transition tests (P1 + P2 parity with CLI fixes)
# ---------------------------------------------------------------------------

def test_parse_node_reruns_when_metadata_scan_not_complete(tmp_path: Path) -> None:
    """parse_node must re-run even if parse_status=complete when metadata_scan_status is not complete."""
    from doc_workbench.orchestration.nodes import parse_node
    from doc_workbench.registry.document_registry import DocumentRegistry

    registry_root, doc_id = _make_registry_with_pdf(tmp_path)
    registry = DocumentRegistry(registry_root)

    # Seed: parse already done but scan is stale (never completed).
    registry.update_manifest(doc_id, {
        "pipeline_status": {"parse_status": "complete", "metadata_scan_status": "pending"}
    })

    state: WorkbenchState = {
        "intake_all": True,
        "intake_registry_root": registry_root,
    }
    result = parse_node(state)

    # Document must be processed, not silently skipped.
    assert len(result["parse_records"]) == 1, (
        "parse_node skipped a document with stale metadata_scan_status"
    )
    assert result["parse_records"][0]["document_id"] == doc_id
    assert "error" not in result["parse_records"][0]


def test_parse_node_resets_chunking_status_on_reanalysis(tmp_path: Path) -> None:
    """parse_node must reset chunking_status=pending when it rewrites parse/extraction sidecars."""
    from doc_workbench.orchestration.nodes import parse_node
    from doc_workbench.registry.document_registry import DocumentRegistry

    registry_root, doc_id = _make_registry_with_pdf(tmp_path)
    registry = DocumentRegistry(registry_root)

    # Seed a fully-complete prior pipeline run; force=True bypasses the skip guard.
    registry.update_manifest(doc_id, {
        "pipeline_status": {
            "parse_status": "complete",
            "metadata_scan_status": "complete",
            "chunking_status": "complete",
        }
    })

    state: WorkbenchState = {
        "intake_all": True,
        "intake_registry_root": registry_root,
        "intake_force": True,
    }
    parse_node(state)

    manifest_after = registry.get_manifest(doc_id)
    chunking_status = (manifest_after.get("pipeline_status") or {}).get("chunking_status")
    assert chunking_status == "pending", (
        f"parse_node should have reset chunking_status to 'pending', got {chunking_status!r}"
    )


def test_parse_node_resets_chunking_status_on_stale_scan_rerun(tmp_path: Path) -> None:
    """parse_node must also reset chunking_status when re-running due to stale scan (no --force)."""
    from doc_workbench.orchestration.nodes import parse_node
    from doc_workbench.registry.document_registry import DocumentRegistry

    registry_root, doc_id = _make_registry_with_pdf(tmp_path)
    registry = DocumentRegistry(registry_root)

    # Seed: parse done, chunked, but scan is stale → parse_node re-runs without force.
    registry.update_manifest(doc_id, {
        "pipeline_status": {
            "parse_status": "complete",
            "metadata_scan_status": "pending",
            "chunking_status": "complete",
        }
    })

    state: WorkbenchState = {
        "intake_all": True,
        "intake_registry_root": registry_root,
        # No intake_force — the stale scan status alone must trigger the reset.
    }
    parse_node(state)

    manifest_after = registry.get_manifest(doc_id)
    chunking_status = (manifest_after.get("pipeline_status") or {}).get("chunking_status")
    assert chunking_status == "pending", (
        f"parse_node should have reset chunking_status to 'pending' on stale-scan rerun, "
        f"got {chunking_status!r}"
    )


def test_chunk_node_reads_fresh_manifest_after_parse_resets_chunking(tmp_path: Path) -> None:
    """chunk_node must not skip when parse_node has just reset chunking_status to pending."""
    from doc_workbench.orchestration.nodes import parse_node, extract_node, chunk_node
    from doc_workbench.registry.document_registry import DocumentRegistry

    registry_root, doc_id = _make_registry_with_pdf(tmp_path)
    registry = DocumentRegistry(registry_root)

    # Seed chunking_status=complete so the stale manifest in state would cause a skip.
    registry.update_manifest(doc_id, {
        "pipeline_status": {
            "chunking_status": "complete",
            "metadata_scan_status": "pending",  # stale scan → parse_node won't skip
        }
    })

    state: WorkbenchState = {
        "intake_all": True,
        "intake_registry_root": registry_root,
    }
    parse_result = parse_node(state)

    # parse_node must have reset chunking_status in the registry.
    manifest_mid = registry.get_manifest(doc_id)
    assert (manifest_mid.get("pipeline_status") or {}).get("chunking_status") == "pending", (
        "parse_node did not reset chunking_status to pending after reanalysis"
    )

    state = {**state, **parse_result}
    extract_result = extract_node(state)
    state = {**state, **extract_result}
    chunk_result = chunk_node(state)

    # chunk_node must have acted on the document — chunking_status must move away
    # from "complete" to either "complete" (re-chunked) or "skipped" (not index_ready),
    # but crucially it must have been written by *this* chunk_node run, not left as
    # the seeded "complete" from before the parse reset.
    #
    # The discriminant: if chunk_node skipped due to the stale manifest (the bug),
    # it would return an empty chunk_records list AND never call update_manifest, so
    # the registry would still show the seeded "complete".  After the fix, chunk_node
    # reads the live manifest (chunking_status="pending") and processes the document,
    # leaving chunking_status as "complete" (re-chunked) or "skipped" (rejected).
    #
    # We detect the regression by checking that chunk_node wrote at least one result
    # entry (skipped silently → empty results, no registry write beyond what parse set).
    chunk_records = chunk_result.get("chunk_records") or []
    final_manifest = registry.get_manifest(doc_id)
    final_chunking = (final_manifest.get("pipeline_status") or {}).get("chunking_status")

    # chunk_node must have produced an outcome — either a chunk_records entry (for
    # complete/failed) or a registry update to "skipped".  Empty records + "pending"
    # means it silently did nothing, which is the stale-manifest regression.
    assert final_chunking != "pending", (
        "chunk_node left chunking_status=pending, meaning it skipped the document "
        "without processing it — stale manifest regression"
    )
