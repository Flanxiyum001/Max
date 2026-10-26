# Proposal format for `max improve run`

```yaml
id: unique-kebab-id            # required, used for report/branch names
category: code                 # required: code | skill | docs | config | …
title: one-line summary        # shown in the commit message
rationale: |
  Why this change; what defect or improvement it addresses.
targets:                       # informational only — the patch decides
  - src/max/config.py
patch: |
  diff --git a/src/max/config.py b/src/max/config.py
  …unified diff against HEAD…
# alternatively: patch_file: my-change.patch   (relative to this file)
```

## What happens

`max improve run proposals/<id>.yaml` → baseline tests → isolated git
worktree on `max/auto/<id>` → patch → static + security gates → candidate
tests → verdict:

* `promoted` — auto-promote allowed by `config/policy.yaml` *and* all gates
  green; applied to the working tree, re-tested, committed as
  `max(improve): <id> …`. Undo with `max improve rollback <id>`.
* `pending_approval` — safe but outside the auto-promote categories (or
  touches existing tests / a sensitive category). A human runs
  `max improve approve <id>`, which re-runs every gate before applying.
* `rejected` — any gate failed; nothing is applied. Report saved to
  `eval/reports/<id>.json`.

## Gates (all recorded in the report)

| Gate | Fails when |
|---|---|
| `patch-applies` | diff does not apply cleanly to HEAD |
| `compile` | changed Python file has a syntax error |
| `static-analysis` | error-severity rule hit (eval/exec/shell=True/os.system/unsafe yaml/no timeout/…) |
| `protected-paths` | diff touches `policy.yaml`, its hash, the pipeline itself, `core/tools.py`, `.git/**` |
| `deny-patterns` | added line matches a deny pattern (`chmod 777`, `verify=False`, `0.0.0.0`, …) |
| `secret-scan` | added line looks like a credential (sk-/ghp_/AKIA/private key/…) |
| `no-test-regression` | candidate has more failures/errors or fewer passes than baseline |
| policy decision | sensitive category, existing-test edit, or size limits → human |

## Example

```yaml
id: docs-add-health-note
category: docs
title: document the health check in README
rationale: Operators need a one-liner to verify the service.
patch: |
  diff --git a/README.md b/README.md
  --- a/README.md
  +++ b/README.md
  @@ -1,3 +1,4 @@
   # MAX
  +
  +Health check: `python -m max health`
```

(Regenerate the hunk numbers against your current README — `git diff` after
making the edit by hand is the reliable way to produce a valid patch.)
