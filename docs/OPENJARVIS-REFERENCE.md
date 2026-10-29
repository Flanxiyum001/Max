# OpenJarvis as an engineering reference for MAX

Reference: https://github.com/open-jarvis/OpenJarvis (Apache-2.0).

Upstream documentation:
- https://github.com/open-jarvis/OpenJarvis/blob/main/docs/architecture/overview.md
- https://github.com/open-jarvis/OpenJarvis/blob/main/docs/architecture/design-principles.md
- https://github.com/open-jarvis/OpenJarvis/blob/main/docs/user-guide/system-access.md

MAX remains independently implemented; this change does not vendor or execute OpenJarvis code.

## Relevant design principles

| OpenJarvis principle | Existing MAX capability | Direction |
| --- | --- | --- |
| Local-first inference; cloud optional | Local Ollama and separately configurable OpenAI-compatible cloud providers | Choose models explicitly and verify a real response before claiming connectivity |
| Separate model engine, agent and tools | Core agent registry, local PC controller, policy/approval gate and Discord bridge | Direct user utterances must be routed through one strict desktop-action gate |
| Evidence and trace-driven evaluation | Evaluation harness and guarded self-improvement | Record which actions were planned, approved and actually verified rather than assuming success |
| Memory locality | Local settings profiles and caller-scoped user memory | Preserve separate privacy boundaries for PC, Discord and remote companions |
| Sandboxing and explicit capabilities | One-time local UI approvals and foreground-target verification | Do not grant generated model text the ability to approve computer control |

## Important scope limit

The OpenJarvis system-access documentation states it does NOT yet provide general computer-use tools for desktop screenshots, pointer or keyboard control. Its shell and file tools are powerful and depend on operator-selected permissions and optional sandboxing. OpenJarvis is not an off-the-shelf solution for the full Premiere and phone-call reel.

MAX has a narrower, native Windows foreground keyboard adapter. It can send specific reviewed shortcuts to supported visible windows only with a one-shot on-device approval. It does not visually inspect the active Premiere sequence, select clips by appearance, export a rendered file, or make actual telephone calls.

## This release milestone

Previously, spoken phrases like "Max, switch to Premiere" bypassed the action parser because only ordinary chat commands stripped the assistant name. Addressed user phrases now go through the same approval-gated parser as plain commands, including:

- "Max, switch to Adobe Premiere Pro"
- "Max, play the timeline in Premiere" (Space toggles playback)
- "Max, go to beginning of timeline in Premiere" (Home)
- "Max, go to end of timeline in Premiere" (End)
- "Max, add a marker in Premiere" (M)
- "Max, mark in point in Premiere" (I)
- "Max, mark out point in Premiere" (O)
- "Max, cut at playhead in Premiere" (Ctrl+K, modifies sequence)
- "Max, export video in Premiere" (Ctrl+M, opens export dialog only)

Each must be confirmed in the local Max PC interface. Custom Premiere keymaps and panel focus affect the outcome. Headless tests check routing and approval safety; only a real Windows test with a sample Premiere project can establish end-to-end behavior.

## Deferred

General vision-guided desktop operation, autonomous editing and rendering, actual telephony, natural-language multi-step tool orchestration, live GPU efficiency benchmarks, and reliable streaming wake-word recognition are NOT proven by this PR.

A safer next milestone is opt-in read-only observation of one Premiere window, with screenshot privacy controls and verification that a requested action visibly occurred before taking additional actions.
