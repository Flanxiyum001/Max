# Local desktop awareness preview

This milestone adds four Windows tools to the existing local orb/assistant
session registry. Hosted and cloud sessions cannot discover them, even with
cloud personal tools enabled. Existing tool-loop approval enforcement remains
in charge. The local voice preview is an independent branch.

- `desktop.active_window` and `desktop.list_windows`: owner-approved,
  bounded title/handle metadata only; no screenshots or continuous monitoring.
- `desktop.focus_window`: owner approves the exact handle/title, then MAX
  checks the window still exists and has the expected title, requests focus
  through Windows, and verifies the foreground handle. OS refusal is a failure.
- `system.gpu`: local, read-only NVIDIA utilization, VRAM and temperature
  using a fixed nvidia-smi command and a five-second timeout.

Titles are untrusted content; they may contain confidential text or prompt
injection. Never treat them as instructions or send them to a remote service.
The owner must keep the configured local inference endpoint truly local.
This preview has no new screen capture, keyboard/mouse injection, browser
automation, API credentials, AI delegation, or calendar/email integrations.
Those require separate reviewed milestones. No new Python dependencies.

## Acceptance

Run the normal desktop orb with a verified local model. Ask MAX which windows
are open, deny once and verify nothing is returned; then approve. Ask to focus
one known window and verify the resulting foreground window. Ask for GPU usage.
Repeat with cloud or hosted configuration and verify the tools are absent.
Test a stale window selection and Windows focus refusal. Real Windows hardware
acceptance is pending. Roll back by reverting this milestone.
