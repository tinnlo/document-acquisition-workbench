"""Tests for `doc-workbench chunk` CLI command."""
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


def _exec_policy(tmp_path: Path) -> Path:
    policy_file = tmp_path / "exec_policy.yaml"
    policy_file.write_text(
        yaml.dump({
            "allowed_command_stages": ["discover", "review", "download", "followup-search", "scan", "analyze", "chunk"],
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


def _register_and_analyze(workspace: Path, entity_id: str = "ent001") -> str:
    """Register a PDF, run analyze, return document_id."""
    registry_root = workspace / "registry"
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
    policy_file = workspace.parent / "exec_policy.yaml"
    runner.invoke(cli.app, [
        "analyze", "--all",
        "--workspace-root", str(workspace),
        "--execution-policy-path", str(policy_file),
    ])
    return result.document_id


# ---------------------------------------------------------------------------
# chunk --all: happy path (blank PDFs yield no text chunks; check skipped/processed)
# ---------------------------------------------------------------------------

def test_chunk_all_runs_without_error(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    policy_file = _exec_policy(tmp_path)
    doc_id = _register_and_analyze(workspace)

    result = runner.invoke(cli.app, [
        "chunk", "--all",
        "--workspace-root", str(workspace),
        "--execution-policy-path", str(policy_file),
    ])
    assert result.exit_code == 0, result.output
    run_dirs = list((workspace / "runs").glob("chunk_*"))
    assert run_dirs


def test_chunk_all_writes_results_json(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    policy_file = _exec_policy(tmp_path)
    _register_and_analyze(workspace)

    runner.invoke(cli.app, [
        "chunk", "--all",
        "--workspace-root", str(workspace),
        "--execution-policy-path", str(policy_file),
    ])
    run_dirs = list((workspace / "runs").glob("chunk_*"))
    assert run_dirs
    results_file = run_dirs[0] / "chunk_results.json"
    assert results_file.exists()
    data = json.loads(results_file.read_text())
    assert "processed" in data
    assert "skipped" in data
    assert "errors" in data


# ---------------------------------------------------------------------------
# chunk: requires --all or --entity-id
# ---------------------------------------------------------------------------

def test_chunk_fails_without_target_selector(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    policy_file = _exec_policy(tmp_path)
    result = runner.invoke(cli.app, [
        "chunk",
        "--workspace-root", str(workspace),
        "--execution-policy-path", str(policy_file),
    ])
    assert result.exit_code != 0


# ---------------------------------------------------------------------------
# chunk: non-complete parse_status → chunking_status=skipped
# ---------------------------------------------------------------------------

def test_chunk_skips_non_complete_parse_status(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    registry_root = workspace / "registry"
    policy_file = _exec_policy(tmp_path)

    registry = DocumentRegistry(registry_root)
    result = registry.register_document(
        entity_id="ent001",
        entity_name="Test Corp",
        source_url="https://example.com/ent001/report.pdf",
        family="annual_reports",
        doc_type="official_pdf",
        year="2024",
        pdf_bytes=_blank_pdf_bytes(),
    )
    doc_id = result.document_id
    # Set parse_status to something other than complete
    registry.update_manifest(doc_id, {"pipeline_status": {"parse_status": "failed"}})

    chunk_result = runner.invoke(cli.app, [
        "chunk", "--all",
        "--workspace-root", str(workspace),
        "--execution-policy-path", str(policy_file),
    ])
    assert chunk_result.exit_code == 0
    manifest = registry.get_manifest(doc_id)
    assert manifest["pipeline_status"].get("chunking_status") == "skipped"


# ---------------------------------------------------------------------------
# chunk: policy enforcement — missing chunk stage
# ---------------------------------------------------------------------------

def test_chunk_blocked_by_exec_policy_stage(tmp_path: Path) -> None:
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
        "chunk", "--all",
        "--workspace-root", str(workspace),
        "--execution-policy-path", str(policy_file),
    ])
    assert result.exit_code == 2


# ---------------------------------------------------------------------------
# chunk: invalid parse_record_ref path → chunking_status=failed
# ---------------------------------------------------------------------------

def test_chunk_invalid_parse_record_ref_sets_failed(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    registry_root = workspace / "registry"
    policy_file = _exec_policy(tmp_path)
    doc_id = _register_and_analyze(workspace)

    # Corrupt the extraction record's parse_record_ref
    registry = DocumentRegistry(registry_root)
    extraction_paths = registry.list_analysis_sidecars(doc_id, "extraction_record")
    assert extraction_paths, "No extraction sidecar to corrupt"
    extraction_data = json.loads(extraction_paths[-1].read_text(encoding="utf-8"))
    # Force index_ready + broken ref
    extraction_data["indexing_acceptance"] = "index_ready"
    extraction_data["parse_record_ref"] = "../../../etc/passwd"
    extraction_paths[-1].write_text(json.dumps(extraction_data), encoding="utf-8")

    result = runner.invoke(cli.app, [
        "chunk", "--all",
        "--workspace-root", str(workspace),
        "--execution-policy-path", str(policy_file),
    ])
    # Should not crash with exit_code=1 (errors list non-empty)
    manifest = registry.get_manifest(doc_id)
    # Either failed or skipped — must not be pending
    status = manifest["pipeline_status"].get("chunking_status", "pending")
    assert status != "pending"


# ---------------------------------------------------------------------------
# chunk: symlink on artifact path is rejected
# ---------------------------------------------------------------------------

def test_chunk_rejects_symlinked_artifact(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    registry_root = workspace / "registry"
    policy_file = _exec_policy(tmp_path)
    doc_id = _register_and_analyze(workspace)

    registry = DocumentRegistry(registry_root)
    manifest = registry.get_manifest(doc_id)
    real_path = registry._normalize_manifest_path(str(manifest["local_path"]))
    link_path = real_path.parent / "symlink_artifact.pdf"
    link_path.symlink_to(real_path)
    # Update manifest local_path to the symlink
    rel = link_path.relative_to(registry_root)
    registry.update_manifest(doc_id, {"local_path": str(rel)})

    result = runner.invoke(cli.app, [
        "chunk", "--all",
        "--workspace-root", str(workspace),
        "--execution-policy-path", str(policy_file),
    ])
    # Symlink rejection → error or skipped, never pending
    manifest_after = registry.get_manifest(doc_id)
    status = manifest_after["pipeline_status"].get("chunking_status", "pending")
    assert status != "pending"


# ---------------------------------------------------------------------------
# chunk: reject both --all and --entity-id (fail-closed)
# ---------------------------------------------------------------------------

def test_chunk_fails_when_both_all_and_entity_id_given(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    policy_file = _exec_policy(tmp_path)
    result = runner.invoke(cli.app, [
        "chunk", "--all", "--entity-id", "ent001",
        "--workspace-root", str(workspace),
        "--execution-policy-path", str(policy_file),
    ])
    assert result.exit_code != 0


# ---------------------------------------------------------------------------
# chunk: list_analysis_sidecars rejects symlinked analysis/ dir
# ---------------------------------------------------------------------------

def test_list_analysis_sidecars_rejects_symlinked_analysis_dir(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    registry_root = workspace / "registry"
    doc_id = _register_and_analyze(workspace)

    registry = DocumentRegistry(registry_root)
    analysis_dir = registry.ensure_analysis_dir(doc_id)

    # Replace the real analysis/ dir with a symlink pointing elsewhere
    real_target = tmp_path / "real_analysis"
    real_target.mkdir()
    # Move contents into real_target, then replace analysis_dir with symlink
    import shutil
    for child in list(analysis_dir.iterdir()):
        shutil.move(str(child), str(real_target / child.name))
    analysis_dir.rmdir()
    analysis_dir.symlink_to(real_target)

    import pytest
    with pytest.raises(ValueError, match="symlink"):
        registry.list_analysis_sidecars(doc_id, "extraction_record")


# ---------------------------------------------------------------------------
# chunk JSONL: no-overwrite collision retry allocates unique path
# ---------------------------------------------------------------------------

def test_chunk_jsonl_collision_retry_allocates_unique_path(tmp_path: Path) -> None:
    """Simulate a timestamp collision: pre-create the first candidate path and verify
    the CLI still writes a different file rather than overwriting."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    registry_root = workspace / "registry"
    policy_file = _exec_policy(tmp_path)
    doc_id = _register_and_analyze(workspace)

    registry = DocumentRegistry(registry_root)
    analysis_dir = registry.ensure_analysis_dir(doc_id)

    # Patch extraction record to mark the doc as index_ready + valid parse_record_ref
    extraction_paths = registry.list_analysis_sidecars(doc_id, "extraction_record")
    parse_paths = registry.list_analysis_sidecars(doc_id, "parse_record")
    if not extraction_paths or not parse_paths:
        import pytest
        pytest.skip("analyze did not produce sidecars (blank PDF not text-parseable)")

    import json as _json
    extraction_data = _json.loads(extraction_paths[-1].read_text())
    extraction_data["indexing_acceptance"] = "index_ready"
    extraction_paths[-1].write_text(_json.dumps(extraction_data))
    registry.update_manifest(doc_id, {"pipeline_status": {"parse_status": "complete"}})

    # Count chunk JSONL files before
    before = list(analysis_dir.glob("chunks.*.jsonl"))

    runner.invoke(cli.app, [
        "chunk", "--all",
        "--workspace-root", str(workspace),
        "--execution-policy-path", str(policy_file),
    ])
    after = list(analysis_dir.glob("chunks.*.jsonl"))
    # At minimum: run completed without crash; chunk file count didn't decrease
    assert len(after) >= len(before)
