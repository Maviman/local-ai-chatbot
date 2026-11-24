"""fx - the aesthetic layer's seam (FD-1; FOUNDATION_CONTRACT section 5).

    install(root, theme) -> FX | NullFX     once, first statement of MainWindow._build_shell
    get(widget)          -> FX | NullFX     getattr(widget.winfo_toplevel(), 'fx', None) or NULL
    teardown(root)                          once, in MainWindow.destroy before the after-sweep
    kill_switch_active() -> bool            LOCALAI_FX=0 or <theme.DATA_DIR>/fx_disabled, read NOW

Every hook in app code goes through ``fx.get(widget)`` (or the window's own
``self.fx``) and calls ``emit`` / ``transitions.begin`` / ``end`` /
``sound.enabled_for``. Under NullFX all of those are no-ops, so a site keeps its
legacy behaviour when fx is off, absent (standalone screens in the fixes tests,
a Toplevel without fx) or failed to install.

Install order (contract section 5): kill switch -> palette snapshot -> default
font normalisation + display fonts -> PrefsStore (seeded from
theme.last_loaded_raw(), no second file read) + theme.register_store -> Clock
(level_fn = prefs.effective_motion) + input recorder -> Transitions -> the
Tcl-filtered root <Map> binding (sound.attach_window) -> root.fx. Any failure
undoes the partial state (clock stopped, prefs closed, store unregistered) and
returns NULL.

B10: install also builds fx.motion = motion.Motion(fx) after the transitions
(a failure there leaves the no-op namespace, never fails the install). emit
calls motion.on_emit(widget, event, data) after the Tk subscribers and before
sound.emit, in its own try/except - it is not a subscribe() registration, so
subscriber counts are unchanged. The first root <Map> asks the clock for the
one-time boot pass (later(root, "boot", 0, motion.boot_pass)).

B13: install also binds Alt+M (quick_mute.install(fx): fx.quick_mute; a failure
leaves Alt+M unbound, never fails the install). Its bindings go at teardown with
the prefs subscriptions (fx._prefs_unsubs).

Teardown order (HANDOFF B-5): flush-marked clock one-shots run -> prefs.close(0.5)
-> clock stopped (refuses new entries, AM-3) -> subscribers dropped -> sound
detached -> store unregistered -> root.fx = NULL. Idempotent.

Never raises into app code: emit, get, install, teardown and subscriber
dispatch all isolate exceptions and log once per (site, kind) via _log().
Exception, by design: with LOCALAI_FX_STRICT=1 (harness/tests) an emit made off
the Tk thread raises AssertionError in that (non-Tk) thread.

Stdlib only. This module schedules nothing itself; all timing is the clock's.
"""

import os
import sys
import threading

from .. import theme as _theme
from . import clock as _clock_mod
from . import fonts as _fonts_mod
from . import palette as _palette_mod
from . import prefs as _prefs_mod
from . import sound as _sound_mod
from . import transitions as _transitions_mod

try:
    from . import motion as _motion_mod      # B10; a broken module leaves motion the no-op namespace
except Exception as _e:  # noqa: BLE001
    _motion_mod = None
    _motion_import_error = repr(_e)

try:
    from . import quick_mute as _quick_mute_mod      # B13: Alt+M; a broken module leaves Alt+M unbound
except Exception as _e:  # noqa: BLE001
    _quick_mute_mod = None

__all__ = ["EVENTS", "MARKER_NAME", "FX", "NullFX", "NULL", "install", "get", "teardown", "kill_switch_active"]

# AESTHETIC_SPEC section 4.3: the only names areas emit through the seam.
EVENTS = frozenset({
    "nav.route", "nav.denied",
    "load.begin", "load.end",
    "app.shutdown",
    "toast.shown", "confirm.shown",
    "chat.launch", "chat.queued_dispatch", "chat.stop_requested", "chat.first_output",
    "wait.progress",
    "reply.done",
    "image.finished", "image.rendered",
    "context.level",
    "settings.saved",
})

MARKER_NAME = "fx_disabled"          # <theme.DATA_DIR>/fx_disabled (AM-9; any content)
_KILL_VALUES = ("0", "false", "off", "no")
PREFS_CLOSE_TIMEOUT_S = 0.5          # inside the 800 ms shutdown grace (B-5)
_BASE_SCALING = 96.0 / 72.0          # Tk's 'tk scaling' at 100 % (96 DPI); px(n) == n there
_MAX_EMIT_DEPTH = 8                  # a subscriber emitting from inside dispatch, bounded
_NESTED_BUDGET = 512                 # total callbacks run by NESTED emits per outermost emit (fan-out bound)
_COUNTER_CAP = 256                   # distinct counter names kept per FX
_UNKNOWN_LOG_CAP = 64                # distinct unknown event names logged per FX (own set: never crowds others)
_LOG_CAP = 1024                      # other log-once keys per FX

# Threads that installed a live FX (the Tk threads). get() makes no Tk call from any
# other thread (contract 0). Before any install, the main thread is the only Tk thread.
_tk_threads = set()

# Live installed FXs, oldest first: when one is torn down while it owns theme.py's
# single store slot, the newest still-live FX's store takes the slot back (two roots).
_live = []

# --- logging -----------------------------------------------------------------------
_logged = set()


def _log(msg):
    """One line to stderr (desktop.log in production). Never raises."""
    try:
        sys.stderr.write("fx: " + str(msg).rstrip("\n") + "\n")
        sys.stderr.flush()
    except Exception:
        pass


def _log_once(key, msg):
    if key in _logged or len(_logged) >= 4096:
        return          # logged before - or the set is full: stay quiet rather than log on every call
    _logged.add(key)
    _log(msg)


def _site(fn):
    try:
        f = getattr(fn, "__func__", fn)
        return "%s.%s" % (getattr(f, "__module__", None) or "?",
                          getattr(f, "__qualname__", None) or type(fn).__name__)
    except Exception:
        return "?"


def _noop_unsubscribe(*args, **kwargs):
    return None


class _NullNamespace(object):
    """motion / overlays (later phases) until they exist: any attribute is a no-op callable."""

    __slots__ = ()

    def __getattr__(self, name):
        if name.startswith("__"):
            raise AttributeError(name)
        return _noop

    def __repr__(self):
        return "<fx null namespace>"


def _noop(*args, **kwargs):
    return None


_NULL_NS = _NullNamespace()


class _NullFonts(object):
    """NullFX.fonts: the fonts module's surface without touching Tk. display(size)
    returns the TERMINAL fallback font tuple (a valid Tk font description - the
    fxDisplay* named fonts do not exist when fx is off); resolve() is the module's
    own pure fallback; every other function is a no-op; constants pass through."""

    __slots__ = ()

    @staticmethod
    def display(size="M", *args, **kwargs):
        try:
            return _fonts_mod.FALLBACK[size.upper()]
        except Exception:
            return _fonts_mod.FALLBACK["M"]

    @staticmethod
    def resolve(face=None, size="M", *args, **kwargs):
        return _NullFonts.display(size)

    @staticmethod
    def has_family(*args, **kwargs):
        return False

    def __getattr__(self, name):
        if name.startswith("__"):
            raise AttributeError(name)
        v = getattr(_fonts_mod, name, None)
        if v is None or callable(v):
            return _noop
        return v

    def __repr__(self):
        return "<fx null fonts>"


_NULL_FONTS = _NullFonts()


# --- kill switch ---------------------------------------------------------------------
def kill_switch_active():
    """True when fx must stay off: env LOCALAI_FX=0 (also false/off/no) or the
    marker file <theme.DATA_DIR>/fx_disabled exists. Read NOW (never cached),
    cheap, never raises."""
    try:
        v = os.environ.get("LOCALAI_FX")
        if v is not None and v.strip().lower() in _KILL_VALUES:
            return True
    except Exception:
        pass
    try:
        data_dir = getattr(_theme, "DATA_DIR", None)
        if data_dir is None:
            return False          # not configured yet (tests, the harness, early start-up): nothing to check
        # B12-H1 (narrowed by the orchestrator): a DATA_DIR that is SET but unusable - not a str / PathLike, an
        # empty string, or a check that raises - cannot be checked for the marker, so it reads as the switch
        # being ON (fail closed: fx.sound.boot() then gives the NullEngine), still without raising
        if not isinstance(data_dir, (str, os.PathLike)) or not data_dir:
            return True
        if os.path.exists(os.path.join(data_dir, MARKER_NAME)):
            return True
    except Exception:
        return True
    return False


# --- subscriptions -------------------------------------------------------------------
class _Sub(object):
    __slots__ = ("prefix", "cb", "owner", "live")

    def __init__(self, prefix, cb):
        self.prefix = prefix
        self.cb = cb
        self.owner = None
        self.live = True


def _prefix_matches(prefix, event):
    if not prefix:
        return True
    return event == prefix or event.startswith(prefix + ".")


# --- the installed FX ----------------------------------------------------------------
class FX(object):
    """The installed aesthetic layer of one root window. Built only by install()."""

    def __init__(self, root, theme):
        self.enabled = True
        self.root = root
        self.theme = theme
        self.clock = None
        self.prefs = None
        self.palette = {}
        self.fonts = _fonts_mod
        self.transitions = _transitions_mod.NullTransitions()
        self.sound = _sound_mod.NULL_ENGINE
        self.motion = _NULL_NS
        self.overlays = _NULL_NS
        self.counters = {}
        self.strict = os.environ.get("LOCALAI_FX_STRICT") == "1"
        self.thread_violations = 0
        self.errors = []                 # (site, repr) of isolated subscriber/sound errors, capped
        self.map_count = 0
        self._thread_ident = threading.get_ident()
        self._scaling = _BASE_SCALING
        self._subs = []
        self._owner_subs = {}            # (id(owner.tk), owner Tk path) -> [_Sub]
        self._owner_tag = "FxSeamOwner%x" % id(self)
        self._prefs_unsubs = []
        self._depth = 0
        self._nested_budget = _NESTED_BUDGET
        self._logged = set()
        self._unknown_logged = set()
        self._store_registered = False
        self._map_cmd = None
        self._map_script = None
        self._map_path = None
        self._tearing = False
        self._torn = False

    def __repr__(self):
        return "<fx.FX root=%r%s>" % (str(self.root) if self.root is not None else None,
                                      " torn-down" if self._torn else "")

    # --- helpers ---------------------------------------------------------------------
    def _log_once(self, key, msg):
        if key in self._logged or len(self._logged) >= _LOG_CAP:
            return
        self._logged.add(key)
        _log(msg)

    def _log_unknown(self, event):
        """Unknown event names have their own bounded set, so a stream of dynamic
        names can never crowd out (silence) any other diagnostic."""
        name = event[:60]
        if name in self._unknown_logged:
            return
        if len(self._unknown_logged) >= _UNKNOWN_LOG_CAP:
            self._log_once(("unknown-cap",), "fx.emit: more than %d distinct unknown event names - no longer "
                                             "logged (still counted as unknown_event)" % _UNKNOWN_LOG_CAP)
            return
        self._unknown_logged.add(name)
        _log("fx.emit: unknown event %r (not in the 4.3 catalogue)" % (name,))

    def _record_error(self, site, exc):
        if len(self.errors) < 200:
            self.errors.append((site, repr(exc)))

    def _on_tk_thread(self):
        return threading.get_ident() == self._thread_ident

    def _bump(self, key):
        self.counters[key] = self.counters.get(key, 0) + 1

    # --- the seam --------------------------------------------------------------------
    def emit(self, /, *args, **data):
        """emit(widget_or_None, event, **data). Tk-side subscribers synchronously, in
        registration order, each isolated; then sound.emit(event, data). Unknown
        events are counted (and still dispatched). Never raises - except the STRICT
        off-thread assertion, which raises in the offending worker thread.

        Positional (widget, event) is the normal form; then data keys named 'widget'
        / 'event' are ordinary data. The keyword form emit(widget=w, event='x') and
        emit(w, event='x') are accepted too. `self` is positional-only, so a data key
        named 'self' is just data. Nested emits (a subscriber emitting) are bounded in
        depth (_MAX_EMIT_DEPTH) and in total callbacks per outermost emit
        (_NESTED_BUDGET), so a re-emitting subscriber set cannot fan out."""
        if threading.get_ident() != self._thread_ident:
            self.thread_violations += 1
            if self.strict:
                raise AssertionError("fx.emit(%r) called off the Tk thread (%s)"
                                     % (args[1] if len(args) > 1 else data.get("event"),
                                        threading.current_thread().name))
            self._log_once(("thread",), "fx.emit called off the Tk thread - ignored")
            return None
        try:
            if self._torn:
                return None
            if len(args) == 2:
                widget, event = args
            elif len(args) == 1 and "event" in data:
                widget, event = args[0], data.pop("event")
            elif not args and "event" in data:
                event = data.pop("event")
                widget = data.pop("widget", None)
            else:
                self._bump("bad_event")
                self._log_once(("args", len(args)), "fx.emit expects (widget, event, **data); got %d positional "
                                                     "arguments - dropped" % len(args))
                return None
            if not isinstance(event, str) or not event:
                self._bump("bad_event")
                self._log_once(("bad", type(event).__name__), "fx.emit with a non-string event %r - dropped"
                               % (event,))
                return None
            if event in EVENTS:
                self._bump(event)
            else:
                self._bump("unknown_event")
                if event in self.counters or len(self.counters) < _COUNTER_CAP:
                    self._bump(event)       # per-name count too, bounded (a dynamic name cannot grow the dict)
                self._log_unknown(event)
            if self._depth >= _MAX_EMIT_DEPTH:
                self._bump("depth_dropped")
                self._log_once(("depth",), "fx.emit nested deeper than %d (a subscriber loop?) - %r dropped"
                               % (_MAX_EMIT_DEPTH, event))
                return None
            if self._depth == 0:
                self._nested_budget = _NESTED_BUDGET
            elif self._nested_budget <= 0:
                self._bump("budget_dropped")
                self._log_once(("budget",), "fx.emit: nested emits ran %d callbacks under one outermost emit "
                                            "(a subscriber loop?) - %r dropped" % (_NESTED_BUDGET, event))
                return None
            self._depth += 1
            try:
                nested = self._depth > 1
                for rec in list(self._subs):
                    if not rec.live or not _prefix_matches(rec.prefix, event):
                        continue
                    if nested:
                        if self._nested_budget <= 0:
                            self._bump("budget_dropped")
                            break
                        self._nested_budget -= 1
                    try:
                        rec.cb(widget, event, dict(data))
                    except Exception as e:
                        site = _site(rec.cb)
                        self._record_error(site, e)
                        self._log_once(("sub", site, type(e).__name__),
                                       "fx subscriber %s raised %r on %s - isolated" % (site, e, event))
                    if self._torn:
                        return None
            finally:
                self._depth -= 1
            # B10: motion before sound, isolated (not a subscriber: subscriber_count() is unchanged)
            try:
                self.motion.on_emit(widget, event, data)
            except Exception as e:
                self._record_error("motion.on_emit", e)
                self._log_once(("motion", type(e).__name__), "fx motion.on_emit raised %r - isolated" % (e,))
            if self._torn:
                return None
            try:
                self.sound.emit(event, data)
            except Exception as e:
                self._record_error("sound.emit", e)
                self._log_once(("sound", type(e).__name__), "fx sound.emit raised %r - isolated" % (e,))
        except Exception as e:
            self._log_once(("emit", type(e).__name__), "fx.emit failed: %r" % (e,))
        return None

    def subscribe(self, prefix, cb, owner=None):
        """cb(widget, event, data) for every emitted event equal to the dotted prefix or
        under it ('' or '*' = every event). Returns an idempotent unsubscribe callable.
        With owner (a Tk widget) the subscription is dropped on the owner's <Destroy>,
        through a bindtag of this FX's own (the owner's own bind('<Destroy>') cannot
        clobber it). Bad arguments, a dead owner or a torn-down FX register nothing and
        return a no-op unsubscribe. Never raises."""
        try:
            if not self._on_tk_thread():
                if self.strict:
                    raise AssertionError("fx.subscribe called off the Tk thread")
                self._log_once(("sub-thread",), "fx.subscribe called off the Tk thread - ignored")
                return _noop_unsubscribe
            if self._torn or self._tearing:
                return _noop_unsubscribe
            if prefix is None:
                prefix = ""
            if not isinstance(prefix, str) or not callable(cb):
                self._log_once(("sub-args", type(prefix).__name__),
                               "fx.subscribe(%r, %r): needs a str prefix and a callable - ignored"
                               % (prefix, type(cb).__name__))
                return _noop_unsubscribe
            prefix = prefix.strip()
            if prefix == "*":
                prefix = ""
            prefix = prefix.rstrip(".")
            rec = _Sub(prefix, cb)
            if owner is not None:
                try:
                    if not owner.winfo_exists():
                        return _noop_unsubscribe
                    ikey = self._attach_owner_tag(owner)
                    key = (ikey, str(owner))
                except Exception as e:
                    self._log_once(("owner", type(e).__name__), "fx.subscribe: owner unusable (%r) - ignored" % (e,))
                    return _noop_unsubscribe
                rec.owner = key
                self._owner_subs.setdefault(key, []).append(rec)
            self._subs.append(rec)

            def unsubscribe(rec=rec):
                self._drop(rec)
                return None
            return unsubscribe
        except AssertionError:
            raise
        except Exception as e:
            self._log_once(("subscribe", type(e).__name__), "fx.subscribe failed: %r" % (e,))
            return _noop_unsubscribe

    def _drop(self, rec):
        if not rec.live:
            return
        rec.live = False
        try:
            self._subs.remove(rec)
        except ValueError:
            pass
        if rec.owner is not None:
            lst = self._owner_subs.get(rec.owner)
            if lst and rec in lst:
                lst.remove(rec)
                if not lst:
                    self._owner_subs.pop(rec.owner, None)

    def _attach_owner_tag(self, owner):
        """Put this FX's owner tag FIRST in the owner's bindtags (an earlier <Destroy>
        handler returning 'break' - the owner's own, its class, its toplevel or 'all' -
        cannot then skip it). The tag's class binding is per interpreter and carries
        that interpreter's key, so owners with the same path in two interpreters
        (two roots in one process) never collide. -> the interpreter key."""
        tag = self._owner_tag
        ikey = id(owner.tk)
        if not owner.bind_class(tag, "<Destroy>"):
            owner.bind_class(tag, "<Destroy>", lambda e, k=ikey: self._on_owner_destroy(e, k))
        tags = owner.bindtags()
        if tag not in tags:
            owner.bindtags((tag,) + tuple(tags))
        return ikey

    def _on_owner_destroy(self, event, ikey=None):
        try:
            key = (ikey, str(event.widget))
        except Exception:
            return None
        for rec in self._owner_subs.pop(key, []):
            rec.live = False
            try:
                self._subs.remove(rec)
            except ValueError:
                pass
        return None

    def subscriber_count(self):
        return len(self._subs)

    def level(self):
        """'full' | 'reduced' | 'off': clock.effective_level() (prefs.effective_motion,
        downgraded while the watchdog degrade is active). 'off' once torn down."""
        try:
            if self._torn or self.clock is None:
                return "off"
            return self.clock.effective_level()
        except Exception as e:
            self._log_once(("level", type(e).__name__), "fx.level failed: %r - 'off'" % (e,))
            return "off"

    def px(self, n):
        """round(n * tk scaling / (96/72)): design pixels at 100 % -> device pixels.
        The scaling is sampled at install. A non-number comes back unchanged."""
        try:
            return int(round(float(n) * self._scaling / _BASE_SCALING))
        except Exception:
            return n

    # --- install-time wiring ------------------------------------------------------------
    def _sample_scaling(self):
        try:
            s = float(self.root.tk.call("tk", "scaling"))
            if s > 0 and s == s and s != float("inf"):
                self._scaling = s
        except Exception as e:
            self._log_once(("scaling", type(e).__name__), "fx: tk scaling unreadable (%r) - px() is 1:1" % (e,))

    def _bind_map(self):
        """bind <root> <Map> {+if {"%W" eq "<root>"} {<cmd>}}: the Tcl-side filter keeps
        children's <Map> events (which reach the root's tag too) out of Python."""
        try:
            path = str(self.root)
            cmd = self.root.register(self._on_map)
            script = 'if {"%W" eq {' + path + '}} {' + cmd + '}'
            self.root.tk.call("bind", path, "<Map>", "+" + script)
            self._map_cmd, self._map_script, self._map_path = cmd, script, path
        except Exception as e:
            self._log_once(("map-bind", type(e).__name__), "fx: root <Map> binding not installed: %r" % (e,))

    def _unbind_map(self):
        cmd, script, path = self._map_cmd, self._map_script, self._map_path
        self._map_cmd = self._map_script = self._map_path = None
        if cmd is None:
            return
        try:
            cur = str(self.root.tk.call("bind", path, "<Map>"))
            if script in cur:
                lines = [ln for ln in cur.split("\n") if ln != script]
                self.root.tk.call("bind", path, "<Map>", "\n".join(lines).strip("\n"))
        except Exception:
            pass
        try:
            self.root.deletecommand(cmd)
        except Exception:
            pass

    def _on_map(self):
        if self._torn:
            return None
        self.map_count += 1
        if self.map_count == 1 and self.clock is not None and self.motion is not _NULL_NS:
            try:
                self.clock.later(self.root, "boot", 0, self.motion.boot_pass)      # B10: the one-time boot pass
            except Exception as e:
                self._log_once(("boot", type(e).__name__), "fx: boot pass not scheduled: %r" % (e,))
        try:
            hwnd = int(str(self.root.wm_frame()), 16)
        except Exception as e:
            self._log_once(("hwnd", type(e).__name__), "fx: root HWND unreadable: %r" % (e,))
            return None
        try:
            self.sound.attach_window(hwnd)
        except Exception as e:
            self._record_error("sound.attach_window", e)
            self._log_once(("attach", type(e).__name__), "fx: sound.attach_window raised %r" % (e,))
        return None

    def _on_face(self, path, value):
        if self._torn or not self._on_tk_thread():
            return
        _fonts_mod.apply_face(self.root, value)

    def _on_ink(self, path, value):
        if self._torn:
            return
        # Recompute from THIS FX's preset: palette.set_ink rebuilds from the module-global
        # snapshot, which a second root may have taken with another phosphor. The global
        # is refreshed too, but only while it is still this FX's preset.
        preset = self.palette.get("preset") if isinstance(self.palette, dict) else None
        try:
            glob = _palette_mod.current().get("preset")
        except Exception:
            glob = None
        if preset is None or preset == glob:
            self.palette = _palette_mod.set_ink(value)
        else:
            self.palette = _palette_mod.tones(preset, value)

    # --- teardown (B-5) ---------------------------------------------------------------
    def _teardown(self):
        if self._tearing or self._torn:
            return
        self._tearing = True
        clk, prefs = self.clock, self.prefs
        # 1. flush-marked one-shots (e.g. a pending volume commit) run now
        if clk is not None:
            try:
                clk.run_flush_marked()
            except Exception as e:
                self._log_once(("td-flush", type(e).__name__), "fx.teardown: run_flush_marked raised %r" % (e,))
        self._torn = True
        self.enabled = False
        # 2. prefs: flush pending writes and stop the writer (bounded)
        if prefs is not None:
            try:
                prefs.close(PREFS_CLOSE_TIMEOUT_S)
            except Exception as e:
                self._log_once(("td-prefs", type(e).__name__), "fx.teardown: prefs.close raised %r" % (e,))
        # 3. stop the clock: refuses new entries from here on (AM-3)
        if clk is not None:
            try:
                clk.stop()
            except Exception as e:
                self._log_once(("td-clock", type(e).__name__), "fx.teardown: clock.stop raised %r" % (e,))
        # 4. drop subscribers (ours and our prefs subscriptions)
        for rec in self._subs:
            rec.live = False
        self._subs = []
        self._owner_subs = {}
        for unsub in self._prefs_unsubs:
            try:
                unsub()
            except Exception:
                pass
        self._prefs_unsubs = []
        # 5. detach the HWND / sound
        try:
            self.sound.detach()
        except Exception as e:
            self._log_once(("td-sound", type(e).__name__), "fx.teardown: sound.detach raised %r" % (e,))
        self._unbind_map()
        # 6. unregister the store - only if it is still ours (a second root may have registered its own)
        self._unregister_store()
        self.transitions = _transitions_mod.NullTransitions()

    def _unregister_store(self):
        """Give up theme.py's single store slot - only if it is still ours (a second
        root may have registered its own). The slot then goes back to the newest
        other FX that is still live (its root still exists), else to None."""
        try:
            _live.remove(self)
        except ValueError:
            pass
        if not self._store_registered:
            return
        self._store_registered = False
        try:
            if getattr(_theme, "_store", None) is self.prefs:
                _theme.register_store(_successor_store())
        except Exception as e:
            self._log_once(("td-store", type(e).__name__), "fx: register_store(None) raised %r" % (e,))

    def _undo_partial(self):
        """install() failed part-way: stop the clock, close prefs, unregister the store."""
        self._torn = True
        self.enabled = False
        for unsub in self._prefs_unsubs:
            try:
                unsub()
            except Exception:
                pass
        self._prefs_unsubs = []
        if self.clock is not None:
            try:
                self.clock.stop()
            except Exception:
                pass
        if self.prefs is not None:
            try:
                self.prefs.close(PREFS_CLOSE_TIMEOUT_S)
            except Exception:
                pass
        self._unregister_store()
        self._unbind_map()
        self._subs = []
        self._owner_subs = {}


# --- NullFX ---------------------------------------------------------------------------
class NullFX(object):
    """The same surface with nothing behind it: no clock, no prefs, no thread, no
    file writes. Every hook is safe against it."""

    enabled = False

    def __init__(self):
        self.root = None
        self.theme = None
        self.clock = None
        self.prefs = None
        self.fonts = _NULL_FONTS                 # fonts surface with no Tk work; display() -> TERMINAL tuple
        self.transitions = _transitions_mod.NullTransitions()
        self.sound = _sound_mod.NULL_ENGINE
        self.motion = _NULL_NS
        self.overlays = _NULL_NS

    @property
    def palette(self):
        return _palette_mod.tones("amber")      # a fresh dict per access: the singleton cannot be polluted

    @property
    def counters(self):
        return {}

    def emit(self, /, *args, **data):
        return None

    def subscribe(self, /, *args, **kwargs):
        return _noop_unsubscribe

    def subscriber_count(self, /, *args, **kwargs):
        return 0

    def level(self, /, *args, **kwargs):
        return "off"

    def px(self, /, n=0, *args, **kwargs):
        return n

    def __repr__(self):
        return "<fx.NullFX>"


NULL = NullFX()


# --- module API -------------------------------------------------------------------------
def _fx_alive(f):
    """A live FX whose root still exists (and whose prefs are open). Never raises."""
    try:
        if f._torn or f._tearing or f.prefs is None or getattr(f.prefs, "closed", False):
            return False
        if f._thread_ident != threading.get_ident():
            return False            # another thread's Tk: never touch it from here
        return bool(f.root.winfo_exists())
    except Exception:
        return False


def _successor_store():
    """The store of the newest other live FX (two roots in one process), or None."""
    for f in reversed(list(_live)):
        if _fx_alive(f) and f._store_registered:
            return f.prefs
    return None


def _prune_live():
    """Drop this thread's entries whose root died without a teardown (bounded registry)."""
    me = threading.get_ident()
    _live[:] = [f for f in _live if f._thread_ident != me or _fx_alive(f)]


def _raw_choice(fxraw, key, choices, default):
    try:
        v = fxraw.get(key) if isinstance(fxraw, dict) else None
        return v if isinstance(v, str) and v in choices else default
    except Exception:
        return default


def _set_root_fx(root, value):
    try:
        root.fx = value
    except Exception:
        pass


def install(root, theme):
    """Build and attach the FX for `root` (see module docstring for the order).
    -> FX, or NULL when a kill switch is active or anything fails (partial state
    undone). Calling it again on a root whose FX is live returns that FX."""
    try:
        cur = getattr(root, "fx", None)
    except Exception:
        cur = None
    if isinstance(cur, FX) and not cur._torn and not cur._tearing:
        return cur
    # 1. kill switches (read now, never at import)
    if kill_switch_active():
        _set_root_fx(root, NULL)
        return NULL
    fx = FX(root, theme)
    try:
        raw = _theme.last_loaded_raw()
        fxraw = raw.get("fx") if isinstance(raw, dict) else None
        face = _raw_choice(fxraw, "display_face", _prefs_mod.DISPLAY_FACES, "industrial")
        ink = _raw_choice(fxraw, "instrument_ink", _prefs_mod.INSTRUMENT_INKS, "phosphor")
        # 2. palette snapshot
        fx.palette = _palette_mod.snapshot(theme, ink)
        # 3. fonts
        _fonts_mod.normalise_default_fonts(root)
        _fonts_mod.ensure(root, face)
        # 4. the preference store (the single writer), then register it with theme.py
        fx.prefs = _prefs_mod.PrefsStore(_theme.THEME_PATH, raw, theme, log=_log)
        _theme.register_store(fx.prefs)
        fx._store_registered = True
        # 5. the clock + its input recorder
        fx.clock = _clock_mod.Clock(root, level_fn=fx.prefs.effective_motion, log=_log)
        fx.clock.install_recorder()
        # 6. transitions
        fx.transitions = _transitions_mod.Transitions(fx)
        # B10: motion (GAP_FX 1.9); a failure keeps the no-op namespace and never fails the install
        if _motion_mod is not None:
            try:
                fx.motion = _motion_mod.Motion(fx)
            except Exception as e:
                fx.motion = _NULL_NS
                fx._log_once(("motion", type(e).__name__), "fx: motion not installed: %r" % (e,))
        # sound: NullEngine in P1; LOCALAI_SFX=0 pins it to NullEngine for good
        if os.environ.get("LOCALAI_SFX", "").strip() == "0":
            fx.sound = _sound_mod.NULL_ENGINE
        else:
            fx.sound = _sound_mod.get()
        try:
            fx.sound.bind_store(fx.prefs)
        except Exception as e:
            fx._log_once(("bind_store", type(e).__name__), "fx: sound.bind_store raised %r" % (e,))
        fx._prefs_unsubs.append(fx.prefs.subscribe("fx.display_face", fx._on_face))
        fx._prefs_unsubs.append(fx.prefs.subscribe("fx.instrument_ink", fx._on_ink))
        # B13: Alt+M quick mute (ST-14); it removes its bindings through fx._prefs_unsubs at teardown. A failure
        # leaves Alt+M unbound and never fails the install.
        if _quick_mute_mod is not None:
            try:
                _quick_mute_mod.install(fx)
            except Exception as e:
                fx._log_once(("quick_mute", type(e).__name__), "fx: quick mute not installed: %r" % (e,))
        fx._sample_scaling()
        # 7. the Tcl-filtered root <Map> binding (wrapped: never fails the install)
        fx._bind_map()
        # 8. attach
        root.fx = fx
        _tk_threads.add(fx._thread_ident)
        _prune_live()
        _live.append(fx)
    except Exception as e:
        _log("fx.install failed (%s: %r) - running without fx" % (type(e).__name__, e))
        fx._undo_partial()
        _set_root_fx(root, NULL)
        return NULL
    return fx


def get(widget):
    """The FX of widget's toplevel, or NULL (no fx, a Toplevel without fx, a
    destroyed widget, None, a falsy .fx). Off the Tk thread it returns NULL at once,
    without any Tk call (contract 0). Never raises."""
    if widget is None:
        return NULL
    try:
        me = threading.get_ident()
        if me not in _tk_threads and (_tk_threads or me != threading.main_thread().ident):
            return NULL
        top = widget.winfo_toplevel()
        fx = getattr(top, "fx", None) or NULL
        if isinstance(fx, FX) and fx._torn:
            return NULL
        return fx
    except Exception:
        return NULL


def _same_widget(a, b):
    try:
        return a is b or (a.tk is b.tk and str(a) == str(b))
    except Exception:
        return False


def teardown(root):
    """Idempotent (B-5 order; see module docstring). Leaves root.fx = NULL. Never raises.
    Only tears down an FX installed on THIS root: a widget whose .fx merely aliases
    another root's FX just has its .fx cleared."""
    try:
        fx = getattr(root, "fx", None)
    except Exception:
        fx = None
    if isinstance(fx, FX) and _same_widget(fx.root, root):
        try:
            fx._teardown()
        except Exception as e:
            _log_once(("teardown", type(e).__name__), "fx.teardown failed: %r" % (e,))
    _set_root_fx(root, NULL)
    return None
