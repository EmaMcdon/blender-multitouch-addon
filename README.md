# Multitouch Viewport Navigation for Blender (Windows)

A Blender add-on for Windows touchscreens and pen tablets. It brings touch behaviour modelled on Blender's Android demo build to desktop Blender.

Tested with Blender 5.3 on Windows 11 with a Huion pen display.

## Features

- **Touch navigation**: tap to click, one-finger orbit/pan/scroll, two-finger pan and pinch zoom, two-finger twist to roll the view (can be turned off: *Twist to Roll*).
- **Multi-finger taps**: 2, 3, 4 and 5 fingers, each set to Right Click, Undo, Redo, Focus Mode, Maximize Area or Previous View. A 2-finger tap right-clicks by default.
- **5-finger hold**: opens a touch pie (Mode, Undo, Redo, Previous View, Focus Mode, Bigger UI, Palm Rejection, Frame Selected).
- **Cancel a view move**: while two fingers move the view, put a third finger down near them (*Cancel Finger*, default 4 cm) and the view snaps back. Touches farther away, like your arm, are ignored.
- **One Action at a Time** (optional): a two-finger gesture only pans, zooms or rolls, whichever you start with.
- **Object edit by touch** (press and hold an object): drag moves it; with a second finger, slide rotates (*Object Slide*: Trackball, or Rotate like R), twist spins and pinch scales. Keyboard Shift makes it precise, Ctrl snaps (5°, 0.1 scale, grid). Value pills show the move, rotation and scale as you go. It has its own *One Action at a Time*.
- **Edit Mode by touch**: hold a vertex, edge or face to grab it (whichever the select mode picks), or hold part of an existing selection to move all of it. With Blender's snapping on, the hold runs Blender's own move (G) steered by the finger, so Face Project and other snap targets work as with the mouse.
- **Loop Cut and Knife by touch**: while Loop Cut (Ctrl+R), Knife, Poly Build, Bisect or Offset Edge Loops runs, sliding a finger hovers (shows the preview) and a tap clicks right there.
- **Bigger selection dots**: vertex and face dots, edges and object outlines grow smoothly while you use touch, and shrink back after 5 seconds without a finger or when you use the mouse or pen.
- **Pinch zoom in 2D editors**: two fingers pan and pinch-zoom the Node, Image / UV and Sequencer editors, keeping the point between the fingers in place.
- **Scroll headers sideways**: hold a header or the tool settings bar until the ring fills, then slide left or right to reach settings that don't fit.
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
- **With snapping on, a hold only moves.** The hold then runs Blender's own G, which can't rotate or scale with a second finger. Turn the snap magnet off to rotate and scale by touch.
- **Windows only.** It uses Win32 touch and raw-input APIs, so it does nothing on macOS or Linux.

## Tried and dropped

Fixes that were tried, failed in real use and were taken out, so nobody has to try them again:

- **Making the Location slide work top-down (X → Z).** Several helpers were tried: clearing the axis lock, probing the fields above and below the held one (this lit the fields up one after another), clamping the slide to the row centres, and ignoring finger drift for a short window. None of them worked reliably with a real finger, and the probe's flashing fields got in the way. Blender drops the multi-selection when the slide ends off the fields, and its Android build has the same limit.

## License

GPL-3.0-or-later. See [LICENSE](LICENSE).
