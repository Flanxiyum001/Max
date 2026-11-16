# Milestone 13 — opt-in cloud-to-local inference fallback trial

**Review branch only.** This is a single public text prompt, not a MAX assistant process, tool executor, voice interface, workflow worker or production service.

The existing OpenAI-compatible and Anthropic adapters accept real owner-supplied endpoints and keys through the opt-in `gateway_trial`. This milestone adds a second explicitly configured, reviewed OpenAI-compatible **local** route to the same gateway.

The gateway tries the approved cloud provider first. It may send the *same public test prompt* to the reviewed local endpoint only after HTTP **429**, HTTP **503**, or a definite **connection establishment error/timeout**. It will **not** retry on remote read/write timeout, partial stream, authentication failure, malformed response, budget rejection, unknown inference destination or confidentiality restrictions. It ignores all model tool proposals.

## Setup

Install MAX on a trusted Windows or Linux machine per [local text trial](LOCAL_TEXT_TRIAL.md), then configure and validate the cloud variables described there. The cloud destination must be explicitly `cloud` and cloud prices/spend ceiling must be known.

Install and run an independent local OpenAI Chat Completions-compatible model server such as Ollama or a llama.cpp compatible server. Download the model separately, verify it is actually executing inference on the machine and verify that its server **does not forward prompts to a cloud model**. A local proxy or FreeLLMAPI localhost address is *not* enough to claim verified local inference. Keep the service bound to loopback and review host firewall/DNS behavior.

Example (PowerShell, using a **placeholder** model name and a self-managed local server):

```powershell
$env:MAX_TRIAL_FALLBACK_ENABLED = 'yes'
$env:MAX_TRIAL_FALLBACK_LOCAL_EGRESS_REVIEWED = 'yes'
$env:MAX_TRIAL_FALLBACK_ENDPOINT = 'http://127.0.0.1:11434/v1/chat/completions'
$env:MAX_TRIAL_FALLBACK_MODEL = '<installed-local-model-id>'
$env:MAX_TRIAL_FALLBACK_CONTEXT_TOKENS = '4096'
.\.venv\Scripts\python.exe -m max_core.gateway_fallback_trial
```

The three primary cloud and budget gates (`MAX_TRIAL_CLOUD_ALLOWED=yes`, reviewed `MAX_TRIAL_MAX_COST_USD`, and exact published input/output token prices) are still mandatory. The cloud key stays on the cloud request. The local request never receives that key, and no key is stored by this code. The text response reports which of the two declared provider routes was used.

The local route has zero *API token charge* in the dev trial; it does not claim that electricity, hardware and network usage cost nothing. Cloud cost admission is an estimate, **not** provider billing verification or a guaranteed cap.

## Security and acceptance

- The prompt must be public and non-sensitive; do not use credentials, personal files, health data or private conversation history.
- Both routes remain untrusted model output; no MCP tool proposal, file access, system shell or task completion is permitted.
- Gateway streaming remains sticky and never falls back after partial output.
- The local model is not automatically installed, started, updated, selected or attested.
- No production API keys, MCP accounts, secret store or desktop device were accessed in this PR.
- Tests use only `httpx.MockTransport`, including cloud success, 429/503, pre-request connect errors, denied local claims, strict key separation and uncertain-failure rejection.

**Owner merges only after CI and manual review.** Running a real inference trial requires the owner to supply an approved cloud provider and a genuinely local installed model. No deploy occurs from this PR.
