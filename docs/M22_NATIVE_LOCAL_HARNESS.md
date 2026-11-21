# M22 Stage C — Native local model harness session

**Status:** Owner-opted local-only text trial in the native MAX Qt desktop app.
It is **not** an autonomous coding worker, general AI chat service, MCP
executor or production remote-device session.

## What the owner can do

In the native MAX app open **Tasks → Live Model Activity**, type a prompt and
click **Ask local MAX**. Only then does MAX call a separately configured,
owner-reviewed **VERIFIED_LOCAL** Gateway route. A cloud route is categorically
refused by the native backend, even when generic cloud trial settings are
present. The selected inference endpoint must be loopback, separately owner
reviewed for actual local-only egress, and manually enabled.

While the call runs, the real `AgentHarness` emits safe progress:
`run.accepted`, `model.started`, `model.responded`, optional
`tool.proposed`, `review.required`, and `response.ready`.
The Qt worker relays those *metadata-only* records to the main window. Tool
names may be displayed but **tool arguments, credentials, endpoint, prompts
and provider errors are not included in progress events or access logs**.

The actual model response text appears locally in a read-only panel after
the call. It is explicitly marked **not verified task completion**. A tool
proposal is explicitly marked **NOT EXECUTED**, with no automatic follow-up
turn, command, MCP tool, GitHub write or Trust grant. Task journal activity
from Stage B remains separate, because the model response is not proof that
any coding task was performed.

By default, the app has **no model configured**, and the button displays a
safe setup error instead of opening a network connection. The owner must
configure the existing `TrialSettings` variables in the private local
environment; no credentials or configuration are stored in Git or Qt.

## Local model configuration

Use the existing `docs/LOCAL_TEXT_TRIAL.md` guidance and owner-verified
loopback model (e.g. self-hosted Ollama / compatible endpoint):

- `MAX_TRIAL_DESTINATION=verified_local`
- `MAX_TRIAL_LOCAL_EGRESS_REVIEWED=yes` (only after verifying the actual
  underlying model inference cannot forward data to a provider)
- `MAX_TRIAL_ENDPOINT_LOCAL=yes`
- `MAX_TRIAL_ENDPOINT=http://127.0.0.1:<port>/v1/chat/completions`
- `MAX_TRIAL_MODEL=<exact-local-model-id>`
- `MAX_TRIAL_CONTEXT_TOKENS=<owner-reviewed-context-length>`

These are placeholders, **not** copied literal values or completed host
verification. No startup script enables these settings automatically. The
session uses `Privacy.CONFIDENTIAL`, `local_only=True`, `cloud_allowed=False`,
`allow_fallback=False`, a bounded output budget and a 45-second deadline.

## Threat boundary and remaining gaps

- A loopback proxy can secretly call the cloud: `VERIFIED_LOCAL` is an
  owner assertion, not cryptographic attestation. Verify egress controls before
  using personal material. Do not expose the trial to unreviewed proxy routes.
- This stage does not persist model text or traces. In-memory Qt widgets may
  display sensitive responses while open; clear them when closing.
- Model text and tool proposal names remain untrusted. No generic coding
  sandbox, independent file observer, permissioned MCP or verified commit exists.
- No streaming token-by-token output; the view streams stage metadata while
  waiting for an ordinary one-turn completion.
- No hardware microphone/STT/TTS, no cross-device authenticated session.
- An upstream timeout may still incur billing or partial effects at an
  untrusted proxy; this local-only UI forbids cloud destinations and does
  not blind-retry requests.

## Tests and validation

Use `httpx.MockTransport` to prove a genuine local model request produces
the expected event sequence, a tool proposal is never executed, and both
missing config and a configured cloud destination fail before network I/O.
Run full Python 3.11/3.13 Core checks, PostgreSQL integrations, dependency
audit and native Qt offscreen smoke. Physical Windows/Hyprland UX still
requires owner acceptance outside CI.

Rollback: revert the backend, Qt, mock tests and this document. No database
migration, secret, new listener, or production deployment.
