# M20 — Original MAX conversational persona + deferred feminine voice

## Scope and character

MAX will have an **original** conversational identity influenced by the
qualities the owner appreciates in JARVIS and FRIDAY: calm competence, quick
reasoning, warmth, precise language, helpful initiative and subtle humour.
The assistant does not role-play these characters, use their dialogue or
suggest it has fictional capabilities. Ultron is not a model for hostility,
deception, control or disregard for the owner; only composure under pressure
is a useful general quality.

The owner now prefers an **original feminine voice**, despite the name MAX.
No voice service, actor impersonation, voice clone, licensed asset or specific
voice ID is selected in this milestone. The exact voice is awaiting the
owner's later choice. A British-English locale is a preference, not a
functioning accent guarantee.

## Assistant presence and greeting direction

The text persona now allows one brief, context-aware greeting at a new conversation when the host supplies reliable local time and daypart. It may use “Flanx” or “boss” occasionally, and a concise acknowledgement such as “Understood, boss” when it fits. It must not infer the time, repeat greetings during an active session, or force a title into every answer. These are presentation preferences only.

The text persona does not implement scheduled briefings, wake-word listening, automatic audio greetings, or persistent session lifecycle detection. Those require separate runtime and voice work.

## Personal-assistant operating behavior

The persona guides MAX to produce source-grounded briefings, use task tools for explicit commitments when available, report task state without overstating completion, rely only on owner-approved relevant memory, respect host-controlled voice activation and the selected licensed voice, and apply the owner’s stated priorities only when relevant. These rules guide presentation and tool use; they do not create calendar access, scheduling, task execution, memory controls, microphone access or permissions.

## Code delivered

- `max_core.persona.persona_message()` returns a fixed, reusable system
  instruction for a safe text style. Modes: conversational, work, briefing,
  focus. These are presentation settings, **never** security permissions.
- `with_persona(request)` adds a single trusted system instruction only
  for a request without existing system messages. It uses the full immutable
  GatewayRequest validation and leaves privacy, route, scope, budgets,
  required capabilities and cloud admission untouched. Over-budget inputs
  are rejected, not silently truncated.
- `VoicePreference()` records the chosen feminine presentation and
  British-English locale but defaults to **no provider and no voice ID**.
  It never opens a microphone or runs TTS.
- The existing single-turn local text trial accepts explicit
  `MAX_TRIAL_PERSONA=yes` opt-in for testing the instruction against a
  separately owner-reviewed model. The default is **no** for backward
  compatibility. There is no automatic live provider setup, real-world tool
  execution, session persistence or OS automation.
- Mock-only tests assert messaging, safety reminders, inert tool proposals,
  strict voice selection and existing Gateway admission requirements.

## Test the text persona on a trusted PC

Read [local text trial](LOCAL_TEXT_TRIAL.md) before testing. Set up the same
*owner-verified* provider and its privacy/cost controls as for any gateway
trial, then set `MAX_TRIAL_PERSONA=yes` in your local environment.
This only changes a model's input instructions for that one explicit test.
It does not confer permissions. The owner prompt must be public and
non-sensitive; cloud inference may incur costs.

## Voice selection remains pending

The final voice provider, license, voice ID, timbre and playback constraints
must be owner-selected and verified. Test candidate female voices for
understandability, interruptibility, latency, pronunciation and device load.
No runtime voice gender/accent or synthesis is asserted by metadata.

## Known limits / release blockers

Prompt instructions are **not a guarantee of model behaviour** and cannot
replace actual capability checks, MAX Trust or external-effect evidence.
Persona modes are not yet connected to an always-on live voice assistant or
authenticated interactive chat; the trial is text-only. The separate voice
pipeline is still adapter-injected and not attached to a licensed voice.

Review after CI: Python 3.11/3.13 lint, format, typing, PostgreSQL,
full tests, pip-audit; inspect how GatewayRequest capacity is validated.
Physical device and live provider tests remain pending.
No database migrations, secrets, providers or deployments are changed.
Rollback by reverting this PR.
