#!/usr/bin/env python3
"""Summarize a CSV file: shape, missing values, distinct counts, numeric stats.

Usage: python summarize.py <path-to-csv>

Output format (stable, parsed by MAX's evaluation suite):

    rows: <n>
    columns: <n> (<name>, <name>, ...)
    missing: <name>=<n>, ...
    distinct: <name>=<n>, ...
    numeric:
      <name>: min=<v> max=<v> mean=<v>
"""

from __future__ import annotations

import csv
import sys
from pathlib import Path


def summarize(path: Path) -> str:
    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        if not reader.fieldnames:
            raise ValueError("CSV has no header row")
        columns = list(reader.fieldnames)
        rows = list(reader)

    missing = {c: 0 for c in columns}
    distinct: dict[str, set[str]] = {c: set() for c in columns}
    numeric: dict[str, list[float]] = {c: [] for c in columns}

    for row in rows:
        for column in columns:
            value = (row.get(column) or "").strip()
            if value == "":
                missing[column] += 1
                continue
            distinct[column].add(value)
            try:
                numeric[column].append(float(value))
            except ValueError:
                pass

    lines = [f"rows: {len(rows)}", f"columns: {len(columns)} ({', '.join(columns)})"]
    lines.append("missing: " + ", ".join(f"{c}={missing[c]}" for c in columns))
    lines.append("distinct: " + ", ".join(f"{c}={len(distinct[c])}" for c in columns))
    numeric_lines = []
    for column in columns:
        values = numeric[column]
        # a column is numeric when every non-missing value parsed
        non_missing = len(rows) - missing[column]
        if values and len(values) == non_missing:
            mean = sum(values) / len(values)
            numeric_lines.append(
                f"  {column}: min={min(values):g} max={max(values):g} mean={mean:.2f}"
            )
    if numeric_lines:
        lines.append("numeric:")
        lines.extend(numeric_lines)
    return "\n".join(lines)


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: summarize.py <path-to-csv>", file=sys.stderr)
        return 2
    path = Path(argv[1])
    if not path.is_file():
        print(f"error: no such file: {path}", file=sys.stderr)
        return 2
    if path.suffix.lower() != ".csv":
        print(f"error: not a .csv file: {path}", file=sys.stderr)
        return 2
    try:
        print(summarize(path))
    except (ValueError, UnicodeDecodeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
