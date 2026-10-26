# Stage 3 — Prioritized Next-Stage Plan

Ordered by (value × confidence) / effort, with the evidence that motivates
each item. P0 items resolve limitations found by measurement in Stage 2.

## P0 — before daily use

1. **Real-provider verification (unblocks everything else).**
   No FreeLLM endpoint/key existed in this environment, so the
   OpenAI-compatible adapter is verified only against tests and mocks.
   Action: set `MAX_BASE_URL`/`MAX_MODEL`/`FREELLM_API_KEY` from your
   provider's docs, run `max health`, then a real `max ask`, then re-run
   `max eval run`. Add 2–3 live-provider scenarios (auth failure, 429,
   streaming if supported) gated behind `requires: [live]`.
2. **Run the benchmark on the actual laptop.** All numbers here come from a
   container (2 vCPU). Re-run `max bench --configs A,D` on the Hyprland box
   and record laptop numbers in `docs/BENCHMARKS.md`.
3. **Human-approval separation for sensitive changes.** Today the policy
   hash is re-pinnable by anything with the user's shell. Move
   `max improve approve` + `max improve trust-policy` to a second OS user
   (or require a signed policy file), keeping the model's tools unable to
   reach either. Motivation: Stage 2 §SECURITY limitation 1.

## P1 — high value, low risk

4. **Streaming responses** in `openai_compat` (chunked SSE) — real
   conversations will feel much faster; the mock already proves the parse
   path via the nanobot/OpenClaw experiments.
5. **Skill-script execution tool** — a sandboxed `skill_run` tool so the
   model can execute a promoted skill's `scripts/` (currently deliberate:
   skill scripts run only under the trusted eval harness).
6. **Memory search over session history** (Hermes-style on-demand
   retrieval) instead of the current bounded note store — JSONL is already
   in place.
7. **Systemd user units** for MAX (`max health` watchdog) *if* the laptop
   is meant to keep it always on — opt-in, loopback only, no auto-exposure.

## P2 — when the OpenClaw gateway is actually wanted

8. **MAX ↔ OpenClaw conversation path**: either (a) enable the gateway's
   OpenAI surface and point a MAX provider profile at it (adapter already
   fits), or (b) bridge `openclaw mcp serve` as a registered MCP tool set in
   the agent (client already verified). Decide by measuring B end-to-end
   with a real model.
9. **Channel pairing** (Telegram first) *only* on the gateway with sandbox
   enabled and shell denied — per OpenClaw docs; never on the initial
   profile used in Stage 2.
10. **Port `csv-summary`-style skills to OpenClaw** if its skill system is
    adopted, or keep skills MAX-only to avoid two sources of truth.

## P3 — research track (only with the RTX 4060 machine)

11. Local inference (`llama.cpp`/vLLM) feasibility study for the 8 GB host —
    OpenClaw's docs already warn their smallest recipe needs an 8 GiB floor,
    so expect to offload to the 4060 box.
12. Hermes self-evolution's GEPA-style mutation as an *optional* proposal
    generator feeding MAX's gated pipeline (never a second learning loop —
    one writer only, per the brief).

## Explicitly deferred (not doing now)

| Item | Why deferred |
|---|---|
| Replacing MAX Core with OpenClaw | fails the resource budget (~1 GB idle) and duplicates the skill/eval loop |
| DSPy/GEPA dependency | needs paid tokens; AGPL evolver avoided; current gates are hand-written and tested |
| AGPL `Darwinian Evolver` integration | license contamination |
| Copying hermes-agent source | large pinned dep tree, no measured benefit over its documented patterns |
| Prompt-injection "detectors" as a security boundary | scanners are heuristics; permission policy remains the control |
| Auto-public exposure, tunnels, systemd install, connected messaging accounts | explicitly out of scope for this stage |
| Real streaming-quality benchmarks vs real models | blocked on P0-1 credentials |

## Progress criteria for Stage 3

* live-provider scenarios green on the laptop,
* laptop `max bench` numbers appended to `docs/BENCHMARKS.md`,
* one sensitive-category proposal proven to require out-of-band approval,
* MAX→OpenClaw end-to-end conversation either measured (config B row with a
  real latency) or documented as dropped.
