# M26 — MAX Engineering Discipline (first-party skill)

**Purpose:** An original, MAX-specific adaptation of the general coding-behaviour
principles described in [multica-ai/andrej-karpathy-skills](https://github.com/multica-ai/andrej-karpathy-skills).
The upstream repository packages advice for Claude/Cursor, **not** a runnable
agent, a source of authority, or a tool framework. Its README and SKILL.md
identify an MIT licence; MAX uses fresh wording and no copied code.

## What is implemented

The reviewed, static `max_core.engineering_skill.ENGINEERING_DISCIPLINE`
supplies engineering-only guidance when the trusted host explicitly requests
`persona_message(ReplyMode.ENGINEERING)` or
`with_persona(request, mode=ReplyMode.ENGINEERING)`.

This mode specialises MAX for one-owner development:

1. Examine project instructions, live code and tests; surface materially risky
   unknowns rather than inventing certainty.
2. Choose a small, maintainable, relevant patch with no unrelated clean-up.
3. Define an observable definition of done, add regression tests where useful,
   and report checks on the **specific commit** rather than assumed success.
4. Respect MAX's owner-review PR workflow and all existing Gateway, Trust,
   memory, and provider-egress boundaries.

Normal conversation, work briefings, and focus mode are **not changed**.

For the opt-in terminal chat (`python -m max_core.chat`), configure
`MAX_TRIAL_PERSONA=yes` and type `/mode engineering`.
Use `/mode conversation` to return to ordinary chat. The terminal prints
the active mode; `/reset` still clears the conversational history. Mode
commands are handled locally before messages reach a model. No new model,
gateway, network destination, or credential is configured by this patch.

Other trusted host integrations can select the mode explicitly through the
existing `with_persona` contract. This is **not automatically wired** into
all agents, the native Qt UI, or untrusted skills.

## Safety, validation and limitations

The skill is *prompt guidance only*. It does not confer repository, shell,
filesystem, PR, approval, or deployment permissions. It cannot attest that a
model obeyed an instruction; the host must independently observe tool effects
and test results. There is no downloaded runtime skill, third-party dependency,
dynamic import, instruction source update or hidden personal profile in code.

Tests cover deterministic mode scoping, user command parsing/session messages,
and preservation of the original Gateway security flags by `with_persona`.
Real-model behaviour and effectiveness benchmarks are not established here.
No production deployment or owner-approved merge is performed.

**Rollback:** revert this focused PR. No data migration, schema change or
persistent configuration is created. The original terminal chat mode remains
available throughout.

**Source attribution:** principles inspired by the project maintained at
https://github.com/multica-ai/andrej-karpathy-skills (MIT noted in its
README/SKILL.md); this implementation is an original MAX-specific rewrite.
