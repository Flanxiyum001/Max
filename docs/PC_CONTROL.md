# Windows PC control

MAX's Windows orb can operate the PC by voice or text: "open Chrome",
"open YouTube and search lo-fi", "open my Gmail", "turn the volume up",
"switch to Spotify", "type this into Notepad".

## Turn it on

In `.env` next to `start-max.cmd`:

    MAX_PC_CONTROL=full

Then start MAX with `start-max.cmd`. With this setting, MAX runs these tools
without asking first:

| Tool | What it does |
| --- | --- |
| `apps.open` | open a website or launch an installed app |
| `desktop.list_windows`, `desktop.active_window`, `desktop.focus_window` | see window titles and switch windows |
| `pc.type_text` | type text into the focused window |
| `pc.press_keys` | shortcuts such as `ctrl+l`, `alt+tab`, `win+d`, `enter`, media and volume keys |
| `pc.click`, `pc.move_mouse`, `pc.scroll` | mouse at screen pixel positions |
| `pc.screen_size` | screen size and pointer position |
| `pc.screenshot`, `files.save_document` | see the screen; save documents to the MAX folder (see [SCREEN_VISION.md](SCREEN_VISION.md)) |

Without the setting, the tools are still offered but each action asks first,
as before.

## Limits and safety

- **Emergency stop:** move the mouse into the top-left corner of the main
  screen. While it is there, every PC-control action fails.
- Only for a **local** model on Windows. Cloud models and the hosted web
  server never get these tools, and the setting is ignored for them.
- `shell.run` (`MAX_ENABLE_SHELL=yes`) still asks before every command.
- **MAX cannot see the screen yet.** No screenshots are taken. She works through
  apps, URLs, window titles and the keyboard, which covers most voice tasks.
  Clicking a specific button only works if the pixel position is known. Screen
  vision would need a vision-capable model and is a separate milestone.
- With no approval, a misheard command or instructions hidden in a web page or
  window title that the model follows will run immediately. Window titles and
  web text are untrusted. Keep `MAX_PC_CONTROL` off when you are not using it.
- Windows blocks synthetic input into apps running as administrator when MAX
  is not elevated; those actions fail with `input_blocked_by_windows`.

Roll back by removing `MAX_PC_CONTROL` from `.env` or reverting this change.
