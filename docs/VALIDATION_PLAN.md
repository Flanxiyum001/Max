# MAX Phase 0B validation and Phase 1 test plan

**Phase 0B:** docs only. Do not deploy, install packages or connect accounts merely to check this PR.

## Documentation-level validation

1. Fetch each of the 19 paths in [REQUIREMENT_TRACEABILITY.md](REQUIREMENT_TRACEABILITY.md); reject empty/404.
2. Enumerate all changed files from `max...docs/phase0b-architecture-closure` and reject runtime, secret, identity or unrelated changes.
3. Parse Markdown relative hyperlinks and check target files exist. Exclude external URLs from offline existence tests; spot-check primary sources independently.
4. Render and visually inspect **eight** Mermaid diagrams in [ARCHITECTURE_DIAGRAMS.md](ARCHITECTURE_DIAGRAMS.md). A syntactically present graph is not proof that renderer succeeds.
5. Scan proposed docs for contradicted branch name `main` vs actual default `max`, invented shipping claims, unverified rates, unauthorized credentials and undefined owner decisions.
6. Verify Phase 0B PR contains risks, tests **actually run**, exclusions, rollback, and a manual merge gate.
7. Review GitHub CI check-runs independently; lack of status is 'not run/not reported', not passed.

## Next PR acceptance suite (implementation only)

| Slice | Minimum tests |
| --- | --- |
| API/auth | Deny unauthenticated; expired session; CSRF/origin; resource owner mismatch; rate limiting; redaction |
| Task persistence | Restart between commit/outbox delivery; cancellation; worker crash; duplicate result; bounded retry |
| Gateway | OpenAI and Anthropic normalization; streaming; malformed tool payload; rate limit; provider outage; privacy fallback |
| Trust | Tool schema validation; forbidden action; replayed approval; changed recipient/amount; audited denial |
| Browser | Private IP/metadata deny; redirect SSRF; downloads; prompt-injection attempt; no owner Chrome profile |
| Skills | Tampered hash; unknown license; excessive permissions; sandbox escape attempt; downgrade and rollback |
| Nodes | Enrollment/revocation; cross-node lease; command injection; quota; shutdown with unsaved work |
| Memory & vault | Provenance, correction, export, deletion; restricted leakage; encrypted backup/restore/key loss |
| Desktop and voice | Windows/Hyprland focus/game test; microphone mute; interruption; license and latency |
| Communications | Signed webhooks, caller auth, quiet hours, regional provider route, duplicate message id |

## Evidence per PR

Include immutable commit SHA, exact CLI commands where used, exit code and stdout summary, test count, CI link, environment limitations, screenshots or anonymized fixtures, risks and manual review gate. Never invent a test result or a benchmark. A missing physical device/provider test remains an explicit pre-production blocker.
