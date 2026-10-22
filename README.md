### ZMK keyboard layout

Keyboard layout inspired by the miryoku layout, added support for french and a gaming layer.


### Building locally

With Docker Desktop running: `.\build-local.ps1` (or `.\build-local.ps1 left` for one half).
Firmware lands in `..\firmware\`. GitHub Actions still builds on every push.

### Battery monitor (Windows)

`tools/battery_tray.pyw` shows both halves' battery levels in the system tray
(`pip install bleak pystray pillow`, then run it with `pythonw`). The left half forwards
the right half's level thanks to `config/corne_left.conf`.

### Layer display (Windows)

`tools/layer_overlay.pyw` shows the active layer in a small always-on-top window
(Ctrl+Alt+K to show or hide). Layer keys send unused F13–F21 keys as signals; the tool
swallows them. It reads `config/corne.keymap`, so restart it after keymap edits.

### Credits
This keyboard layout is based on or uses the [Miryoku](https://github.com/manna-harbour/miryoku) 
layout designed by [Manna Harbour](https://github.com/manna-harbour), licensed under GPL-2.0.
