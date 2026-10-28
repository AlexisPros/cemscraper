"""JSON / JSONL export of collected questions."""

from __future__ import annotations

import json
from collections.abc import Iterable
from pathlib import Path
from typing import Any, Literal

ExportFormat = Literal["json", "jsonl"]
VALIDATION_KEYS = ("validation_status", "validation_issues")


def shape(record: dict[str, Any], include_validation: bool) -> dict[str, Any]:
    if include_validation:
        return record
    return {k: v for k, v in record.items() if k not in VALIDATION_KEYS}


def export_records(
    records: Iterable[dict[str, Any]], path: Path, fmt: ExportFormat, *, include_validation: bool = False
) -> int:
    """Write records atomically (temp file + rename). Returns the number written."""
    rows = [shape(r, include_validation) for r in records]
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as handle:
        if fmt == "json":
            json.dump(rows, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        else:
            for row in rows:
                handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    tmp.replace(path)
    return len(rows)
