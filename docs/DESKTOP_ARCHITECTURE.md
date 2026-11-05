# MAX desktop architecture

**Status:** Proposed, not platform verified.

The companion is a presentation surface for MAX's local/remote task runtime. Render a cyan/turquoise glass-like orb with a dark core, restrained motion and visible idle/listening/thinking/speaking/working/error states.

Requirements: move, resize, persist position, minimize, never steal focus, accessible keyboard controls, gaming/focus quiet mode, reduced-motion setting, clear microphone indicator and emergency stop.

Windows 11: prototype transparent click-through/interactive window behavior, DPI scaling, multi-monitor placement and fullscreen-game behavior.

Arch Linux/Hyprland: validate Wayland compositor layer rules, overlay behavior, input regions, portals and screen capture permissions on a real machine. Do not assume cross-platform window flags guarantee equivalent behavior.

Mobile: responsive authenticated web UI, task history and owner approvals; background microphone and push notifications are subject to browser/platform limits.

**Tests:** overlay, focus, gaming, multi-monitor, restart state, accessibility, reduced motion, battery/GPU load and permissions.

## Phase 0B desktop decision and physical proof

[Tauri 2 window flags](https://v2.tauri.app/reference/config/) document transparent and always-on-top configuration but **do not guarantee compositor behavior**. Proposed TypeScript/Tauri + shared responsive UI is contingent on Windows 11 and Arch/Hyprland proof-of-concepts. Alternative Electron may be chosen for a specific missing integration despite memory overhead; native Qt/GTK may be needed for platform-specific overlay handling.

**Device test grid:** compositor/protocol (Wayland vs XWayland), window floating/pinned, nonstealing focus, click-through regions, fullscreen games, HDR/scaling, multiple monitors, pause/mute during focus mode, keyboard accessibility, reduced motion, logging in/out, microphone permissions and GPU load. A test on one desktop platform does not clear the other. iPhone is foreground authenticated web only in MVP; no unconditional push/background wake promise. Reference diagrams: [ARCHITECTURE_DIAGRAMS.md](ARCHITECTURE_DIAGRAMS.md).
