# MAX — Reliability & production readiness audit
Date: 2026-10-30 (IST)

## Evidence reviewed

- GitHub default branch source and current CI for Linux and Windows.
- On baseline main commit 81798a5843536043e1d3c8d902ac3aac7a442f03, Linux CI: **497 passed** (1 warning); evaluation suite: **14/14**; Windows PC smoke: **PASS**. These exercise mocks/headless flows, not physical devices.
- Railway production service source: Flanxiyum001/Max main, **SUCCESS** deployment at that commit. Read-only deploy logs confirm **Discord READY**, response mode **wake**, one allowed channel, and prior **channel_not_allowlisted** suppressions. The service has a persistent volume mounted at /data. These statements do not assert that data was restored from or written to that volume, or that the provider answered a new prompt.
- Code paths reviewed: desktop app + voice/Whisper, speech setup, token-authenticated remote phone companion, provider connect/test, Discord router/config/store, and approval-gated Windows app automation.

## Verified problems and fixes proposed in this branch

### 1. [P1, correctness] Hands-free rapid restart can overlap microphones
Previous behavior: stop sets a shared Event; the active sounddevice capture may still be blocked. An immediate enable call regarded the old worker as inactive because the stop Event was set, cleared that same Event, and spawned a second capture worker. Two recorders could then compete for the device, and the old thread could resume using the newly cleared Event.
Fix: serialize microphone lifecycle changes, never clear the stop signal while a prior capture thread is alive, and require a fresh user enable after it exits. Push-to-talk and speech model installation also reject an active *or stopping* capture. Both desktop interfaces report actual state instead of always claiming the microphone started. Event-driven regression tests cover start/stop/restart, no double capture, TALK conflict, and installer conflict.

### 2. [P2, correctness] Connection probe verification can remain stale
Previous behavior: after a successful TEST AI response, a later 401/429/network error returned an error message but left model_verified true. Fix: every explicit test clears the verified flag before making its request; only a new nonempty response sets it true. Simulated positive/negative tests do not make paid calls.

### 3. [P1, configuration] Discord messages silently ignored outside one allowed channel
Live evidence: deployment is online; its wake-mode startup says one allowed channel and earlier routing logs show channel_not_allowlisted. That is authorization behavior, not proof of model failure.
Fix: an explicit new flag MAX_DISCORD_ALLOW_ALL_GUILD_CHANNELS defaults OFF. Only with response_mode=wake, explicit MAX_DISCORD_GUILD_IDS, and opt-in enabled can MAX be awakened in any channel of those selected servers. Existing ignored channels, persistent admin denies, DM restrictions, and per-channel sleep state remain in force. The user must choose the server IDs and configure this deliberately after reviewing access/credit cost; no Railway variable has been changed.

## Limitations not solved by this patch

- **Windows/voice:** CI has no access to the actual microphone, speaker, GPU drivers, or installed Premiere. It does not prove Whisper accuracy, voice-to-voice latency, app focus, or actual timeline edits. Faster Whisper works in 3-second chunks, not streaming continuous STT with VAD.
- **Premiere and desktop control:** only named Windows apps and exact shortcuts with local one-shot approval. No screenshot understanding, autonomous visual editing, completed export verification, or arbitrary GUI interaction.
- **Phone companion:** private token-authenticated browser control, not a cellular telephone call. Windows must remain running. No approvals through phone or generated text.
- **Providers:** configured model name and successful network connection alone are not proof of a real usable answer. TEST AI is an opt-in request which may cost credits. No live credential/provider call was made by this audit.
- **Discord runtime:** the bot has connected, but end-to-end replies in the owner's private server, provider health after real messages, model-cost usage, channel content intent status, and stored wake state after an actual restart still need operator verification.
- **Security:** do not expose local Windows action automation to untrusted users or treat an app shortcut as a sandboxed computer-use engine. External agent/tool integrations need separate capability review and permission boundaries.

## Prioritized next steps

1. Review and merge only when Linux CI, Windows PC workflow, evaluation suite, and new boundary regressions all succeed. No force push, secret changes, production deployment or app permissions changes are required for the patch.
2. If broad wake coverage is desired, set the three scoped opt-in variables documented in docs/DISCORD.md, and verify one selected guild, one previously blocked channel, one ignored/denied channel, wake/chat/sleep, and rate-limit behavior before expanding.
3. On the owner's Windows PC, test fresh speech model installation, MIC OFF/ON while a capture is in flight, push-to-talk, cloud/local provider explicit health check, and a disposable Premiere project. Validate **observed behavior**, not only on-screen success text.
4. Longer term: dedicated single TTS worker, VAD + audio device calibration, opt-in redacted local trace diagnostics, verified read-only desktop observation and workflow confirmation, granular per-tool policy, and resource/cost telemetry.

## Release boundaries

This audit and its associated code changes were developed on a separate GitHub branch. Do not treat them as live in Railway until PR merge, deployment and runtime confirmation. No production keys, Discord permissions, data volume or external accounts were modified.
