from __future__ import annotations

import json
from pathlib import Path
from typing import Iterator

from doc_workbench.knowledge.models import ChunkRecord


def write_chunk_jsonl(chunks: Iterator[ChunkRecord], output_path: Path) -> int:
    """Write ChunkRecords line-by-line from iterator to a JSONL file.

    Opens the file with ``"x"`` (exclusive create) so that it fails with
    :class:`FileExistsError` rather than silently overwriting an existing
    file.  Callers that want collision-retry behaviour should catch
    ``FileExistsError``, generate a new timestamp path, and retry.

    Streams from the iterator without accumulating the full list in memory.
    Returns the number of chunks written.
    """
    count = 0
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("x", encoding="utf-8") as fh:
        for chunk in chunks:
            fh.write(json.dumps(chunk.to_dict(), ensure_ascii=False))
            fh.write("\n")
            count += 1
    return count


def read_chunk_jsonl(path: Path) -> Iterator[dict]:
    """Yield one parsed dict per line without loading the full file into memory."""
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                yield json.loads(line)
