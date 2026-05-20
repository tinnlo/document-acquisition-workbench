"""Tests for `doc-workbench analyze` CLI command."""
from __future__ import annotations

import json
from io import BytesIO
from pathlib import Path

import yaml
from pypdf import PdfWriter
from typer.testing import CliRunner

from doc_workbench import cli
from doc_workbench.registry.document_registry import DocumentRegistry

runner = CliRunner()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _blank_pdf_bytes(pages: int = 2) -> bytes:
    writer = PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(width=200, height=200)
    writer.add_metadata({"/Title": "Test Report 2024"})
    buf = BytesIO()
    writer.write(buf)
    return buf.getvalue()


def _exec_policy(tmp_path: Path, extra_stages: list[str] | None = None) -> Path:
    stages = ["discover", "review", "download", "followup-search", "scan", "analyze", "chunk"]
    if extra_stages:
        stages += extra_stages
    policy_file = tmp_path / "exec_policy.yaml"
    policy_file.write_text(
        yaml.dump({
            "allowed_command_stages": stages,
            "allowed_source_families": ["*"],
            "download": {
                "enabled": True,
                "max_count": 50,
                "max_file_size_bytes": 52_428_800,
                "allowed_mime_types": ["application/pdf", "text/html"],
            },
            "followup_search": {"enabled": True},
            "registry": {"root_restriction": "registry"},
        }),
        encoding="utf-8",
    )
    return policy_file


def _register_downloaded_pdf(registry_root: Path, entity_id: str = "ent001") -> str:
    """Register a PDF with download_status=complete and return its document_id."""
    registry = DocumentRegistry(registry_root)
    result = registry.register_document(
        entity_id=entity_id,
        entity_name="Test Corp",
        source_url=f"https://example.com/{entity_id}/report.pdf",
        family="annual_reports",
        doc_type="official_pdf",
        year="2024",
        pdf_bytes=_blank_pdf_bytes(),
    )
    return result.document_id


# ---------------------------------------------------------------------------
# analyze --all: happy path
# ---------------------------------------------------------------------------

def test_analyze_all_processes_downloaded_documents(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    registry_root = workspace / "registry"
    doc_id = _register_downloaded_pdf(registry_root)
    policy_file = _exec_policy(tmp_path)

    result = runner.invoke(cli.app, [
        "analyze",
        "--all",
        "--workspace-root", str(workspace),
        "--execution-policy-path", str(policy_file),
    ])

    assert result.exit_code == 0, result.output
    # Check at least one analyze_results.json was written in a run dir
    run_dirs = list((workspace / "runs").glob("analyze_*"))
    assert run_dirs, "No analyze run directory created"
    results_file = run_dirs[0] / "analyze_results.json"
    assert results_file.exists()
    data = json.loads(results_file.read_text())
    assert any(r["document_id"] == doc_id for r in data["processed"])


def test_analyze_all_writes_parse_and_extraction_sidecars(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    registry_root = workspace / "registry"
    doc_id = _register_downloaded_pdf(registry_root)
    policy_file = _exec_policy(tmp_path)

    runner.invoke(cli.app, [
        "analyze", "--all",
        "--workspace-root", str(workspace),
        "--execution-policy-path", str(policy_file),
    ])

    registry = DocumentRegistry(registry_root)
    parse_sidecars = registry.list_analysis_sidecars(doc_id, "parse_record")
    extraction_sidecars = registry.list_analysis_sidecars(doc_id, "extraction_record")
    assert parse_sidecars, "No parse_record sidecar written"
    assert extraction_sidecars, "No extraction_record sidecar written"


# ---------------------------------------------------------------------------
# analyze --entity-id: filters correctly
# ---------------------------------------------------------------------------

def test_analyze_entity_id_only_processes_that_entity(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    registry_root = workspace / "registry"
    _register_downloaded_pdf(registry_root, entity_id="ent001")
    _register_downloaded_pdf(registry_root, entity_id="ent002")
    policy_file = _exec_policy(tmp_path)

    result = runner.invoke(cli.app, [
        "analyze", "--entity-id", "ent001",
        "--workspace-root", str(workspace),
        "--execution-policy-path", str(policy_file),
    ])
    assert result.exit_code == 0, result.output

    run_dirs = list((workspace / "runs").glob("analyze_*"))
    data = json.loads((run_dirs[0] / "analyze_results.json").read_text())
    assert all(r.get("entity_id") == "ent001" for r in data["processed"]), data["processed"]


# ---------------------------------------------------------------------------
# analyze: requires --all or --entity-id
# ---------------------------------------------------------------------------

def test_analyze_fails_without_target_selector(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    policy_file = _exec_policy(tmp_path)
    result = runner.invoke(cli.app, [
        "analyze",
        "--workspace-root", str(workspace),
        "--execution-policy-path", str(policy_file),
    ])
    assert result.exit_code != 0


# ---------------------------------------------------------------------------
# analyze: skips already-analyzed documents (no --force)
# ---------------------------------------------------------------------------

def test_analyze_skips_already_complete_without_force(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    registry_root = workspace / "registry"
    doc_id = _register_downloaded_pdf(registry_root)
    policy_file = _exec_policy(tmp_path)

    # Both parse_status and metadata_scan_status must be complete for the skip-guard to trigger.
    from doc_workbench.registry.document_registry import DocumentRegistry as _R
    _R(registry_root).update_manifest(doc_id, {"pipeline_status": {"parse_status": "complete", "metadata_scan_status": "complete"}})

    result = runner.invoke(cli.app, [
        "analyze", "--all",
        "--workspace-root", str(workspace),
        "--execution-policy-path", str(policy_file),
    ])
    assert result.exit_code == 0
    run_dirs = sorted((workspace / "runs").glob("analyze_*"))
    data = json.loads((run_dirs[-1] / "analyze_results.json").read_text())
    assert any(r["document_id"] == doc_id for r in data["skipped"]), data


# ---------------------------------------------------------------------------
# analyze: --force re-runs analysis and writes new sidecar
# ---------------------------------------------------------------------------

def test_analyze_force_reruns_and_writes_new_sidecar(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    registry_root = workspace / "registry"
    doc_id = _register_downloaded_pdf(registry_root)
    policy_file = _exec_policy(tmp_path)

    # First run (blank PDF → ocr_fallback, will produce a sidecar regardless)
    runner.invoke(cli.app, [
        "analyze", "--all",
        "--workspace-root", str(workspace),
        "--execution-policy-path", str(policy_file),
    ])

    registry = DocumentRegistry(registry_root)
    sidecars_before = len(registry.list_analysis_sidecars(doc_id, "parse_record"))

    # Force re-run (even if parse_status != complete, --force overrides)
    runner.invoke(cli.app, [
        "analyze", "--all", "--force",
        "--workspace-root", str(workspace),
        "--execution-policy-path", str(policy_file),
    ])

    sidecars_after = len(registry.list_analysis_sidecars(doc_id, "parse_record"))
    assert sidecars_after > sidecars_before


# ---------------------------------------------------------------------------
# analyze: policy enforcement — missing analyze stage
# ---------------------------------------------------------------------------

def test_analyze_blocked_by_exec_policy_stage(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    policy_file = tmp_path / "policy.yaml"
    policy_file.write_text(
        yaml.dump({
            "allowed_command_stages": ["discover"],
            "allowed_source_families": ["*"],
            "download": {"enabled": True, "max_count": 10, "max_file_size_bytes": 1000, "allowed_mime_types": ["application/pdf"]},
            "followup_search": {"enabled": False},
            "registry": {"root_restriction": "registry"},
        }),
        encoding="utf-8",
    )
    result = runner.invoke(cli.app, [
        "analyze", "--all",
        "--workspace-root", str(workspace),
        "--execution-policy-path", str(policy_file),
    ])
    assert result.exit_code == 2


# ---------------------------------------------------------------------------
# analyze: skips documents where download_status != complete
# ---------------------------------------------------------------------------

def test_analyze_skips_documents_with_incomplete_download(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    registry_root = workspace / "registry"
    doc_id = _register_downloaded_pdf(registry_root)
    # Manually corrupt the download_status
    from doc_workbench.registry.document_registry import DocumentRegistry as _R
    _R(registry_root).update_manifest(doc_id, {"pipeline_status": {"download_status": "failed"}})

    policy_file = _exec_policy(tmp_path)
    result = runner.invoke(cli.app, [
        "analyze", "--all",
        "--workspace-root", str(workspace),
        "--execution-policy-path", str(policy_file),
    ])
    assert result.exit_code == 0
    run_dirs = list((workspace / "runs").glob("analyze_*"))
    data = json.loads((run_dirs[0] / "analyze_results.json").read_text())
    assert any(r["document_id"] == doc_id for r in data["skipped"])
    assert data["processed"] == []


# ---------------------------------------------------------------------------
# analyze: reject both --all and --entity-id (fail-closed)
# ---------------------------------------------------------------------------

def test_analyze_fails_when_both_all_and_entity_id_given(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    policy_file = _exec_policy(tmp_path)
    result = runner.invoke(cli.app, [
        "analyze", "--all", "--entity-id", "ent001",
        "--workspace-root", str(workspace),
        "--execution-policy-path", str(policy_file),
    ])
    assert result.exit_code != 0


# ---------------------------------------------------------------------------
# analyze: rejects symlinked artifact
# ---------------------------------------------------------------------------

def test_analyze_rejects_symlinked_artifact(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    registry_root = workspace / "registry"
    doc_id = _register_downloaded_pdf(registry_root)
    policy_file = _exec_policy(tmp_path)

    registry = DocumentRegistry(registry_root)
    manifest = registry.get_manifest(doc_id)
    real_path = registry._normalize_manifest_path(str(manifest["local_path"]))
    link_path = real_path.parent / "symlink_artifact.pdf"
    link_path.symlink_to(real_path)
    rel = link_path.relative_to(registry_root)
    registry.update_manifest(doc_id, {"local_path": str(rel)})

    runner.invoke(cli.app, [
        "analyze", "--all",
        "--workspace-root", str(workspace),
        "--execution-policy-path", str(policy_file),
    ])
    manifest_after = registry.get_manifest(doc_id)
    # Symlink rejection → error entry; parse_status must NOT be complete
    parse_status = manifest_after.get("pipeline_status", {}).get("parse_status", "pending")
    assert parse_status != "complete"


# ---------------------------------------------------------------------------
# analyze: stale scan guard (P1)
# ---------------------------------------------------------------------------

def test_analyze_reruns_when_metadata_scan_pending(tmp_path: Path) -> None:
    """analyze should re-run even if parse_status=complete when metadata_scan_status is not complete."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    registry_root = workspace / "registry"
    doc_id = _register_downloaded_pdf(registry_root)
    policy_file = _exec_policy(tmp_path)

    # Simulate a prior analyze pass: parse done, but scan was never completed.
    from doc_workbench.registry.document_registry import DocumentRegistry as _R
    _R(registry_root).update_manifest(doc_id, {"pipeline_status": {"parse_status": "complete", "metadata_scan_status": "pending"}})

    result = runner.invoke(cli.app, [
        "analyze", "--all",
        "--workspace-root", str(workspace),
        "--execution-policy-path", str(policy_file),
    ])
    assert result.exit_code == 0, result.output
    run_dirs = sorted((workspace / "runs").glob("analyze_*"))
    data = json.loads((run_dirs[-1] / "analyze_results.json").read_text())
    # Document must be in processed, not skipped
    assert any(r["document_id"] == doc_id for r in data["processed"]), data
    assert not any(r["document_id"] == doc_id for r in data["skipped"]), data


# ---------------------------------------------------------------------------
# analyze: chunking invalidation (P2)
# ---------------------------------------------------------------------------

def test_analyze_resets_chunking_status_when_reanalyzed(tmp_path: Path) -> None:
    """analyze --force should reset chunking_status=pending so chunk --all re-emits fresh output."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    registry_root = workspace / "registry"
    doc_id = _register_downloaded_pdf(registry_root)
    policy_file = _exec_policy(tmp_path)

    # Simulate a fully-complete prior pipeline run.
    from doc_workbench.registry.document_registry import DocumentRegistry as _R
    _R(registry_root).update_manifest(doc_id, {
        "pipeline_status": {
            "parse_status": "complete",
            "metadata_scan_status": "complete",
            "chunking_status": "complete",
        }
    })

    result = runner.invoke(cli.app, [
        "analyze", "--all", "--force",
        "--workspace-root", str(workspace),
        "--execution-policy-path", str(policy_file),
    ])
    assert result.exit_code == 0, result.output

    registry = DocumentRegistry(registry_root)
    manifest_after = registry.get_manifest(doc_id)
    chunking_status = manifest_after.get("pipeline_status", {}).get("chunking_status", "pending")
    assert chunking_status == "pending", f"Expected chunking_status=pending after re-analyze, got {chunking_status!r}"


# ---------------------------------------------------------------------------
# analyze: scan --force followed by analyze --all (P1 regression)
# ---------------------------------------------------------------------------

def test_analyze_reruns_after_forced_scan_refresh(tmp_path: Path) -> None:
    """scan --force must invalidate parse_status so analyze --all does not skip on stale extraction."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    registry_root = workspace / "registry"
    doc_id = _register_downloaded_pdf(registry_root)
    policy_file = _exec_policy(tmp_path)

    # Seed a fully-complete prior pipeline state (mirrors what a real first-pass produces).
    from doc_workbench.registry.document_registry import DocumentRegistry as _R
    _R(registry_root).update_manifest(doc_id, {
        "pipeline_status": {
            "parse_status": "complete",
            "metadata_scan_status": "complete",
            "chunking_status": "complete",
        }
    })

    # Re-scan with --force (simulates updated PDF metadata).
    runner.invoke(cli.app, [
        "scan", "--all", "--force",
        "--workspace-root", str(workspace),
        "--execution-policy-path", str(policy_file),
    ])

    # After a forced scan, parse_status must be reset so analyze --all re-runs.
    manifest_after_scan = _R(registry_root).get_manifest(doc_id)
    assert manifest_after_scan.get("pipeline_status", {}).get("parse_status") != "complete", (
        "scan --force should have reset parse_status to pending"
    )

    # Confirm analyze --all processes (not skips) the document.
    result = runner.invoke(cli.app, [
        "analyze", "--all",
        "--workspace-root", str(workspace),
        "--execution-policy-path", str(policy_file),
    ])
    assert result.exit_code == 0, result.output
    run_dirs = sorted((workspace / "runs").glob("analyze_*"))
    data = json.loads((run_dirs[-1] / "analyze_results.json").read_text())
    assert any(r["document_id"] == doc_id for r in data["processed"]), (
        "analyze --all must re-process after scan --force, not skip"
    )
    assert not any(r["document_id"] == doc_id for r in data["skipped"]), data
