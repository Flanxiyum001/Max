# Optional living-room TV tools

MAX can send TV commands from the local desktop on the owner's home network. The existing PIN-paired Android TV remote remains available and is preferred for compatible Android/Google TVs.

## Setup

- Roku: enable local external control in TV settings and configure its private IPv4 address.
- Android/Google TV: the optional ADB transport requires an explicitly authorized network debugging connection and Android platform-tools installed locally. Prefer PIN pairing instead of leaving debugging enabled; disable debugging when not needed.
- Set `MAX_TV_KIND=androidtv` or `roku` and `MAX_TV_HOST=192.168.1.50` in the local environment. Only RFC 1918 IPv4 addresses are accepted.

## Tools

- `tv.play(app, title)`: sends a search/launch request, not proof that playback started.
- `tv.open_app(app)`: requests an allowlisted app launch.
- `tv.press(key, times)`: sends an allowlisted remote key.

Each operation requires owner approval by default. The optional `MAX_TV_CONTROL=auto` setting applies only to the three registered TV tools in a verified-local desktop session. These tools are unavailable in hosted web and cloud-model sessions. No physical-device compatibility or playback success is implied by mock tests. Samsung Tizen and LG webOS are not supported.
