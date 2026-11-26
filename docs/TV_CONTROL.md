# TV control

Say "MAX, play Stranger Things on Netflix" from any room; MAX drives the living-room TV over home Wi-Fi.

## Supported TVs
| `MAX_TV_KIND` | TVs | How |
|---|---|---|
| `androidtv` | Android TV, Google TV (Sony, TCL, Mi, Chromecast w/ Google TV), Fire TV | `adb` over Wi-Fi, port 5555 |
| `roku` | Roku TVs and sticks | Roku External Control Protocol, port 8060 |

Samsung Tizen and LG webOS are not supported yet.

## Setup
1. Give the TV a fixed IP in your router (e.g. `192.168.1.50`).
2. **Android/Google TV:** Settings → About → tap *Build* 7× → Developer options → enable *USB debugging* / *Network debugging*. Install Android platform-tools on the PC (`winget install Google.PlatformTools`). The first time, accept the "Allow debugging" prompt on the TV (tick *Always allow*).
   **Roku:** Settings → System → Advanced → *Control by mobile apps* → *Network access: Enabled*.
3. In `.env`: `MAX_TV_KIND=androidtv`, `MAX_TV_HOST=192.168.1.50`. Optional `MAX_TV_CONTROL=auto` so MAX doesn't ask you to approve each TV action.

## Tools
- `tv.play(app, title)` — Roku: searches inside the app and starts playback. Android: opens the app's search for the title; MAX then presses `ok` (via `tv.press`) to start it.
- `tv.open_app(app)` — netflix, youtube, prime video, disney+, hotstar, spotify.
- `tv.press(key, times)` — power, home, back, up/down/left/right, ok, play/pause, volume, mute.

## Safety
Only private-LAN IPs are accepted, app names come from a fixed allowlist, and titles are character-restricted. All `tv.*` tools are EFFECTFUL (approval required) unless `MAX_TV_CONTROL=auto`.
