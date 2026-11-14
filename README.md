# Multitouch Viewport Navigation for Blender (Windows)

A Blender add-on for Windows touchscreens and pen tablets. It brings touch behaviour modelled on Blender's Android demo build to desktop Blender.

Tested with Blender 5.3 on Windows 11 with a Huion pen display.

## Features

- **Touch navigation**: tap to click, one-finger orbit/pan/scroll, two-finger pan and pinch zoom.
- **Multi-finger taps**: 2, 3 and 4 fingers do Undo, Redo and Focus Mode (configurable).
- **Palm rejection**: touches on the pen hand's side are ignored while the pen is near and for a set time after it leaves. Right-handed, left-handed and distance modes are available.
- **Auto-perspective**: one-finger orbit leaves the automatic orthographic view (orthographic chosen with numpad 5 is kept).
- **Number fields by touch**: drag sideways on a value to change it like a mouse, with the touch point as zero. Add a second finger for Ctrl (snap).
- **Hold and slide**: hold a field until the ring fills, then slide over other fields to edit several at once.
- **Bigger UI for fingers**: the interface grows when a finger touches a panel. Touching the viewport, or using the mouse or pen, brings it back to normal size.
- **Region overlap aware**: the see-through part of the 3D View sidebar and toolbar counts as viewport, as it does in Blender.
- **Pie menus**: taps work in pie menus in sculpt and paint modes.

## Install

1. Download `multitouch_viewport.py`.
2. In Blender: *Edit → Preferences → Add-ons → Install from Disk…* and pick the file.
3. Enable **Multitouch Viewport Navigation**. It starts automatically; its settings are in the 3D View sidebar, *Touch* tab.

## Requirements

- Windows (uses the Win32 touch, pointer and raw-input APIs).
- Blender 4.0 or newer (developed on 5.3).

## Known issues

- **Multi-value slide in Transform → Location only works bottom-up.** Holding X and sliding down to Z doesn't select all three fields, but sliding from Z up to X does. This is a Blender limitation: the official Android build behaves the same way. Rotation and Scale work in both directions.
- **A resting arm can trigger the bigger UI.** If your arm touches any panel or header after the pen leaves the screen, the UI scales up as if a finger touched it. Raise *After Pen Leaves* in the Touch tab (for example to 2000 ms) to ignore touches on the pen side for longer.
- **Windows only.** It uses Win32 touch and raw-input APIs, so it does nothing on macOS or Linux.
