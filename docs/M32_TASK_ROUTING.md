# M32: Task-based model routing for FreeLLMAPI, and readable failures

## What it does
With `MAX_TRIAL_MODEL=auto` (FreeLLMAPI), MAX now picks a routing strategy per message:

| Task (detected from your words)                         | Model requested |
|---------------------------------------------------------|-----------------|
| quick: greetings, time, short questions                 | `auto:fast`     |
| chat: everyday conversation                             | `auto` (your fallback chain) |
| code: code, scripts, bugs, errors, SQL, regex...        | `auto:smart`    |
| reasoning: explain/why/plan/compare/math, long messages | `auto:smart`    |

FreeLLMAPI then chooses the actual provider (Groq, Gemini, OpenRouter...) and fails over between them.
Classification is a local keyword check: no extra model call, no extra data sent anywhere.
If a routed strategy fails (for example a profile name your FreeLLMAPI doesn't have), MAX
retries once with the base `auto` model, unless a tool already ran in that turn.

## Settings (all optional)
- `MAX_ROUTE_QUICK`, `MAX_ROUTE_CHAT`, `MAX_ROUTE_CODE`, `MAX_ROUTE_REASONING`: any model id your
  FreeLLMAPI accepts, e.g. `MAX_ROUTE_CODE=auto:coding` after you create a "coding" profile in its
  dashboard, or a fixed id like `gemini-2.5-flash`.
- `MAX_ROUTING=off`: always use `MAX_TRIAL_MODEL`.
- A fixed (non-`auto`) `MAX_TRIAL_MODEL` is never swapped unless you set a `MAX_ROUTE_*` value.

## Failures are now visible
The orb says why a call failed (key rejected, server unreachable, rate-limited, timeout...), and the
server log records `model call failed: task=... model=... code=...` with sanitized codes only.
"model not configured" also logs the configuration code (for example `cloud_budget_required`).

## Hosting note
Render cannot reach `http://localhost:3001` on your PC. `MAX_TRIAL_ENDPOINT` must be a public HTTPS
URL of your FreeLLMAPI (a tunnel such as Cloudflare Tunnel, or FreeLLMAPI running on a server),
ending in `/v1/chat/completions`.
