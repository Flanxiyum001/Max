# MAX — OpenClaw adoption evaluation

**Status:** Review recommendation, 2026-11-05; no installation, execution or security penetration testing performed.

## Decision

**Do not adopt OpenClaw as the MAX runtime or fork it during the first implementation slice.** MAX retains first-party identity, durable jobs, approval authority, audit, finance/vault policy and node security. Use its public design as a reference, not a hidden or rebranded dependency. Revisit integrating individual openly licensed components behind a sandboxed adapter if tests justify complexity.

## Evidence and tradeoffs

Official sources: [OpenClaw source/README](https://github.com/openclaw/openclaw), [product documentation](https://docs.openclaw.ai/), [security guide](https://docs.openclaw.ai/gateway/security), [sandboxing](https://docs.openclaw.ai/gateway/sandboxing), [backend matrix](https://docs.openclaw.ai/gateway/sandboxing/supported-capability-matrix).

| Requirement | OpenClaw public capability | MAX fit / qualification |
| --- | --- | --- |
| Gateway/chat channels | Integrated gateway, sessions, channel adapters | Strong reference, but integration surface/authority bigger than initial MAX slice |
| Agent orchestration | Sessions and multi-agent routing | Useful pattern; not proof of MAX-specific durable and transactional jobs |
| Tool execution | Host tool calls, configurable sandbox | **High risk if default host tools are exposed**; MAX requires deny by default |
| Skills/plugins | Skill procedures, plugins/registry | Useful patterns; acquired instructions/code remain untrusted |
| Browser | Browser integration and configurable isolated browser | Needs network egress restrictions, cookies, uploads/downloads threat analysis |
| MCP | Pluggable tool ecosystem | MCP transport credentials do not replace MAX policy |
| Security | Documented hardening, allowlists, auth and sandbox policies | Nontrivial configuration and audit burden; sandbox off by default |
| Local devices | Companion/nodes | MAX needs owner-scoped, attested/revocable jobs on Windows and Arch |
| Ownership | OpenClaw MIT licensed upstream according to README | Licensing permits evaluation, not automatic safe composability or permission to copy branding |

## Options considered

1. **Full adoption:** shortest route to early chats/channels, highest coupling; security/control semantics not tailored to MAX. Reject now.
2. **Fork:** control source but inherits maintenance burden, security patches, third-party notices and upgrade conflicts. Reject now.
3. **Adapter/reuse later:** acquire a specific tested feature with pinned version, licence, SBOM, network and capability review. **Conditionally allowed later**, no blanket runtime import.
4. **MAX thin custom runtime:** slightly more initial engineering, but explicit permissions and durable execution with smaller attack surface. **Preferred**.

## Re-evaluation gates

A future proposal must supply: exact OpenClaw version/license and dependencies; reproduction of needed feature; data access map; no host-shell escape; independent MAX Trust policy and revocation; cancellation and recovery semantics; credential isolation; sandbox test; maintenance budget; rollback. It must never silently replace MAX identity or call its native tool policy 'MAX Trust'.

## Potential design lessons without dependency

Model adapters, channel abstraction, isolated browser profiles, plugin provenance, agent session separation, structured event stream, explicit security documentation. **Do not run the upstream quick-install script as part of Phase 0.**
