# Development Workflow — Codex

## Roles

- **Owner:** product direction and approval for sensitive/high-impact actions; complete beginner in engineering.
- **Technical lead:** architecture, stack decisions, task breakdown, Codex prompts, reviews, testing expectations, security, and documentation.
- **Codex:** primary implementation worker inside the repository.

## Repository rules

- Keep `max` (the verified repository default branch) stable; implement work on a fresh feature branch created from the latest owner-merged `max`.
- Prefer small, reviewable pull requests.
- Do not commit secrets, tokens, private documents, local databases, generated credentials, or personal data.
- Never modify unrelated projects or infrastructure.
- Update relevant documentation and tests with each change.
- Use pinned dependencies and reproducible setup instructions.
- Do not add large frameworks without a written reason.

## Required Codex assignment format

1. Objective and user outcome.
2. Context and relevant architecture documents.
3. Scope and explicitly out-of-scope work.
4. Files/modules expected to change.
5. Security, privacy, and permission requirements.
6. Step-by-step implementation guidance.
7. Tests: unit, integration, security, and regression as appropriate.
8. Acceptance criteria expressed as observable outcomes.
9. Commands run and actual results.
10. Risks, limitations, migrations, and rollback plan.

## Verification expectations

Codex must report:
- Files changed.
- Design decisions and deviations.
- Tests actually executed, with pass/fail counts.
- Lint/type/build results.
- Security considerations.
- Manual steps still required.
- Evidence for any claimed working behaviour.

Do not claim a feature works because it compiles. Test the relevant user-visible outcome. Do not conceal skipped or failing tests.

## Change control

- No production deployments without explicit authorization.
- No payment, document sharing, destructive, or permission-escalating action without the required approval.
- No self-modification of security policy or production executable code.
- Preserve rollback paths and database migration safety.

## Owner-controlled milestone PR protocol (2026-11-05)
- Deliver **one focused milestone per pull request** against `max`. Do not stack on an unmerged milestone unless the owner explicitly requests it.
- Coding worker prepares design rationale, changed-file inventory, tested commands/results, untested checks, security considerations, rollback and outstanding questions.
- **Owner reviews and merges each PR manually**. Never auto-merge, claim CI success without evidence, or deploy on the owner's behalf.
- If prerequisite PR is unmerged, hold the dependent milestone or explicitly flag the stacking choice instead of modifying `max`.
- For Phase 0B, only architectural documentation changes are authorized. No runtime implementation, device operations, cloud resources or account linking.
