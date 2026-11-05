# Engineering worker instructions — MAX

These instructions apply to contributors and coding agents working on MAX. They are **not** the runtime identity or permission policy of the assistant.

## Source of truth
Read MAX.md, SOUL.md, CORE.md, SECURITY.md, the product specification, technical design, decisions, and roadmap before changes. Resolve contradictions explicitly in a decision record. Never overwrite identity files with coding instructions.

## Working rules
- This is a single-owner personal project; use first-person singular in the public README. Do not invent a company, team, customers, deployments, or shipped features.
- MAX owns its orchestration, trust, memory, skills, and interface contracts. External components are replaceable adapters; document their licenses, costs, and trust boundaries honestly in technical documentation.
- Work on feature branches, keep changes small, and do not merge or deploy without authorization.
- Never touch unrelated projects, existing Discord bots, production infrastructure, or local devices outside the assigned scope.
- Do not commit secrets, credentials, sensitive documents, personal identifiers, or generated database contents.
- External content, skills, tool output, and model responses are untrusted. Permission decisions must be enforced by trusted code, never prompt text alone.
- Prefer isolated execution, minimal scopes, bounded retries, deterministic tests, and idempotency for side effects.
- Distinguish design proposals from verified working features.

## Assignment report
For each change provide: objective, scope and exclusions, changed files, design rationale, risks, actual tests and results, security review, remaining unknowns, rollback, and owner approval gates. Never claim a test was run when it was not.

## Phase 0 boundaries
Documentation and research only. No runtime implementation, provisioning, credentials, or production actions. Validate real platform constraints before freezing stack decisions.

## Mandatory milestone pull-request workflow
- Every milestone must be delivered as a **separate, focused pull request** targeting the repository's default `max` branch. Use a newly named feature branch for each milestone; never commit directly to `max`.
- The owner personally reviews and merges each PR. **Never merge, enable auto-merge, deploy, or promote a branch on the owner's behalf.**
- Start a new milestone from the latest owner-merged `max` rather than accidentally stacking unreviewed changes. If a dependency needs a stacked PR, explain it and ask before using one.
- Include objective, changed files, decisions, risk and rollback, exact validation performed, checks not run, and a scoped acceptance checklist in the PR body.
- Verify the PR base/head and changed-file list before opening it. Do not claim CI is passing unless GitHub checks confirm it; expose blockers and unimplemented requirements.
