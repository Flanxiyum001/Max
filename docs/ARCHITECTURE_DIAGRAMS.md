# MAX architecture diagrams — trust boundaries and flows

**Design only.** Arrows describe candidate interfaces, not deployed or proven connections. All sensitive actions pass trusted MAX Trust; no LLM or acquired skill can grant authority.

## 1 — Overall topology

```mermaid
flowchart LR
    Win[Windows MAX Presence] --> API[Authenticated MAX API]
    Linux[Arch Hyprland Presence] --> API
    IPhone[iPhone web foreground client] --> API
    API --> Trust[MAX Trust]
    Trust --> Core[MAX Core tasks and delegation]
    Core --> Gateway[MAX Gateway]
    Gateway --> Provider[Cloud and local model adapters]
    Core --> Skills[Capability and skill registry]
    Core --> DB[(PostgreSQL state)]
    Core --> Memory[MAX Memory policy]
    Memory --> DB
    Skills --> Executor[Sandboxed tool runner]
    Executor --> Nodes[Enrolled execution nodes]
    Executor --> Browser[Isolated browser]
    Core --> Voice[MAX Voice sessions]
    Core --> Comms[Optional official communication adapters]
    Core --> Audit[(Audit and evidence)]
    Supervisor[Independent supervisor] --> API
```

## 2 — Agent orchestration and verification

```mermaid
flowchart TD
    Request[Authenticated goal] --> Persist[Persist task and limits]
    Persist --> Plan[Planner proposal]
    Plan --> Delegate[Scoped specialist agent]
    Delegate --> Propose[Typed tool proposal]
    Propose --> Authorize{MAX Trust authorizes?}
    Authorize -->|no| Deny[Audit denial]
    Authorize -->|yes| Execute[Isolated executor with lease]
    Execute --> Observe[Observed tool result]
    Observe --> Verify{Independent verification}
    Verify -->|pass| Complete[Commit verified outcome and lesson]
    Verify -->|fail| Retry{Budget and side-effect safety?}
    Retry -->|yes| Delegate
    Retry -->|no| Escalate[Checkpoint and escalate]
```

## 3 — Model Gateway

```mermaid
flowchart TD
    Core[MAX Core] --> Classify[Privacy and capability classification]
    Classify --> Select{Policy and budget routing}
    Select -->|local-only| Local[Local adapter]
    Select -->|cloud allowed| Format{Transport adapter}
    Format --> OpenAI[OpenAI-compatible adapter]
    Format --> Anthropic[Anthropic Messages adapter]
    OpenAI --> OptionalProxy[Optional vetted FreeLLMAPI proxy]
    Anthropic --> OptionalProxy
    OpenAI --> Direct[Direct eligible provider]
    Anthropic --> Direct
    Local --> Normalize[Normalized streaming response]
    Direct --> Normalize
    OptionalProxy --> Normalize
    Normalize --> Validate[Tool proposal validation, not authorization]
    Validate --> Core
```

## 4 — Skill acquisition lifecycle

```mermaid
flowchart TD
    Gap[Verified capability gap] --> Discover[Discover approved sources]
    Discover --> Provenance[Pin source hash version license]
    Provenance --> Inspect[Static and permission inspection]
    Inspect --> Safe{Policy approves installation?}
    Safe -->|no| Reject[Reject and log]
    Safe -->|yes| Sandbox[Install into isolated runner]
    Sandbox --> Test[Functional and negative tests]
    Test --> Pass{Passed?}
    Pass -->|no| Quarantine[Quarantine or rollback]
    Pass -->|yes| Grant[Owner-scoped capability registration]
    Grant --> Monitor[Monitor use and audit]
    Monitor --> Revoke[Version pin, revoke or rollback]
```

## 5 — Memory and Evolution

```mermaid
flowchart LR
    Outcome[Verified task outcome and feedback] --> Proposal[Propose lesson]
    Proposal --> Classify{Memory vs code or policy}
    Classify -->|low-risk approved memory| Validate[Provenance and duplicate checks]
    Validate --> Store[(Versioned MAX Memory)]
    Store --> Recall[Scoped retrieval]
    Recall --> Core[MAX Core]
    Classify -->|executable or security change| Review[Isolated branch and regression tests]
    Review --> Owner{Owner review}
    Owner -->|approve| Change[Controlled promotion]
    Owner -->|deny| Reject[Discard or revise]
```

## 6 — Windows/Linux nodes

```mermaid
flowchart TD
    Trust[MAX Trust job authorization] --> Queue[(Scoped job queue)]
    Queue --> Claim[Outbound authenticated node claims job]
    Claim --> Device{Enrolled Windows or Arch node?}
    Device -->|no| Reject[Reject and revoke]
    Device -->|yes| Verify[Validate lease, scope and policy digest]
    Verify --> Isolate[Prepare isolated workspace]
    Isolate --> Execute[Permit bounded task]
    Execute --> Results[Return redacted logs and evidence]
    Results --> Core[MAX Core reconciles state]
    Core --> Audit[(Audit)]
    Results --> Cleanup[Cleanup and optional safe shutdown approval]
```

## 7 — Voice interaction

```mermaid
flowchart TD
    Mic[Opt-in local microphone] --> Wake[Wake-word or push-to-talk]
    Wake --> VAD[Voice activity detection]
    VAD --> STT[Speech recognition]
    STT --> Trust[Session auth and intent risk check]
    Trust --> Core[MAX Core]
    Core --> TTS[Licensed original TTS]
    TTS --> Output[Desktop speaker or authorized call media]
    Interrupt[User barge-in, mute, or cancel] --> Stop[Cancel generation and audio playback]
    Stop --> Core
    Stop --> TTS
```

## 8 — Security and approval boundary

```mermaid
flowchart LR
    Untrusted[Web, email, skills, models, voice] --> Parse[Untrusted data parsing]
    Parse --> Plan[Typed proposal]
    OwnerUI[Authenticated owner UI] --> Approval[Bound single-use approval]
    Plan --> Decision{MAX Trust policy}
    Approval --> Decision
    Decision -->|deny| Log[(Denial audit)]
    Decision -->|allow| Execution[Restricted execution]
    Execution --> Evidence[Independent evidence check]
    Evidence --> Log
    Execution --> Store[Scoped DB, device, or channel access]
    Policy[Versioned security policy controlled by owner] --> Decision
```

## Diagram acceptance criteria

Review against [THREAT_MODEL.md](THREAT_MODEL.md), [DATA_AND_STORAGE.md](DATA_AND_STORAGE.md), [MODEL_GATEWAY.md](MODEL_GATEWAY.md), [DEVICE_EXECUTION.md](DEVICE_EXECUTION.md) and [TELEPHONY_ARCHITECTURE.md](TELEPHONY_ARCHITECTURE.md). Render every Mermaid graph and test negative cases at each approval, provider, and execution boundary during implementation. Rendering and runtime integration are **not verified by these diagrams alone**.
