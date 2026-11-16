# M16 stage 2 — Offline golden-output regression comparator

**Status:** Review-only library, no live replay or autonomous evaluation.

## Goal and distinction

MAX's Milestone 5 timeline includes **signed task-event pagination metadata**, but
not prompts, normalized model responses, tool arguments or independently verified
ground-truth answers. A timeline cursor is not a signed model-output trace. This
PR **does not** scrape, export or persist personal timelines.

Instead, `OfflineGoldenSuite` accepts separately reviewed fixtures and already
observed `GatewayResponse` values. It compares expectations without starting a
model, replaying network requests or changing MAX's task state. This is a safe
first piece of a future full golden-task replay harness.

## Included

- Immutable `GoldenCase` contracts scoped to a task kind, fully qualified
  `RouteIdentity` and exact catalogue revision.
- A trusted caller must explicitly assert `owner_reviewed=True`. This flag is
  a local contract check **not a cryptographic signature or authentication**.
  Future fixture storage must verify real owner review outside this library.
- Expected answer represented by a SHA-256 digest; optional exact ordered tool
  names and prohibited tool names. A fixture must assert at least one behavior.
- Pure deterministic comparison with failed results for missing observations,
  output drift, wrong route, wrong model, wrong revision, unexpected or
  prohibited tools. Unknown and duplicate observations are rejected rather than
  silently skewing a score.
- Bounded inputs (500 cases/observations). Reports expose only fixture IDs,
  counts and fixed failure codes, never prompts, tool arguments or answer text.
- Synthetic-only unit tests and a mocked normalized GatewayResponse round-trip.

## Excluded / risk

No real provider calls, prompt capture, automated fixture creation, provenance
signatures, shadow routing, latency measurement, cost telemetry, quality-based
routing decisions, automatic corrections, memory writes, skill execution, or
production configuration.

Exact text-digest comparison is intentionally brittle and only proves equality
to a locally reviewed expectation. A digest of guessable sensitive text may
still reveal information by dictionary attack; **do not put sensitive real user
responses or traces into fixtures or CI**. Matching tool names is not proof of
argument correctness, tool authorization or real-world effect. Caller-reported
route identities cannot independently prove a proxy's actual upstream.

## Acceptance and gates

- [ ] GitHub Actions full Python 3.11/3.13 lint, formatting, mypy,
      PostgreSQL migrations, all unit/security tests and dependency audit.
- [ ] No missing, duplicated, unreviewed or unexpected case can be marked passed.
- [ ] Changes in route, revision, provider, answer and tool choice are detected.
- [ ] Reports and error strings never contain raw answer or tool arguments.
- [ ] Existing gateway routing/privacy and evaluation contracts remain unchanged.
- [ ] Human review of fixture provenance, data classification and export process.

## Future stages

1. Owner-approved export process that starts with synthetic fixtures, verifies
   actual signed event provenance and uses a separate encrypted store when real
   traces are ever authorized. Require explicit review, retention/deletion and
   data minimization; never upload personal traces to Git or CI by default.
2. Offline fake-adapter replay of full normalized protocol fixtures with
   deterministic acceptance tests for denial, tool argument validation and
   citation/answer requirements.
3. Opt-in, aggregate-only per-route latency/token/cost measurements. Measure
   actual p95 from sufficiently large samples; do not call proxy rate cards
   exact bills or fabricate time-to-first-audio for text-only models.
4. Signed independent review before promoting evidence to existing
   `OfflineEvaluationBook`; no self-voting model route changes.

Rollback: close draft PR, or revert the three scoped files after manual owner
review. No migrations or runtime behavior changes.
