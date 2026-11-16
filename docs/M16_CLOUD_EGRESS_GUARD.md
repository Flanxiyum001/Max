# M16 stage 1 — Conservative cloud credential egress guard

**Status:** Owner-review implementation, not a deployed secret-scanning proxy.

## What this PR changes

The gateway's existing cloud admission policy still requires explicit cloud permission,
a cost ceiling and PUBLIC/INTERNAL classification. Stage 1 adds another **deny**
condition: when any prompt message contains a recognized high-confidence credential
pattern, cloud models are removed from `Gateway.eligible()` and cannot be selected
or used as a fallback. An explicitly verified local inference route remains eligible,
subject to the existing policy.

Both `HTTPAdapter.complete()` and `HTTPAdapter.stream()` also independently
reject recognized credential text **before building an HTTP request**, even if a
caller bypasses `Gateway` and invokes the adapter directly. Denial uses a fixed
error code, without echoing any secret, match, span or prompt.

The scanner is stateless and does not store prompts, publish telemetry or call
other software. Examples of targeted syntax include PEM private-key headers,
known credential prefixes, Authorization bearer/basic credentials, and environment
assignments for sensitive configuration keys. Tests contain synthetic strings only.

## Critical limitations and decisions

- **Not an exhaustive PII/secret scrubber.** Unknown formats, plain personal
  details, base64 or other encodings, screenshots, audio, files, tool arguments
  and indirect prompt disclosures are not detected. False negatives and false
  positives remain possible. No claim of structurally impossible leaks is made.
- **Deny instead of rewrite.** Automatically replacing secrets can corrupt
  code, commands and user requests. Rehydrating model text with actual credentials
  risks disclosure through a second channel. No token vault, rehydration or
  silently changed prompt is introduced in this stage.
- The scanner is an extra safety net, **not permission to classify a sensitive
  request as PUBLIC**, send confidential text to cloud, or auto-attest a loopback
  proxy as local. Existing `InferenceDestination.UNKNOWN` rejection remains.
- Only current plain-text GatewayRequest messages are guarded. Future multimodal
  or tool-result transports require their own verified mandatory egress gate.
- This is a local library change: no remote service configuration, new secrets,
  database migration, scheduled process, provider calls or production deployment.

## Acceptance checklist

- [ ] CI on Python 3.11 and 3.13: Ruff lint/format, strict mypy,
      PostgreSQL migrations, complete tests and dependency audit.
- [ ] Synthetic PEM/provider/env/bearer patterns are recognized.
- [ ] A cloud-only request with a recognized secret sends **zero** HTTP requests,
      including when fallback is enabled.
- [ ] With an approved verified-local alternative, cloud is skipped and local
      admission remains subject to existing constraints.
- [ ] Direct HTTPAdapter completion and streaming paths reject before network I/O.
- [ ] Existing privacy, budget, UNKNOWN destination and fallback tests remain green.
- [ ] Independent security review of false-negative/false-positive examples,
      missing data surfaces and proxy egress assumptions.

## Next stage

Consider a local-only reviewed scanner + optional reversible tokenization
**only after** deciding token-vault lifecycle, bounded retention, key handling,
encoding/multimodal coverage, subprocess isolation, explicit owner consent,
and per-destination network policy. Add adversarial fixtures, independent
network-exfiltration testing and deterministic rollback first.

Rollback: close this draft PR without merging, or revert these five scoped
files after owner review. Nothing is deployed automatically.
