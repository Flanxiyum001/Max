# M42: Web orb ready for everyday tasks

- **Context fix:** `MAX_TRIAL_CONTEXT_TOKENS` 8192 → 32768. MAX counts prompt size in bytes, so tools + persona
  + a few turns of history went over 8k and every request failed with `no_eligible_provider`.
- **Personal tools on the web:** `MAX_CLOUD_PERSONAL_TOOLS=yes` turns on memory, reminders and notes.
  These facts are sent to the cloud model (FreeLLMAPI) when relevant.
- **Memory survives restarts:** Render's free disk is wiped on every restart. `src/max_core/memory_repo.py`
  restores `state/web/*` from the private Max-memory repo at startup (first run imports `MEMORY.md`), uploads
  changed files every 60 s and on shutdown.
  Setup: create a GitHub fine-grained token → only repository *Max-memory* → *Contents: Read and write*,
  then add it on Render as `MAX_MEMORY_GITHUB_TOKEN`.
- **Email / WhatsApp from the web:** the web session now passes `MAX_EMAIL_*` / `MAX_WHATSAPP_*` settings through.

Limits: Render's free instance sleeps after ~15 minutes idle, so background jobs only run while it is awake
(the first request after sleep takes ~1 minute). WhatsApp voice calls need the Windows desktop app.
