# MAX desktop architecture

**Status:** Proposed, not platform verified.

The companion is a presentation surface for MAX's local/remote task runtime. Render a cyan/turquoise glass-like orb with a dark core, restrained motion and visible idle/listening/thinking/speaking/working/error states.

Requirements: move, resize, persist position, minimize, never steal focus, accessible keyboard controls, gaming/focus quiet mode, reduced-motion setting, clear microphone indicator and emergency stop.

Windows 11: prototype transparent click-through/interactive window behavior, DPI scaling, multi-monitor placement and fullscreen-game behavior.

Arch Linux/Hyprland: validate Wayland compositor layer rules, overlay behavior, input regions, portals and screen capture permissions on a real machine. Do not assume cross-platform window flags guarantee equivalent behavior.

Mobile: responsive authenticated web UI, task history and owner approvals; background microphone and push notifications are subject to browser/platform limits.

**Tests:** overlay, focus, gaming, multi-monitor, restart state, accessibility, reduced motion, battery/GPU load and permissions.
