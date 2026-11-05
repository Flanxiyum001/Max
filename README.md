# MAX

### My personal AI assistant. Built around the way I think, work, and live.

I'm building **MAX**, my own voice-first AI assistant. This is a personal project—not a company product, not a team platform, and not a reskinned chatbot. My goal is to create an assistant that feels genuinely useful in everyday life: someone I can talk to naturally, trust with approved tasks, and improve over time.

I want MAX to have **his own identity**, remember what matters, work across my devices, and learn from experience. Most importantly, I want to own the architecture that makes him work—not depend on another assistant framework to decide what he can do.

> **Current stage:** Architecture and development foundation. The capabilities below are what I'm working toward. I'm not claiming that the complete application is already implemented or deployed.

## What I'm building

- **A personal presence.** A floating holographic orb with a dark glass centre, cyan-blue light, expressive motion, and a calm, minimal interface.
- **Natural conversation.** Voice activation, fluid back-and-forth responses, interruption, and a distinctive, original British-English voice.
- **An assistant that actually gets things done.** MAX should be able to plan, research, code, coordinate tasks, use my approved tools, and verify the outcome instead of merely telling me something was completed.
- **Memory with meaning.** Long-term preferences, project context, corrections, and lessons—with transparency and control over what is kept.
- **A growing skillset.** MAX should discover capabilities, test new skills in isolation, and improve workflows over time without gaining permissions on his own.
- **One assistant across my devices.** A desktop companion for Windows 11 and Arch Linux with Hyprland, plus a private iPhone browser interface.
- **Control and privacy first.** Sensitive files, communication, local device access, and financial actions stay behind clear authorization.

## How I envision MAX

I am designing MAX as a collection of **first-party modules** that I can build, replace, and improve independently:

| Module | Responsibility |
|---|---|
| **MAX Core** | Thinking, planning, task orchestration, and coordination |
| **MAX Gateway** | Model selection, normalized responses, streaming, and resource budgets |
| **MAX Trust** | Identity, permissions, approvals, and audit trails |
| **MAX Memory** | Personal context, project knowledge, retention, and retrieval |
| **MAX Skills** | Finding, developing, testing, and managing capabilities |
| **MAX Voice** | Listening, understanding, speaking, and interruption |
| **MAX Presence** | Desktop holographic companion and mobile interface |
| **MAX Nodes** | Approved work on my own computers |
| **MAX Evolution** | Learning from results, evaluation, and controlled self-improvement |
| **MAX Connect** | Optional connections to communication and other personal tools |

These are architectural parts of **my own stack**, not separate products. I plan to keep MAX modular and manageable from the start, then expand each capability only after it is tested.

## Platforms I want to support

| Platform | Experience I want |
|---|---|
| Windows 11 | Floating orb, background voice, gaming/focus mode, and authorized local tasks |
| Arch Linux + Hyprland | Lightweight companion and local execution, with compositor-specific testing |
| iPhone | Secure browser-based chat, tasks, notifications, and foreground voice where supported |

Some operating-system features—particularly always-on-top windows under Wayland and background audio in mobile browsers—require real-device validation.

## The rules I want MAX to follow

A model can **propose** what to do, but it should never grant itself permission to do it. MAX should ask before consequential actions, use only the access I've authorized, and prove when a task actually succeeds.

My financial credentials, passwords, OTPs, UPI PINs, and card security codes should never become ordinary assistant memory. Sensitive documents and actions must use dedicated protections. MAX's ability to improve should never mean silently changing his own security rules.

## My development approach

I'm treating this as a long-term engineering project. First I establish the architecture, security model, task system, and roadmap. Then I build small working versions, measure them, fix the weak spots, and expand carefully. I want **one dependable assistant**, not dozens of impressive-looking features that fail when I actually need them.

The design documents are here: [Architecture](ARCHITECTURE.md) · [Product specification](docs/PRODUCT_SPECIFICATION.md) · [Technical design](docs/TECHNICAL_DESIGN.md) · [Security](docs/SECURITY_ARCHITECTURE.md) · [Roadmap](docs/ROADMAP.md) · [Implementation plan](docs/IMPLEMENTATION_PLAN.md).

[MAX.md](MAX.md) and the other root identity documents define who I want MAX to be. [AGENTS.md](AGENTS.md) is separate and contains development instructions. The actual application will have to implement and enforce these rules in code.

---

**MAX is my personal AI project. My goal is simple: make him more useful, more capable, and more dependable—without losing control of what he does.**
