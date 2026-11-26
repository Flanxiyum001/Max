# M40: Self-healing, evolution, background jobs and communications

Status: implemented and unit-tested; **not yet tested against real providers or on the owner's PC**.

## Self-healing supervisor — `src/max_core/supervisor.py`
A separate process that runs MAX, restarts it after a crash (backoff 2 s → 60 s), restarts it when an
optional health URL fails 3 checks in a row, and **stops** after 5 restarts in 10 minutes (crash loop),
writing JSON-lines to `%LOCALAPPDATA%\MAX\supervisor.log`. A clean exit (owner closed MAX) is not restarted.
Enable with `MAX_SUPERVISED=yes` before `start-max.cmd`.

## Evolution — `src/max_core/evolution.py`
Every finished tool step (tool name, status, fixed error code only) goes into `evolution.sqlite3` in the
data dir. `evolution.review` turns patterns (≥50% failures over ≥5 runs, the same error 3+ times, 3+ owner
denials) into candidate lessons. `evolution.decide` (owner approval required) activates one; active lessons
are added as guidance to each turn. A lesson retires itself once the tool runs cleanly 10 times. Lessons are
text only: they cannot change code, permissions, Trust, vault or payments.

## Background jobs — `src/max_core/jobs.py`
`jobs.create(name, schedule, tool, arguments)` — the owner approves once, then that exact call runs on its
own on a UTC cron schedule. Never schedulable: `shell.*`, `pc.*`, `apps.*`, `comms.call`, `jobs.*`,
`evolution.*`. Limits: 20 jobs, 48 runs/job/day, auto-pause after 3 failures in a row. `jobs.list`,
`jobs.enable`, `jobs.remove`. Turn off with `MAX_BACKGROUND_JOBS=no`.

## Communications — `src/max_core/comms/`
| Channel | Provider (India) | Out | In |
|---|---|---|---|
| Email | Any SMTP/IMAP (Gmail + app password) | `comms.send_email` | `comms.read_email` |
| WhatsApp | Meta WhatsApp Cloud API (official) | `comms.send_whatsapp` | webhook `/api/comms/whatsapp` → `comms.inbox` |
| Phone | Exotel (Twilio cannot do India-to-India, see TELEPHONY_ARCHITECTURE.md) | `comms.call` rings the owner, then connects the contact | webhook `/api/comms/exotel/incoming` → `comms.inbox` |

Rules: MAX only contacts people saved with `comms.add_contact`; every send/call needs owner approval;
identical sends within the same hour are deduplicated; inbound content is labelled untrusted. WhatsApp
webhooks are checked with the app secret (`X-Hub-Signature-256`); Exotel webhooks need `?key=<secret>`.
Webhooks run on the web app (`python -m max_core.web`, e.g. on Render) — set `MAX_PUBLIC_URL` to it.

Known limits: WhatsApp free-form text only works within 24 h of the contact's last message (templates not
yet supported); calls bridge two people (no MAX voice on the phone line yet); comms tools only load when a
channel is configured, to save model context.
