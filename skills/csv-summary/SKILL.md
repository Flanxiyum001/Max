---
name: csv-summary
description: Summarize a CSV file — row and column counts, missing values, distinct counts and numeric min/max/mean. Use when asked to profile, audit or quickly understand a CSV dataset.
license: MIT
compatibility: Python 3.11+ standard library only; no network access required.
metadata:
  author: max
  stage: "2"
  version: "1.0"
---

# csv-summary

## When to Use

Use this skill when the user asks to profile, audit, validate or get a quick
overview of a CSV file (row counts, missing data, numeric ranges).

## Procedure

1. Confirm the file path and that it is a `.csv` file inside the workspace.
2. Run the bundled script:

   ```bash
   python scripts/summarize.py <path-to-csv>
   ```

3. Report the summary verbatim, then add at most two sentences of
   interpretation (largest sources of missing data, suspicious ranges).

## Pitfalls

- The script reads the file as UTF-8 with a header row; semicolon-delimited
  files must be converted first.
- Files larger than ~50 MB should be sampled by the user first.
- Never modify the source CSV with this skill; it is read-only.

## Verification

- `rows:` and `columns:` lines are present,
- the column count matches the header,
- missing counts are `0` for a file you know is complete.
