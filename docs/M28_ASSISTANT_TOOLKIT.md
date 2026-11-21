# M28 — Personal assistant toolkit (chat + orb)

**Status:** Implemented; unit, mock-HTTP and offscreen-Qt tested.
**Not yet verified against a real model, real websites or a real Windows desktop.**

MAX can now remember things about me, keep reminders and to-dos, search and read
the web, check the weather and this PC's status, open websites and apps, and
(opt-in) run commands. The same tools work in the terminal chat
(`python -m max_core.chat`) and in the orb window (`python -m max_core.native_desktop`).
The orb now talks to **either a local model or a free cloud API**.

## Tools

| Tool | Does | Asks me first? | Cloud model gets it? |
|---|---|---|---|
| `memory.remember` / `memory.forget` | save / delete a fact about me | yes | only with opt-in |
| `memory.recall` | search remembered facts | no | only with opt-in |
| `reminders.add` / `reminders.complete` | add / finish reminder or to-do | yes | only with opt-in |
| `reminders.list` | list open reminders | no | only with opt-in |
| `system.status` | OS, CPU, memory, disk, battery | no | only with opt-in |
| `shell.run` | run a command (60 s limit) | yes, every exact command | only with opt-in; **off unless `MAX_ENABLE_SHELL=yes`** |
| `apps.open` | open a URL or launch an app by name | yes | yes |
| `web.search` / `web.read` | DuckDuckGo search / read a public page | no | yes |
| `weather.current` | weather + 3-day forecast (Open-Meteo) | no | yes |
| `clock.now`, `math.calculate` | time, arithmetic | no | yes |
| `workspace.*`, `notes.write` | one folder (from M23) | read: no / write: yes | never |

- Remembered facts (up to 15 recent) are given to a **local** model each turn as
  data, so MAX knows me. Cloud models never receive them unless I opt in.
- Due reminders pop up in the orb (with a beep) and in the chat before the prompt.
- In the orb, an approval looks like: *"MAX wants to run reminders.add … Enter =
  allow · Esc = deny"*. No answer within 90 s = denied.
- Data lives in `%LOCALAPPDATA%\MAX\` (Windows) or `~/.local/share/max/`
  (`memory.json`, `reminders.json`); override with `MAX_DATA_DIR`.

## Setup: put settings in a `.env` file in the folder you run MAX from

Free cloud API (any OpenAI-compatible `/chat/completions` endpoint):

```
MAX_TRIAL_DESTINATION=cloud
MAX_TRIAL_CLOUD_ALLOWED=yes
MAX_TRIAL_ENDPOINT=https://<provider>/v1/chat/completions
MAX_TRIAL_MODEL=<model id>
MAX_TRIAL_API_KEY=<your key>
MAX_TRIAL_CONTEXT_TOKENS=8192
MAX_TRIAL_MAX_COST_USD=0
MAX_TRIAL_INPUT_USD_PER_MILLION=0
MAX_TRIAL_OUTPUT_USD_PER_MILLION=0
# Optional: let the cloud model use memory/reminders/PC status (sends that data to the provider)
# MAX_CLOUD_PERSONAL_TOOLS=yes
```

Local (Ollama):

```
MAX_TRIAL_DESTINATION=verified_local
MAX_TRIAL_LOCAL_EGRESS_REVIEWED=yes
MAX_TRIAL_ENDPOINT_LOCAL=yes
MAX_TRIAL_ENDPOINT=http://127.0.0.1:11434/v1/chat/completions
MAX_TRIAL_MODEL=qwen2.5:7b
MAX_TRIAL_CONTEXT_TOKENS=8192
```

Other switches: `MAX_ENABLE_SHELL=yes`, `MAX_ENABLE_WEB=no`, `MAX_WORKSPACE_DIR=...`,
`MAX_DATA_DIR=...`, `MAX_TRIAL_PERSONA=yes`. Real environment variables override `.env`.
`pip install psutil` adds CPU %, memory and battery to `system.status`.

## Security notes

- Web tools refuse non-http(s) URLs and any host resolving to a private, loopback or
  link-local address (re-checked on every redirect), cap responses at 2 MB and return
  text only. Search queries go to DuckDuckGo; weather to Open-Meteo.
- `apps.open` accepts only http(s) URLs or a plain app name (no shell characters),
  launched without a shell.
- `shell.run` is a real shell: it is off by default and each exact command is shown
  for approval. Approving a command gives it my user's full rights.
- Web pages and tool output are labelled untrusted data; a malicious page can make
  the model *propose* actions, but effectful ones still need my Enter / `y`.
- The DuckDuckGo HTML results page is not an official API and may change.

## Tests

`tests/test_assistant_tools.py` (store, due parsing, web/weather with mocked HTTP,
private-address refusal, registry policy local vs cloud, session memory injection,
no memory leak to cloud) and `tests/test_native_orb.py` (approve/deny via Enter/Esc,
reminder announcement).

Rollback: revert this PR. No migration; the JSON data files can be deleted.
