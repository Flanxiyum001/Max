# MAX

**One assistant. One identity. Your devices. Your control.**

MAX is our independently designed, voice-first personal AI system. Its purpose is to turn natural conversation into **authorized, verifiable work**—while remembering what matters, improving with experience, and staying firmly under its owner's control.

MAX is **not a repackaged agent framework**. We own the product architecture, orchestration, permissions, memory lifecycle, task execution, skill governance, and user experience. External integrations, model engines, and open-source libraries may be used behind replaceable interfaces where they solve a genuine engineering need; none should define MAX's identity or have unchecked authority.

> **Project status:** Architecture and engineering foundation. The full application has **not** been built, deployed, or verified yet. Capabilities below describe our design goals, not features that are already working.

## The experience we're building

- **A living desktop presence.** A small, translucent holographic orb with a luminous cyan ring, subtle blue reflections, and responsive animation. It stays out of the way during games and focused work.
- **Voice that feels natural.** Wake or push-to-talk, conversational speech, interruption, quick cancellations, and a distinctive original British-English voice.
- **A reliable personal operator.** MAX can plan multi-step work, choose specialized workers, use approved tools, preserve progress across restarts, and verify results before reporting success.
- **Memory that can be trusted.** Useful preferences, project context, corrections, and task history—with provenance, privacy boundaries, and owner-controlled correction or deletion.
- **Skills that grow.** MAX can find, assess, test, and propose new skills, then improve reusable workflows without silently expanding its permissions.
- **Your machines, securely coordinated.** Windows 11 and Arch Linux/Hyprland companions, plus an iPhone browser interface; sensitive device operations require narrow, device-enforced authorization.
- **Control over important actions.** Confidential documents are protected; external sends, sensitive changes, and every payment are gated by explicit confirmation.

## Our stack, by design

The product is divided into small first-party components, each with a clear responsibility:

| MAX component | What it owns |
|---|---|
| **MAX Core** | Intent, planning, specialist coordination, and task state |
| **MAX Gateway** | Model-independent intelligence interface, selection, streaming, and budgets |
| **MAX Trust** | Identity, policy checks, approvals, data boundaries, and audit |
| **MAX Memory** | Structured memory, provenance, knowledge retrieval, and retention |
| **MAX Skills** | Skill discovery, inspection, sandbox testing, versioning, and rollback |
| **MAX Voice** | Wake-up, speech, interruption, and turn-taking |
| **MAX Presence** | Holographic desktop companion and responsive mobile UI |
| **MAX Nodes** | Authorized, isolated execution on enrolled devices |
| **MAX Evolution** | Evaluations, lessons, controlled improvements, and recovery |
| **MAX Connect** | Optional communications, personal services, and device integrations |

These are **logical modules**, not ten separately deployed services. We will start with a manageable modular core and split processes only when isolation, performance, or reliability demands it.

## The principle behind every feature

**A model can suggest an action. It cannot grant itself the right to perform it.** Authorization is checked independently, execution is scoped, consequential actions require the appropriate confirmation, and completion must be backed by observable evidence.

MAX never stores banking PINs, one-time passwords, or card security codes. It must not bypass account security, workplace controls, or game protections. Sensitive files and personal history are not ordinary agent training material.

## Supported platforms — planned

| Platform | Intended experience |
|---|---|
| **Windows 11** | Floating orb, background voice, gaming-aware focus, enrolled execution node |
| **Arch Linux + Hyprland** | Equivalent companion, subject to real Wayland/window-manager verification |
| **iPhone browser** | Private responsive interface, task status, and foreground voice where supported |

Platform capabilities are subject to real-device tests. In particular, uninterrupted always-on-top behavior on Wayland and background voice in mobile browsers are **not** assumed.

## Development state

Phase 0 is dedicated to architecture research, security boundaries, technology evaluation, and a documented implementation sequence. We will then build and test **one working vertical slice at a time** instead of generating a huge, unverified application.

Start here: [Architecture](ARCHITECTURE.md) · [Product specification](docs/PRODUCT_SPECIFICATION.md) · [Technical design](docs/TECHNICAL_DESIGN.md) · [Security design](docs/SECURITY_ARCHITECTURE.md) · [Roadmap](docs/ROADMAP.md) · [Implementation plan](docs/IMPLEMENTATION_PLAN.md).

MAX's operating identity lives in [MAX.md](MAX.md), [SOUL.md](SOUL.md), [CORE.md](CORE.md), and related root documents. [AGENTS.md](AGENTS.md) separately governs the engineering worker. These files specify requirements; **runtime code and tests must enforce the rules**.

---

*Build the foundation correctly. Prove each capability. Make MAX better without surrendering control.*
