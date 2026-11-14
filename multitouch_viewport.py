bl_info = {
    "name": "Multitouch Viewport Navigation",
    "author": "Claude",
    "version": (3, 14, 3),
    "blender": (4, 0, 0),
    "location": "View3D > Sidebar > Touch",
    "description": "Touch navigation modelled on Blender's Android demo build: tap to click, "
                   "1-finger orbit/pan/scroll, 2-finger pan & pinch zoom, "
                   "2/3/4-finger tap for Undo/Redo/Focus Mode (Windows).",
    "category": "3D View",
}

# Gesture model ported from the Bcon26 Android demo branch
# (projects.blender.org/Brainzman/blender, branch "android-demo",
#  GHOST_SystemSDL::processTouchEvent + wm_event_system.cc touch drag modes).

import ctypes
import math
import os as _os
import sys
import time
from ctypes import wintypes

try:  # Allows the gesture engine to be unit-tested outside Blender.
    import bpy
    from mathutils import Vector, Quaternion
except ImportError:  # pragma: no cover
    bpy = None

IS_WINDOWS = sys.platform.startswith("win")

WHEEL_DELTA = 120
SCROLL_PIXELS_PER_NOTCH = 30.0   # vertical drag pixels per wheel notch
TAP_TIMEOUT_S = 0.5              # multi-finger tap window (same as the demo build)
DRAG_THRESHOLD_POINTS = 5.0      # scaled by DPI / 96 (15 px at Android's 288 DPI)

_LOG_ENABLED = [False]
_LOG_PATH = _os.path.join(_os.path.expanduser("~"), "multitouch_debug.log")


_LOG_MAX_BYTES = 1000000     # start a fresh log past ~1 MB (keeps one .old copy)
_LOG_ERRORS = [0.0, 0]        # (window start, count): throttle repeated errors


def _append_log(text):
    try:
        try:
            if _os.path.getsize(_LOG_PATH) > _LOG_MAX_BYTES:
                old = _LOG_PATH + ".old"
                if _os.path.exists(old):
                    _os.remove(old)
                _os.replace(_LOG_PATH, old)
        except OSError:
            pass
        with open(_LOG_PATH, "a", encoding="utf-8") as fh:
            fh.write(text)
    except Exception:
        pass


_LOG_BUF = []   # Debug Log lines, written once per modal tick (file I/O per line lags)


def _flush_log():
    if _LOG_BUF:
        text = "".join(_LOG_BUF)
        del _LOG_BUF[:]
        _append_log(text)


def _log(msg):
    if _LOG_ENABLED[0]:
        _LOG_BUF.append(msg + "\n")
        if len(_LOG_BUF) > 500:
            _flush_log()


def _log_info(msg):
    """Lifecycle messages are always logged, regardless of the Debug Log toggle."""
    _flush_log()
    _append_log("%s %s\n" % (time.strftime("%H:%M:%S"), msg))


def _log_error(msg):
    """Errors are always logged (and printed), regardless of the Debug Log toggle,
    but at most 20 per minute so a repeating error can't flood the disk."""
    now = time.time()
    if now - _LOG_ERRORS[0] > 60.0:
        _LOG_ERRORS[0], _LOG_ERRORS[1] = now, 0
    _LOG_ERRORS[1] += 1
    if _LOG_ERRORS[1] > 20:
        return
    print("[Multitouch] " + msg)
    _append_log("%s ERROR %s\n" % (time.strftime("%H:%M:%S"), msg))


# Runtime state lives in the module, NOT on the Scene: scene properties are part of
# the undo history, so Ctrl+Z (or the 2-finger-tap Undo) would switch the add-on off.
_RUNNING = [None]   # the live operator instance
_STOP = [False]     # set by the Stop button
_PENDING_REPORT = [None]


def _run_tap_action(action, cx, cy):
    """Runs from a bpy.app.timers callback, i.e. outside the modal handler, the same
    way a key-map item would run ed.undo / ed.redo / screen.screen_full_area."""
    try:
        ctx = bpy.context
        if other_modal_running(ctx):
            # Undo/redo/focus while G/R/S (or any tool) runs frees data that tool is
            # still drawing from: Blender crashes in drawTransformPixel.
            _log_info("tap action %s skipped: a modal tool is running" % action)
            return None
        target = find_target(ctx, cx, cy)
        if target is None:
            win = ctx.window_manager.windows[0]
            area = next((a for a in win.screen.areas if a.type == 'VIEW_3D'), win.screen.areas[0])
        else:
            win, area = target[0], target[1]
        region = next((r for r in area.regions if r.type == 'WINDOW'), area.regions[0])
        with ctx.temp_override(window=win, screen=win.screen, area=area, region=region):
            if action == 'UNDO':
                res, label = bpy.ops.ed.undo(), "Undo"
            elif action == 'REDO':
                res, label = bpy.ops.ed.redo(), "Redo"
            elif action in ('FOCUS', 'MAXIMIZE'):
                res = bpy.ops.screen.screen_full_area(use_hide_panels=(action == 'FOCUS'))
                label = "Focus Mode" if action == 'FOCUS' else "Maximize Area"
            else:
                return None
        _log("TAP ACTION %s -> %s" % (action, res))
        if 'FINISHED' in res:
            _PENDING_REPORT[0] = label
    except Exception:
        import traceback
        _log_error("tap action %s:\n%s" % (action, traceback.format_exc()))
    return None  # one-shot timer


# ---------------------------------------------------------------------------
# Win32 plumbing
# ---------------------------------------------------------------------------
TOUCH_API_AVAILABLE = False
if IS_WINDOWS:
    user32 = ctypes.WinDLL("user32", use_last_error=True)

    PT_TOUCH = 2
    PT_PEN = 3
    POINTER_FLAG_INRANGE = 0x00000002
    POINTER_FLAG_INCONTACT = 0x00000004
    POINTER_FLAG_DOWN = 0x00010000
    POINTER_FLAG_UP = 0x00040000
    POINTER_FLAG_CANCELED = 0x00008000

    WM_MOUSEWHEEL = 0x020A
    GA_ROOT = 2

    MOUSEEVENTF_LEFTDOWN = 0x0002
    MOUSEEVENTF_LEFTUP = 0x0004
    MOUSEEVENTF_MIDDLEDOWN = 0x0020
    MOUSEEVENTF_MIDDLEUP = 0x0040
    MOUSEEVENTF_RIGHTDOWN = 0x0008
    MOUSEEVENTF_RIGHTUP = 0x0010

    # Mouse messages that Windows synthesises from touch/pen carry this signature
    # in GetMessageExtraInfo(); bit 0x80 set means touch (clear means pen).
    MI_WP_SIGNATURE = 0xFF515700
    SIGNATURE_MASK = 0xFFFFFF00
    SIGNATURE_TOUCH_BIT = 0x80

    class POINT(ctypes.Structure):
        _fields_ = [("x", wintypes.LONG), ("y", wintypes.LONG)]

    class POINTER_INFO(ctypes.Structure):
        _fields_ = [
            ("pointerType", wintypes.DWORD),
            ("pointerId", wintypes.UINT),
            ("frameId", wintypes.UINT),
            ("pointerFlags", wintypes.DWORD),
            ("sourceDevice", wintypes.HANDLE),
            ("hwndTarget", wintypes.HWND),
            ("ptPixelLocation", POINT),
            ("ptHimetricLocation", POINT),
            ("ptPixelLocationRaw", POINT),
            ("ptHimetricLocationRaw", POINT),
            ("dwTime", wintypes.DWORD),
            ("historyCount", wintypes.UINT),
            ("InputData", ctypes.c_int32),
            ("dwKeyStates", wintypes.DWORD),
            ("PerformanceCount", ctypes.c_uint64),
            ("ButtonChangeType", ctypes.c_int),
        ]

    class MSG(ctypes.Structure):
        _fields_ = [
            ("hwnd", wintypes.HWND),
            ("message", wintypes.UINT),
            ("wParam", wintypes.WPARAM),
            ("lParam", wintypes.LPARAM),
            ("time", wintypes.DWORD),
            ("pt", POINT),
        ]

    HOOKPROC = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM)

    try:
        user32.GetPointerType.argtypes = [wintypes.UINT, ctypes.POINTER(wintypes.DWORD)]
        user32.GetPointerType.restype = wintypes.BOOL
        user32.GetPointerInfo.argtypes = [wintypes.UINT, ctypes.POINTER(POINTER_INFO)]
        user32.GetPointerInfo.restype = wintypes.BOOL
        user32.GetPointerFrameInfo.argtypes = [wintypes.UINT, ctypes.POINTER(wintypes.UINT),
                                               ctypes.POINTER(POINTER_INFO)]
        user32.GetPointerFrameInfo.restype = wintypes.BOOL

        user32.WindowFromPoint.argtypes = [POINT]
        user32.WindowFromPoint.restype = wintypes.HWND
        user32.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
        user32.GetAncestor.restype = wintypes.HWND
        user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT,
                                        wintypes.WPARAM, wintypes.LPARAM]
        user32.PostMessageW.restype = wintypes.BOOL

        user32.SetWindowsHookExW.argtypes = [ctypes.c_int, HOOKPROC, wintypes.HINSTANCE, wintypes.DWORD]
        user32.SetWindowsHookExW.restype = wintypes.HHOOK
        user32.UnhookWindowsHookEx.argtypes = [wintypes.HHOOK]
        user32.UnhookWindowsHookEx.restype = wintypes.BOOL
        user32.CallNextHookEx.argtypes = [wintypes.HHOOK, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM]
        user32.CallNextHookEx.restype = wintypes.LPARAM
        user32.GetMessageExtraInfo.argtypes = []
        user32.GetMessageExtraInfo.restype = wintypes.LPARAM

        user32.SetCursorPos.argtypes = [ctypes.c_int, ctypes.c_int]
        user32.SetCursorPos.restype = wintypes.BOOL
        user32.mouse_event.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.DWORD,
                                       wintypes.DWORD, ctypes.POINTER(ctypes.c_ulong)]
        user32.mouse_event.restype = None

        _GetCurrentThreadId = ctypes.windll.kernel32.GetCurrentThreadId
        _GetCurrentThreadId.restype = wintypes.DWORD
        TOUCH_API_AVAILABLE = True
    except (AttributeError, OSError):
        TOUCH_API_AVAILABLE = False

    WH_GETMESSAGE = 3
    PM_REMOVE = 1
    WM_POINTERUPDATE = 0x0245
    WM_POINTERDOWN = 0x0246
    WM_POINTERUP = 0x0247
    WM_POINTERENTER = 0x0249
    WM_POINTERLEAVE = 0x024A


def _system_dpi():
    if not IS_WINDOWS:
        return 96
    try:
        return int(user32.GetDpiForSystem()) or 96
    except Exception:
        return 96


# ---------------------------------------------------------------------------
# Gesture engine - a straight port of the demo build's touch state machine.
# Pure Python (no bpy), screen pixels, y-down. Feed it pointer events and it
# returns a list of high-level actions for the operator to apply.
# ---------------------------------------------------------------------------
NONE, TAP_PENDING, DRAG, ORBIT1, TWO_PAN, MULTI_TAP, SUPPRESSED, HOLD, HOLD_TWO = range(9)
_MODE_NAMES = ("NONE", "TAP_PENDING", "DRAG", "ORBIT1", "TWO_PAN", "MULTI_TAP", "SUPPRESSED",
               "HOLD", "HOLD_TWO")

HOLD_TIME_S = 0.5          # press-and-hold time to start editing
HOLD_PREVIEW_S = 0.15      # when the "growing outline" feedback starts


def _wrap_angle(a):
    while a > math.pi:
        a -= 2.0 * math.pi
    while a < -math.pi:
        a += 2.0 * math.pi
    return a


class PalmFilter:
    """Drops touches made by the palm / arm of the hand holding the pen.

    While the pen hovers or touches the screen (and for a grace period after it leaves),
    new touches are rejected unless they land in the "free zone" of the other hand:
      zone 'RIGHT'    right-handed: only touches left of the pen by more than the gap
      zone 'LEFT'     left-handed: only touches right of the pen by more than the gap
      zone 'DISTANCE' any touch farther than the gap from the pen tip
      zone 'NONE'     no touch at all while the pen is near
    Touches the screen itself marks as palms (HID Confidence off) are always rejected.
    A touch that landed shortly before the pen came into range (palm first, pen after)
    is cancelled when the pen arrives. A rejected touch stays rejected until it lifts.
    """
    PALM_FIRST_S = 0.3

    def __init__(self):
        self.rejected = set()
        self.live = {}            # pid -> [x, y, t_down] of touches passed on
        self._enter_seen = 0.0
        self.count = 0            # rejected touches (for the log)
        self.details = []         # why (for the log)
        self.allowed = []         # touches let through while the pen was near (for the log)

    @staticmethod
    def pen_active(pen, t, grace_s):
        if pen["in"]:
            return True
        return bool(pen["left_t"]) and t - pen["left_t"] < grace_s

    @staticmethod
    def free(pen, x, y, zone, gap_px):
        if zone == 'RIGHT':
            return x < pen["x"] - gap_px
        if zone == 'LEFT':
            return x > pen["x"] + gap_px
        if zone == 'DISTANCE':
            return math.hypot(x - pen["x"], y - pen["y"]) > gap_px
        return False

    def _note(self, why, x, y, pen):
        self.count += 1
        if len(self.details) < 6:
            self.details.append("%s at (%d,%d) pen (%d,%d) %s" % (
                why, x, y, pen["x"], pen["y"], "in range" if pen["in"] else "left"))

    def _cancel(self, pid, out, t, why="cancel", pen=None):
        x, y, _t0 = self.live.pop(pid)
        self.rejected.add(pid)
        if pen is not None:
            self._note(why, x, y, pen)
        else:
            self.count += 1
        out.append((pid, x, y, False, True, True, t))

    def filter(self, events, pen, palms, zone, gap_px, grace_s, now):
        out = []
        # The pen just came into range: cancel touches that landed just before it, on
        # the pen hand's side (the palm usually lands first).
        if pen["in"] and pen["enter_t"] > self._enter_seen:
            self._enter_seen = pen["enter_t"]
            for pid, (x, y, t0) in list(self.live.items()):
                if t0 >= pen["enter_t"] - self.PALM_FIRST_S and not self.free(pen, x, y, zone, gap_px):
                    self._cancel(pid, out, now, "palm-first", pen)
        # The screen flagged an ongoing touch as a palm.
        for pid in [q for q in self.live if q in palms]:
            self._cancel(pid, out, now, "screen palm", pen)
        for ev in events:
            pid, x, y, is_down, is_up, canceled, t = ev
            if pid in self.rejected:
                if is_up:
                    self.rejected.discard(pid)
                    palms.discard(pid)
                continue
            if is_down:
                if pid in palms or (self.pen_active(pen, t, grace_s)
                                    and not self.free(pen, x, y, zone, gap_px)):
                    self._note("screen palm" if pid in palms else "near pen", x, y, pen)
                    if not is_up:
                        self.rejected.add(pid)
                    continue
                self.live[pid] = [x, y, t]
                if self.pen_active(pen, t, grace_s) and len(self.allowed) < 6:
                    self.allowed.append("(%d,%d) pen (%d,%d)" % (x, y, pen["x"], pen["y"]))
            elif is_up:
                self.live.pop(pid, None)
                palms.discard(pid)
            elif pid in palms and pid in self.live:
                self._cancel(pid, out, t, "screen palm", pen)
                continue
            elif pid in self.live:
                self.live[pid][0], self.live[pid][1] = x, y
            out.append(ev)
        return out

    def reset(self):
        self.rejected.clear()
        self.live.clear()


class TouchEngine:
    """Actions emitted (tuples):
      ('press', x, y)                     first finger down (operator may allow a hold here)
      ('tap', x, y)                       one finger down/up without moving
      ('drag_begin', sx, sy)              one finger crossed the drag threshold
      ('drag', dx, dy, x, y)              one-finger motion (also after 2->1 transition)
      ('two_begin', cx, cy)               two-finger pan/pinch gesture starts
      ('two', dx, dy, zoom, cx, cy)       centroid delta + pinch ratio (>1 = spread)
      ('two_to_one', x, y)                one finger lifted, the other keeps navigating
      ('multi_tap', n, cx, cy)            2/3/4-finger tap
      ('end',)                            current gesture finished or was suppressed
    Press-and-hold (only when the operator set hold_allowed after 'press'):
      ('hold_progress', p, x, y)          0 < p < 1 while the finger stays still
      ('hold', x, y)                      hold completed: editing starts
      ('hold_drag', dx, dy, x, y)         one finger moves while editing
      ('hold_two_begin', cx, cy)          second finger added while editing
      ('hold_two', dx, dy, zoom, dangle, cx, cy)  centroid move, pinch ratio, twist (rad, y-down)
      ('hold_one', x, y)                  back to one finger while editing
      ('hold_end',) / ('hold_cancel',)    all fingers lifted / input canceled
    """

    def __init__(self, threshold_px=None, tap_timeout=TAP_TIMEOUT_S,
                 hold_time=HOLD_TIME_S, hold_preview=HOLD_PREVIEW_S):
        if threshold_px is None:
            threshold_px = max(DRAG_THRESHOLD_POINTS, DRAG_THRESHOLD_POINTS * _system_dpi() / 96.0)
        self.threshold = float(threshold_px)
        self.tap_timeout = tap_timeout
        self.hold_time = hold_time
        self.hold_preview = hold_preview
        self.reset()

    # -- helpers ----------------------------------------------------------
    def reset(self):
        self.touches = {}          # pid -> [x, y, start_x, start_y]  (insertion ordered)
        self.mode = NONE
        self.start = (0.0, 0.0)
        self.prev = (0.0, 0.0)
        self.prev_dist = 0.0
        self.prev_angle = 0.0
        self.tap_t0 = 0.0
        self.tap_count = 0
        self.hold_allowed = False
        self.hold_slop = 1.0       # drag threshold multiplier while a hold is allowed

    def _two_metrics(self):
        a, b = list(self.touches.values())[:2]
        c = ((a[0] + b[0]) * 0.5, (a[1] + b[1]) * 0.5)
        return c, math.hypot(b[0] - a[0], b[1] - a[1]), math.atan2(b[1] - a[1], b[0] - a[0])

    def tick(self, t):
        """Call regularly (every modal timer tick) to drive press-and-hold timing."""
        out = []
        if self.mode != TAP_PENDING or not self.hold_allowed or len(self.touches) != 1:
            return out
        x, y = next(iter(self.touches.values()))[:2]
        elapsed = t - self.tap_t0
        if elapsed >= self.hold_time:
            self.mode = HOLD
            self.prev = (x, y)
            out.append(('hold', x, y))
        elif elapsed >= self.hold_preview:
            out.append(('hold_progress', (elapsed - self.hold_preview) /
                        (self.hold_time - self.hold_preview), x, y))
        return out

    def _centroid(self):
        n = len(self.touches)
        return (sum(t[0] for t in self.touches.values()) / n,
                sum(t[1] for t in self.touches.values()) / n)

    def _two(self):
        a, b = list(self.touches.values())[:2]
        return a, b

    def _begin_two(self, mode):
        a, b = self._two()
        self.mode = mode
        self.start = self.prev = ((a[0] + b[0]) * 0.5, (a[1] + b[1]) * 0.5)
        self.prev_dist = math.hypot(b[0] - a[0], b[1] - a[1])

    def _tap_valid(self, t):
        if t - self.tap_t0 > self.tap_timeout:
            return False
        thr2 = self.threshold * self.threshold
        for x, y, sx, sy in self.touches.values():
            if (x - sx) ** 2 + (y - sy) ** 2 >= thr2:
                return False
        return True

    def _suppress(self, out):
        if self.mode not in (SUPPRESSED, NONE, TAP_PENDING, MULTI_TAP):
            out.append(('end',))
        self.mode = SUPPRESSED

    # -- events -----------------------------------------------------------
    def down(self, pid, x, y, t):
        out = []
        if pid in self.touches:
            return out
        prev_mode = self.mode
        self.touches[pid] = [float(x), float(y), float(x), float(y)]
        n = len(self.touches)

        if prev_mode in (HOLD, HOLD_TWO):
            if n == 2:
                self.mode = HOLD_TWO
                c, self.prev_dist, self.prev_angle = self._two_metrics()
                self.prev = c
                out.append(('hold_two_begin',) + c)
            return out  # extra fingers beyond two are ignored while editing

        if n == 1:
            self.mode = TAP_PENDING
            self.start = self.prev = (float(x), float(y))
            self.tap_t0 = t
            self.tap_count = 0
            self.hold_allowed = False
            out.append(('press', float(x), float(y)))
        elif prev_mode == MULTI_TAP:
            # Replacing an already released finger does not extend the same tap.
            if n <= 4 and n == self.tap_count + 1:
                self.tap_count = n
                self.start = self._centroid()
            else:
                self._suppress(out)
        elif n == 2:
            if prev_mode in (DRAG, ORBIT1):
                out.append(('end',))
            if prev_mode == TAP_PENDING:
                self.tap_count = 2
                self._begin_two(MULTI_TAP)
            else:
                self._begin_two(TWO_PAN)
                out.append(('two_begin',) + self.start)
        else:
            if prev_mode in (DRAG, ORBIT1, TWO_PAN):
                out.append(('end',))
                self.mode = SUPPRESSED
            else:
                self._suppress(out)
        return out

    def move(self, pid, x, y, t):
        out = []
        tp = self.touches.get(pid)
        if tp is None:
            return out
        tp[0], tp[1] = float(x), float(y)

        if self.mode == HOLD:
            dx, dy = x - self.prev[0], y - self.prev[1]
            if (dx or dy) and pid == next(iter(self.touches)):
                out.append(('hold_drag', dx, dy, float(x), float(y)))
                self.prev = (float(x), float(y))
            return out
        if self.mode == HOLD_TWO:
            c, dist, ang = self._two_metrics()
            dx, dy = c[0] - self.prev[0], c[1] - self.prev[1]
            zoom = dist / self.prev_dist if self.prev_dist > 1e-3 else 1.0
            dang = _wrap_angle(ang - self.prev_angle)
            if dx or dy or abs(zoom - 1.0) > 1e-6 or abs(dang) > 1e-6:
                out.append(('hold_two', dx, dy, zoom, dang, c[0], c[1]))
            self.prev, self.prev_dist, self.prev_angle = c, dist, ang
            return out

        if self.mode == TAP_PENDING:
            sx, sy = self.start
            thr = self.threshold * (self.hold_slop if self.hold_allowed else 1.0)
            if (x - sx) ** 2 + (y - sy) ** 2 >= thr ** 2:
                self.mode = DRAG
                out.append(('drag_begin', sx, sy))
                out.append(('drag', x - sx, y - sy, float(x), float(y)))
                self.prev = (float(x), float(y))
            return out

        if self.mode == MULTI_TAP:
            if self._tap_valid(t):
                return out
            if self.tap_count == 2 and len(self.touches) == 2:
                self.mode = TWO_PAN
                out.append(('two_begin',) + self.start)
            else:
                self._suppress(out)
                return out

        if self.mode in (DRAG, ORBIT1):
            dx, dy = x - self.prev[0], y - self.prev[1]
            if dx or dy:
                out.append(('drag', dx, dy, float(x), float(y)))
                self.prev = (float(x), float(y))
        elif self.mode == TWO_PAN and len(self.touches) == 2:
            a, b = self._two()
            c = ((a[0] + b[0]) * 0.5, (a[1] + b[1]) * 0.5)
            dist = math.hypot(b[0] - a[0], b[1] - a[1])
            dx, dy = c[0] - self.prev[0], c[1] - self.prev[1]
            zoom = dist / self.prev_dist if self.prev_dist > 1e-3 else 1.0
            if dx or dy or abs(zoom - 1.0) > 1e-6:
                out.append(('two', dx, dy, zoom, c[0], c[1]))
            self.prev = c
            self.prev_dist = dist
        return out

    def up(self, pid, x, y, t, canceled=False):
        out = []
        tp = self.touches.get(pid)
        if tp is None:
            return out
        tp[0], tp[1] = float(x), float(y)
        n = len(self.touches)

        if self.mode in (HOLD, HOLD_TWO):
            del self.touches[pid]
            if canceled:
                out.append(('hold_cancel',))
                self.reset()
            elif not self.touches:
                out.append(('hold_end',))
                self.reset()
            elif self.mode == HOLD_TWO and len(self.touches) == 1:
                rem = next(iter(self.touches.values()))
                self.mode = HOLD
                self.prev = (rem[0], rem[1])
                out.append(('hold_one', rem[0], rem[1]))
            elif self.mode == HOLD_TWO:
                c, self.prev_dist, self.prev_angle = self._two_metrics()
                self.prev = c
            return out

        if self.mode == TAP_PENDING and n == 1:
            if not canceled:
                out.append(('tap', float(x), float(y)))
            self.reset()
            return out

        if self.mode in (DRAG, ORBIT1) and n == 1:
            if not canceled:
                dx, dy = x - self.prev[0], y - self.prev[1]
                if dx or dy:
                    out.append(('drag', dx, dy, float(x), float(y)))
            out.append(('end',))
            self.reset()
            return out

        if self.mode == MULTI_TAP:
            if not canceled and self._tap_valid(t):
                del self.touches[pid]
                if not self.touches:
                    if 2 <= self.tap_count <= 4:
                        out.append(('multi_tap', self.tap_count) + self.start)
                    self.reset()
                return out
            if not canceled and self.tap_count == 2 and n == 2:
                self.mode = TWO_PAN
                out.append(('two_begin',) + self.start)
            else:
                self._suppress(out)

        if self.mode == TWO_PAN:
            if not canceled and n == 2:
                del self.touches[pid]
                rem = next(iter(self.touches.values()))
                self.mode = ORBIT1
                self.prev = (rem[0], rem[1])
                self.prev_dist = 0.0
                out.append(('two_to_one', rem[0], rem[1]))
                return out
            self._suppress(out)

        del self.touches[pid]
        if not self.touches:
            self.reset()
        return out


# ---------------------------------------------------------------------------
# Blender-side helpers
# ---------------------------------------------------------------------------
def _ui_scale():
    try:
        return bpy.context.preferences.system.ui_scale or 1.0
    except Exception:
        return 1.0


def _desktop_height():
    """Blender (GHOST Win32) stores window.y bottom-up against the height of the whole
    virtual desktop (all monitors), i.e. window.y = desktop_h - client_bottom."""
    if IS_WINDOWS:
        try:
            h = user32.GetSystemMetrics(79)  # SM_CYVIRTUALSCREEN
            if h > 0:
                return h
        except Exception:
            pass
    return 0


def _screen_to_local(window, sx, sy):
    """Windows screen coords (y-down) -> Blender window-local (y-up).
    Verified on a 3-monitor desktop (4523 px tall) for main and Preferences windows."""
    client_bottom = _desktop_height() - window.y
    return sx - window.x, client_bottom - sy


# Overlapping side/header regions are tested before WINDOW: they are drawn on
# top of it and share its bounds.
_REGION_PRIORITY = (
    'HUD', 'UI', 'TOOLS', 'TOOL_PROPS', 'TOOL_HEADER', 'ASSET_SHELF_HEADER', 'ASSET_SHELF',
    'CHANNELS', 'NAVIGATION_BAR', 'EXECUTE', 'HEADER', 'FOOTER', 'PREVIEW', 'WINDOW',
)


# 3D View sidebar / toolbar with Region Overlap: below their panels they are see-through
# and Blender hands touches there to the viewport. The add-on does the same, using what
# the region actually drew: the lowest drawn pixel of each region (read when it redraws).
_OVERLAP = {}          # region pointer -> (lowest drawn y, region height)
_OVERLAP_HANDLES = []


def _overlap_draw():
    try:
        reg = bpy.context.region
        if reg is None or reg.type not in ('UI', 'TOOLS'):
            return
        import gpu
        h = int(reg.height)
        if h < 2:
            return
        buf = gpu.state.active_framebuffer_get().read_color(int(reg.width) // 2, 0, 1, h, 4, 0,
                                                             'FLOAT')
        buf.dimensions = h * 4
        low = h
        for y in range(h):
            if buf[y * 4 + 3] > 0.01:
                low = y
                break
        _OVERLAP[reg.as_pointer()] = (low, h)
    except Exception:
        pass


def _overlap_register():
    _overlap_unregister()
    for rtype in ('UI', 'TOOLS'):
        try:
            _OVERLAP_HANDLES.append((rtype, bpy.types.SpaceView3D.draw_handler_add(
                _overlap_draw, (), rtype, 'POST_PIXEL')))
        except Exception:
            pass


def _overlap_unregister():
    while _OVERLAP_HANDLES:
        rtype, h = _OVERLAP_HANDLES.pop()
        try:
            bpy.types.SpaceView3D.draw_handler_remove(h, rtype)
        except Exception:
            pass
    _OVERLAP.clear()


def _see_through(area, region, ry):
    """True when (region-local) height ry is below everything this sidebar/toolbar draws."""
    if area.type != 'VIEW_3D' or region.type not in ('UI', 'TOOLS'):
        return False
    e = _OVERLAP.get(region.as_pointer())
    return e is not None and e[1] == int(region.height) and ry < e[0] - 1


def find_area_under_cursor(sx, sy, window):
    """Returns (area, region, (region_x, region_y), (win_x, win_y)) or None."""
    lx, ly = _screen_to_local(window, sx, sy)
    for area in window.screen.areas:
        if not (area.x <= lx <= area.x + area.width and area.y <= ly <= area.y + area.height):
            continue
        for rtype in _REGION_PRIORITY:
            for region in area.regions:
                if region.type != rtype or region.width <= 1 or region.height <= 1:
                    continue
                if (region.x <= lx <= region.x + region.width and
                        region.y <= ly <= region.y + region.height):
                    if _see_through(area, region, ly - region.y):
                        continue
                    return area, region, (lx - region.x, ly - region.y), (lx, ly)
    return None


def find_target(context, sx, sy):
    for window in context.window_manager.windows:
        hit = find_area_under_cursor(int(sx), int(sy), window)
        if hit is not None:
            return (window,) + hit
    return None


_OWN_MODAL = {"VIEW3D_OT_multitouch_navigate"}


def other_modal_running(context):
    """True while another modal tool runs (G/R/S transform, knife, box select, ...).
    Touch then just drives the cursor so that tool's own constraints (G then Z, ...)
    stay in charge."""
    try:
        for w in context.window_manager.windows:
            for op in w.modal_operators:
                if op is not None and getattr(op, "bl_idname", "") not in _OWN_MODAL:
                    return True
    except Exception:
        pass
    return False


def window_under(context, sx, sy):
    """Any Blender window containing the point. The top bar (workspace tabs, menus) and
    the status bar are *global* areas that aren't listed in screen.areas, so
    find_target() can't see them; touches there still have to reach Blender."""
    for window in context.window_manager.windows:
        lx, ly = _screen_to_local(window, sx, sy)
        if 0 <= lx <= window.width and 0 <= ly <= window.height:
            return window
    return None


# ---------------------------------------------------------------------------
# Press-and-hold editing: pick, select, move / twist-rotate / pinch-scale
# ---------------------------------------------------------------------------
_HOLD_PICK_RADIUS = 30.0        # px (x UI scale) for vertices and non-mesh objects
_HOLD_MAX_VERTS = 20000        # skip vertex picking on huge meshes (Python loop)
_HOLD_MODES = {'OBJECT', 'EDIT_MESH'}


def _region_co(window, region, sx, sy):
    lx, ly = _screen_to_local(window, sx, sy)
    return Vector((lx - region.x, ly - region.y))


def hold_pick(context, target):
    """What a press-and-hold at this point would grab, or None."""
    from bpy_extras import view3d_utils as v3u
    window, area, region, (rx, ry), _l = target
    if area.type != 'VIEW_3D' or region.type != 'WINDOW':
        return None
    rv3d = area.spaces.active.region_3d
    co = Vector((rx, ry))
    radius = _HOLD_PICK_RADIUS * _ui_scale()
    if context.mode == 'OBJECT':
        depsgraph = context.evaluated_depsgraph_get()
        origin = v3u.region_2d_to_origin_3d(region, rv3d, co)
        direction = v3u.region_2d_to_vector_3d(region, rv3d, co)
        hit, _loc, _nor, _idx, ob, _mat = context.scene.ray_cast(depsgraph, origin, direction)
        if hit and ob is not None:
            return {"kind": "OBJECT", "obj": ob.original.name}
        best = None  # cameras, lights, empties: pick by their origin
        for ob in context.visible_objects:
            p = v3u.location_3d_to_region_2d(region, rv3d, ob.matrix_world.translation)
            if p is not None:
                d = (p - co).length
                if d <= radius and (best is None or d < best[0]):
                    best = (d, ob.name)
        return {"kind": "OBJECT", "obj": best[1]} if best else None
    if context.mode == 'EDIT_MESH':
        import bmesh
        best = None
        for ob in context.objects_in_mode:
            if ob.type != 'MESH':
                continue
            bm = bmesh.from_edit_mesh(ob.data)
            if len(bm.verts) > _HOLD_MAX_VERTS:
                continue
            mw = ob.matrix_world
            for v in bm.verts:
                if v.hide:
                    continue
                p = v3u.location_3d_to_region_2d(region, rv3d, mw @ v.co)
                if p is not None:
                    d = (p - co).length
                    if d <= radius and (best is None or d < best[0]):
                        best = (d, ob.name, v.index)
        return {"kind": "VERT", "obj": best[1], "index": best[2]} if best else None
    return None


def hold_select(context, pick):
    """Select what was picked (keeps an existing multi-selection if it's part of it)."""
    ob = bpy.data.objects.get(pick["obj"])
    if ob is None:
        return
    if pick["kind"] == "OBJECT":
        if not ob.select_get():
            for o in context.selected_objects:
                o.select_set(False)
            ob.select_set(True)
        context.view_layer.objects.active = ob
        return
    import bmesh
    bm = bmesh.from_edit_mesh(ob.data)
    bm.verts.ensure_lookup_table()
    v = bm.verts[pick["index"]]
    # Edit Mode: always grab just the held vertex (entering Edit Mode often has
    # everything selected, which would move the whole mesh).
    for o in context.objects_in_mode:
        if o.type != 'MESH':
            continue
        b2 = bmesh.from_edit_mesh(o.data)
        for seq in (b2.faces, b2.edges, b2.verts):
            for e in seq:
                e.select = False
        if o is not ob:
            bmesh.update_edit_mesh(o.data, loop_triangles=False, destructive=False)
    vmode, emode, fmode = context.tool_settings.mesh_select_mode
    if fmode and not vmode:
        for f in v.link_faces:
            f.select = True
    elif emode and not vmode:
        for e in v.link_edges:
            e.select = True
    v.select = True
    bm.select_flush_mode()
    bmesh.update_edit_mesh(ob.data, loop_triangles=False, destructive=False)


class HoldEdit:
    """Moves / rotates / scales the selection directly (no modal transform operator),
    so one- and two-finger input can be combined freely like a photo app."""

    def __init__(self, context, window, area, region):
        import bmesh
        self.window, self.area, self.region = window, area, region
        self.rv3d = area.spaces.active.region_3d
        self.drag = Vector((0.0, 0.0))      # accumulated finger motion, region px (y up)
        # Object Mode: respect the padlocks (Lock Location / Rotation / Scale) of the
        # active object while moving, not only after release. Free axes are expressed in
        # the space its Location lives in (its parent's, or world).
        self.free = [(i, Vector(a)) for i, a in enumerate(self._AXES)]
        act = context.view_layer.objects.active if context.mode == 'OBJECT' else None
        if act is not None and any(act.lock_location):
            basis = self._location_basis(act)
            self.free = [(i, basis[i]) for i in range(3) if not act.lock_location[i]]
        self.T = Vector((0.0, 0.0, 0.0))
        self.R = Quaternion()
        self.S = 1.0
        self.changed = False
        self.objects = []   # (object, base matrix_world)
        self.meshes = []    # (object, matrix_world_inv, [(vert index, base world co)])
        pts = []
        if context.mode == 'OBJECT':
            sel = list(context.selected_objects)
            sel_set = set(sel)
            for o in sel:
                p, skip = o.parent, False
                while p is not None:
                    if p in sel_set:
                        skip = True  # moves with its selected parent
                        break
                    p = p.parent
                if not skip:
                    self.objects.append((o, o.matrix_world.copy(), o.location.copy(),
                                         o.rotation_euler.copy(), o.scale.copy()))
                    pts.append(o.matrix_world.translation.copy())
        else:
            for o in context.objects_in_mode:
                if o.type != 'MESH':
                    continue
                bm = bmesh.from_edit_mesh(o.data)
                mw = o.matrix_world.copy()
                verts = [(v.index, mw @ v.co) for v in bm.verts if v.select and not v.hide]
                if verts:
                    self.meshes.append((o, mw.inverted_safe(), verts))
                    pts.extend(c for _i, c in verts)
        self.valid = bool(pts)
        self.pivot = sum(pts, Vector()) / len(pts) if pts else Vector()

    def _matrix(self):
        from mathutils import Matrix
        return (Matrix.Translation(self.pivot + self.T) @ self.R.to_matrix().to_4x4()
                @ Matrix.Scale(self.S, 4) @ Matrix.Translation(-self.pivot))

    def _write(self, M):
        import bmesh
        for o, base, loc, rot, scl in self.objects:
            o.matrix_world = M @ base
            # Hold locked channels in place while editing (Blender's own transform does).
            for i in range(3):
                if o.lock_location[i]:
                    o.location[i] = loc[i]
                if o.lock_scale[i]:
                    o.scale[i] = scl[i]
                if o.lock_rotation[i] and o.rotation_mode not in ('QUATERNION', 'AXIS_ANGLE'):
                    o.rotation_euler[i] = rot[i]
        for o, inv, verts in self.meshes:
            bm = bmesh.from_edit_mesh(o.data)
            bm.verts.ensure_lookup_table()
            for i, wco in verts:
                bm.verts[i].co = inv @ (M @ wco)
            bmesh.update_edit_mesh(o.data, loop_triangles=False, destructive=False)
        self.area.tag_redraw()

    def apply(self):
        self.changed = True
        self._write(self._matrix())
        if len(self.free) == 1 and self.T.length > 0.0:
            i, a = self.free[0]
            axis_line_show(self.pivot + self.T, a, i)

    _AXES = ((1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0))   # Vector() at use: importable outside Blender

    @staticmethod
    def _location_basis(ob):
        if ob.parent is None:
            return tuple(Vector(a) for a in HoldEdit._AXES)
        m = (ob.parent.matrix_world @ ob.matrix_parent_inverse).to_3x3()
        return tuple(Vector(m.col[i]).normalized() for i in range(3))

    def _update_translation(self):
        """Finger offset -> translation that keeps the grabbed point under the finger,
        restricted to the unlocked axes: 3 free = view plane, 2 = the plane they span,
        1 = the line (closest point to the ray under the finger), 0 = no move."""
        from bpy_extras import view3d_utils as v3u
        region, rv3d, piv = self.region, self.rv3d, self.pivot
        p2 = v3u.location_3d_to_region_2d(region, rv3d, piv)
        if p2 is None:
            return
        g = p2 + self.drag                     # where the pivot should appear on screen
        n_free = len(self.free)
        if n_free == 3:
            self.T = v3u.region_2d_to_location_3d(region, rv3d, g, piv) - piv
            return
        if n_free == 0:
            self.T = Vector((0.0, 0.0, 0.0))
            return
        o = v3u.region_2d_to_origin_3d(region, rv3d, g)
        r = v3u.region_2d_to_vector_3d(region, rv3d, g).normalized()
        w = piv - o
        if n_free == 2:
            nrm = self.free[0][1].cross(self.free[1][1]).normalized()
            t_view = v3u.region_2d_to_location_3d(region, rv3d, g, piv) - piv
            t_proj = t_view - nrm * t_view.dot(nrm)   # view-plane move, flattened
            d = r.dot(nrm)
            t_ray = (o + r * (w.dot(nrm) / d) - piv) if abs(d) > 1e-4 else None
            # Seen nearly edge-on, the exact ray hit races off to the horizon: use the
            # flattened move there instead.
            if t_ray is None or abs(d) < 0.3 or t_ray.length > 3.0 * t_proj.length + 1e-6:
                self.T = t_proj
            else:
                self.T = t_ray
            return
        a = self.free[0][1]
        b = a.dot(r)
        if 1.0 - b * b < 1e-6:
            return                             # axis points straight at the viewer
        t = (b * r.dot(w) - a.dot(w)) / (1.0 - b * b)
        self.T = a * t

    def status(self):
        if len(self.free) == 1:
            return "Move along %s (locked axes)" % "XYZ"[self.free[0][0]]
        if len(self.free) == 2:
            return "Move in %s%s (locked axis)" % ("XYZ"[self.free[0][0]], "XYZ"[self.free[1][0]])
        return None

    def move(self, sx0, sy0, sx1, sy1):
        self.drag += Vector((sx1 - sx0, -(sy1 - sy0)))
        self._update_translation()
        self.apply()

    TRACKBALL_PX_PER_HALF_TURN = 500.0   # x UI scale: two-finger slide this far = 180 deg

    def two(self, dx, dy, zoom, dangle, cx, cy):
        if dx or dy:
            # Trackball (like R then R): sliding both fingers rolls the object in that
            # direction about the pivot. Screen y is down: dragging right turns the front
            # towards the right (about view-up), dragging down tips it down (about view-right).
            k = math.pi / (self.TRACKBALL_PX_PER_HALF_TURN * _ui_scale())
            vr = self.rv3d.view_rotation
            right = vr @ Vector((1.0, 0.0, 0.0))
            up = vr @ Vector((0.0, 1.0, 0.0))
            self.R = (Quaternion(up, dx * k) @ Quaternion(right, dy * k) @ self.R).normalized()
        self.S = max(1e-3, min(self.S * zoom, 1e3))
        if dangle:
            # Screen y points down, so a positive twist is clockwise on screen: that's a
            # positive rotation about the axis pointing *into* the screen.
            into_screen = self.rv3d.view_rotation @ Vector((0.0, 0.0, -1.0))
            self.R = Quaternion(into_screen, dangle) @ self.R
        self.apply()

    def cancel(self):
        from mathutils import Matrix
        self._write(Matrix.Identity(4))
        self.changed = False


_AXIS_LINE = [None, None]   # [draw handle, (center, direction, axis index)]
_AXIS_COLORS = ((1.0, 0.2, 0.32, 1.0), (0.54, 0.86, 0.0, 1.0), (0.16, 0.56, 1.0, 1.0))


def _draw_axis_line():
    data = _AXIS_LINE[1]
    if data is None:
        return
    try:
        import gpu
        from gpu_extras.batch import batch_for_shader
        c, d, i = data
        L = 1.0e4
        shader = gpu.shader.from_builtin('POLYLINE_UNIFORM_COLOR')
        batch = batch_for_shader(shader, 'LINES', {"pos": [c - d * L, c + d * L]})
        vp = gpu.state.viewport_get()
        shader.uniform_float("viewportSize", (vp[2], vp[3]))
        shader.uniform_float("lineWidth", 2.0 * _ui_scale())
        shader.uniform_float("color", _AXIS_COLORS[i])
        gpu.state.blend_set('ALPHA')
        batch.draw(shader)
        gpu.state.blend_set('NONE')
    except Exception:
        pass


def axis_line_show(center, direction, index):
    _AXIS_LINE[1] = (center.copy(), direction.copy(), index)
    if _AXIS_LINE[0] is None:
        _AXIS_LINE[0] = bpy.types.SpaceView3D.draw_handler_add(
            _draw_axis_line, (), 'WINDOW', 'POST_VIEW')


def axis_line_hide():
    _AXIS_LINE[1] = None
    if _AXIS_LINE[0] is not None:
        try:
            bpy.types.SpaceView3D.draw_handler_remove(_AXIS_LINE[0], 'WINDOW')
        except Exception:
            pass
        _AXIS_LINE[0] = None


_THEME_BACKUP = [None]   # (outline_width, vertex_size, edge_width) while the hold glow is shown


def theme_grow(p):
    """Grow the selection outline / vertex dots with hold progress p in [0, 1]."""
    th = bpy.context.preferences.themes[0].view_3d
    if _THEME_BACKUP[0] is None:
        _THEME_BACKUP[0] = (th.outline_width, th.vertex_size, th.edge_width)
    ow, vs, ew = _THEME_BACKUP[0]
    p = max(0.0, min(1.0, p))
    new = (min(5, ow + int(round(p * 3))), min(32, int(round(vs * (1.0 + 1.5 * p)))),
           min(32, ew + int(round(p * 2))))
    if (th.outline_width, th.vertex_size, th.edge_width) != new:
        th.outline_width, th.vertex_size, th.edge_width = new


def _redraw_3d_views():
    try:
        for w in bpy.context.window_manager.windows:
            for ar in w.screen.areas:
                if ar.type == 'VIEW_3D':
                    ar.tag_redraw()
    except Exception:
        pass


def theme_restore():
    if _THEME_BACKUP[0] is None:
        return
    try:
        th = bpy.context.preferences.themes[0].view_3d
        th.outline_width, th.vertex_size, th.edge_width = _THEME_BACKUP[0]
    except Exception:
        pass
    _THEME_BACKUP[0] = None


def _push_undo(window, message):
    def _do():
        try:
            with bpy.context.temp_override(window=window, screen=window.screen):
                bpy.ops.ed.undo_push(message=message)
        except Exception:
            import traceback
            _log_error("undo_push:\n" + traceback.format_exc())
        return None
    bpy.app.timers.register(_do, first_interval=0.0)


def _near_area_edge(area, lx, ly):
    m = 4.0 * _ui_scale()
    return (lx <= area.x + m or lx >= area.x + area.width - m or
            ly <= area.y + m or ly >= area.y + area.height - m)


def _over_navigation_gizmo(area, region, rx, ry):
    """Approximate hit box for the View3D navigation gizmo (axes + zoom/pan buttons)."""
    try:
        prefs = bpy.context.preferences.view
        space = area.spaces.active
        if not (prefs.show_navigate_ui and space.show_gizmo and space.show_gizmo_navigate):
            return False
        fac = _ui_scale()
        size = getattr(prefs, "gizmo_size_navigate_v3d", 80)
        right = region.width
        top = region.height
        # Region overlap: the sidebar / headers are drawn on top of the main region.
        for r in area.regions:
            if r.type == 'UI' and r.width > 1 and r.x + r.width >= area.x + area.width - 2:
                right -= r.width
            if r.type in ('HEADER', 'TOOL_HEADER') and r.height > 1 and r.y >= region.y + region.height - r.height - 2:
                top -= r.height
        w = (size + 20) * fac
        h = (size + 150) * fac
        return right - w <= rx <= right and top - h <= ry <= top
    except Exception:
        return False


def _over_node(area, region, rx, ry):
    try:
        tree = area.spaces.active.edit_tree
        if tree is None:
            return False
        vx, vy = region.view2d.region_to_view(rx, ry)
        fac = _ui_scale()
        vx, vy = vx / fac, vy / fac  # view space -> node space
        pad = 12.0  # sockets stick out past the node border
        for node in tree.nodes:
            loc = getattr(node, "location_absolute", node.location)
            w = node.width if node.type != 'FRAME' else node.dimensions.x
            h = node.dimensions.y
            if node.type == 'FRAME':
                continue  # dragging on a frame background pans, like empty space
            if loc.x - pad <= vx <= loc.x + w + pad and loc.y - h - pad <= vy <= loc.y + pad:
                return True
    except Exception:
        pass
    return False


_SCROLL_SPACES = {'OUTLINER', 'PROPERTIES', 'PREFERENCES', 'FILE_BROWSER',
                  'TEXT_EDITOR', 'CONSOLE', 'INFO', 'SPREADSHEET'}
_PAN2D_SPACES = {'NODE_EDITOR', 'IMAGE_EDITOR', 'CLIP_EDITOR', 'SEQUENCE_EDITOR',
                 'DOPESHEET_EDITOR', 'GRAPH_EDITOR', 'NLA_EDITOR'}
_SCRUB_SPACES = {'DOPESHEET_EDITOR', 'GRAPH_EDITOR', 'NLA_EDITOR', 'SEQUENCE_EDITOR', 'CLIP_EDITOR'}
_MOUSE_REGIONS = {'HEADER', 'TOOL_HEADER', 'FOOTER', 'NAVIGATION_BAR', 'EXECUTE',
                  'HUD', 'ASSET_SHELF_HEADER'}
_PAINT_MODES = {'SCULPT', 'SCULPT_CURVES', 'PAINT_TEXTURE', 'PAINT_VERTEX', 'PAINT_WEIGHT',
                'PAINT_GPENCIL', 'PAINT_GREASE_PENCIL', 'SCULPT_GPENCIL', 'SCULPT_GREASE_PENCIL',
                'WEIGHT_GPENCIL', 'WEIGHT_GREASE_PENCIL', 'VERTEX_GPENCIL', 'VERTEX_GREASE_PENCIL'}


_EVENT_VK = {'ZERO': 0x30, 'ONE': 0x31, 'TWO': 0x32, 'THREE': 0x33, 'FOUR': 0x34,
             'FIVE': 0x35, 'SIX': 0x36, 'SEVEN': 0x37, 'EIGHT': 0x38, 'NINE': 0x39,
             'ACCENT_GRAVE': 0xC0, 'TAB': 0x09, 'SPACE': 0x20, 'COMMA': 0xBC,
             'PERIOD': 0xBE, 'SLASH': 0xBF, 'SEMI_COLON': 0xBA, 'QUOTE': 0xDE,
             'MINUS': 0xBD, 'EQUAL': 0xBB, 'LEFT_BRACKET': 0xDB, 'RIGHT_BRACKET': 0xDD,
             'BACK_SLASH': 0xDC, 'DEL': 0x2E, 'HOME': 0x24, 'END': 0x23,
             'F1': 0x70, 'F2': 0x71, 'F3': 0x72, 'F4': 0x73, 'F5': 0x74, 'F6': 0x75,
             'F7': 0x76, 'F8': 0x77, 'F9': 0x78, 'F10': 0x79, 'F11': 0x7A, 'F12': 0x7B}
_MENU_OPS = {'wm.call_menu_pie', 'wm.call_menu', 'wm.call_panel', 'wm.toolbar',
             'wm.search_menu', 'wm.context_menu_enum', 'wm.context_pie_enum',
             'wm.toolbar_fallback_pie', 'wm.call_asset_shelf_popover'}


def _menu_key_vks(context):
    """(key code, ctrl, shift, alt) of every key that opens a pie or menu in the 3D View."""
    vks = set()
    try:
        kc = context.window_manager.keyconfigs.user
        for km in kc.keymaps:
            if km.space_type not in ('VIEW_3D', 'EMPTY'):
                continue          # other editors' menu keys don't matter over the viewport
            for kmi in km.keymap_items:
                if not kmi.active or kmi.idname not in _MENU_OPS or kmi.map_type != 'KEYBOARD':
                    continue
                t = kmi.type
                if len(t) == 1 and t.isalpha():
                    vk = ord(t)
                elif t in _EVENT_VK:
                    vk = _EVENT_VK[t]
                else:
                    continue
                mods = tuple(bool(getattr(kmi, k)) if getattr(kmi, k) != -1 else None
                             for k in ('ctrl', 'shift', 'alt'))
                vks.add((vk,) + mods)
    except Exception:
        pass
    return vks


def _menu_key_match(vks, vk):
    """Is this key press (with the modifiers held right now) a menu key?"""
    held = None
    for k in vks:
        if k[0] != vk:
            continue
        if held is None:
            gs = user32.GetKeyState
            held = (bool(gs(0x11) & 0x8000), bool(gs(0x10) & 0x8000), bool(gs(0x12) & 0x8000))
        if all(w is None or w == h for w, h in zip(k[1:], held)):
            return True
    return False


# ---------------------------------------------------------------------------
# Touch mode: fingers get a bigger UI (and no tooltips); mouse / pen get it back.
# The user's own values are kept in the add-on preferences, so an auto-saved
# preferences file never keeps the enlarged scale: it is undone at the next start.
# ---------------------------------------------------------------------------
TUI_ANIM_S = 0.22
TUI_ENTER_DELAY_S = 0.15     # after the last finger lifts (never re-layout under a finger)
TUI_EXIT_MOUSE_PX = 40.0     # real mouse travel that ends touch mode
TUI_EXIT_QUIET_S = 0.6       # ...but only this long after the last touch
TUI_SETTLE_S = 0.05          # after the grow animation: layout has been redrawn
# Touching these editors' main area returns the normal size (they are canvases).
_TUI_CANVAS = {'VIEW_3D', 'IMAGE_EDITOR', 'NODE_EDITOR', 'SEQUENCE_EDITOR', 'CLIP_EDITOR',
               'GRAPH_EDITOR', 'DOPESHEET_EDITOR', 'NLA_EDITOR'}
# Panel / list regions that can scroll to keep the touched field under the finger.
_TUI_PAN_REGIONS = {'UI', 'WINDOW', 'TOOLS', 'TOOL_PROPS', 'HUD', 'CHANNELS'}
_TUI = {"active": False, "anim": None, "pending": False}


def _tui_prefs():
    try:
        return bpy.context.preferences.addons[__name__].preferences
    except Exception:
        return None


def tui_enter():
    ap = _tui_prefs()
    if ap is None or _TUI["active"] or not ap.touch_ui:
        return
    v = bpy.context.preferences.view
    if not ap.touch_ui_active:
        ap.base_ui_scale = v.ui_scale
        ap.base_tooltips = v.show_tooltips
        ap.touch_ui_active = True
    _TUI["active"] = True
    v.show_tooltips = False
    target = max(0.5, min(4.0, ap.base_ui_scale * ap.touch_ui_scale))
    _TUI["anim"] = (v.ui_scale, target, time.perf_counter(), False)
    _log_info("touch mode on: UI scale %.2f -> %.2f" % (v.ui_scale, target))


def tui_exit(immediate=False):
    ap = _tui_prefs()
    _TUI["active"] = False
    _TUI["pending"] = False
    if ap is None or not ap.touch_ui_active:
        return
    v = bpy.context.preferences.view
    v.show_tooltips = ap.base_tooltips
    if immediate:
        _TUI["anim"] = None
        v.ui_scale = ap.base_ui_scale
        ap.touch_ui_active = False
    else:
        _TUI["anim"] = (v.ui_scale, ap.base_ui_scale, time.perf_counter(), True)
    _log_info("touch mode off: UI scale back to %.2f" % ap.base_ui_scale)


def tui_tick(now):
    an = _TUI["anim"]
    if an is None:
        return
    a, b, t0, finish = an
    f = min(1.0, (now - t0) / TUI_ANIM_S)
    e = 1.0 - (1.0 - f) ** 3                       # ease out
    val = round(a + (b - a) * e, 3)
    try:
        v = bpy.context.preferences.view
        if abs(v.ui_scale - val) > 1e-4:
            v.ui_scale = val
    except Exception:
        _TUI["anim"] = None
        return
    if f >= 1.0:
        _TUI["anim"] = None
        if finish:
            ap = _tui_prefs()
            if ap is not None:
                ap.touch_ui_active = False


def tui_restore_saved():
    """Blender started (or the add-on was re-enabled) while touch mode was on."""
    ap = _tui_prefs()
    if ap is not None and ap.touch_ui_active:
        try:
            v = bpy.context.preferences.view
            v.ui_scale = ap.base_ui_scale
            v.show_tooltips = ap.base_tooltips
            ap.touch_ui_active = False
            _log_info("restored UI scale %.2f after touch mode" % ap.base_ui_scale)
        except Exception:
            pass
    _TUI["active"] = False
    _TUI["anim"] = None


UI_HOLD_TIME_S = 0.35
UI_HOLD_SLOP = 2.0
_RING = {"handle": None, "space": None, "region": 0, "xy": (0, 0), "p": 0.0}


def _ring_draw():
    try:
        if bpy.context.region is None or bpy.context.region.as_pointer() != _RING["region"]:
            return
        import gpu
        from gpu_extras.batch import batch_for_shader
        x, y = _RING["xy"]
        p = max(0.0, min(1.0, _RING["p"]))
        sc = _ui_scale()
        r = (10.0 + 16.0 * p) * sc
        n = 40
        pts = [(x + r * math.cos(2 * math.pi * i / n), y + r * math.sin(2 * math.pi * i / n))
               for i in range(n + 1)]
        col = (0.33, 0.62, 1.0)          # bright accent: readable on dark and light themes
        sh = gpu.shader.from_builtin('POLYLINE_UNIFORM_COLOR')
        gpu.state.blend_set('ALPHA')
        sh.uniform_float("viewportSize", gpu.state.viewport_get()[2:])
        sh.uniform_float("lineWidth", (2.0 if p < 1.0 else 4.0) * sc)
        sh.uniform_float("color", (col[0], col[1], col[2], 0.5 + 0.5 * p))
        batch_for_shader(sh, 'LINE_STRIP', {"pos": pts}).draw(sh)
        gpu.state.blend_set('NONE')
    except Exception:
        pass


def _ring_show(target, p):
    _ring_hide()
    try:
        _w, area, region, (rx, ry), _l = target
        space = type(area.spaces.active)
        _RING["handle"] = space.draw_handler_add(_ring_draw, (), region.type, 'POST_PIXEL')
        _RING["space"] = space
        _RING["region"] = region.as_pointer()
        _RING["rtype"] = region.type
        _RING["area"] = area
        _RING["xy"] = (rx, ry)
        _RING["p"] = p
    except Exception:
        _RING["handle"] = None


def _ring_progress(p):
    _RING["p"] = p
    try:
        if _RING["handle"] is not None:
            _RING["area"].tag_redraw()
    except Exception:
        pass


def _ring_hide():
    h = _RING["handle"]
    _RING["handle"] = None
    if h is not None:
        try:
            _RING["space"].draw_handler_remove(h, _RING["rtype"])
        except Exception:
            pass
        try:
            _RING["area"].tag_redraw()
        except Exception:
            pass


# Open menus, popovers and search popups are temporary regions in the screen's
# runtime region list (bScreen.regionbase), which Python doesn't expose. Its place in
# the struct is found once by matching the area list next to it (areabase, whose first
# and last entries Python does know), and the region rect by matching a known region.
_POPUP_LAYOUT = {}


def _popup_rects(window):
    """Window-local (xmin, xmax, ymin, ymax) of every open menu / popup, or []."""
    if not IS_WINDOWS:
        return []
    try:
        import ctypes
        screen = window.screen
        areas = list(screen.areas)
        if not areas:
            return []
        words = (ctypes.c_uint64 * 256).from_address(screen.as_pointer())
        k = _POPUP_LAYOUT.get("areabase")
        first, last = areas[0].as_pointer(), areas[-1].as_pointer()
        if k is None or words[k] != first or words[k + 1] != last:
            k = next((i for i in range(254) if words[i] == first and words[i + 1] == last), None)
            if k is None:
                return []
            _POPUP_LAYOUT["areabase"] = k
        r_off = _POPUP_LAYOUT.get("winrct")
        if r_off is None:
            ref = max((r for a in areas for r in a.regions if r.width > 1 and r.height > 1),
                      key=lambda r: r.width * r.height)
            ints = (ctypes.c_int32 * 512).from_address(ref.as_pointer())
            want = (ref.x, ref.x + ref.width - 1, ref.y, ref.y + ref.height - 1)
            r_off = next((i for i in range(4, 508) if tuple(ints[i:i + 4]) == want), None)
            if r_off is None:
                return []
            _POPUP_LAYOUT["winrct"] = r_off
        rects = []
        reg = words[k + 2]
        while reg and len(rects) < 16:
            ints = (ctypes.c_int32 * (r_off + 4)).from_address(reg)
            rects.append(tuple(ints[r_off:r_off + 4]))
            reg = ctypes.c_uint64.from_address(reg).value      # ARegion.next
        return rects
    except Exception:
        return []


def popup_under(context, sx, sy):
    """The window whose open menu / popup contains the screen point, or None."""
    for window in context.window_manager.windows:
        lx, ly = _screen_to_local(window, sx, sy)
        for x0, x1, y0, y1 in _popup_rects(window):
            if x0 <= lx <= x1 and y0 <= ly <= y1:
                return window
    return None


def _numdrag_region(area, region):
    """Regions full of number fields: Properties editor, sidebars, redo panel, prefs."""
    if area.type in ('PROPERTIES', 'PREFERENCES'):
        return region.type == 'WINDOW'
    return region.type in ('UI', 'TOOLS', 'HUD', 'TOOL_PROPS')


def classify_one_finger(context, target, scroll_one_finger):
    """Mirror of wm_touch_drag_start_mode(): what a 1-finger drag does at its origin."""
    window, area, region, (rx, ry), (lx, ly) = target
    if _near_area_edge(area, lx, ly):
        return 'MOUSE'            # area resize / join action zones
    rtype = region.type
    if rtype in _MOUSE_REGIONS:
        return 'MOUSE'
    if rtype not in ('WINDOW', 'PREVIEW'):
        return 'SCROLL' if scroll_one_finger else 'MOUSE'   # sidebars, toolbars, channels
    st = area.type
    if st == 'VIEW_3D':
        return 'MOUSE' if _over_navigation_gizmo(area, region, rx, ry) else 'VIEW3D'
    if st in _SCROLL_SPACES:
        return 'SCROLL' if scroll_one_finger else 'MOUSE'
    if st in _SCRUB_SPACES and ry >= region.height - 22.0 * _ui_scale():
        return 'MOUSE'            # one-finger timeline scrubbing
    if st == 'NODE_EDITOR' and _over_node(area, region, rx, ry):
        return 'MOUSE'            # drag nodes / links
    if st in _PAN2D_SPACES:
        return 'PAN2D'
    return 'MOUSE'


def classify_two_finger(target):
    window, area, region, _r, _l = target
    if area.type == 'VIEW_3D' and region.type in ('WINDOW', 'HEADER', 'TOOL_HEADER'):
        return 'VIEW3D'           # orbit/pan gestures work under overlapping regions too
    if area.type in _PAN2D_SPACES and region.type in ('WINDOW', 'PREVIEW'):
        return 'PAN2D'
    return 'SCROLL'


def apply_view3d(rv3d, region, pan_dx, pan_dy, zoom, rot_dx, rot_dy):
    """Pan/zoom/orbit a RegionView3D. Pixel deltas are y-down (screen)."""
    if _TUI["active"]:
        tui_exit()   # also under the see-through sidebar / toolbar: that's the viewport
    if abs(zoom - 1.0) > 1e-4:
        rv3d.view_distance = max(0.001, min(rv3d.view_distance / max(zoom, 1e-3), 1.0e6))
    if pan_dx or pan_dy:
        # 1:1 like the phone build: whatever sits at the view's pivot depth stays under
        # the fingers. Unproject two screen points on that plane and move by the difference.
        from bpy_extras import view3d_utils as v3u
        c = Vector((region.width * 0.5, region.height * 0.5))
        piv = rv3d.view_location.copy()
        a = v3u.region_2d_to_location_3d(region, rv3d, c, piv)
        b = v3u.region_2d_to_location_3d(region, rv3d, c + Vector((pan_dx, -pan_dy)), piv)
        rv3d.view_location = piv - (b - a)
    if rot_dx or rot_dy:
        az = -rot_dx / (region.width or 1) * math.pi
        el = -rot_dy / (region.height or 1) * math.pi
        q = rv3d.view_rotation
        m = q.to_matrix()
        right = Vector((m[0][0], m[1][0], m[2][0]))
        rv3d.view_rotation = (Quaternion((0.0, 0.0, 1.0), az) @ Quaternion(right, el) @ q).normalized()


# Blender's own orbit leaves an orthographic axis view (numpad 1/3/7) for perspective
# when Preferences > Navigation > Auto Perspective is on - but not when ortho was chosen
# on purpose (numpad 5, or already ortho before snapping). Blender keeps that memory
# internally; Python can't read it, so it is tracked here the same way.
_PERSP_STATE = {}   # rv3d pointer -> [perspective, last perspective rotation, auto,
                    #                  rotation when ortho began, time ortho began]
_SNAP_WINDOW_S = 0.6   # smooth view: Blender may go ortho first and turn the view after


def _turned(a, b):
    return max(abs(x - y) for x, y in zip(a, b)) > 1e-3


def track_auto_persp(rv3d, now=None):
    now = time.perf_counter() if now is None else now
    key = rv3d.as_pointer()
    persp = rv3d.view_perspective
    rot = tuple(rv3d.view_rotation)
    st = _PERSP_STATE.get(key)
    if st is None:
        # Unknown history: an ortho side view almost always came from a numpad snap.
        _PERSP_STATE[key] = [persp, rot, persp == 'ORTHO' and rv3d.is_orthographic_side_view,
                             rot, now - 10.0]
        return
    prev_persp, persp_rot, auto, ortho_rot, ortho_t = st
    if persp != 'ORTHO':
        auto = False
        persp_rot = rot
    elif prev_persp != 'ORTHO':
        # Just became ortho. The view turned too -> a snap to an axis view (automatic);
        # same view -> numpad 5 (chosen).
        auto = _turned(rot, persp_rot)
        ortho_rot, ortho_t = rot, now
    elif not auto and now - ortho_t < _SNAP_WINDOW_S and _turned(rot, ortho_rot):
        auto = True        # the snap's smooth-view animation turned it right after
    st[:] = [persp, persp_rot, auto, ortho_rot, ortho_t]


def leave_auto_ortho(rv3d):
    """Called when a touch orbit starts: back to perspective like Blender's orbit."""
    try:
        if not bpy.context.preferences.inputs.use_auto_perspective:
            return
    except Exception:
        return
    track_auto_persp(rv3d)
    st = _PERSP_STATE.get(rv3d.as_pointer())
    if st is None:
        return
    if st[2] and rv3d.view_perspective == 'ORTHO':
        rv3d.view_perspective = 'PERSP'
        st[0], st[2] = 'PERSP', False
    # The view is about to turn because of this orbit, not a numpad snap's animation:
    # close the snap window so a chosen (numpad 5) ortho stays ortho.
    st[4] = -1.0e9


def post_mouse_wheel(sx, sy, delta):
    if not TOUCH_API_AVAILABLE:
        return
    _LAST_INJECT[0] = (sx, sy)
    user32.SetCursorPos(int(sx), int(sy))
    hwnd = user32.WindowFromPoint(POINT(int(sx), int(sy)))
    if not hwnd:
        return
    top = user32.GetAncestor(hwnd, GA_ROOT) or hwnd
    wparam = ((int(delta) & 0xFFFF) << 16) & 0xFFFFFFFF
    lparam = ((int(sy) & 0xFFFF) << 16) | (int(sx) & 0xFFFF)
    user32.PostMessageW(top, WM_MOUSEWHEEL, wparam, lparam)


_BUTTON_FLAGS = {
    'LEFT': (MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP) if IS_WINDOWS else (0, 0),
    'MIDDLE': (MOUSEEVENTF_MIDDLEDOWN, MOUSEEVENTF_MIDDLEUP) if IS_WINDOWS else (0, 0),
    'RIGHT': (MOUSEEVENTF_RIGHTDOWN, MOUSEEVENTF_RIGHTUP) if IS_WINDOWS else (0, 0),
}


def get_cursor():
    if not TOUCH_API_AVAILABLE:
        return None
    pt = POINT()
    if user32.GetCursorPos(ctypes.byref(pt)):
        return (pt.x, pt.y)
    return None


_LAST_INJECT = [None]   # last cursor position we gave Blender


def inject_button(button, down, sx, sy):
    """Real (non-touch-signed) mouse input, so the hook lets it through."""
    if not TOUCH_API_AVAILABLE:
        return
    _LAST_INJECT[0] = (sx, sy)
    user32.SetCursorPos(int(sx), int(sy))
    flags = _BUTTON_FLAGS[button]
    user32.mouse_event(flags[0] if down else flags[1], 0, 0, 0, None)


def inject_move(sx, sy):
    if TOUCH_API_AVAILABLE:
        _LAST_INJECT[0] = (sx, sy)
        user32.SetCursorPos(int(sx), int(sy))


_CONTINUOUS_SAVED = [None]   # user's "Continuous Grab" while a touch number drag runs


def numdrag_start(ctl, dx, dy):
    """Press on the number field at touch-down, then follow the finger like a mouse.

    Windows moves the system cursor to the finger on its own, so the drag keeps its own
    cursor position (never reads the system one). Continuous Grab (hidden cursor that
    wraps around) is switched off for the drag so Blender reads plain positions.
    """
    ctl.kind = 'NUMDRAG'
    ctl.button = 'LEFT'
    ctl.last_xy = (ctl.start[0], ctl.start[1])
    # The touch point is "no change": the movement spent deciding the drag direction is
    # dropped, so the value only follows the slide from here on.
    inject_button('LEFT', True, *ctl.last_xy)


def numdrag_move(ctl, dx, dy):
    # Relative to the real cursor: with Continuous Grab, Blender warps it at the edges.
    cur = get_cursor() or ctl.last_xy
    x, y = cur[0] + dx, cur[1] + dy
    ctl.last_xy = (x, y)
    inject_move(x, y)


def _restore_continuous():
    if _CONTINUOUS_SAVED[0] is not None:
        try:
            bpy.context.preferences.inputs.use_mouse_continuous = _CONTINUOUS_SAVED[0]
        except Exception:
            pass
        _CONTINUOUS_SAVED[0] = None
    return None


def numdrag_end_later():
    # After Blender has handled the button release.
    if bpy is not None and not bpy.app.timers.is_registered(_restore_continuous):
        bpy.app.timers.register(_restore_continuous, first_interval=0.3)


_VK = {'CTRL': 0x11, 'SHIFT': 0x10}


def inject_key(key, down):
    if TOUCH_API_AVAILABLE:
        user32.keybd_event(_VK[key], 0, 0 if down else 0x0002, None)   # KEYEVENTF_KEYUP



# ---------------------------------------------------------------------------
# Raw Input finger counter. With Windows' 3/4-finger touch gestures switched on,
# Windows keeps fingers 3+ for itself and Blender only gets two pointers. The
# touchscreen's own HID reports still reach a raw-input listener, and every
# Windows-compatible touchscreen reports a "Contact Count" (usage page 0x0D,
# usage 0x54): the real number of fingers down.
# ---------------------------------------------------------------------------
class RawTouchCounter:
    """Reads the touchscreen's HID reports through Raw Input.

    Always: counts fingers (Contact Count, usage page 0x0D / usage 0x54) per touch, even
    the ones Windows keeps for its own 3/4-finger gestures.
    In Raw mode (emit=True): also turns the reports into finger down/move/up events in
    screen pixels and feeds them to the gesture engine, so touches don't wait for
    Windows' gesture recognizer (60-140 ms for multi-finger touches)."""
    WM_INPUT = 0x00FF
    RID_INPUT = 0x10000003
    RIDI_PREPARSEDDATA = 0x20000005
    RIM_TYPEHID = 2
    RIDEV_INPUTSINK = 0x00000100
    RIDEV_REMOVE = 0x00000001
    HIDP_STATUS_SUCCESS = 0x00110000
    PID_BASE = 100000            # engine ids for raw contacts (never clash with pointer ids)

    def __init__(self):
        self.ok = False
        self.hwnd = None
        self.count = [0]
        self.reports = [0]
        self._prep = {}           # hDevice -> preparsed data buffer
        self._slot_cache = {}     # hDevice -> finger slots
        self.last_t = 0.0         # perf_counter of the last report
        self.gesture = None       # current raw touch: t0, t_last, maxc, pts{id: [x0,y0,x,y]}, rng
        self._buf = ctypes.create_string_buffer(8192)
        # --- Raw mode (emitting finger events) ---
        self.emit = False
        self.events = None        # the hook's event list
        self.down_ids = None      # the hook's set of fingers down
        self.display = None       # (left, top, right, bottom, rotation) the touchscreen covers
        self.active = {}          # engine pid -> (x, y) of fingers down
        self._frame = {}          # contact id -> (tip, x, y) collected for the current frame
        self._expect = 0
        self.cal_err = [0.0]      # worst raw-vs-Windows landing difference (px), for the log
        self.cal_n = [0]
        self.pidmap = {}          # raw contact id -> engine pid
        self._next_pid = self.PID_BASE
        self.pending_up = {}      # engine pid -> (x, y, lift time): lifts held back for debounce
        self.bounces = [0]        # lifts that turned out to be contact bounce (for the log)
        self.palms = set()        # engine pids the screen marked as palm (Confidence off)
        self._conf_devs = set()   # devices that report the Confidence usage at all
        try:
            self._hid = ctypes.WinDLL("hid")
        except OSError:
            self._hid = None

    class _RID(ctypes.Structure):
        _fields_ = [("usUsagePage", wintypes.USHORT), ("usUsage", wintypes.USHORT),
                    ("dwFlags", wintypes.DWORD), ("hwndTarget", wintypes.HWND)]

    class _HDR(ctypes.Structure):
        _fields_ = [("dwType", wintypes.DWORD), ("dwSize", wintypes.DWORD),
                    ("hDevice", wintypes.HANDLE), ("wParam", wintypes.WPARAM)]

    class _VCAPS(ctypes.Structure):   # HIDP_VALUE_CAPS (72 bytes)
        _fields_ = [("UsagePage", wintypes.USHORT), ("ReportID", ctypes.c_ubyte),
                    ("IsAlias", ctypes.c_ubyte), ("BitField", wintypes.USHORT),
                    ("LinkCollection", wintypes.USHORT), ("LinkUsage", wintypes.USHORT),
                    ("LinkUsagePage", wintypes.USHORT), ("IsRange", ctypes.c_ubyte),
                    ("IsStringRange", ctypes.c_ubyte), ("IsDesignatorRange", ctypes.c_ubyte),
                    ("IsAbsolute", ctypes.c_ubyte), ("HasNull", ctypes.c_ubyte),
                    ("Reserved", ctypes.c_ubyte), ("BitSize", wintypes.USHORT),
                    ("ReportCount", wintypes.USHORT), ("Reserved2", wintypes.USHORT * 5),
                    ("UnitsExp", wintypes.ULONG), ("Units", wintypes.ULONG),
                    ("LogicalMin", ctypes.c_long), ("LogicalMax", ctypes.c_long),
                    ("PhysicalMin", ctypes.c_long), ("PhysicalMax", ctypes.c_long),
                    ("Usage", wintypes.USHORT * 8)]

    def _caps(self, prep, usage):
        caps = (self._VCAPS * 32)()
        n = wintypes.USHORT(32)
        st = self._hid.HidP_GetSpecificValueCaps(0, 0x01, 0, usage, caps, ctypes.byref(n), prep)
        if (st & 0xFFFFFFFF) != self.HIDP_STATUS_SUCCESS:
            return {}
        return {caps[i].LinkCollection: (caps[i].LogicalMin, caps[i].LogicalMax)
                for i in range(n.value)}

    def _slots(self, hdev, prep):
        """[(link collection, xmin, xmax, ymin, ymax)] - one per finger slot in a report."""
        slots = self._slot_cache.get(hdev)
        if slots is not None:
            return slots
        slots = []
        try:
            xs, ys = self._caps(prep, 0x30), self._caps(prep, 0x31)
            for link in sorted(xs):
                if link in ys:
                    slots.append((link,) + xs[link] + ys[link])
        except Exception:
            slots = []
        self._slot_cache[hdev] = slots
        return slots

    def register(self, hwnd):
        if self._hid is None or not hwnd:
            return False
        rid = self._RID(0x0D, 0x04, self.RIDEV_INPUTSINK, hwnd)   # touch screen
        u = user32
        u.RegisterRawInputDevices.argtypes = [ctypes.c_void_p, wintypes.UINT, wintypes.UINT]
        u.RegisterRawInputDevices.restype = wintypes.BOOL
        u.GetRawInputData.argtypes = [wintypes.HANDLE, wintypes.UINT, ctypes.c_void_p,
                                      ctypes.POINTER(wintypes.UINT), wintypes.UINT]
        u.GetRawInputData.restype = wintypes.UINT
        u.GetRawInputDeviceInfoW.argtypes = [wintypes.HANDLE, wintypes.UINT, ctypes.c_void_p,
                                             ctypes.POINTER(wintypes.UINT)]
        u.GetRawInputDeviceInfoW.restype = wintypes.UINT
        h = self._hid
        h.HidP_GetUsageValue.argtypes = [ctypes.c_int, wintypes.USHORT, wintypes.USHORT,
                                         wintypes.USHORT, ctypes.POINTER(wintypes.ULONG),
                                         ctypes.c_void_p, ctypes.c_void_p, wintypes.ULONG]
        h.HidP_GetUsageValue.restype = ctypes.c_long
        h.HidP_GetSpecificValueCaps.argtypes = [ctypes.c_int, wintypes.USHORT, wintypes.USHORT,
                                                wintypes.USHORT, ctypes.c_void_p,
                                                ctypes.POINTER(wintypes.USHORT), ctypes.c_void_p]
        h.HidP_GetSpecificValueCaps.restype = ctypes.c_long
        h.HidP_GetUsages.argtypes = [ctypes.c_int, wintypes.USHORT, wintypes.USHORT,
                                     ctypes.c_void_p, ctypes.POINTER(wintypes.ULONG),
                                     ctypes.c_void_p, ctypes.c_void_p, wintypes.ULONG]
        h.HidP_GetUsages.restype = ctypes.c_long
        self.ok = bool(u.RegisterRawInputDevices(ctypes.byref(rid), 1, ctypes.sizeof(rid)))
        self.hwnd = hwnd if self.ok else None
        return self.ok

    def unregister(self):
        if self.ok:
            rid = self._RID(0x0D, 0x04, self.RIDEV_REMOVE, None)
            user32.RegisterRawInputDevices(ctypes.byref(rid), 1, ctypes.sizeof(rid))
        self.ok = False

    def _preparsed(self, hdev):
        prep = self._prep.get(hdev)
        if prep is None:
            size = wintypes.UINT(0)
            user32.GetRawInputDeviceInfoW(hdev, self.RIDI_PREPARSEDDATA, None, ctypes.byref(size))
            if not size.value:
                return None
            prep = ctypes.create_string_buffer(size.value)
            if user32.GetRawInputDeviceInfoW(hdev, self.RIDI_PREPARSEDDATA, prep,
                                             ctypes.byref(size)) == 0xFFFFFFFF:
                return None
            self._prep[hdev] = prep
        return prep

    # -- Raw mode: screen mapping and finger events ---------------------------------
    def to_screen(self, x, y, xmin, xmax, ymin, ymax):
        left, top, right, bottom, rot = self.display
        u = (x - xmin) / float(max(1, xmax - xmin))
        v = (y - ymin) / float(max(1, ymax - ymin))
        if rot == 2:      # display rotated 90 degrees
            u, v = 1.0 - v, u
        elif rot == 3:    # 180
            u, v = 1.0 - u, 1.0 - v
        elif rot == 4:    # 270
            u, v = v, 1.0 - u
        return left + u * (right - left), top + v * (bottom - top)

    def _send(self, pid, x, y, down, up, canceled, t):
        self.events.append((pid, x, y, down, up, canceled, t))
        if len(self.events) > 2048:
            del self.events[:1024]

    BOUNCE_S = 0.04              # a finger back within 40 ms...
    BOUNCE_PX = 30.0             # ...and 30 px of where it lifted is the same touch

    def _lift(self, pid, x, y, now):
        # Hold the lift back briefly: touchscreens can report a landing finger as
        # down-up-down. Windows filters that; we must too, or one tap = a double-click.
        self.active.pop(pid, None)
        self.pending_up[pid] = (x, y, now)
        for cid in [c for c, p in self.pidmap.items() if p == pid]:
            del self.pidmap[cid]

    def flush(self, now):
        """Send lifts that weren't followed by a bounce."""
        for pid, (x, y, t) in list(self.pending_up.items()):
            if now - t >= self.BOUNCE_S:
                del self.pending_up[pid]
                self.down_ids.discard(pid)
                self._send(pid, x, y, False, True, False, t)

    def _commit_frame(self, now):
        self.flush(now)
        seen = set()
        for cid, fr in self._frame.items():
            tip, x, y = fr[0], fr[1], fr[2]
            conf = fr[3] if len(fr) > 3 else True
            pid = self.pidmap.get(cid)
            if tip and not conf:
                # Palm: mark it (the filter rejects it); the new-contact path below still
                # gives it an id so its lift is matched.
                if pid is None:
                    self._next_pid += 1
                    pid = self._next_pid
                    self.pidmap[cid] = pid
                    self.palms.add(pid)
                    self.active[pid] = (x, y)
                    self.down_ids.add(pid)
                    self._send(pid, x, y, True, False, False, now)
                    seen.add(pid)
                    continue
                self.palms.add(pid)
            if tip:
                if pid is None:
                    # New contact: a bounce of a finger that just lifted, or a new finger.
                    best = None
                    for ppid, (px, py, _t) in self.pending_up.items():
                        d = math.hypot(px - x, py - y)
                        if d <= self.BOUNCE_PX and (best is None or d < best[0]):
                            best = (d, ppid)
                    if best is not None:
                        pid = best[1]
                        px, py, _t = self.pending_up.pop(pid)
                        self.bounces[0] += 1
                        self.pidmap[cid] = pid
                        self.active[pid] = (px, py)
                    else:
                        self._next_pid += 1
                        pid = self._next_pid
                        self.pidmap[cid] = pid
                        self.active[pid] = (x, y)
                        self.down_ids.add(pid)
                        self._send(pid, x, y, True, False, False, now)
                        seen.add(pid)
                        continue
                seen.add(pid)
                old = self.active.get(pid)
                if old is None or abs(old[0] - x) >= 0.5 or abs(old[1] - y) >= 0.5:
                    self.active[pid] = (x, y)
                    self._send(pid, x, y, False, False, False, now)
            elif pid is not None and pid in self.active:
                seen.add(pid)
                self._lift(pid, x, y, now)
        for pid in [p for p in self.active if p not in seen]:
            x, y = self.active[pid]           # contact vanished without a tip-off report
            self._lift(pid, x, y, now)
        self._frame = {}
        self._expect = 0

    def cancel_all(self, now=None):
        """Lost the touch stream (another app came to the front, or reports stopped)."""
        now = now or time.perf_counter()
        for pid, (x, y) in list(self.active.items()):
            self.down_ids.discard(pid)
            self._send(pid, x, y, False, True, True, now)
        for pid, (x, y, _t) in list(self.pending_up.items()):
            self.down_ids.discard(pid)
            self._send(pid, x, y, False, True, True, now)
        self.active.clear()
        self.pending_up.clear()
        self.pidmap.clear()
        self._frame = {}
        self._expect = 0

    def note_pointer_down(self, px, py):
        """Windows' own landing position for a finger: compare with ours (diagnostic)."""
        if not self.active:
            return
        d = min(math.hypot(px - ax, py - ay) for ax, ay in self.active.values())
        self.cal_n[0] += 1
        if d > self.cal_err[0]:
            self.cal_err[0] = d

    def handle(self, lparam, foreground=True):
        """Called from the message hook for WM_INPUT. Doesn't consume the message."""
        if not foreground:
            if self.emit and self.active:
                self.cancel_all()
            return
        size = wintypes.UINT(ctypes.sizeof(self._buf))
        hdr_size = ctypes.sizeof(self._HDR)
        n = user32.GetRawInputData(lparam, self.RID_INPUT, self._buf, ctypes.byref(size), hdr_size)
        if n in (0, 0xFFFFFFFF) or n < hdr_size + 8:
            return
        hdr = self._HDR.from_buffer(self._buf)
        if hdr.dwType != self.RIM_TYPEHID:
            return
        prep = self._preparsed(hdr.hDevice)
        if prep is None:
            return
        size_hid, n_reports = ctypes.cast(ctypes.byref(self._buf, hdr_size),
                                          ctypes.POINTER(wintypes.DWORD * 2)).contents
        base = hdr_size + 8
        val = wintypes.ULONG(0)
        hid = self._hid
        ok = self.HIDP_STATUS_SUCCESS
        slots = self._slots(hdr.hDevice, prep)
        emit = self.emit and self.display is not None and bool(slots)
        now = time.perf_counter()
        g = self.gesture
        if g is None:
            g = self.gesture = {"t0": now, "t_last": now, "maxc": 0, "pts": {}, "rng": 1}
        g["t_last"] = now
        self.last_t = now
        usages = (wintypes.USHORT * 16)()
        for i in range(n_reports):
            off = base + i * size_hid
            if off + size_hid > n:
                break
            rep = ctypes.byref(self._buf, off)
            st = hid.HidP_GetUsageValue(0, 0x0D, 0, 0x54, ctypes.byref(val), prep, rep, size_hid)
            self.reports[0] += 1
            count = val.value if (st & 0xFFFFFFFF) == ok else 0
            # Hybrid mode: a frame's first report carries the contact count, the
            # following reports of the same frame carry 0 and more contacts.
            if count > 0:
                if self._expect and self._frame and emit:
                    self._commit_frame(now)          # previous frame came up short
                self._frame = {}
                self._expect = count
                if count > self.count[0]:
                    self.count[0] = count
                if count > g["maxc"]:
                    g["maxc"] = count
            remaining = self._expect - len(self._frame)
            for link, xmin, xmax, ymin, ymax in slots[:max(0, remaining)]:
                nu = wintypes.ULONG(16)
                if (hid.HidP_GetUsages(0, 0x0D, link, usages, ctypes.byref(nu), prep, rep,
                                       size_hid) & 0xFFFFFFFF) != ok:
                    continue
                got = usages[:nu.value]
                tip = 0x42 in got
                if 0x47 in got:
                    self._conf_devs.add(hdr.hDevice)
                conf = (0x47 in got) or hdr.hDevice not in self._conf_devs
                if (hid.HidP_GetUsageValue(0, 0x0D, link, 0x51, ctypes.byref(val), prep, rep,
                                           size_hid) & 0xFFFFFFFF) != ok:
                    continue
                cid = val.value
                if (hid.HidP_GetUsageValue(0, 0x01, link, 0x30, ctypes.byref(val), prep, rep,
                                           size_hid) & 0xFFFFFFFF) != ok:
                    continue
                x = val.value
                if (hid.HidP_GetUsageValue(0, 0x01, link, 0x31, ctypes.byref(val), prep, rep,
                                           size_hid) & 0xFFFFFFFF) != ok:
                    continue
                y = val.value
                if tip:
                    g["rng"] = max(1, xmax - xmin)
                    pt = g["pts"].get(cid)
                    if pt is None:
                        g["pts"][cid] = [x, y, x, y]
                    else:
                        pt[2], pt[3] = x, y
                if emit:
                    sx, sy = self.to_screen(x, y, xmin, xmax, ymin, ymax)
                    self._frame[cid] = (tip, sx, sy, conf)
                else:
                    self._frame[cid] = (tip, 0, 0, conf)
            if self._expect and len(self._frame) >= self._expect:
                if emit:
                    self._commit_frame(now)
                else:
                    self._frame = {}
                    self._expect = 0

    @staticmethod
    def moved_fraction(g):
        """Largest finger travel in a raw touch, as a fraction of the screen width."""
        best = 0.0
        for x0, y0, x, y in g["pts"].values():
            best = max(best, math.hypot(x - x0, y - y0) / float(g["rng"]))
        return best


class POINTER_DEVICE_INFO(ctypes.Structure):
    _fields_ = [("displayOrientation", wintypes.DWORD), ("device", wintypes.HANDLE),
                ("pointerDeviceType", ctypes.c_int), ("monitor", wintypes.HANDLE),
                ("startingCursorId", wintypes.ULONG), ("maxActiveContacts", wintypes.USHORT),
                ("productString", wintypes.WCHAR * 520)]


def touch_display_rect():
    """Screen rectangle (pixels) the touchscreen covers, plus its rotation, or None."""
    try:
        u = user32
        u.GetPointerDevices.argtypes = [ctypes.POINTER(wintypes.UINT), ctypes.c_void_p]
        u.GetPointerDevices.restype = wintypes.BOOL
        u.GetPointerDeviceRects.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.RECT),
                                            ctypes.POINTER(wintypes.RECT)]
        u.GetPointerDeviceRects.restype = wintypes.BOOL
        n = wintypes.UINT(0)
        if not u.GetPointerDevices(ctypes.byref(n), None) or not n.value:
            return None
        devs = (POINTER_DEVICE_INFO * n.value)()
        if not u.GetPointerDevices(ctypes.byref(n), devs):
            return None
        for d in devs[:n.value]:
            if d.pointerDeviceType == 3:          # POINTER_DEVICE_TYPE_TOUCH
                dev_rc, disp_rc = wintypes.RECT(), wintypes.RECT()
                if u.GetPointerDeviceRects(d.device, ctypes.byref(dev_rc), ctypes.byref(disp_rc)):
                    if disp_rc.right > disp_rc.left and disp_rc.bottom > disp_rc.top:
                        return (disp_rc.left, disp_rc.top, disp_rc.right, disp_rc.bottom,
                                int(d.displayOrientation))
    except Exception:
        pass
    return None


def touch_px_per_cm():
    """Screen pixels per centimetre of the touchscreen (from its physical size)."""
    try:
        u = user32
        n = wintypes.UINT(0)
        if u.GetPointerDevices(ctypes.byref(n), None) and n.value:
            devs = (POINTER_DEVICE_INFO * n.value)()
            if u.GetPointerDevices(ctypes.byref(n), devs):
                for d in sorted(devs[:n.value], key=lambda d: d.pointerDeviceType != 3):
                    if d.pointerDeviceType == 3:        # touchscreen (pen digitisers span all monitors)
                        dev_rc, disp_rc = wintypes.RECT(), wintypes.RECT()
                        if u.GetPointerDeviceRects(d.device, ctypes.byref(dev_rc),
                                                   ctypes.byref(disp_rc)):
                            cm = (dev_rc.right - dev_rc.left) / 1000.0   # himetric = 0.01 mm
                            px = disp_rc.right - disp_rc.left
                            if cm > 5 and px > 0:
                                return px / cm
    except Exception:
        pass
    try:
        return user32.GetDpiForSystem() / 2.54
    except Exception:
        return 40.0


def _blender_main_hwnd():
    """Top-level visible window of this (Blender's UI) thread."""
    found = []
    proc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def cb(hwnd, _lp):
        if user32.IsWindowVisible(hwnd):
            found.append(hwnd)
        return True
    user32.EnumThreadWindows(_GetCurrentThreadId(), proc(cb), 0)
    return found[0] if found else None

# ---------------------------------------------------------------------------
# WH_GETMESSAGE hook: reads WM_POINTER touch input, drops touch-synthesised mouse buttons
# ---------------------------------------------------------------------------
# Touch pointer messages are read by the hook and then hidden from Blender. Otherwise
# Windows' default handling turns every touch into mouse input and moves the system
# cursor to the finger - which fights the add-on's own cursor (number drags jitter).
HIDE_TOUCH_POINTERS = True


class TouchHook:
    # Mouse messages Windows synthesises from touch. WM_MOUSEMOVE (0x0200) is included:
    # every new touch would otherwise teleport Blender's cursor to the finger, which
    # makes a running G/R/S jump. The gesture engine moves the cursor itself.
    _SYNTH_BUTTONS = frozenset({0x0200, 0x0201, 0x0202, 0x0203, 0x0204, 0x0205,
                                0x0206, 0x0207, 0x0208, 0x0209})

    def __init__(self):
        self.installed = False
        self.hook_handle = None
        self.events = []
        self.down_ids = set()
        self.max_lag_ms = [0]   # worst Windows->Blender delivery delay since last read
        self.raw = RawTouchCounter()
        # Pen state for palm rejection: in range (hover or contact), position, times.
        self.pen = {"in": False, "x": 0, "y": 0, "enter_t": 0.0, "left_t": 0.0, "t": 0.0,
                    "seen": False, "src": ""}
        self.ptypes = {}          # pointer type -> messages seen (diagnostic)
        # Pie / menu keys (from Blender's keymap) and when one last opened a menu. In
        # sculpt/paint modes a viewport tap only clicks while a menu is probably open.
        self.menu_vks = set()
        self.menu = {"t": 0.0, "down": {}}
        # Real mouse / pen-cursor movement (not ours): ends touch mode.
        self.mouse = {"dist": 0.0, "last": None}
        self.hook_err = [None]    # first exception inside the hook (diagnostic)
        self._proc = None

    def install(self):
        if not TOUCH_API_AVAILABLE:
            return False
        if self.installed:
            return True

        synth = self._SYNTH_BUTTONS
        down_ids = self.down_ids
        events = self.events
        lagbox = self.max_lag_ms
        raw = self.raw
        pen = self.pen
        ptypes = self.ptypes
        hook_err = self.hook_err
        menu = self.menu
        menu_vks = self.menu_vks
        mouse = self.mouse
        cur = POINT()

        def pen_note(inr, src, x=None, y=None):
            # The pen is seen three ways: Wintab messages (Blender's Tablet API = Wintab or
            # Automatic with a Wintab driver), pen-tagged mouse messages, or WM_POINTER pen
            # messages (Windows Ink). Without a position, the cursor is where the pen is.
            now = time.perf_counter()
            if x is None:
                if pen["src"] == "pointer" and now - pen["t"] < 1.0:
                    return                    # exact pointer reports are flowing: use those
                if down_ids:
                    # Touch navigation moves the cursor: it isn't the pen's position now.
                    x, y = pen["x"], pen["y"]
                elif user32.GetCursorPos(ctypes.byref(cur)):
                    x, y = cur.x, cur.y
                else:
                    return
            if inr and not pen["in"]:
                pen["enter_t"] = now
            elif pen["in"] and not inr:
                pen["left_t"] = now
            pen["in"] = inr
            pen["x"], pen["y"] = x, y
            pen["t"] = now
            if not pen["seen"] or pen["src"] != src:
                ptypes[src] = ptypes.get(src, 0)
            ptypes[src] = ptypes.get(src, 0) + 1
            pen["seen"] = True
            pen["src"] = src
        raw.events = events
        raw.down_ids = down_ids
        raw.fell_back = False
        tick = ctypes.windll.kernel32.GetTickCount

        def hook_proc(nCode, wParam, lParam):
            try:
                if nCode >= 0 and wParam == PM_REMOVE:
                    msg = ctypes.cast(lParam, ctypes.POINTER(MSG)).contents
                    m = msg.message
                    if m in (0x0100, 0x0104):                       # WM_KEYDOWN / SYSKEYDOWN
                        vk = msg.wParam & 0xFF
                        if vk in (0x1B, 0x0D):                     # Esc / Enter close menus
                            menu["t"] = 0.0
                        elif (not (msg.lParam & 0x40000000)                 # not a repeat
                              and _menu_key_match(menu_vks, vk)):
                            now = time.perf_counter()
                            menu["t"] = now
                            menu["down"][vk] = now
                    elif m in (0x0101, 0x0105):                     # WM_KEYUP / SYSKEYUP
                        t0 = menu["down"].pop(msg.wParam & 0xFF, None)
                        if t0 is not None and time.perf_counter() - t0 > 0.3:
                            menu["t"] = 0.0    # pie held and released: it picked and closed
                    if m in synth:
                        extra = user32.GetMessageExtraInfo() & 0xFFFFFFFF
                        touch_made = ((extra & SIGNATURE_MASK) == MI_WP_SIGNATURE
                                      and (extra & SIGNATURE_TOUCH_BIT))
                        if m in (0x0201, 0x0204, 0x0207) and not touch_made:
                            menu["t"] = 0.0    # pen / mouse click: closes or uses the menu
                        if m == 0x0200 and not touch_made:
                            px, py = msg.pt.x, msg.pt.y
                            inj = _LAST_INJECT[0]
                            if inj is None or abs(px - inj[0]) > 2 or abs(py - inj[1]) > 2:
                                last = mouse["last"]
                                if last is not None:
                                    mouse["dist"] += abs(px - last[0]) + abs(py - last[1])
                                mouse["last"] = (px, py)
                        from_touch = ((extra & SIGNATURE_MASK) == MI_WP_SIGNATURE and
                                      (extra & SIGNATURE_TOUCH_BIT))
                        # Touch clicks are re-created by the gesture engine; pen and
                        # real mouse input (no touch signature) pass straight through.
                        if from_touch:
                            msg.message = 0x0000  # WM_NULL
                        elif (extra & SIGNATURE_MASK) == MI_WP_SIGNATURE:
                            pen_note(True, "mouse")          # mouse message made by the pen
                    elif m in (0x7FF0, 0x7FF5, 0x7FF8):
                        # Wintab: WT_PACKET (0x7FF0), WT_PROXIMITY (0x7FF5: LOWORD(lParam)
                        # non-zero = pen entering, zero = leaving), others = pen activity.
                        if m == 0x7FF5:
                            pen_note(bool(msg.lParam & 0xFFFF), "wintab")
                        else:
                            pen_note(True, "wintab")
                    elif m == RawTouchCounter.WM_INPUT and raw.ok:
                        # RIM_INPUT (0) = Blender is the foreground app; RIM_INPUTSINK (1) =
                        # some other app is: never act on those touches.
                        raw.handle(msg.lParam, (msg.wParam & 0xFF) == 0)
                    elif m in (WM_POINTERDOWN, WM_POINTERUPDATE, WM_POINTERUP,
                               WM_POINTERENTER, WM_POINTERLEAVE):
                        pid = msg.wParam & 0xFFFF
                        ptype = wintypes.DWORD()
                        got_type = user32.GetPointerType(pid, ctypes.byref(ptype))
                        k = ptype.value if got_type else -1
                        ptypes[k] = ptypes.get(k, 0) + 1
                        if got_type and ptype.value == PT_PEN:
                            # Only watched (palm rejection), never consumed: Blender gets the pen.
                            info = POINTER_INFO()
                            if user32.GetPointerInfo(pid, ctypes.byref(info)):
                                fl = info.pointerFlags
                                inr = (m != WM_POINTERLEAVE and
                                       bool(fl & (POINTER_FLAG_INRANGE | POINTER_FLAG_INCONTACT)))
                                pen_note(inr, "pointer", info.ptPixelLocation.x,
                                         info.ptPixelLocation.y)
                        elif got_type and ptype.value == PT_TOUCH:
                            info = POINTER_INFO()
                            if user32.GetPointerInfo(pid, ctypes.byref(info)):
                                flags = info.pointerFlags
                                lag = (tick() - info.dwTime) & 0xFFFFFFFF
                                if not raw.emit and lagbox[0] < lag < 600000:
                                    lagbox[0] = lag
                                is_down = m == WM_POINTERDOWN or bool(flags & POINTER_FLAG_DOWN)
                                is_up = m in (WM_POINTERUP, WM_POINTERLEAVE) or bool(flags & POINTER_FLAG_UP)
                                if raw.emit:
                                    landing = is_down and not is_up
                                    if landing and time.perf_counter() - raw.last_t > 0.5:
                                        # Windows has a finger but the raw stream is silent:
                                        # this screen's raw data isn't usable. Fall back.
                                        raw.emit = False
                                        raw.fell_back = True
                                        raw.cancel_all()
                                    else:
                                        if landing:
                                            raw.note_pointer_down(info.ptPixelLocation.x,
                                                                  info.ptPixelLocation.y)
                                        if HIDE_TOUCH_POINTERS:
                                            msg.message = 0x0000   # see HIDE_TOUCH_POINTERS
                                        return user32.CallNextHookEx(self.hook_handle, nCode,
                                                                     wParam, lParam)
                                if is_up:
                                    down_ids.discard(pid)
                                elif is_down:
                                    down_ids.add(pid)
                                events.append((pid, info.ptPixelLocation.x, info.ptPixelLocation.y,
                                               is_down and not is_up, is_up,
                                               bool(flags & POINTER_FLAG_CANCELED),
                                               time.perf_counter()))
                                if len(events) > 2048:
                                    del events[:1024]
                                if HIDE_TOUCH_POINTERS:
                                    msg.message = 0x0000
            except Exception:
                if hook_err[0] is None:
                    import traceback
                    hook_err[0] = traceback.format_exc()
            return user32.CallNextHookEx(self.hook_handle, nCode, wParam, lParam)

        self._proc = HOOKPROC(hook_proc)
        self.hook_handle = user32.SetWindowsHookExW(WH_GETMESSAGE, self._proc, None, _GetCurrentThreadId())
        if not self.hook_handle:
            print(f"[Multitouch] SetWindowsHookExW failed, error {ctypes.get_last_error()}")
            self._proc = None
            return False
        self.installed = True
        try:
            hwnd = _blender_main_hwnd()
            if self.raw.register(hwnd):
                _log_info("raw touch input registered (window %s)" % hwnd)
            else:
                _log_info("raw touch input unavailable (error %d)" % ctypes.get_last_error())
        except Exception:
            import traceback
            _log_error("raw input register:\n" + traceback.format_exc())
        print("[Multitouch] Hook installed.")
        return True

    def uninstall(self):
        try:
            self.raw.unregister()
        except Exception:
            pass
        if self.installed and self.hook_handle:
            user32.UnhookWindowsHookEx(self.hook_handle)
        self.hook_handle = None
        self._proc = None
        self.installed = False
        self.events.clear()
        self.down_ids.clear()

    def drain(self):
        if not self.events:
            return []
        ev = self.events[:]
        del self.events[:len(ev)]
        return ev


# ---------------------------------------------------------------------------
# Operator
# ---------------------------------------------------------------------------
_TAP_ACTIONS = [
    ('NONE', "Nothing", ""),
    ('UNDO', "Undo", "Undo the last action"),
    ('REDO', "Redo", "Redo the last undone action"),
    ('FOCUS', "Focus Mode", "Toggle fullscreen area with all panels hidden"),
    ('MAXIMIZE', "Maximize Area", "Toggle maximized area"),
]


class _Controller:
    """The thing a running gesture is driving (one per gesture)."""
    __slots__ = ("kind", "window", "area", "region", "rv3d", "button", "wheel", "wheel_x", "last_xy",
                 "anchor", "start", "acc", "decided", "keys", "fxy")

    def __init__(self, kind, area=None, region=None, rv3d=None, window=None):
        self.kind = kind
        self.window = window
        self.area = area
        self.region = region
        self.rv3d = rv3d
        self.button = None
        self.wheel = 0.0
        self.wheel_x = 0.0
        self.last_xy = None
        self.anchor = None   # world point pinned under the fingers (2-finger 3D View)
        self.start = None    # touch-down point (number-field drags press there)
        self.acc = None      # movement held back until a scroll's direction is known
        self.decided = True
        self.keys = []       # modifier keys held for this gesture
        self.fxy = None      # first finger's last position (number drags follow only it)


# Main regions that aren't View2D: they still scroll with the mouse wheel (which is
# safe there, since the wheel doesn't zoom them).
_NON_VIEW2D_SPACES = {'TEXT_EDITOR', 'CONSOLE'}


if bpy is not None:

    class VIEW3D_OT_multitouch_navigate(bpy.types.Operator):
        """Touch navigation modelled on Blender's Android demo build"""
        bl_idname = "view3d.multitouch_navigate"
        bl_label = "Multitouch Navigate"
        bl_options = {'REGISTER'}

        @classmethod
        def poll(cls, context):
            return IS_WINDOWS and TOUCH_API_AVAILABLE

        # -- lifecycle --------------------------------------------------------
        def invoke(self, context, event):
            if not (IS_WINDOWS and TOUCH_API_AVAILABLE):
                self.report({'ERROR'}, "Multitouch currently supports Windows only.")
                return {'CANCELLED'}
            _LOG_ENABLED[0] = bool(context.scene.multitouch_props.diag_log)
            _log_info("INVOKE requested (already running: %s)" % (_RUNNING[0] is not None))
            if _RUNNING[0] is not None:
                self.report({'INFO'}, "Multitouch is already running.")
                return {'CANCELLED'}
            _STOP[0] = False
            self._hook = TouchHook()
            if not self._hook.install():
                _log_error("touch hook install failed")
                self.report({'ERROR'}, "Failed to install touch hook.")
                return {'CANCELLED'}
            self._engine = TouchEngine()
            self._hook.menu_vks.clear()
            self._hook.menu_vks.update(_menu_key_vks(context))
            self._palm = PalmFilter()
            self._tui_last_touch = 0.0
            self._anchor = None      # what the finger touched while the UI grows
            self._pen_reported = False
            self._px_per_cm = touch_px_per_cm()
            _log_info("touchscreen: %.1f px per cm" % self._px_per_cm)
            raw = self._hook.raw
            self._raw_fallback_reported = False
            if context.scene.multitouch_props.touch_source == 'RAW' and raw.ok:
                raw.display = touch_display_rect()
                raw.emit = raw.display is not None
                _log_info("touch input: %s (touchscreen covers %s)"
                          % ("RAW" if raw.emit else "WINDOWS, no touchscreen rect", raw.display))
            else:
                _log_info("touch input: WINDOWS (raw available: %s)" % raw.ok)
            self._ctl = None
            self._hold_pick = None   # (pick dict, target) after a 'press' on something grabbable
            self._ui_hold = None     # press-and-hold on a panel field (held mouse button)
            self._hold_sel = False
            self._hold_edit = None
            self._rest_cursor = get_cursor()   # where Blender thinks the cursor is
            self._restore_cursor = None
            self._touching = False
            self._raw_block = False      # 3+ fingers on the glass: ignore pointer gestures
            self._raw_last = None        # (finished raw touch, perf_counter when finished)
            self._pending_tap = None     # (tap action, perf_counter) waiting for the raw count
            self._last_touch = None
            wm = context.window_manager
            self._timer = wm.event_timer_add(1.0 / 120.0, window=context.window)
            wm.modal_handler_add(self)
            _RUNNING[0] = self
            _log_info("START threshold=%.1fpx, blender %s" % (self._engine.threshold, bpy.app.version_string))
            self.report({'INFO'}, "Multitouch active (stop it from the Touch panel).")
            return {'RUNNING_MODAL'}

        def finish(self, context):
            _log_info("FINISH")
            _RUNNING[0] = None
            _STOP[0] = False
            self._end_controller()
            self._hold_reset()
            if self._timer is not None:
                context.window_manager.event_timer_remove(self._timer)
                self._timer = None
            if self._hook is not None:
                self._hook.uninstall()
                self._hook = None
            self.report({'INFO'}, "Multitouch navigation stopped.")
            return {'CANCELLED'}

        def modal(self, context, event):
            try:
                return self._modal(context, event)
            except Exception:
                import traceback
                _log_error("modal crashed:\n" + traceback.format_exc())
                return {'PASS_THROUGH'}

        def _modal(self, context, event):
            if _RUNNING[0] is not self:
                return {'CANCELLED'}   # a stopped/replaced instance: leave quietly
            if _STOP[0]:
                return self.finish(context)
            if event.type != 'TIMER':
                return {'PASS_THROUGH'}
            if _PENDING_REPORT[0]:
                self.report({'INFO'}, _PENDING_REPORT[0])
                _PENDING_REPORT[0] = None

            eng = self._engine
            t_tick = time.perf_counter()
            raw = self._hook.raw
            if raw.emit and raw.pending_up:
                raw.flush(t_tick)
            if raw.emit and raw.active and t_tick - raw.last_t > 0.3:
                raw.cancel_all(t_tick)   # reports stopped without a lift: don't leave fingers stuck
            if raw.fell_back and not self._raw_fallback_reported:
                self._raw_fallback_reported = True
                _log_info("raw touch data stopped arriving: switched to Windows touch input")
                self.report({'WARNING'}, "Multitouch: raw touch input not usable, using Windows input")
            events = self._hook.drain()
            if events:
                self._last_touch = (events[-1][1], events[-1][2])
            self._raw_update(context)
            try:
                for w in context.window_manager.windows:
                    for a in w.screen.areas:
                        if a.type == 'VIEW_3D':
                            r3 = a.spaces.active.region_3d
                            if r3 is not None:
                                track_auto_persp(r3)
            except Exception:
                pass
            sys_lag = self._hook.max_lag_ms[0]
            if sys_lag:
                self._hook.max_lag_ms[0] = 0
                if sys_lag > 60:
                    _log_info("HITCH: Blender picked up a touch %d ms after it happened "
                              "(Blender's main loop was busy)" % sys_lag)
            if events:
                # Hitch diagnostics (always on, cheap): how late did Blender hand us this
                # touch, and how long did we take with it?
                late = (t_tick - events[0][6]) * 1000.0
                if late > 60.0:
                    _log_info("HITCH: touch reached the add-on %.0f ms late (%d events queued)"
                              % (late, len(events)))
            if events or self._hook.down_ids:
                if not self._touching and self._rest_cursor is not None:
                    _LAST_INJECT[0] = self._rest_cursor   # cursor as of the last idle tick
                self._touching = True
            else:
                if self._touching:
                    # Touch just ended. Windows moved the (hidden) system cursor to the
                    # finger, but Blender never saw that (synthesised moves are dropped):
                    # put it back where Blender last saw it.
                    self._touching = False
                    back = self._restore_cursor or _LAST_INJECT[0]
                    self._restore_cursor = None
                    if back is not None:
                        user32.SetCursorPos(int(back[0]), int(back[1]))
                # Otherwise only a real mouse moves it: that's where Blender's cursor is.
                self._rest_cursor = get_cursor() or self._rest_cursor
            props = context.scene.multitouch_props
            pen = self._hook.pen
            if pen["in"] and t_tick - pen["t"] > (0.5 if pen["src"] == "mouse" else 2.0):
                # No pen report for a while: assume it left. (Pen-made mouse moves have no
                # 'left' message, so that source times out sooner.)
                pen["in"] = False
                pen["left_t"] = pen["t"]
            if props.palm_reject:
                pf = self._palm
                if pen["seen"] and not self._pen_reported:
                    self._pen_reported = True
                    _log_info("palm rejection: pen detected (via %s)" % pen["src"])
                events = pf.filter(events, pen, self._hook.raw.palms, props.palm_zone,
                                   props.palm_gap_cm * self._px_per_cm,
                                   props.palm_grace_ms / 1000.0, t_tick)
                if pf.count:
                    _log_info("palm rejection: rejected %d touch(es) via %s: %s"
                              % (pf.count, pen["src"], "; ".join(pf.details)))
                    pf.count = 0
                    pf.details = []
                if pf.allowed:
                    _log_info("palm rejection: allowed other-hand touch(es): " + "; ".join(pf.allowed))
                    pf.allowed = []
            elif self._hook.raw.palms:
                self._hook.raw.palms.clear()
            if events:
                self._tui_last_touch = t_tick
                self._hook.mouse["dist"] = 0.0
                self._hook.mouse["last"] = None
            for pid, x, y, is_down, is_up, canceled, t in events:
                if is_down:
                    if not eng.touches:
                        self._tui_touch_down(context, x, y)
                    self._apply_all(context, eng.down(pid, x, y, t))
                elif is_up:
                    self._apply_all(context, eng.up(pid, x, y, t, canceled))
                else:
                    self._apply_all(context, eng.move(pid, x, y, t))
            self._apply_all(context, eng.tick(time.perf_counter()))
            if (self._ctl is not None and self._ctl.kind == 'NUMDRAG'
                    and not eng.touches):
                self._end_controller()   # never leave the button or Ctrl held
            # Touch mode
            if not eng.touches:
                quiet = t_tick - self._tui_last_touch
                if (_TUI["active"] and quiet >= TUI_EXIT_QUIET_S
                        and (self._hook.mouse["dist"] > TUI_EXIT_MOUSE_PX or pen["in"])):
                    tui_exit()
            tui_tick(t_tick)
            self._anchor_tick(context, t_tick)
            _flush_log()
            spent = (time.perf_counter() - t_tick) * 1000.0
            if spent > 30.0:
                _log_info("HITCH: add-on took %.0f ms for %d touch events" % (spent, len(events)))
            return {'PASS_THROUGH'}

        def _apply_all(self, context, actions):
            raw = self._hook.raw
            if raw.ok and raw.last_t and not raw.emit:
                kept = []
                for a in actions:
                    k = a[0]
                    if self._raw_block:
                        continue        # Windows' 3/4-finger gesture: Blender stays out of it
                    if k in ('tap', 'multi_tap'):
                        # Wait until the raw count says how many fingers there really were
                        # (Windows may have kept some of them).
                        self._pending_tap = (a, time.perf_counter())
                        continue
                    kept.append(a)
                actions = kept
            if True:
                for a in actions:
                    try:
                        self._apply(context, a)
                    except ReferenceError:
                        self._ctl = None  # area/region freed by a layout change
                    except Exception:
                        import traceback
                        _log_error("applying %r:\n%s" % (a, traceback.format_exc()))

        # -- raw finger count ------------------------------------------------------
        _RAW_IDLE_S = 0.07         # no report for this long = all fingers lifted
        _RAW_TAP_MAX_S = 0.6       # a multi-finger tap is shorter than this...
        _RAW_TAP_MAX_MOVE = 0.03   # ...and no finger travels more than 3% of the screen

        def _raw_update(self, context):
            raw = self._hook.raw
            if not raw.ok:
                return
            now = time.perf_counter()
            g = raw.gesture
            if raw.emit:
                if g is not None and now - g["t_last"] > self._RAW_IDLE_S:
                    raw.gesture = None
                    if raw.bounces[0]:
                        _log_info("RAW: filtered %d contact bounce(s)" % raw.bounces[0])
                        raw.bounces[0] = 0
                    if raw.cal_n[0]:
                        raw.cal_err[0] = 0.0   # mapping verified (0-1 px); no longer logged
                        raw.cal_n[0] = 0
                return
            if g is not None and g["maxc"] >= 3 and not self._raw_block:
                # 3+ fingers: whatever Windows passes on is part of its gesture (or ours).
                self._raw_block = True
                _log("RAW: %d fingers, blocking pointer gestures" % g["maxc"])
                if self._hold_edit is not None:
                    self._hold_edit.cancel()
                    self._hold_header(None)
                if self._hold_pick is not None:
                    self._hold_reset()
                self._end_controller()
                self._pending_tap = None
            if g is not None and now - g["t_last"] > self._RAW_IDLE_S:
                raw.gesture = None
                self._raw_last = (g, now)
                n = g["maxc"]
                dur = g["t_last"] - g["t0"]
                mv = RawTouchCounter.moved_fraction(g)
                _log("RAW: touch done, %d fingers, %.0f ms, moved %.3f" % (n, dur * 1000.0, mv))
                if n >= 3 and dur <= self._RAW_TAP_MAX_S and mv <= self._RAW_TAP_MAX_MOVE:
                    self._pending_tap = None
                    x, y = self._last_touch or (0, 0)
                    self._apply(context, ('multi_tap', min(n, 4), x, y))
            if self._raw_block and raw.gesture is None and not self._hook.down_ids:
                if self._engine.mode == NONE or now - raw.last_t > 1.0:
                    self._engine.reset()
                    self._raw_block = False
                    self._pending_tap = None
            pend = self._pending_tap
            if pend is not None and raw.gesture is None and now - raw.last_t > self._RAW_IDLE_S:
                self._pending_tap = None
                last = self._raw_last
                if last is not None and last[0]["maxc"] >= 3 and abs(last[1] - pend[1]) < 0.6:
                    return   # it was a 3/4-finger touch, already handled from the raw data
                self._apply(context, pend[0])
            elif pend is not None and now - pend[1] > 1.0:
                self._pending_tap = None
                self._apply(context, pend[0])   # raw data never came: don't lose the tap

        # -- press-and-hold editing ---------------------------------------------
        def _hold_reset(self):
            self._ui_hold_release()
            self._hold_pick = None
            self._hold_sel = False
            self._hold_edit = None
            if _AXIS_LINE[0] is not None:
                axis_line_hide()
                _redraw_3d_views()
            if _THEME_BACKUP[0] is not None:
                theme_restore()
                _redraw_3d_views()

        # -- helpers: follow one finger, never the centroid ---------------------------
        def _primary_xy(self):
            """Position of the finger that started the gesture (the first one down)."""
            for tp in self._engine.touches.values():
                return (tp[0], tp[1])
            return None

        def _is_snap_finger(self):
            """A second touch counts as the Ctrl (snap) finger unless it is where the
            dragging hand's palm / knuckles land: just below the finger."""
            pts = [(tp[0], tp[1]) for tp in self._engine.touches.values()]
            if len(pts) < 2:
                return False
            (fx, fy), (sx, sy) = pts[0], pts[1]
            cm = getattr(self, "_px_per_cm", 40.0)
            below = sy > fy + 0.5 * cm and abs(sx - fx) < 8.0 * cm
            return not below

        # Press-and-hold on a panel = a held mouse button: drag up/down over other
        # fields to edit several at once (Blender's multi-value drag), then sideways.
        # -- touch mode: grow at touch-down, keep the touched thing under the finger --
        def _tui_touch_down(self, context, x, y):
            """First finger down. On the interface: grow it now. On the viewport (or
            another editor's canvas): back to the normal size."""
            self._anchor = None
            ap = _tui_prefs()
            if ap is None or not ap.touch_ui:
                return
            if popup_under(context, x, y) is not None:
                return      # on an open menu / search popup: leave the UI size alone
            target = find_target(context, x, y)
            if target is None:
                if window_under(context, x, y) is not None:   # top bar / status bar
                    tui_enter()
                return
            window, area, region, (rx, ry), _l = target
            if region.type == 'WINDOW' and area.type in _TUI_CANVAS:
                if _TUI["active"]:
                    tui_exit()
                return
            if _TUI["active"]:
                return
            try:
                vx, vy = region.view2d.region_to_view(rx, ry)
                sys0 = context.preferences.system.ui_scale
            except Exception:
                tui_enter()
                return
            self._anchor = {"win": window, "area": area, "region": region,
                            "v": (vx, vy), "sys0": sys0, "f": (float(x), float(y)),
                            "off": (0.0, 0.0), "settled": None, "tap": None,
                            "pan": region.type in _TUI_PAN_REGIONS}
            tui_enter()

        def _anchor_screen(self, context):
            """Where the touched content is on screen now (layout as last drawn)."""
            an = self._anchor
            region, win = an["region"], an["win"]
            k = context.preferences.system.ui_scale / an["sys0"]
            vx, vy = an["v"]
            rx, ry = region.view2d.view_to_region(vx * k, vy * k, clip=False)
            lx, ly = region.x + rx, region.y + ry
            return (lx + win.x, _desktop_height() - win.y - ly), (rx, ry)

        def _anchor_tick(self, context, now):
            an = self._anchor
            if an is None:
                return
            try:
                (sx, sy), (rx, ry) = self._anchor_screen(context)
                fx, fy = an["f"]
                busy = _TUI["anim"] is not None or an["settled"] is None
                st = self._ui_hold
                if (an["pan"] and busy and abs(sy - fy) >= 1.0 and self._ctl is None
                        and not (st is not None and st["down"])):
                    # Scroll so the touched field stays under the finger (when the
                    # panel is long enough to scroll; otherwise touches are remapped).
                    with context.temp_override(window=an["win"], area=an["area"],
                                               region=an["region"]):
                        bpy.ops.view2d.pan(deltax=0, deltay=int(round(-(sy - fy))))
                    (sx, sy), (rx, ry) = self._anchor_screen(context)
            except Exception:
                self._anchor = None     # region gone (layout change)
                return
            an["off"] = (sx - fx, sy - fy)
            if _RING["handle"] is not None and _RING["region"] == an["region"].as_pointer():
                if abs(_RING["xy"][0] - rx) + abs(_RING["xy"][1] - ry) >= 1.0:
                    _RING["xy"] = (rx, ry)
                    _ring_progress(_RING["p"])
            if _TUI["anim"] is not None:
                an["settled"] = None
            elif an["settled"] is None:
                an["settled"] = now
            settled = an["settled"] is not None and now - an["settled"] >= TUI_SETTLE_S
            if an["tap"] is not None and settled:
                a = an["tap"]
                an["tap"] = None
                _log_info("touch mode: tap remapped by (%d,%d)" % an["off"])
                self._apply(context, a)
            if settled and an["tap"] is None and not self._engine.touches:
                self._anchor = None

        def _amap(self, x, y):
            """A point of the current touch, moved with the content it touched."""
            an = self._anchor
            if an is None:
                return (x, y)
            return (x + an["off"][0], y + an["off"][1])

        def _ui_hold_release(self):
            st = getattr(self, "_ui_hold", None)
            self._ui_hold = None
            _ring_hide()
            if st is None:
                return
            if st["down"]:
                _log_info("panel hold: released after moving (%d, %d)" % tuple(st["moved"]))
            if st["ctrl"]:
                inject_key('CTRL', False)
            if st["down"]:
                inject_button('LEFT', False, *(get_cursor() or st["xy"]))

        def _ui_hold_event(self, a):
            kind = a[0]
            st = self._ui_hold
            if kind == 'hold_progress':
                _ring_progress(a[1])
                return
            if kind == 'hold':
                st["down"] = True
                st["xy"] = self._amap(a[1], a[2])
                st["fxy"] = self._primary_xy() or (a[1], a[2])
                _ring_progress(1.0)
                inject_button('LEFT', True, *st["xy"])
                _log_info("panel hold: pressed at (%d,%d)" % st["xy"])
            elif not st["down"]:
                return
            elif kind in ('hold_drag', 'hold_two'):
                # Follow the first finger only: a palm or knuckle touching down below it
                # must not move the cursor.
                p = self._primary_xy()
                if p is None:
                    return
                dx, dy = p[0] - st["fxy"][0], p[1] - st["fxy"][1]
                st["fxy"] = p
                if not (dx or dy):
                    return
                cur = get_cursor() or st["xy"]
                st["xy"] = (cur[0] + dx, cur[1] + dy)
                st["moved"][0] += dx
                st["moved"][1] += dy
                inject_move(*st["xy"])
            elif kind == 'hold_two_begin':
                st["fxy"] = self._primary_xy() or st["fxy"]
                if not st["ctrl"] and self._is_snap_finger():
                    inject_key('CTRL', True)            # second finger = Ctrl (snap)
                    st["ctrl"] = True
            elif kind == 'hold_one':
                st["fxy"] = self._primary_xy() or st["fxy"]
                if st["ctrl"]:
                    inject_key('CTRL', False)
                    st["ctrl"] = False
            if kind in ('hold_end', 'hold_cancel'):
                self._hold_reset()

        def _hold_header(self, text):
            try:
                if self._hold_pick is not None:
                    self._hold_pick[1][1].header_text_set(text)
            except Exception:
                pass

        def _apply_hold(self, context, a):
            kind = a[0]
            if kind == 'press':
                self._hold_reset()
                target = find_target(context, a[1], a[2])
                eng = self._engine
                eng.hold_time = HOLD_TIME_S
                eng.hold_preview = HOLD_PREVIEW_S
                eng.hold_slop = 1.0
                if (target is not None and _numdrag_region(target[1], target[2])
                        and not other_modal_running(context)):
                    self._ui_hold = {"down": False, "ctrl": False, "xy": (a[1], a[2]),
                                     "moved": [0.0, 0.0]}
                    self._hold_pick = ('UI', target)
                    eng.hold_allowed = True
                    eng.hold_time = UI_HOLD_TIME_S        # shorter, and wobble-tolerant:
                    eng.hold_preview = 0.08               # fingers never stay perfectly still
                    eng.hold_slop = UI_HOLD_SLOP
                    _ring_show(target, 0.0)
                    return
                if (target is None or context.mode not in _HOLD_MODES
                        or other_modal_running(context)):
                    return
                _w, area, region, (rx, ry), _l = target
                if (area.type != 'VIEW_3D' or region.type != 'WINDOW'
                        or _over_navigation_gizmo(area, region, rx, ry)):
                    return
                pick = hold_pick(context, target)
                if pick is not None:
                    self._hold_pick = (pick, target)
                    self._engine.hold_allowed = True
                return
            if self._hold_pick is None:
                return
            pick, target = self._hold_pick
            if pick == 'UI':
                if self._ui_hold is not None:
                    self._ui_hold_event(a)
                return
            window, area, region = target[0], target[1], target[2]
            if kind in ('hold_progress', 'hold') and not self._hold_sel:
                hold_select(context, pick)
                self._hold_sel = True
            if kind == 'hold_progress':
                theme_grow(a[1])
                _redraw_3d_views()
            elif kind == 'hold':
                theme_grow(1.0)
                _redraw_3d_views()
                he = HoldEdit(context, window, area, region)
                self._hold_edit = he if he.valid else None
                self._hold_header("Touch edit: drag to move | 2nd finger: slide to roll, twist to spin, pinch to scale")
            elif self._hold_edit is not None:
                he = self._hold_edit
                if kind == 'hold_drag':
                    dx, dy, x, y = a[1:]
                    he.move(x - dx, y - dy, x, y)
                elif kind == 'hold_two':
                    he.two(*a[1:])
                if kind in ('hold_drag', 'hold_two') and he.status():
                    self._hold_header(he.status())
                elif kind == 'hold_cancel':
                    he.cancel()
            if kind in ('hold_end', 'hold_cancel'):
                he = self._hold_edit
                self._hold_header(None)
                if he is not None and he.changed and kind == 'hold_end':
                    _push_undo(window, "Touch Transform")
                self._hold_reset()

        # -- controllers --------------------------------------------------------
        def _begin_controller(self, context, kind, target, sx, sy):
            window, area, region, _r, _l = target
            rv3d = None
            if kind == 'VIEW3D':
                if region.type != 'WINDOW':
                    region = next((r for r in area.regions if r.type == 'WINDOW'), region)
                rv3d = area.spaces.active.region_3d
            ctl = _Controller(kind, area, region, rv3d, window)
            if kind == 'MOUSE':
                ctl.button = 'LEFT'
            elif kind == 'PAN2D':
                ctl.button = 'MIDDLE'
            if ctl.button:
                inject_button(ctl.button, True, sx, sy)
            ctl.last_xy = (sx, sy)
            ctl.start = (sx, sy)
            if kind == 'SCROLL' and _numdrag_region(area, region):
                ctl.decided = False      # sideways = drag a number field, up/down = scroll
                ctl.acc = [0.0, 0.0]
            elif kind == 'VIEW3D' and target[2].type == 'UI':
                ctl.decided = False      # N sidebar: sideways = drag a number field, else orbit
                ctl.acc = [0.0, 0.0]
            self._ctl = ctl
            _log("BEGIN %s in %s/%s at (%d,%d)" % (kind, area.type, region.type, sx, sy))

        @staticmethod
        def _surface_under(context, ctl, sx, sy):
            """World point under the screen position: the visible surface if there is
            one, else the point at the view centre's depth."""
            from bpy_extras import view3d_utils as v3u
            region, rv3d = ctl.region, ctl.rv3d
            co = _region_co(ctl.window, region, sx, sy)
            try:
                origin = v3u.region_2d_to_origin_3d(region, rv3d, co)
                direction = v3u.region_2d_to_vector_3d(region, rv3d, co)
                hit, loc, _n, _i, _ob, _m = context.scene.ray_cast(
                    context.evaluated_depsgraph_get(), origin, direction)
                if hit:
                    return loc.copy()
            except Exception:
                pass
            return v3u.region_2d_to_location_3d(region, rv3d, co, rv3d.view_location)

        def _end_controller(self):
            ctl = self._ctl
            self._ctl = None
            if ctl is not None:
                for k in ctl.keys:
                    inject_key(k, False)
                ctl.keys = []
            if ctl is not None and ctl.button and ctl.last_xy:
                if ctl.kind == 'NUMDRAG':
                    inject_button(ctl.button, False, *(get_cursor() or ctl.last_xy))
                    return
                inject_button(ctl.button, False, *ctl.last_xy)
            if ctl is not None:
                _log("END %s" % ctl.kind)
        def _scroll(self, ctl, dy, x, y, dx=0.0):
            # Scroll the region itself (1:1 with the finger, like the demo's touch pan).
            # Never send a mouse wheel to View2D regions: over the empty, transparent part
            # of an overlapping sidebar/toolbar the wheel falls through to the 3D View and
            # zooms it instead.
            if ctl.region is not None and ctl.window is not None and not (
                    ctl.region.type == 'WINDOW' and ctl.area.type in _NON_VIEW2D_SPACES):
                ctl.wheel += dy
                ctl.wheel_x += dx
                iy, ix = int(ctl.wheel), int(ctl.wheel_x)
                if iy or ix:
                    ctl.wheel -= iy
                    ctl.wheel_x -= ix
                    w = ctl.window
                    with bpy.context.temp_override(window=w, screen=w.screen,
                                                   area=ctl.area, region=ctl.region):
                        bpy.ops.view2d.pan(deltax=-ix, deltay=iy)
                    ctl.region.tag_redraw()
                return
            ctl.wheel += dy * (WHEEL_DELTA / SCROLL_PIXELS_PER_NOTCH)
            if abs(ctl.wheel) >= WHEEL_DELTA:
                notches = int(ctl.wheel / WHEEL_DELTA)
                post_mouse_wheel(x, y, notches * WHEEL_DELTA)
                ctl.wheel -= notches * WHEEL_DELTA

        # -- action dispatch ----------------------------------------------------
        def _apply(self, context, a):
            kind = a[0]
            props = context.scene.multitouch_props
            if kind != 'hold_progress':
                _log("ACT %r" % (a,))

            busy = other_modal_running(context)
            if busy and self._hold_edit is not None and kind.startswith('hold'):
                # Can't happen through 'press' (holds are refused while a tool runs),
                # but never edit underneath a running tool: drop the hold.
                self._hold_edit.cancel()
                self._hold_header(None)
                self._hold_reset()
                return
            if kind == 'press' or kind.startswith('hold'):
                self._apply_hold(context, a)
                return
            if self._hold_pick is not None:
                self._hold_reset()   # the press turned into a tap / drag / gesture

            if busy:
                # A Blender tool is running (G/R/S, knife, ...). Touch drives the cursor
                # *relatively* (like a trackpad) so the model moves with the finger and
                # never jumps to where the finger lands. Tap = confirm (left click where
                # the cursor already is), 2-finger tap = cancel (right click).
                # Never move the view, scroll, or run undo/redo underneath it.
                ctl = self._ctl
                rest = self._rest_cursor
                if kind in ('two_begin', 'two', 'two_to_one', 'multi_tap', 'tap', 'drag_begin') \
                        and ctl is not None and ctl.kind in ('VIEW3D', 'SCROLL'):
                    self._end_controller()
                    ctl = None
                if kind == 'multi_tap':
                    if a[1] == 2 and rest is not None:
                        inject_button('RIGHT', True, *rest)
                        inject_button('RIGHT', False, *rest)
                        self._restore_cursor = rest
                        _log("BUSY: 2-finger tap -> cancel")
                    return
                if kind in ('two_begin', 'two', 'two_to_one'):
                    return
                if kind == 'tap':
                    self._end_controller()
                    p = rest if rest is not None else (a[1], a[2])
                    inject_button('LEFT', True, *p)
                    inject_button('LEFT', False, *p)
                    self._restore_cursor = p
                    _log("BUSY: tap -> confirm")
                    return
                if kind == 'drag' and ctl is not None and ctl.kind in ('VIEW3D', 'SCROLL'):
                    # e.g. G pressed in the middle of an orbit: stop orbiting, follow.
                    self._end_controller()
                    ctl = None
                if kind in ('drag_begin', 'drag') and (ctl is None or ctl.kind == 'CURSOR'):
                    if ctl is None:
                        w = window_under(context, a[-2] if kind == 'drag' else a[1],
                                         a[-1] if kind == 'drag' else a[2])
                        ctl = _Controller('CURSOR', window=w)
                        start = rest if rest is not None else (a[1], a[2])
                        ctl.last_xy = (float(start[0]), float(start[1]))
                        self._ctl = ctl
                    if kind == 'drag':
                        vx, vy = ctl.last_xy[0] + a[1], ctl.last_xy[1] + a[2]
                        ctl.last_xy = (vx, vy)
                        inject_move(vx, vy)
                    self._restore_cursor = ctl.last_xy
                    return

            if kind == 'tap':
                self._end_controller()
                an = self._anchor
                if an is not None and (_TUI["anim"] is not None or an["settled"] is None
                                       or time.perf_counter() - an["settled"] < TUI_SETTLE_S):
                    an["tap"] = a          # the UI is still growing: click when it is done
                    return
                x, y = self._amap(a[1], a[2])
                if popup_under(context, x, y) is not None:
                    inject_button('LEFT', True, x, y)     # open menu / F3 search: click it
                    inject_button('LEFT', False, x, y)
                    return
                target = find_target(context, x, y)
                if target is None:
                    # Top bar (workspace tabs, menus) and status bar are global areas.
                    if window_under(context, x, y) is not None:
                        inject_button('LEFT', True, x, y)
                        inject_button('LEFT', False, x, y)
                    return
                _w, area, region, (rx, ry), _l = target
                if (area.type == 'VIEW_3D' and region.type == 'WINDOW'
                        and context.mode in _PAINT_MODES
                        and not _over_navigation_gizmo(area, region, rx, ry)):
                    menu = self._hook.menu
                    if not (menu["t"] and time.perf_counter() - menu["t"] < 120.0):
                        return  # touch never sculpts or paints; that's the pen's job
                    # A pie / menu opened by a key is over the viewport: this tap is for it.
                    menu["t"] = 0.0
                    inject_button('LEFT', True, x, y)
                    inject_button('LEFT', False, x, y)
                    return
                inject_button('LEFT', True, x, y)
                inject_button('LEFT', False, x, y)

            elif kind == 'drag_begin':
                self._end_controller()
                sx, sy = self._amap(a[1], a[2])
                _log_info("drag at (%d,%d), system cursor at %s" % (sx, sy, get_cursor()))
                mw = popup_under(context, sx, sy)
                if mw is not None:
                    ctl = _Controller('MOUSE', window=mw)   # like the mouse on a menu
                    ctl.button = 'LEFT'
                    inject_button('LEFT', True, sx, sy)
                    ctl.last_xy = (sx, sy)
                    self._ctl = ctl
                    return
                target = find_target(context, sx, sy)
                if target is None:
                    w = window_under(context, sx, sy)
                    if w is not None:   # drag in the top bar / status bar: plain mouse drag
                        ctl = _Controller('MOUSE', window=w)
                        ctl.button = 'LEFT'
                        inject_button('LEFT', True, sx, sy)
                        ctl.last_xy = (sx, sy)
                        self._ctl = ctl
                    return
                mode = classify_one_finger(context, target, props.scroll_fingers == 'ONE')
                _w, area, region, (rx, ry), _l = target
                if (mode == 'MOUSE' and area.type == 'VIEW_3D' and region.type == 'WINDOW'
                        and context.mode in _PAINT_MODES
                        and not _over_navigation_gizmo(area, region, rx, ry)):
                    mode = 'VIEW3D'   # never a left-button drag (= a stroke) in paint modes
                self._begin_controller(context, mode, target, sx, sy)

            elif kind == 'drag':
                ctl = self._ctl
                if ctl is None:
                    return
                dx, dy, x, y = a[1:]
                if ctl.kind == 'VIEW3D' and not ctl.decided:
                    ctl.acc[0] += dx
                    ctl.acc[1] += dy
                    ax, ay = ctl.acc
                    if math.hypot(ax, ay) < 8.0 * _ui_scale():
                        return
                    ctl.decided = True
                    if abs(ax) > 1.5 * abs(ay):
                        if self._anchor is not None:
                            ctl.start = self._amap(*self._anchor["f"])
                        numdrag_start(ctl, ax, ay)
                        _log("NUMDRAG (sidebar) at (%d,%d)" % ctl.start)
                        return
                    else:
                        leave_auto_ortho(ctl.rv3d)
                        apply_view3d(ctl.rv3d, ctl.region, 0, 0, 1.0, ax, ay)
                        ctl.area.tag_redraw()
                elif ctl.kind == 'VIEW3D':
                    leave_auto_ortho(ctl.rv3d)
                    apply_view3d(ctl.rv3d, ctl.region, 0, 0, 1.0, dx, dy)
                    ctl.area.tag_redraw()
                elif ctl.kind == 'SCROLL' and not ctl.decided:
                    ctl.acc[0] += dx
                    ctl.acc[1] += dy
                    ax, ay = ctl.acc
                    if math.hypot(ax, ay) < 8.0 * _ui_scale():
                        return
                    ctl.decided = True
                    if abs(ax) > 1.5 * abs(ay):
                        # Sideways: drag the number field under the touch-down point, like
                        # a mouse. A second finger down holds Ctrl (snap).
                        if self._anchor is not None:
                            ctl.start = self._amap(*self._anchor["f"])
                        numdrag_start(ctl, ax, ay)
                        _log("NUMDRAG at (%d,%d)" % ctl.start)
                        return
                    else:
                        self._scroll(ctl, ay, x, y, ax)
                elif ctl.kind == 'SCROLL':
                    self._scroll(ctl, dy, x, y, dx)
                elif ctl.kind == 'NUMDRAG':
                    numdrag_move(ctl, dx, dy)
                    return
                else:  # MOUSE / PAN2D: the held button follows the finger
                    inject_move(*self._amap(x, y))
                ctl.last_xy = (x, y)

            elif kind == 'two_begin':
                ctl = self._ctl
                if ctl is not None and ctl.kind == 'NUMDRAG':
                    ctl.fxy = self._primary_xy()
                    if 'CTRL' not in ctl.keys and self._is_snap_finger():
                        inject_key('CTRL', True)          # second finger = Ctrl (snap)
                        ctl.keys.append('CTRL')
                    return
                self._end_controller()
                cx, cy = a[1], a[2]
                target = find_target(context, cx, cy)
                if target is None:
                    return
                self._begin_controller(context, classify_two_finger(target), target, cx, cy)
                ctl = self._ctl
                if ctl is not None:
                    ctl.decided = True    # number dragging is for one-finger drags only
                if ctl is not None and ctl.kind == 'VIEW3D':
                    ctl.anchor = self._surface_under(context, ctl, cx, cy)

            elif kind == 'two':
                ctl = self._ctl
                if ctl is None:
                    return
                dx, dy, zoom, cx, cy = a[1:]
                if ctl.kind == 'NUMDRAG':
                    # Follow the first finger only (a palm landing must not move it).
                    p = self._primary_xy()
                    if p is not None and ctl.fxy is not None:
                        numdrag_move(ctl, p[0] - ctl.fxy[0], p[1] - ctl.fxy[1])
                    ctl.fxy = p
                    return
                if ctl.kind == 'VIEW3D':
                    zoom = max(zoom, 1e-3) ** props.zoom_speed   # 1.0 = distance follows the pinch 1:1
                    rv3d, region = ctl.rv3d, ctl.region
                    if ctl.anchor is None:
                        apply_view3d(rv3d, region, dx, dy, zoom, 0, 0)
                    else:
                        # Like the phone build: zoom, then slide the view (parallel to the
                        # screen) so the surface point grabbed at touch-down sits exactly
                        # under the fingers again, at its own depth.
                        from bpy_extras import view3d_utils as v3u
                        apply_view3d(rv3d, region, 0, 0, zoom, 0, 0)
                        rv3d.update()
                        co = _region_co(ctl.window, region, cx, cy)
                        under = v3u.region_2d_to_location_3d(region, rv3d, co, ctl.anchor)
                        rv3d.view_location = rv3d.view_location + (ctl.anchor - under)
                        rv3d.update()
                    ctl.area.tag_redraw()
                elif ctl.kind == 'PAN2D':
                    inject_move(cx, cy)   # pinch only zooms in the 3D View (demo behaviour)
                else:
                    self._scroll(ctl, dy, cx, cy, dx)
                ctl.last_xy = (cx, cy)

            elif kind == 'two_to_one':
                ctl = self._ctl
                if ctl is None:
                    return
                x, y = a[1], a[2]
                if ctl.kind == 'NUMDRAG':
                    for k in ctl.keys:
                        inject_key(k, False)
                    ctl.keys = []
                    ctl.fxy = None
                    return        # keep dragging with the remaining finger, no snapping
                if ctl.button:
                    # Re-grab under the remaining finger so the view doesn't jump.
                    inject_button(ctl.button, False, *ctl.last_xy)
                    inject_button(ctl.button, True, x, y)
                ctl.last_xy = (x, y)

            elif kind == 'end':
                ctl = self._ctl
                if (ctl is not None and ctl.kind == 'NUMDRAG'
                        and len(self._engine.touches) == 2):
                    return        # second finger: 'two_begin' follows and adds Ctrl
                self._end_controller()

            elif kind == 'multi_tap':
                self._end_controller()
                n, cx, cy = a[1], a[2], a[3]
                action = {2: props.tap2_action, 3: props.tap3_action, 4: props.tap4_action}.get(n, 'NONE')
                if action != 'NONE':
                    # Never undo from inside the modal handler: defer to a timer.
                    bpy.app.timers.register(
                        lambda: _run_tap_action(action, cx, cy), first_interval=0.0)

    class VIEW3D_OT_multitouch_stop(bpy.types.Operator):
        bl_idname = "view3d.multitouch_stop"
        bl_label = "Stop Multitouch Navigate"

        def execute(self, context):
            _STOP[0] = True
            return {'FINISHED'}

    class MultitouchProps(bpy.types.PropertyGroup):
        diag_log: bpy.props.BoolProperty(
            name="Debug Log",
            description="Write gesture diagnostics to multitouch_debug.log in your home folder",
            default=False,
            update=lambda self, ctx: _LOG_ENABLED.__setitem__(0, self.diag_log),
        )
        scroll_fingers: bpy.props.EnumProperty(
            name="Scroll With",
            description="How lists, panels and sidebars scroll",
            items=[
                ('ONE', "1 or 2 Fingers", "One-finger drag scrolls lists and panels (two fingers also scroll)"),
                ('TWO', "2 Fingers", "Only two fingers scroll; one-finger drag acts like a mouse drag (sliders, box select)"),
            ],
            default='ONE',
        )
        pen_only_paint: bpy.props.BoolProperty(
            name="Pen-Only Painting",
            description="In sculpt/paint/draw modes, touch taps in the viewport don't paint; use the pen",
            default=True,
        )
        touch_source: bpy.props.EnumProperty(
            name="Touch Input",
            description="Where finger input comes from (takes effect when Multitouch is restarted)",
            items=[
                ('RAW', "Raw (faster)", "Read the touchscreen directly: no wait for Windows' "
                                        "gesture recognizer, every finger seen. Falls back to "
                                        "Windows input automatically if the screen's data isn't usable"),
                ('WINDOWS', "Windows", "Use Windows' touch (pointer) messages"),
            ],
            default='RAW',
        )
        palm_reject: bpy.props.BoolProperty(
            name="Palm Rejection",
            description="Ignore touches from the hand holding the pen while the pen is "
                        "near the screen, and touches the screen marks as a palm",
            default=True,
        )
        palm_zone: bpy.props.EnumProperty(
            name="Pen Hand",
            description="Which side of the pen your other hand may still navigate from",
            items=[
                ('RIGHT', "Right-Handed", "Pen in the right hand: touches left of the pen "
                                          "still work, the right side (palm, arm) is ignored"),
                ('LEFT', "Left-Handed", "Pen in the left hand: touches right of the pen "
                                        "still work, the left side is ignored"),
                ('DISTANCE', "Distance", "Any touch farther than the gap from the pen works"),
                ('NONE', "Block All", "No touch at all while the pen is near"),
            ],
            default='RIGHT',
        )
        palm_gap_cm: bpy.props.FloatProperty(
            name="Gap",
            description="How far from the pen tip (cm) a touch must be to count as your "
                        "other hand",
            default=4.0, min=0.0, max=40.0, step=50, precision=1,
        )
        palm_grace_ms: bpy.props.IntProperty(
            name="After Pen Leaves",
            description="Arm timeout: keep rejecting touches on the pen hand's side for this "
                        "long after the pen was last near the screen (ms). Your arm rests on "
                        "the screen between strokes, while the pen is lifted",
            default=200, min=0, max=5000,
        )
        zoom_speed: bpy.props.FloatProperty(
            name="Pinch Zoom Speed",
            description="How strongly a pinch zooms the 3D View (1 = view distance follows "
                        "the finger spread exactly, lower = slower)",
            default=0.75, min=0.1, max=1.5, step=5, precision=2,
        )
        tap2_action: bpy.props.EnumProperty(name="2-Finger Tap", items=_TAP_ACTIONS, default='UNDO')
        tap3_action: bpy.props.EnumProperty(name="3-Finger Tap", items=_TAP_ACTIONS, default='REDO')
        tap4_action: bpy.props.EnumProperty(name="4-Finger Tap", items=_TAP_ACTIONS, default='FOCUS')

    class VIEW3D_PT_multitouch(bpy.types.Panel):
        bl_label = "Multitouch"
        bl_idname = "VIEW3D_PT_multitouch"
        bl_space_type = 'VIEW_3D'
        bl_region_type = 'UI'
        bl_category = "Touch"

        def draw(self, context):
            layout = self.layout
            props = context.scene.multitouch_props
            if not IS_WINDOWS:
                layout.label(text="Windows only.", icon='ERROR')
                return
            if not TOUCH_API_AVAILABLE:
                layout.label(text="Pointer API unavailable.", icon='ERROR')
                return

            col = layout.column(align=True)
            if _RUNNING[0] is not None:
                col.label(text="Active", icon='REC')
                col.operator("view3d.multitouch_stop", icon='X', text="Stop Multitouch")
            else:
                col.operator("view3d.multitouch_navigate", icon='HAND', text="Start Multitouch")

            box = layout.box()
            col = box.column(align=True)
            col.label(text="3D Viewport:")
            col.label(text="  1 finger drag: orbit")
            col.label(text="  2 fingers: pan + pinch zoom")
            col.label(text="  tap: click / select")
            col.label(text="  hold object/vertex: drag moves,")
            col.label(text="    2nd finger twists / pinch scales")
            col.label(text="  sculpt/paint: touch only navigates")
            col.separator()
            col.label(text="2D editors: 1 finger pans")
            col.label(text="Lists & panels: drag scrolls")
            col.label(text="Number fields: drag sideways,")
            col.label(text="  add a 2nd finger to snap (Ctrl)")
            col.label(text="  hold (ring fills), then drag over several")
            col.label(text="  fields to edit them together")
            col.label(text="Timeline top strip: scrub")

            layout.label(text="Multi-Finger Taps:")
            col = layout.column(align=True)
            col.prop(props, "tap2_action", text="2 Fingers")
            col.prop(props, "tap3_action", text="3 Fingers")
            col.prop(props, "tap4_action", text="4 Fingers")

            layout.separator()
            layout.label(text="Touch Input (restart to apply):")
            layout.prop(props, "touch_source", expand=True)
            op = _RUNNING[0]
            if op is not None:
                raw = op._hook.raw
                if raw.emit:
                    layout.label(text="Reading the touchscreen directly", icon='CHECKMARK')
                elif props.touch_source == 'RAW':
                    layout.label(text="Raw unavailable: using Windows", icon='INFO')
            layout.prop(props, "zoom_speed", slider=True)
            layout.prop(props, "scroll_fingers", expand=True)
            layout.prop(props, "pen_only_paint")
            box = layout.box()
            box.prop(props, "palm_reject")
            col = box.column(align=True)
            col.active = props.palm_reject
            col.prop(props, "palm_zone", text="")
            sub = col.column(align=True)
            sub.active = props.palm_zone != 'NONE'
            sub.prop(props, "palm_gap_cm", text="Gap (cm)")
            col.prop(props, "palm_grace_ms", text="After Pen Leaves (ms)")
            ap = _tui_prefs()
            if ap is not None:
                box = layout.box()
                box.prop(ap, "touch_ui")
                sub = box.row()
                sub.active = ap.touch_ui
                sub.prop(ap, "touch_ui_scale", slider=True)
            layout.prop(props, "diag_log")

    classes = (
        MultitouchProps,
        VIEW3D_OT_multitouch_navigate,
        VIEW3D_OT_multitouch_stop,
        VIEW3D_PT_multitouch,
    )


def _force_stop():
    """File loads remove modal handlers without calling finish(): clean up ourselves."""
    _restore_continuous()
    try:
        tui_exit(immediate=True)
    except Exception:
        pass
    op = _RUNNING[0]
    _RUNNING[0] = None
    _STOP[0] = False
    if op is None:
        return
    try:
        op._end_controller()
    except Exception:
        pass
    theme_restore()
    axis_line_hide()
    try:
        if getattr(op, "_timer", None) is not None:
            bpy.context.window_manager.event_timer_remove(op._timer)
            op._timer = None
    except Exception:
        pass
    try:
        if op._hook is not None:
            op._hook.uninstall()
            op._hook = None
    except Exception:
        pass
    _log("FORCE STOP")


if bpy is not None:
    from bpy.app.handlers import persistent

    @persistent
    def _on_load_pre(*_args):
        _force_stop()

    @persistent
    def _on_load_post(*_args):
        _schedule_auto_start()

    class MultitouchAddonPrefs(bpy.types.AddonPreferences):
        bl_idname = __name__

        auto_start: bpy.props.BoolProperty(
            name="Start Automatically",
            description="Start Multitouch when Blender opens and whenever a file is opened",
            default=True,
        )
        touch_ui: bpy.props.BoolProperty(
            name="Bigger UI for Fingers",
            description="When you use touch, the interface grows (and tooltips hide) so "
                        "buttons are easier to hit; the mouse or pen shrinks it back",
            default=True,
            update=lambda self, ctx: (None if self.touch_ui else tui_exit()),
        )
        touch_ui_scale: bpy.props.FloatProperty(
            name="Touch Size",
            description="How much bigger the interface gets in touch mode",
            default=1.3, min=1.05, max=2.0, step=5, precision=2,
        )
        # Internal: the user's own values while touch mode is on (survive auto-save).
        touch_ui_active: bpy.props.BoolProperty(default=False, options={'HIDDEN'})
        base_ui_scale: bpy.props.FloatProperty(default=1.0, options={'HIDDEN'})
        base_tooltips: bpy.props.BoolProperty(default=True, options={'HIDDEN'})

        def draw(self, context):
            self.layout.prop(self, "auto_start")
            row = self.layout.row()
            row.prop(self, "touch_ui")
            sub = row.row()
            sub.active = self.touch_ui
            sub.prop(self, "touch_ui_scale", slider=True)


def _auto_start_enabled():
    try:
        return bool(bpy.context.preferences.addons[__name__].preferences.auto_start)
    except Exception:
        return True


def _auto_start():
    """Timer callback: start the modal operator once a window exists."""
    if _RUNNING[0] is None:
        tui_restore_saved()
    if _RUNNING[0] is not None or not _auto_start_enabled():
        return None
    try:
        wm = bpy.context.window_manager
        win = wm.windows[0] if wm and wm.windows else None
        if win is None or win.screen is None:
            return 0.5                                 # still starting up: try again
        area = next((a for a in win.screen.areas if a.type == 'VIEW_3D'), None) \
            or (win.screen.areas[0] if win.screen.areas else None)
        kw = {"window": win, "screen": win.screen}
        if area is not None:
            kw["area"] = area
            reg = next((r for r in area.regions if r.type == 'WINDOW'), None)
            if reg is not None:
                kw["region"] = reg
        with bpy.context.temp_override(**kw):
            bpy.ops.view3d.multitouch_navigate('INVOKE_DEFAULT')
        _log_info("auto-started")
    except Exception:
        import traceback
        _log_error("auto start:\n" + traceback.format_exc())
    return None


def _schedule_auto_start():
    if bpy is None or not (IS_WINDOWS and TOUCH_API_AVAILABLE):
        return
    if not bpy.app.timers.is_registered(_auto_start):
        bpy.app.timers.register(_auto_start, first_interval=1.0)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)
    bpy.utils.register_class(MultitouchAddonPrefs)
    bpy.types.Scene.multitouch_props = bpy.props.PointerProperty(type=MultitouchProps)
    if _on_load_pre not in bpy.app.handlers.load_pre:
        bpy.app.handlers.load_pre.append(_on_load_pre)
    if _on_load_post not in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.append(_on_load_post)
    _overlap_register()
    _schedule_auto_start()


def unregister():
    if bpy.app.timers.is_registered(_auto_start):
        bpy.app.timers.unregister(_auto_start)
    _force_stop()
    _overlap_unregister()
    if _on_load_pre in bpy.app.handlers.load_pre:
        bpy.app.handlers.load_pre.remove(_on_load_pre)
    if _on_load_post in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.remove(_on_load_post)
    try:
        bpy.utils.unregister_class(MultitouchAddonPrefs)
    except Exception:
        pass
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
    try:
        del bpy.types.Scene.multitouch_props
    except Exception:
        pass


if __name__ == "__main__":
    register()
