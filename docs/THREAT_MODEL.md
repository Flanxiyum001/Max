# MAX threat model — Phase 0B

**Status:** Design review; not a penetration test or proof that controls are implemented.

## Assets and adversaries

Assets: owner identity, MAX account sessions, device enrollment keys, provider tokens, personal files and Aadhaar/passport references, vault content, transaction approval, task-state integrity, task journals and private communications.

Adversaries: malicious webpage/email/skill content, compromised plugin/MCP server, untrusted model response, account/session attacker, stolen device, malicious network peer, compromised node/host dependency and accidental misuse by MAX itself. Assumption: single user does **not** eliminate cross-service, insider, or supply-chain risks.

## Trust boundaries

`Untrusted channels/web/browser/files -> authenticated ingress -> MAX Trust -> MAX Core -> policy-gated capability registry -> isolated workers / provider adapters / device nodes`. Model output, retrieval text and skills never become trusted instructions/credentials. Human approval must flow through the owner's authenticated UI, bound to a typed action digest, not through a phrase in model output.

## STRIDE-style analysis

| Threat | Entry point / impact | Required mitigation | Negative acceptance test |
| --- | --- | --- | --- |
| Prompt injection / tool-output laundering | Web/email/skill persuades model to exfiltrate or run commands | Strict instruction/data separation; independent policy, origin and capability gates | Inject 'ignore prior policy' page and verify no forbidden tool fires |
| Spoofed authorization | Malicious remote client, phone, webhook | Strong user authentication, signed/timestamped webhooks, nonces, expiry, caller challenge | Forged webhook/approval returns deny with audit |
| Elevation / permission drift | Worker adds own tools or grants scopes | Deny-by-default registry, immutable grants, signed owners, privileged code boundary | Skill requests admin scope -> rejected |
| Secrets leak | Logs, prompts, errors, artifacts, remote providers | Secret store, ephemeral injection, redaction, restricted output schema, DLP checks | Sentinel secret absent from output and audit export |
| Browser SSRF / private network probing | Redirects, mixed DNS, downloads | Egress policy, DNS re-resolve checks, loopback/link-local/metadata deny, redirect revalidation | Block metadata endpoint and DNS rebinding |
| Device command injection / lateral movement | Node jobs, path args, CI | Attested enrollment, scoped job leases, allowlisted operations, no arbitrary shell to LLM, sandbox and resource caps | Malformed or cross-device job rejected |
| Irreversible action replay | Retried payment/transfer/message/upload | Strong approval per exact recipient/amount/resource, idempotency and authoritative reconciliation | Replay payment approval -> only first accepted, second denied |
| Data tampering / poisoned memory | Retrieved docs, skills, backup | Provenance, source integrity, classification, human review, version, rollback | Poisoned memory cannot override owner-confirmed facts |
| Replay/denial of service | Event streams, queue, provider API, wake-word | Authentication, bounded concurrency, rate/spend caps, backoff, circuit breakers | Flood -> throttled, no crash loop |
| Backup compromise/ransomware | Cloud files, storage keys, host | Isolated encrypted backups, offsite copies, rotation, restore drills | Lost key / unavailable Drive -> visible restore blocker |
| Loss of audit / repudiation | Tool completion claim | Append-only audit sink where needed, trace/tool IDs, evidence verifier | Mock 'success' without observed evidence -> not completed |
| Over-collection / privacy breach | Recordings, transcripts, location, vault | Opt-in, narrow scopes, retention deletion, sensitive-data minimization | Microphone off means no recording retained |
| Supply chain / malicious update | GitHub dependencies, dynamic skills | Pinned versions, signed provenance, hash/license check, sandbox test, separate owner review | Malicious install script cannot run on host |
| Model/provider fallback privacy bypass | Local-only confidential task | Classification propagated through routing, no cloud fallback | Provider outage remains local-only/failed, not cloud-routed |

## Action classification and policy

- **Tier 0 — public/read-only:** public research, local non-sensitive queries. Authenticated owner session for personal data.
- **Tier 1 — reversible owner data:** edit bounded draft/note inside approved workspace; approval per scope and durable audit.
- **Tier 2 — external/account side effects:** messages, commits, file sharing, deployments, installs, device wake/shutdown; show target and side effects, require explicit appropriate confirmation.
- **Tier 3 — high consequence:** payments, access/security grants, sensitive vault sharing, account recovery; exact per-action challenge + strong recent owner authentication, deny unattended operation.
- **Forbidden under any tier:** storing UPI PIN/OTP/CVV, bypassing MFA/anti-cheat, unauthorized access, silent financial submission, self-granting privileges.

Approval token design: scope=one task + one typed action + digest of canonical parameters + actor/session/device + expiry + single-use nonce; server-side verification consumes token transactionally. Changes to parameters invalidate approval. Model text cannot issue tokens.

## Security gates by boundary

1. **Client ↔ API:** TLS, session freshness, CSRF where cookie-based, origin validation, token expiry, device trust and rate limits.
2. **API ↔ Core:** canonical owner_id/task_id, validated schemas and tenant isolation even for one owner.
3. **Core ↔ model:** prompt minimization, privacy class, no secrets, tool output proposals only.
4. **Core ↔ skills/tools:** scope decisions in trusted policy layer, isolation, allowlisted egress, timeouts and output limits.
5. **Core ↔ nodes:** outbound authenticated enrollment, signed scoped leases, controlled workspaces, no unrestricted shell.
6. **Core ↔ storage:** minimum DB roles, envelope encryption, restore and deletion evidence.
7. **Core ↔ communications:** verified recipients, webhook signature and replay checks, owner approval for high risk.
8. **Evolution ↔ production:** only proposals/branches, CI/security review, manual merge/deploy gates.

## Residual risks and open checks

- Compromised host can undermine sandbox and local OS keyring; isolate restricted vault from developer runtime.
- Browser context alone is not a security boundary against host exploits.
- Audio wake word may false-trigger; require secondary confirmation for consequential actions.
- Wayland overlay/gaming input may cause interruption; measure rather than assume.
- Real India telecom/WhatsApp rules and exact provider service are unverified.
- No proof of deployment hardening or network controls until implementation tests.

## Incident response and release gate

On suspected credential leak: disable affected adapter, revoke and rotate token, preserve minimal audit, assess exposed downstream scopes, notify owner; restore via verified checkpoints. Never fix security incidents by disabling auth. Required CI before a high-risk integration: secrets scan, dependency/license review, authz denial tests, prompt-injection and SSRF cases, cancellation/replay and restore drill. These tests are **requirements, not runs performed in Phase 0**.

See [SECURITY_ARCHITECTURE.md](SECURITY_ARCHITECTURE.md), [DATA_AND_STORAGE.md](DATA_AND_STORAGE.md), [TELEPHONY_ARCHITECTURE.md](TELEPHONY_ARCHITECTURE.md), and [RESEARCH_FINDINGS.md](RESEARCH_FINDINGS.md).
