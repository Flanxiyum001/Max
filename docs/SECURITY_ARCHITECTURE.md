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
