# MAX

**My personal AI assistant. Built as a personal project.**

MAX is my long-term project to build a personal, voice-first AI assistant that works across my devices, understands what I want to accomplish, and helps me carry out tasks safely and reliably.

I'm designing MAX as **my own assistant and software stack**: its identity, experience, orchestration, memory, permissions, task execution, and skill system are defined by this project. I may use established libraries or external integrations where they are genuinely necessary, but they are implementation details—not MAX's identity.

> **Status:** Foundation and architecture in progress. The features below are goals, not claims of a finished application.

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

The [MAX identity](MAX.md), [personality](SOUL.md), and [operating model](CORE.md) describe how I want the assistant to behave. [AGENTS.md](AGENTS.md) contains separate instructions for coding workers; the future runtime must enforce security in code rather than relying on Markdown alone.

---

**MAX is a personal project. I'm building it for myself, one verified capability at a time.**
