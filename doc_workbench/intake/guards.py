"""Shared path-security guards for the intake pipeline.

Both the CLI (``analyze`` / ``chunk``) and the LangGraph intake nodes import
from this module so that the exact same containment, symlink, and size checks
are applied everywhere — no independent reimplementations.
"""
from __future__ import annotations

import re
from pathlib import Path

_PARSE_SIDECAR_RE = re.compile(r"^parse_record\.\d{8}T\d{6}\d+Z\.json$")
_EXTRACTION_SIDECAR_RE = re.compile(r"^extraction_record\.\d{8}T\d{6}\d+Z\.json$")

_DEFAULT_MAX_FILE_BYTES = 52_428_800  # 50 MiB


def check_artifact_path(
    artifact_path: Path,
    registry_root: Path,
    max_file_bytes: int = _DEFAULT_MAX_FILE_BYTES,
) -> None:
    """Raise ``ValueError`` if *artifact_path* fails containment, symlink, or size checks.

    Parameters
    ----------
    artifact_path:
        Resolved path to the artifact file (does not need to exist for the
        containment check, but size/symlink checks are skipped for absent files).
    registry_root:
        Resolved registry root path.  ``artifact_path`` must be inside this tree.
    max_file_bytes:
        Maximum allowed file size in bytes.
    """
    try:
        artifact_path.resolve().relative_to(registry_root.resolve())
    except ValueError:
        raise ValueError(
            f"Artifact path '{artifact_path}' is outside registry root '{registry_root}'. Blocked."
        )
    if artifact_path.exists():
        if artifact_path.is_symlink():
            raise ValueError(
                f"Symlink detected at artifact path {artifact_path}. Read blocked."
            )
        sz = artifact_path.stat().st_size
        if sz > max_file_bytes:
            raise ValueError(
                f"Artifact {artifact_path} size {sz:,} bytes exceeds {max_file_bytes:,} limit."
            )


def check_sidecar_path(
    sidecar_path: Path,
    registry_root: Path,
    max_file_bytes: int = _DEFAULT_MAX_FILE_BYTES,
) -> None:
    """Raise ``ValueError``/``FileNotFoundError`` if *sidecar_path* fails guards.

    Applies containment, existence, symlink, and size checks.  Unlike
    ``check_artifact_path`` the sidecar **must** exist.
    """
    try:
        sidecar_path.resolve().relative_to(registry_root.resolve())
    except ValueError:
        raise ValueError(
            f"Sidecar path '{sidecar_path}' is outside registry root '{registry_root}'. Blocked."
        )
    if not sidecar_path.exists():
        raise FileNotFoundError(f"Sidecar not found: {sidecar_path}")
    if sidecar_path.is_symlink():
        raise ValueError(
            f"Symlink detected at sidecar path {sidecar_path}. Read blocked."
        )
    sz = sidecar_path.stat().st_size
    if sz > max_file_bytes:
        raise ValueError(
            f"Sidecar {sidecar_path} size {sz:,} bytes exceeds {max_file_bytes:,} limit."
        )


def validate_parse_sidecar_basename(ref: str) -> str:
    """Return *ref* after validating it is a safe bare filename.

    Raises ``ValueError`` if *ref* contains directory components or does not
    match ``parse_record.<timestamp>.json``.
    """
    basename = Path(ref).name
    if basename != ref or not _PARSE_SIDECAR_RE.match(basename):
        raise ValueError(
            f"Invalid parse sidecar filename {ref!r}. "
            "Must be a bare filename matching parse_record.<ts>.json"
        )
    return basename


def validate_extraction_sidecar_basename(ref: str) -> str:
    """Return *ref* after validating it is a safe bare filename.

    Raises ``ValueError`` if *ref* contains directory components or does not
    match ``extraction_record.<timestamp>.json``.
    """
    basename = Path(ref).name
    if basename != ref or not _EXTRACTION_SIDECAR_RE.match(basename):
        raise ValueError(
            f"Invalid extraction sidecar filename {ref!r}. "
            "Must be a bare filename matching extraction_record.<ts>.json"
        )
    return basename
