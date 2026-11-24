"""fx/page08/_sound_common.py - what the two B13 sound sections of Settings 08 share (SIGNAL OUTPUT and CUE POLICY).

Not a section: discover() skips modules whose name starts with '_', so this file registers nothing; the two section
modules import it. Tk thread only, no timer, no thread, no file I/O.

    CATEGORY_ROWS            the five category rows (prefs key, the word the row shows), in the approved order
    engine(page)             the sound engine the page talks to (fx.sound: the booted engine, or the NullEngine)
    running(eng)             True for a real engine (the NullEngine is "not running")
    sfx_env_off()            'LOCALAI_SFX=0' (as spelled) when that switch keeps the engine off this session, else None
    render_status(...)       the status line's one text ('' = ready: no line), one writer per section
    test_ready(...)          may TEST / an audition play now (master on, volume > 0, an engine that can play)
    read(page, leaf)         a leaf from page.view (the live store or the read-only shadow), else its schema default
    flag(v) / snap(v)        a bool as the lamp variable's '1' / '0'; a volume readout snapped like the store (nearest 5)
    lamp(page, parent, ...)  a Settings lamp (the screen's own _check helper; the kit LampCheck otherwise)
    labeled_scale(...)       the Settings slider row (the screen's own _labeled_scale; a plain stand-in otherwise)
"""

import math
import os
import tkinter as tk

CATEGORY_ROWS = (("nav", "NAVIGATION"), ("interface", "INTERFACE"), ("comms", "TRANSMISSIONS"),
                 ("system", "SYSTEM"), ("alerts", "ALERTS"))

# the status line (AESTHETIC_SPEC ST-03 / GAP_FX 4.1): shown only while the output is not ready
STATUS_STARTING = "STARTING"
STATUS_MUTED = "SOUND OFF · ALT+M"
STATUS_VOLUME_0 = "VOLUME AT 0"
STATUS_NO_DEVICE = "NO AUDIO DEVICE - CUES OFF"
STATUS_ERROR = "AUDIO ERROR - CUES OFF"
STATUS_NOT_RUNNING = "SOUND ENGINE NOT RUNNING - CUES OFF"
STATUS_ENV_OFF = "SOUND SWITCHED OFF FOR THIS SESSION ({}) - CUES OFF"
WARN_STATUSES = (STATUS_NO_DEVICE, STATUS_ERROR, STATUS_NOT_RUNNING)

_SFX_OFF_VALUES = ("0", "false", "off", "no")      # fx.sound.boot's own reading of LOCALAI_SFX


def engine(page):
    """page.fx.sound: the booted engine, or the NullEngine (fx off, LOCALAI_SFX=0, not booted). Never raises."""
    try:
        return getattr(page.fx, "sound", None)
    except Exception:  # noqa: BLE001
        return None


def running(eng):
    """A real engine (Engine, or a test double that says so): the NullEngine's `enabled` is False."""
    try:
        return eng is not None and bool(getattr(eng, "enabled", False))
    except Exception:  # noqa: BLE001
        return False


def engine_state(eng):
    """eng.status(): 'starting' | 'ready' | 'off' | 'no_device' | 'error'; 'off' when it cannot say. Never raises."""
    try:
        fn = getattr(eng, "status", None)
        s = fn() if callable(fn) else "off"
        return s if isinstance(s, str) else "off"
    except Exception:  # noqa: BLE001 - an engine that raises is in error
        return "error"


def sfx_env_off():
    """'LOCALAI_SFX=<value>' when the environment switch keeps the engine off (the same values fx.sound.boot reads)."""
    try:
        v = os.environ.get("LOCALAI_SFX")
        if v is not None and v.strip().lower() in _SFX_OFF_VALUES:
            return "LOCALAI_SFX=%s" % v.strip()
    except Exception:  # noqa: BLE001
        pass
    return None


def render_status(is_running, state, enabled, volume, env_off=None):
    """The status line's text, '' when the output is ready (the approved page shows no line then). Precedence: no
    engine at all, then the user's own switches (master off, volume 0), then what the engine reports."""
    if not is_running:
        return STATUS_ENV_OFF.format(env_off) if env_off else STATUS_NOT_RUNNING
    if not enabled:
        return STATUS_MUTED
    try:
        if volume is None or float(volume) <= 0:
            return STATUS_VOLUME_0
    except (TypeError, ValueError):
        return STATUS_VOLUME_0
    if state == "starting":
        return STATUS_STARTING
    if state == "no_device":
        return STATUS_NO_DEVICE
    if state == "error":
        return STATUS_ERROR
    if state == "off":
        return STATUS_NOT_RUNNING          # master on, volume > 0, yet off: the engine was shut down
    return ""


def test_ready(is_running, state, enabled, volume):
    """TEST (and the acknowledgement auditions) can play: a real engine that is ready or still starting (a test queued
    while it renders plays when it is ready), master on and a volume above 0. Anything else: TEST is disabled, and the
    status line says why (no dead switch, X-14)."""
    try:
        vol_ok = volume is not None and float(volume) > 0
    except (TypeError, ValueError):
        vol_ok = False
    return bool(is_running and enabled and vol_ok and state in ("ready", "starting"))


def read(page, leaf):
    """A leaf's value from page.view (the live store, or the read-only shadow); its schema default when the view has no
    usable value. Never raises."""
    view = getattr(page, "view", None)
    v = None
    if view is not None:
        try:
            v = view.get(leaf)
        except Exception:  # noqa: BLE001
            v = None
    if v is None:
        try:
            v = page.default(leaf)
        except Exception:  # noqa: BLE001
            v = None
    return v


def flag(value):
    """A bool as a lamp variable's value: Tk's checkbutton on / off values are '1' / '0'."""
    return "1" if bool(value) else "0"


def snap(value, fallback=0):
    """A slider position (a number, or the Tcl variable's text '37.5') -> the volume the store would keep: the nearest
    5, ties up, within 0..100 (prefs.snap_volume's rule). `fallback` for anything unreadable."""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return fallback
    if not math.isfinite(v):
        return fallback
    v = min(100.0, max(0.0, v))
    return int(math.floor(v / 5.0 + 0.5)) * 5


def lamp(page, parent, text, variable, command):
    """A Settings lamp (still a tk.Checkbutton; its text exactly `text`): the screen's own _check helper, so it looks and
    wraps like every other Settings lamp; the kit LampCheck when the screen has none. Its variable holds '1' / '0'."""
    helper = getattr(page.screen, "_check", None)
    if callable(helper):
        return helper(parent, text, variable, command=command)
    try:
        from ...kit.settings import LampCheck
        return LampCheck(parent, page.theme, text=text, variable=variable, command=command, ground=page.t["bg"])
    except Exception:  # noqa: BLE001 - the kit is the look, never a requirement
        return tk.Checkbutton(parent, text=text, variable=variable, command=command, anchor="w",
                              bg=page.t["bg"], fg=page.t["bright"], selectcolor=page.t["bg"])


def labeled_scale(page, parent, label, variable, display):
    """The Settings slider row: label + readout, the kit slider under them (0..100, a key step of 5). -> (scale,
    readout label). The screen's own _labeled_scale helper; a plain row when the screen has none."""
    helper = getattr(page.screen, "_labeled_scale", None)
    if callable(helper):
        return helper(parent, label, variable, 0, 100, display=display, step=5)
    from tkinter import ttk
    t = page.t
    row = tk.Frame(parent, bg=t["bg"])
    row.pack(fill="x", pady=(18, 0))
    tk.Label(row, text=label, bg=t["bg"], fg=t["bright"], anchor="w").pack(side="left")
    readout = tk.Label(row, text=display(variable.get()), bg=t["bg"], fg=t["bright"], anchor="e")
    readout.pack(side="right")
    scale = ttk.Scale(parent, from_=0, to=100, orient="horizontal", variable=variable)
    scale.pack(fill="x", pady=(3, 0))
    variable.trace_add("write", lambda *_a: readout.configure(text=display(variable.get())))
    return scale, readout


def set_state(widget, enabled):
    """configure(state=normal / disabled) on a lamp, a Segmented or a slider, never raising."""
    if widget is None:
        return
    want = "normal" if enabled else "disabled"
    try:
        if str(widget.cget("state")) != want:
            widget.configure(state=want)
    except (tk.TclError, AttributeError, ValueError):
        pass


def set_var(var, value):
    """var.set(value) only when it differs (a write repaints the widgets that follow the variable)."""
    try:
        if var.get() != value:
            var.set(value)
    except (tk.TclError, AttributeError):
        pass
