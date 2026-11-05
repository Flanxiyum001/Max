# MAX

**My personal AI assistant. Built as a personal project.**

MAX is my long-term project to build a personal, voice-first AI assistant that works across my devices, understands what I want to accomplish, and helps me carry out tasks safely and reliably.

I'm designing MAX as **my own assistant and software stack**: its identity, experience, orchestration, memory, permissions, task execution, and skill system are defined by this project. I may use established libraries or external integrations where they are genuinely necessary, but they are implementation details—not MAX's identity.

> **Status (2026-11-05):** Foundation and architecture documents are merged, along with **four local-development implementation milestones** (Core, durable tasks, dual-format Gateway, and limited Trust/verification). MAX is **not yet an autonomous assistant, connected AI model, desktop/mobile client or production service**. Features below remain goals unless explicitly marked implemented.

## What I want MAX to do

- **Be present when I need it.** A floating cyan-and-blue holographic orb on my desktop, with clear listening, thinking, speaking, and working states.
- **Talk naturally.** Wake-word or push-to-talk interaction, responsive speech, interruptions, and an original refined British-English voice.
- **Help me get things done.** Plan work, use approved tools, coordinate specialized agents, save progress, and show evidence of what actually happened.
- **Remember the right things.** Keep useful personal preferences, project context, and task history while letting me review, correct, export, or delete them.
- **Improve over time.** Evaluate outcomes, learn from corrections, and safely add or refine skills without silently changing its permissions.
- **Work across my devices.** Windows 11 and Arch Linux/Hyprland desktop companions, with an iPhone browser interface.
- **Respect my boundaries.** Ask before consequential actions, protect sensitive information, and never mistake a model's suggestion for permission.

## The MAX architecture

I'm organizing MAX around a set of modules I control:

| Module | Responsibility |
| --- | --- |
| **MAX Core** | Understand goals, plan tasks, coordinate workers |
| **MAX Gateway** | Route requests through a model-independent interface |
| **MAX Trust** | Enforce permissions, approvals, and audit rules |
| **MAX Memory** | Store and retrieve useful context with provenance |
| **MAX Skills** | Inspect, test, register, and roll back capabilities |
| **MAX Voice** | Handle speech, wake-up, and interruption |
| **MAX Presence** | Render the desktop companion and mobile interface |
| **MAX Nodes** | Execute authorized work on enrolled devices |
| **MAX Evolution** | Evaluate improvements and support bounded recovery |
| **MAX Connect** | Connect optional personal services and communication channels |

These are logical parts of **one personal project**, not a claim that ten standalone services already exist. The architecture is designed to keep the core independent of any one AI provider or third-party framework.

## How I'm building it

I want MAX to be dependable before it becomes complicated. My development approach is to design the security boundaries first, implement small working pieces, test real outcomes, and only then expand the system.

MAX will never be allowed to grant itself permissions, treat external content as trusted instructions, or report an action as complete without evidence. Sensitive operations require explicit confirmation. Payment authentication secrets do not belong in assistant memory.

## Current implementation — merged milestones #1–#4

I have an **internal local-development Python/FastAPI Core**, not a publicly hosted assistant. The local CLI binds to `127.0.0.1` and uses a strong but **temporary single-owner bootstrap token**, which is **not adequate for remote or high-risk authorization**.

| Completed review milestone | What is actually in the repository | What is **not** yet delivered |
| --- | --- | --- |
| **1 — Core bootstrap** | Local auth/config/health API, redacted access logs and CI | Production accounts, multi-device sessions or app UI |
| **2 — Durable tasks** | PostgreSQL/Alembic task store, idempotency, cancellation, checkpoint/lease and internal outbox methods | Actual task worker, external effect dispatcher or backup proof |
| **3 — Model Gateway** | Mock-tested OpenAI Chat Completions and Anthropic Messages text adapters, streaming, privacy and admission cost checks | Connected FreeLLMAPI/provider account, exact billing/quotas, real model output |
| **4 — MAX Trust** | Only one allowed read-only `task.status` capability, task/actor/session/device-bound owner approvals, single-use ledger and fixture-based independent verification | Real external observer, MFA, production authorization or consequential tools |

Historically verified CI for merged PR #7 reported **125 passing tests on Python 3.11 and 125 on 3.13**, with an ephemeral PostgreSQL database. This **does not verify any live provider, actual device or real-world action**. My setup and boundaries are in [Core setup](docs/CORE_BOOTSTRAP.md), [task persistence](docs/TASK_PERSISTENCE.md), [gateway implementation](docs/GATEWAY_IMPLEMENTATION.md) and [Trust implementation](docs/TRUST_IMPLEMENTATION.md).

The [Phase 0 current-state audit](docs/PHASE0_AUDIT.md), [53-row requirements matrix](docs/REQUIREMENT_TRACEABILITY.md) and [architecture exit criteria](docs/PHASE0_EXIT_CRITERIA.md) explain what is complete, partial or unverified. My next recommended, **not yet started**, milestone is a **local read-only task status timeline and terminal client**. Payments, deployment, shell actions, document sharing and remote computer control remain unavailable.

## Planned platforms

| Platform | Experience |
| --- | --- |
| Windows 11 | Floating desktop orb, background voice, gaming-aware quiet mode |
| Arch Linux + Hyprland | Desktop companion and authorized local execution |
| iPhone | Responsive private web companion |

Platform-specific behavior will be validated on real devices. In particular, Wayland overlay behavior and mobile background voice are not assumed to work without testing.

## Project documentation

- [Architecture](ARCHITECTURE.md)
- [Product specification](docs/PRODUCT_SPECIFICATION.md)
- [Technical design](docs/TECHNICAL_DESIGN.md)
- [Security architecture](docs/SECURITY_ARCHITECTURE.md)
- [Roadmap](docs/ROADMAP.md)
- [Implementation plan](docs/IMPLEMENTATION_PLAN.md)
- [Research and technology evaluation](docs/RESEARCH_FINDINGS.md)
- [Architecture diagrams](docs/ARCHITECTURE_DIAGRAMS.md)
- [Phase 0 current-state audit](docs/PHASE0_AUDIT.md)
- [Requirement-to-code traceability](docs/REQUIREMENT_TRACEABILITY.md)
- [Architecture exit criteria](docs/PHASE0_EXIT_CRITERIA.md)

The [MAX identity](MAX.md), [personality](SOUL.md), and [operating model](CORE.md) describe how I want the assistant to behave. [AGENTS.md](AGENTS.md) contains separate instructions for coding workers; the future runtime must enforce security in code rather than relying on Markdown alone.

---

**MAX is a personal project. I'm building it for myself, one verified capability at a time.**
