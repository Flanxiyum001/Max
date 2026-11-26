# One paired Android TV in MAX Desktop (Windows)

This opt-in feature uses Android TV Remote Service (v2), not ADB. Pair only a TV
you are authorized to control, on a trusted home network. The TV and PC must
be able to reach one another. This is not a remote-access server.

1. Check that you can pair the TV using Google's Google TV remote app, or check
   that Android TV Remote Service is present and up to date on the TV. The
   Kodak PATH_UHDXANDROID firmware has not been tested with MAX.
2. Start MAX once to install the desktop environment. Then, from this checkout
   in PowerShell, install the optional TV adapter and pair with the private IPv4
   address shown in the TV's network settings:

       .\.venv\Scripts\python.exe -m pip install -e ".[tv]"
       .\start-max.cmd --pair-tv <TV IPv4 address>

   Replace the angle-bracketed text with the address. Enter the six characters
   displayed on the TV into that terminal, not into MAX chat. Use a DHCP
   reservation if possible; if the address changes, pair again. USB debugging is
   not needed and can be disabled. Android's official wireless debugging
   support on TVs requires Android 13 or newer; USB debugging alone does not
   pair the PC over Wi-Fi. There is no ADB fallback and no network scanning.
3. Open the MAX orb normally. Type or use its existing Space-to-talk control:

   - "Max, turn on Dad's TV"
   - "Max, open Netflix on Dad's TV"
   - "Max, play The Good Place on Netflix on Dad's TV"
   - "Max, turn off Dad's TV"

   To speak the title, put its exact lower-case name and official Netflix title
   ID in `titles.json` inside the `tv` folder under MAX's data directory. The
   usual Windows path is `%LOCALAPPDATA%\MAX\tv\titles.json` (or `MAX_DATA_DIR\tv`).
   Format: `{"the good place": "<digits from the real Netflix title URL>"}`.
   Alternatively, type `open https://www.netflix.com/title/<ID> on the TV`.
   There is no external title search or guessed ID. Netflix may open the title
   page, ask for a profile, or start playback. MAX reports the request, not
   playback. Never share Netflix credentials or PINs with MAX.

## While the game is in focus

Start MAX with `.\start-max.cmd --tv-hotkey` before your game. Once the app is
running, press Ctrl+Alt+F10 to start recording and press it again to send. This
accepts only the TV commands above; it never falls back to the model. It does
not bring MAX to the foreground or send keys to the game. Set up the microphone
and any existing remote speech consent in MAX first. The shortcut does not
enable always-on listening; if Windows or a game reserves it, use Space in the
orb. Background commands show status rather than speaking over the game.

The TV must keep its remote service reachable to wake. The adapter uses distinct
wake/standby commands, never a blind power toggle. If wake cannot be confirmed,
it does not send Netflix. If the TV is offline in standby, use the physical
remote; this feature does not broadcast wake packets or change TV settings.

## Trust and removal

Only a direct owner orb input or opt-in Windows hotkey transcript can trigger
these fixed operations; model output, web requests and MCP tools cannot call
them. PIN pairing stores a local client key in the MAX user data directory
(keep it private). The upstream protocol library authenticates the client with
the paired certificate but does not validate the server certificate. Keep
this on a trusted, local network and never expose its ports to the internet.
Do not put keys or pairing codes into the repo. To revoke access, remove MAX
from the TV's paired remotes when supported, remove the local TV setup folder,
and start without the hotkey.

References: [Android TV Remote library](https://github.com/tronikos/androidtvremote2)
(Apache-2.0); [Netflix links](https://www.home-assistant.io/integrations/androidtv_remote/);
[Google wireless debugging requirements](https://developer.android.com/tools/adb).
No subscription, bridge, or external title lookup is added. Netflix availability
depends on the TV app, subscription, profile, and region.

