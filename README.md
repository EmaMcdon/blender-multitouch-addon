# Multitouch Viewport Navigation for Blender (Windows)

A Blender add-on for Windows touchscreens and pen tablets. It brings touch behaviour modelled on Blender's Android demo build to desktop Blender.

Tested with Blender 5.3 on Windows 11 with a Huion pen display.

## Features

- **Touch navigation**: tap to click, one-finger orbit/pan/scroll, two-finger pan and pinch zoom, two-finger twist to roll the view (can be turned off: *Twist to Roll*).
- **Multi-finger taps**: 2, 3, 4 and 5 fingers, each set to Undo, Redo, Focus Mode, Maximize Area or Previous View.
- **5-finger hold**: opens a touch pie (Mode, Undo, Redo, Previous View, Focus Mode, Bigger UI, Palm Rejection, Frame Selected).
- **Cancel a view move**: while two fingers move the view, put a third finger down near them (*Cancel Finger*, default 4 cm) and the view snaps back. Touches farther away, like your arm, are ignored.
- **One Action at a Time** (optional): a two-finger gesture only pans, zooms or rolls, whichever you start with.
- **Object Slide**: sliding two fingers on a held object rolls it like a trackball, or turns it like a turntable (*Rotate*).
- **Palm rejection**: touches on the pen hand's side are ignored while the pen is near and for a set time after it leaves. Right-handed, left-handed and distance modes are available.
- **Auto-perspective**: one-finger orbit leaves the automatic orthographic view (orthographic chosen with numpad 5 is kept).
- **Number fields by touch**: drag sideways on a value to change it like a mouse, with the touch point as zero. Add a second finger for Ctrl (snap).
- **Hold and slide**: hold a field until the ring fills, then slide over other fields to edit several at once.
- **Bigger UI for fingers**: the interface grows when a finger touches a panel. Touching the viewport, or using the mouse or pen, brings it back to normal size.
- **Region overlap aware**: the see-through part of the 3D View sidebar and toolbar counts as viewport, as it does in Blender.
- **Menus and popups**: taps work in menus, submenus, pie menus and the F3 search, also in sculpt and paint modes. A one-finger drag scrolls long menus.

## Install

**As an extension (Blender 4.2 and newer):** download `multitouch_viewport-<version>.zip` and drag it into Blender, or use *Edit → Preferences → Get Extensions → ⌄ → Install from Disk…*.

**As a classic add-on:** download `multitouch_viewport.py`, then *Edit → Preferences → Add-ons → Install from Disk…* and pick the file.

Either way, enable **Multitouch Viewport Navigation**. It starts automatically; its settings are in the 3D View sidebar, *Touch* tab.

## Building the extension zip

```
python build_extension.py "path/to/blender.exe"
```

This packs `multitouch_viewport.py` with `blender_manifest.toml` and the license, keeps the manifest version in step with the add-on, validates it and writes the zip to `dist/`.

## Requirements

- Windows (uses the Win32 touch, pointer and raw-input APIs).
- Blender 4.0 or newer (developed on 5.3).

## Known issues

- **Multi-value slide in Transform → Location only works bottom-up.** Holding X and sliding down to Z doesn't select all three fields, but sliding from Z up to X does. This is a Blender limitation: the official Android build behaves the same way. Rotation and Scale work in both directions.
- **A resting arm can trigger the bigger UI.** If your arm touches any panel or header after the pen leaves the screen, the UI scales up as if a finger touched it. Raise *After Pen Leaves* in the Touch tab to ignore touches on the pen side for longer after the pen leaves.
- **Windows only.** It uses Win32 touch and raw-input APIs, so it does nothing on macOS or Linux.

## Tried and dropped

Fixes that were tried, failed in real use and were taken out, so nobody has to try them again:

- **Making the Location slide work top-down (X → Z).** Several helpers were tried: clearing the axis lock, probing the fields above and below the held one (this lit the fields up one after another), clamping the slide to the row centres, and ignoring finger drift for a short window. None of them worked reliably with a real finger, and the probe's flashing fields got in the way. Blender drops the multi-selection when the slide ends off the fields, and its Android build has the same limit.
- **A setting to make the side panels orbit instead of scroll.** It fought with how Blender treats the sidebars and caused odd behaviour, so the sidebars now behave as they do natively.
- **Shrinking the bigger UI back after 20 seconds without input.** It worked, but felt wrong in use, so the UI only shrinks when you touch the viewport or use the mouse or pen.
- **Scrolling a menu by sliding onto its ▼ arrow while holding.** Blender doesn't auto-scroll a menu while the button is held, and the search lists (F3, Add Modifier → Search) never auto-scroll, so a quick one-finger drag scrolls menus instead.
- **Two-finger scrolling in menus.** It worked, but one-finger dragging was enough, so it was removed to keep the gestures simple.
- **5-finger slide to switch workspace tabs.** The slide was detected, but it didn't feel reliable enough, so it was removed. Use Ctrl+Page Up / Ctrl+Page Down instead.

## License

GPL-3.0-or-later. See [LICENSE](LICENSE).
