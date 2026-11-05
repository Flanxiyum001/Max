# MAX security architecture

**Status:** Design requirements; not a completed penetration test.

## Trust boundaries
Owner session; model gateway; untrusted external text; tools and skills; local execution node; browser profile; third-party API; encrypted personal vault; backup and sync destinations.

## Threats and controls
| Threat | Required control | Verification |
| --- | --- | --- |
| Prompt injection in pages or tool results | Data/instruction separation, typed tool schemas, independent policy engine | Injection corpus cannot change grants |
| Tool privilege escalation | Deny-by-default per-operation/device scopes, immutable policy decisions | Denied tool remains denied through fallback |
| Approval replay or substitution | Bind actor, action fingerprint, resource, expiry, nonce and device | Replays and altered requests rejected |
| Credential exfiltration | Secret manager, scoped injection, redaction, outbound restrictions | Secret scan and simulated exfiltration tests |
| Untrusted skill execution | Source/license review, sandbox, network/filesystem limits, pinned version | Malicious skill fixture denied |
| Sensitive document disclosure | Encryption, step-up authentication, recipient check, audit | Wrong recipient and expired grant rejected |
| Duplicate payment or external write | Trusted confirmation, idempotency, authoritative reconciliation | Duplicate requests cannot duplicate side effects |
| Runaway agents/recovery | Timeouts, quotas, cancellation, circuit breakers and dead-letter queue | Fault-injection terminates within budget |

## Risk policy
R0: scoped read-only operations. R1: reversible local changes with prior opt-in. R2: writes, external sends, code changes or exports requiring explicit action approval. R3: financial, destructive, identity, credential or security changes requiring fresh trusted confirmation or denial. Voice or model-generated text alone cannot approve R2/R3.

## Data policy
Never store OTPs, UPI PINs, card CVVs, passwords or tokens in assistant memory. Classify records; minimize prompt content; encrypt in transit and at rest; implement deletion and backup retention. Vault access must not be silently copied into general memory.

## Operational gates
Mandatory tests: permission denial, replay, prompt injection, secret redaction, isolated execution, task cancellation, recovery budgets, backup restore, and safe rollback. No claim of production readiness before evidence exists.

## Expanded Phase 0B threat review

[THREAT_MODEL.md](THREAT_MODEL.md) records additional spoofing, replay, browser SSRF, memory poisoning, device enrollment, supply chain, backup and privacy scenarios. [DATA_AND_STORAGE.md](DATA_AND_STORAGE.md) defines owner-controlled retention, encrypted vault separation and backup/restore gates. [TELEPHONY_ARCHITECTURE.md](TELEPHONY_ARCHITECTURE.md) covers signed webhooks, caller spoofing and India regional restrictions. These are proposed controls and future acceptance tests, **not** evidence of security test execution.

The approval decision is cryptographically bound to owner session, task, typed parameters/destination, action digest, expiry and single-use nonce; model or voice text alone cannot mint approval. Independent policy must be tested on **every** MCP/native/browser/node adapter. Manual owner review and merge for executable/security policy changes remain mandatory.

## Milestone 4 implemented controls and explicit gaps

[TRUST_IMPLEMENTATION.md](TRUST_IMPLEMENTATION.md) documents the newly implemented local-development Trust layer. **Present controls:** hardcoded deny-by-default registry (only task status read), bound task/actor/session/device/call-and-arguments digest, short TTL, atomic owner-approval consumption, auditable denial and independent evidence comparison before task completion. **Not present:** actual high-risk tools, production MFA, remote user identity, signed client/device attestation, tamper-evident audit sink, real independent effect observer, secure external action dispatcher, network egress controls, trustworthy provider token accounting. Those remain blockers before exposing any external effects or personal vault actions.


## Milestone 7 worker boundary — review branch (2026-11-05)

[BOUNDED_WORKER.md](BOUNDED_WORKER.md) limits this first manual local developer worker to *fixed pure* `dev.health` and `dev.add` operations with exact non-sensitive JSON inputs, output and monitored elapsed limits. A task's freeform objective is **untrusted**: an unrecognized `op`, extra field, duplicate key, arbitrary expression, shell command, path or network destination is rejected **before claiming**. Worker execution is a trusted local process with a separately configured local PostgreSQL credential; there is no worker HTTP API or model/skill permission grant. **Do not mistake the existing single-user local bootstrap token for remote worker authentication**. All operation results stay `awaiting_verification` until a separate observer actually proves raw bytes. Failure or uncertain effects quarantine to `needs_reconciliation`; cancellation only reaches `cancelled` after an exact lease-fenced worker acknowledgment. The one-second *post-execution* elapsed monitor is only appropriate for the two constant-work pure operations; **not** a process-level timeout for arbitrary code. No executable task registry extension is authorized without a new threat assessment and isolation layer.


## Milestone 8 — independence is narrow (review-only)

The [Milestone 8 observer](INDEPENDENT_EVIDENCE_OBSERVER.md) independently recomputes only deterministic `dev.add` results using persisted owner task input. The observer neither imports worker execution functions nor accepts worker-supplied verdicts, and cannot grant Trust permissions or execute side effects. A locked atomic verifier rechecks the exact owner/task/input/revision/checkpoint/claimed-digest binding before recording provenance and, on support alone, completion. This is mathematical correctness validation, **not proof that the worker executed**, DB/OS attestation or independent infrastructure. Shared-host/DB administrator compromise, weak local bootstrap identity, plaintext objectives and absence of protected external evidence are unresolved threats; all consequential or remote effects remain disallowed.

## Milestone 9 Stage 2 — gateway endpoint and inference egress are different

`ModelSpec.is_local` means the HTTP endpoint is an explicit loopback host, **not** that the model runs there. The separate `InferenceDestination` is `UNKNOWN` by default and denied entirely; `VERIFIED_LOCAL` requires an owner-reviewed direct local inference path and loopback HTTP; `CLOUD` also includes a local FreeLLMAPI proxy that relays prompts to cloud providers. For cloud inference, `cloud_allowed=True`, `local_only=False`, non-confidential/non-restricted privacy, a USD request limit and validated input/output quote are mandatory **before any send or fallback**. A false `is_local` assumption cannot bypass these checks. Cloud authorization flags must be real booleans rather than truthy strings.

This is **policy enforcement over trusted model metadata**, not runtime verification of actual proxy forwarding: a malicious/misconfigured owner config, changed upstream routes, a compromised proxy, incorrect caller data classification, DNS/SSRF redirection, or real billing anomalies remain threats. `tashfeenahmed/freellmapi` has been selected as a project, but its running instance and upstream policy have **not** been inspected or connected. Gateway request/response/tool/text/event dataclass representations hide payloads; HTTPX and malformed JSON exception chains are suppressed for ordinary logged tracebacks, but arbitrary serialization and explicit exception-context introspection can still reveal data. Keep all sensitive payloads out of application logging and require a separate source/egress verification gate before live connections. No tool, Trust approval, model catalog, deployment, or credentials were introduced.
