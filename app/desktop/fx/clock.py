"""The one clock (T-01 / FD-3, with HANDOFF amendments AM-2 and AM-3).

Every fx motion, loop and one-shot timer runs through a Clock. It is the only
module in fx/ that calls ``after`` / ``after_cancel`` (static grep test).

Design (verified in the transitions prototypes X1/X6/X9):
  * ONE root-owned ``after`` loop, armed only while something is pending
    (tween, loop, later or an un-ended token). Idle = zero wakeups. While
    only far-off work is pending (a 180 ms later, a 1 s loop) it sleeps
    ``max(15, next_due - now)``.
  * Progress comes from ``time_fn`` (perf_counter), clamped to +2 frames per
    tick; the deadline is a PROGRESS bound: p reaches 1.0 at the first tick at
    or after t0 + 2 x duration (A4). t0 is the entry's first tick (or the tick
    its ``wait_until`` hold ends / its cap is reached).
  * Same (owner, slot) is cancel-first: an old tween is FINISHED (final state
    applied), an old loop is stopped, an old later is dropped.
  * Levels are read when an entry starts: OFF applies tweens' final state
    synchronously; REDUCED does that for spatial/blink and runs colour; later()
    and loop() are timing, not motion, and run at every level.
  * Per-tick liveness (``owner.winfo_exists()``; dead owners dropped silently,
    no step, no done), per-entry isolation (a raising callable drops only its
    entry, logged once per (module, slot)), the A3 watchdog (lateness OR cost)
    and the A8 navigation lock guard.
  * Tokens (A2 / AM-2): an un-ended token is aborted on the next clock TIMER
    tick only. The tick is a plain ``after(ms)`` timer, so ``update_idletasks``
    and ``after_idle`` processing can never run it.
  * After ``stop()`` (AM-3) every entry point is a no-op returning None and
    nothing re-arms - including calls made from ``<Destroy>`` handlers that run
    inside ``super().destroy()`` after the teardown sweep.
  * B10 (GAP_FX 1.4 / 1.8): ``tween(..., interruptible=True)`` marks a running
    effect that any key or mouse press finishes at once (final state): the input
    recorder's handlers run before every other handler of that event, so a
    type-on is complete before the key or click acts. Latches and the power-down
    are not interruptible. ``block_input(True)`` makes the recorder swallow every
    key and button press ('break') - the power-down's "swallows clicks" - until
    ``block_input(False)`` or ``stop()``. ``now()`` is the clock's time base.

Stdlib only. All Tk calls happen on the thread that built the Clock.
"""

import math
import os
import sys
import threading
import time
import tkinter as tk
import weakref

__all__ = [
    "TICK_MS", "FRAME_MS", "PROGRESS_CLAMP_FRAMES", "DEADLINE_FACTOR", "USER_INPUT_WINDOW_MS",
    "ESC_SAME_DISPATCH_MS", "WATCHDOG_LATE_MS", "WATCHDOG_LATE_OF", "WATCHDOG_LATE_WINDOW",
    "WATCHDOG_COST_MS", "WATCHDOG_COST_TICKS", "DEGRADE_MS", "KINDS", "WAIT_MAX_DEFAULT_MS",
    "linear", "out_cubic", "in_quad", "steps", "deferred", "Clock",
]

# --- tokens (AESTHETIC_SPEC 5.1) ---------------------------------------------
TICK_MS = 15
FRAME_MS = 15.6
PROGRESS_CLAMP_FRAMES = 2
DEADLINE_FACTOR = 2.0
USER_INPUT_WINDOW_MS = 400
ESC_SAME_DISPATCH_MS = 50

# Watchdog (A3)
WATCHDOG_LATE_MS = 30.0      # (a) a tick is late when interval - FRAME_MS > 30 ms ...
WATCHDOG_LATE_OF = 2         # ... on 2 ...
WATCHDOG_LATE_WINDOW = 3     # ... of the last 3 ticks while a spatial tween is active
WATCHDOG_COST_MS = 12.0      # (b) non-final step callbacks > 12 ms ...
WATCHDOG_COST_TICKS = 2      # ... on 2 consecutive ticks
DEGRADE_MS = 2000            # effective level = reduced for this long after a trip

KINDS = ("spatial", "colour", "blink")
LEVELS = ("full", "reduced", "off")

# A tween with wait_until but no wait_max_ms is held at most this long (spec 5.1
# STRIKE_WAIT_MAX_MS). Never unbounded: a forgotten cap must not keep a 15 ms
# wakeup alive forever.
WAIT_MAX_DEFAULT_MS = 250

TICK_LOG_CAP = 2000
_DUE_SLACK_S = 0.002         # an entry this close to due counts as due (Windows timers fire a hair early)
_ALT_MASK = 0x20000 if sys.platform == "win32" else 0x0008
_MAX_SLEEP_MS = 3600 * 1000  # longest single after(); a far-off later just re-arms (Tcl rejects ~1e19 ms)
_SLOT_CHAIN_MAX = 64         # cancel-first: at most this many done()-chained re-occupations are finished

# Input recorder: a bindtag PREPENDED to every widget's bindtags (so it runs
# before widget-level, class, toplevel and 'all' bindings, and no specific
# binding on another tag can shadow its generic <KeyPress>/<ButtonPress>).
# Existing widgets are tagged at install; later ones on their first <Map> or
# <FocusIn> (pure Tcl, no Python callback) - a widget cannot be clicked before
# it is mapped, nor receive a key before it has focus.
_REC_TAG = "FxClockInputRec"
_REC_TCL = r"""
proc ::fx_clock_rec_tag {w} {
    if {![winfo exists $w]} return
    set t [bindtags $w]
    if {[lsearch -exact $t %(tag)s] < 0} { bindtags $w [linsert $t 0 %(tag)s] }
}
proc ::fx_clock_rec_walk {w} {
    set q [list $w]
    while {[llength $q]} {
        set w [lindex $q 0]
        set q [lrange $q 1 end]
        catch {::fx_clock_rec_tag $w}
        catch {lappend q {*}[winfo children $w]}
    }
}
""" % {"tag": _REC_TAG}
_REC_MAP_SCRIPT = "catch {::fx_clock_rec_tag %W}"


# --- easings (no overshoot, no springs) ----------------------------------------
def _clamp01(p):
    if p != p:          # NaN
        return 1.0
    return 0.0 if p < 0.0 else (1.0 if p > 1.0 else p)


def linear(p):
    return _clamp01(p)


def out_cubic(p):
    p = _clamp01(p)
    return 1.0 - (1.0 - p) ** 3


def in_quad(p):
    p = _clamp01(p)
    return p * p


def steps(n):
    """Indicator easing: floor(p*n)/n, exactly 1.0 at p >= 1."""
    try:
        n = int(n)
    except Exception:
        n = 1
    n = max(1, n)

    def f(p):
        p = _clamp01(p)
        return 1.0 if p >= 1.0 else math.floor(p * n) / float(n)
    f.__name__ = "steps_%d" % n
    return f


# --- deferred() -------------------------------------------------------------------
def _find_callit_code():
    """The code object of the closure tkinter's Misc.after() wraps every
    after()/after_idle() callback in (Python 3.12.10: a nested ``def callit``
    in tkinter/__init__.py). None if this interpreter's tkinter differs."""
    try:
        for c in tk.Misc.after.__code__.co_consts:
            if getattr(c, "co_name", None) == "callit" and \
                    os.path.normcase(c.co_filename) == os.path.normcase(tk.__file__):
                return c
    except Exception:
        pass
    return None


_CALLIT_CODE = _find_callit_code()


def deferred():
    """True when the current call stack contains tkinter's after() wrapper,
    i.e. we are running inside an after()/after_idle() callback (an async_call
    poll, a debounce) rather than directly inside an event binding.

    Pinned to the bundled Python 3.12.10 by code-object identity. If the
    wrapper cannot be found or frame inspection fails for any reason, returns
    False (fail-safe: counts as user)."""
    code = _CALLIT_CODE
    if code is None:
        return False
    try:
        f = sys._getframe(1)
        while f is not None:
            if f.f_code is code:
                return True
            f = f.f_back
    except Exception:
        return False
    return False


# --- logging ---------------------------------------------------------------------
def _default_log(msg):
    """fx._log when the fx package is loaded (production), else stderr. Never raises."""
    try:
        pkg = sys.modules.get(__package__) if __package__ else None
        fn = getattr(pkg, "_log", None) if pkg is not None else None
        if callable(fn):
            fn(msg)
            return
    except Exception:
        pass
    try:
        sys.stderr.write(str(msg).rstrip("\n") + "\n")
    except Exception:
        pass


def _callable_module(fn):
    try:
        f = getattr(fn, "__func__", fn)
        mod = getattr(f, "__module__", None)
        if mod is None:
            inner = getattr(f, "func", None)          # functools.partial
            mod = getattr(inner, "__module__", None)
        return mod or type(fn).__name__
    except Exception:
        return "?"


def _ms(value):
    try:
        v = float(value)
    except Exception:
        return 0.0
    if v != v or v in (float("inf"), float("-inf")) or v < 0:
        return 0.0
    return v


# --- entries ----------------------------------------------------------------------
class _Entry(object):
    __slots__ = ("clock", "owner", "slot", "key", "kind", "nav", "created_locked", "dead", "__weakref__")

    is_tween = False
    is_loop = False
    is_later = False

    def __repr__(self):
        return "<clock %s %r slot=%r%s>" % (type(self).__name__.strip("_").lower(), self.owner, self.slot,
                                            " dead" if self.dead else "")


class _Tween(_Entry):
    __slots__ = ("dur_ms", "step", "done", "ease", "wait_until", "wait_max_s", "valid", "t0",
                 "waiting_since", "p", "finished_by", "n_steps", "interruptible")
    is_tween = True

    @property
    def finished(self):
        return self.dead


class _Loop(_Entry):
    __slots__ = ("interval_s", "step", "due", "n_steps")
    is_loop = True


class _Later(_Entry):
    __slots__ = ("due", "fn", "flush")
    is_later = True


class _Token(object):
    __slots__ = ("on_abort", "live", "aborted", "__weakref__")

    def __init__(self, on_abort):
        self.on_abort = on_abort
        self.live = True
        self.aborted = False

    def __repr__(self):
        return "<clock token %s>" % ("live" if self.live else ("aborted" if self.aborted else "ended"))


_ALL = object()      # internal "match every owner" sentinel


class Clock(object):
    """See module docstring and FOUNDATION_CONTRACT.md section 1."""

    def __init__(self, root, level_fn=lambda: "full", *, time_fn=time.perf_counter, schedule=None, cancel=None,
                 locked_fn=None, log=None):
        self.root = root
        self.level_fn = level_fn if callable(level_fn) else (lambda: "full")
        self.time_fn = time_fn
        self._probe = None
        if schedule is None:
            schedule = lambda ms, fn: root.after(ms, fn)          # noqa: E731  ROOT-owned tick
            # 'after info <id>' raises TclError once the event fired or was cancelled
            # behind our back (e.g. a cancel_pending_after_callbacks(root) sweep run
            # without stopping the clock first) - lets _ensure_armed recover.
            self._probe = lambda i: root.tk.call("after", "info", i)  # noqa: E731
        if cancel is None:
            cancel = lambda i: root.after_cancel(i)               # noqa: E731
        self._schedule = schedule
        self._cancel = cancel
        if locked_fn is None:
            locked_fn = lambda: bool(getattr(root, "_navigation_locked", False))  # noqa: E731
        self.locked_fn = locked_fn
        self._log_fn = log if callable(log) else _default_log
        self._logged = set()
        self.errors = []                 # (module, slot, repr) of isolated callable errors, capped
        self.strict = os.environ.get("LOCALAI_FX_STRICT") == "1"
        self._thread_ident = threading.get_ident()

        self._entries = {}               # key -> entry, insertion-ordered
        self._tokens = []                # live tokens awaiting their abort tick
        self._exempt = []                # weakrefs (or strong refs for non-weakrefable owners)
        self._after_id = None
        self._armed_at = None            # _now() when the pending tick was scheduled
        self._armed_due = None           # absolute due time of the pending tick
        self._armed_ms = None
        self._in_tick = False
        self._frozen = None
        self.stopped = False

        self.degraded_until = None
        self.trips = {"lateness": 0, "cost": 0}
        self._late_hist = []
        self._cost_over = 0
        self.last_step_cost_ms = 0.0
        self.tick_log = []               # (t, interval_ms, cost_ms, n_entries)
        self.ticks = 0

        self._last_input_t = None
        self._last_input_key = None
        self._recorder_installed = False
        self._block_input = False        # B10: the power-down swallows every key / button press

    # --- infrastructure ----------------------------------------------------------
    def _now(self):
        if self._frozen is not None:
            return self._frozen
        return self.time_fn()

    def now(self):
        """The clock's time base in seconds (time_fn, or the freeze_at value). Loop steps get the same
        value as their argument, so a reader can measure elapsed time against it."""
        return self._now()

    def freeze_at(self, t):
        """Test hook: make the clock's notion of now return `t` (None unfreezes)."""
        self._frozen = None if t is None else float(t)

    def _log_once(self, key, msg):
        if key in self._logged:
            return
        self._logged.add(key)
        try:
            self._log_fn(msg)
        except Exception:
            pass

    def _err(self, fn, slot, exc, what):
        mod = _callable_module(fn)
        if len(self.errors) < 200:
            self.errors.append((mod, slot, repr(exc)))
        self._log_once(("err", mod, slot),
                       "fx.clock: %s raised in %s (slot %r): %r - entry dropped" % (what, mod, slot, exc))

    def _thread_ok(self):
        """Entry-point guard. Off the Tk thread: AssertionError in STRICT mode,
        otherwise a logged no-op (never a cross-thread Tk call)."""
        if threading.get_ident() == self._thread_ident:
            return True
        if self.strict:
            raise AssertionError("fx.clock entry point called off the Tk thread (%s)"
                                 % threading.current_thread().name)
        self._log_once(("thread",), "fx.clock: entry point called off the Tk thread - ignored")
        return False

    def _alive(self, owner):
        if owner is None:
            return True
        fn = getattr(owner, "winfo_exists", None)
        if fn is None:
            return True
        try:
            return bool(fn())
        except tk.TclError:
            return False
        except Exception:
            return False

    def _is_locked(self):
        try:
            return bool(self.locked_fn())
        except Exception as e:
            self._log_once(("locked_fn",), "fx.clock: locked_fn raised %r - treated as unlocked" % (e,))
            return False

    @staticmethod
    def _key(owner, slot):
        return (None if owner is None else id(owner), slot)

    def _level_raw(self):
        try:
            lv = self.level_fn()
        except Exception as e:
            self._log_once(("level_fn",), "fx.clock: level_fn raised %r - using 'full'" % (e,))
            return "full"
        return lv if lv in LEVELS else "full"

    def effective_level(self):
        """level_fn(), downgraded to 'reduced' while a watchdog degrade is active."""
        lv = self._level_raw()
        if lv == "full" and self.degraded_until is not None and self._now() < self.degraded_until:
            return "reduced"
        return lv

    # --- arming ------------------------------------------------------------------
    def _next_due(self, now):
        """Earliest absolute time a tick is needed, or None when idle."""
        if self._tokens:
            return now
        due = None
        for e in self._entries.values():
            if e.is_tween:
                return now
            d = e.due
            if due is None or d < due:
                due = d
        return due

    def _ensure_armed(self):
        if self.stopped or self._in_tick:
            return      # a running tick re-arms itself at its end
        now = self._now()
        due = self._next_due(now)
        if due is None:
            self._disarm()
            return
        try:
            ms = int(round(min(float(_MAX_SLEEP_MS), (due - now) * 1000.0)))
        except Exception:
            ms = _MAX_SLEEP_MS
        ms = max(TICK_MS, ms)
        want = now + ms / 1000.0
        if self._after_id is not None:
            if self._armed_due is not None and self._armed_due <= want + 0.0005 and not self._arm_lost(now):
                return          # already armed early enough
            self._disarm()
        try:
            self._after_id = self._schedule(ms, self._tick)
        except Exception as e:
            # The root is gone (or the injected scheduler failed): nothing can ever
            # run again, so stop instead of retrying on every call.
            self._after_id = None
            self._log_once(("schedule",), "fx.clock: scheduling the tick failed (%r) - clock stopped" % (e,))
            self._hard_stop()
            return
        self._armed_at = now
        self._armed_due = want
        self._armed_ms = ms

    def _arm_lost(self, now):
        """True when the pending tick can no longer fire: cancelled/fired behind
        our back (probe), or - with an injected scheduler - overdue by > 1 s."""
        if self._probe is not None:
            try:
                self._probe(self._after_id)
                return False
            except tk.TclError:
                return True
            except Exception:
                return False
        return self._armed_due is not None and now - self._armed_due > 1.0

    def _disarm(self):
        aid, self._after_id = self._after_id, None
        self._armed_due = None
        if aid is not None:
            try:
                self._cancel(aid)
            except Exception:
                pass

    # --- entry points ------------------------------------------------------------
    def _slot_ok(self, slot, what):
        """Slots key a dict: an unhashable one is refused (logged once), never raised."""
        try:
            hash(slot)
            return True
        except Exception as e:
            self._log_once(("slot", what), "fx.clock: %s with unhashable slot %r (%r) - refused"
                           % (what, type(slot).__name__, e))
            return False

    def _clear_slot(self, key):
        """Cancel-first: finish whatever occupies `key` (a finished tween's done()
        may re-occupy the slot, which is finished in turn). A runaway chain
        is cut after _SLOT_CHAIN_MAX finishes: the last occupant is cancelled
        (dead, no done) instead of being silently overwritten."""
        n = 0
        while not self.stopped:
            old = self._entries.get(key)
            if old is None:
                return
            if n >= _SLOT_CHAIN_MAX:
                self._log_once(("chain", key[1]), "fx.clock: slot %r re-occupied by done() %d times in a "
                                                  "row - chain cancelled" % (key[1], n))
                self._end_entry(old, final=False)
                continue
            self._end_entry(old, final=True)
            n += 1

    def _install(self, entry):
        """Cancel-first on (owner, slot), then register `entry`."""
        self._clear_slot(entry.key)
        if not self.stopped:
            self._entries[entry.key] = entry

    def tween(self, owner, slot, duration_ms, step, done=None, *, ease=linear, kind="colour",
              wait_until=None, wait_max_ms=None, valid=None, nav=False, interruptible=False):
        """step may be None (a pure timing tween: done() still runs at the end, at
        every level); any other non-callable step is refused (None). An unknown
        `kind` is treated as 'spatial' (the fail-safe class). wait_until without
        wait_max_ms holds t0 at most WAIT_MAX_DEFAULT_MS. interruptible=True (B10):
        any key or mouse press finishes it (final state) before that input acts."""
        if self.stopped or not self._thread_ok():
            return None
        if not self._alive(owner) or not self._slot_ok(slot, "tween"):
            return None
        if step is not None and not callable(step):
            self._log_once(("step", "tween"), "fx.clock: tween() with non-callable step %r - refused"
                           % (type(step).__name__,))
            return None
        e = _Tween()
        e.clock, e.owner, e.slot, e.key = self, owner, slot, self._key(owner, slot)
        if kind in KINDS:
            e.kind = kind
        else:
            e.kind = "spatial"
            self._log_once(("kind", repr(kind)[:40]), "fx.clock: unknown tween kind %r - treated as 'spatial'"
                           % (kind,))
        e.nav = bool(nav)
        e.interruptible = bool(interruptible)
        e.dead = False
        e.dur_ms = _ms(duration_ms)
        e.step, e.done = step, done
        e.ease = ease if callable(ease) else linear
        e.wait_until = wait_until if callable(wait_until) else None
        e.wait_max_s = (WAIT_MAX_DEFAULT_MS if wait_max_ms is None else _ms(wait_max_ms)) / 1000.0
        e.valid = valid if callable(valid) else None
        e.t0 = None
        e.waiting_since = None
        e.p = 0.0
        e.finished_by = None
        e.n_steps = 0
        e.created_locked = self._is_locked()
        lv = self.effective_level()
        # cancel-first: an old entry in this slot is finished/stopped/dropped first
        self._clear_slot(e.key)
        if self.stopped:          # a finished entry's done() may have stopped the clock
            return None
        if lv == "off" or (lv == "reduced" and e.kind in ("spatial", "blink")) or e.dur_ms <= 0:
            self._apply_final(e, "level" if e.dur_ms > 0 else "zero")
            return e
        self._entries[e.key] = e
        self._ensure_armed()
        return e

    def loop(self, owner, slot, interval_ms, step):
        if self.stopped or not self._thread_ok():
            return None
        if not self._alive(owner) or not callable(step) or not self._slot_ok(slot, "loop"):
            return None
        e = _Loop()
        e.clock, e.owner, e.slot, e.key = self, owner, slot, self._key(owner, slot)
        e.kind, e.nav, e.dead = "loop", False, False
        e.created_locked = self._is_locked()
        e.interval_s = max(TICK_MS, _ms(interval_ms)) / 1000.0
        e.step = step
        e.n_steps = 0
        e.due = self._now() + e.interval_s
        self._install(e)
        if self.stopped:
            return None
        self._ensure_armed()
        return e

    def later(self, owner, slot, ms, fn, *, flush=False):
        if self.stopped or not self._thread_ok():
            return None
        if not self._alive(owner) or not callable(fn) or not self._slot_ok(slot, "later"):
            return None
        e = _Later()
        e.clock, e.owner, e.slot, e.key = self, owner, slot, self._key(owner, slot)
        e.kind, e.nav, e.dead = "later", False, False
        e.created_locked = self._is_locked()
        e.due = self._now() + _ms(ms) / 1000.0
        e.fn = fn
        e.flush = bool(flush)
        self._install(e)
        if self.stopped:
            return None
        self._ensure_armed()
        return e

    def _match(self, owner, slot, kinds):
        out = []
        for e in list(self._entries.values()):
            if owner is not _ALL and e.owner is not owner:
                continue
            if slot is not None and e.slot != slot:
                continue
            if kinds is not None and e.kind not in kinds:
                continue
            out.append(e)
        return out

    def cancel(self, owner, slot=None):
        """Drop matching entries WITHOUT running a final step / fn. owner=None
        addresses root-level (owner None) entries."""
        if self.stopped or not self._thread_ok():
            return None
        for e in self._match(owner, slot, None):
            self._end_entry(e, final=False)
        self._ensure_armed()
        return None

    def finish(self, owner, slot=None, kinds=None):
        """Tweens: step(1.0) then done(); loops: stop; laters: drop."""
        if self.stopped or not self._thread_ok():
            return None
        if isinstance(kinds, str):
            kinds = (kinds,)
        elif kinds is not None:
            try:
                kinds = tuple(kinds)
            except Exception:
                self._log_once(("kinds",), "fx.clock: finish() with bad kinds %r - ignored" % (kinds,))
                return None
        for e in self._match(owner, slot, kinds):
            self._end_entry(e, final=True)
        self._ensure_armed()
        return None

    def finish_all(self):
        if self.stopped or not self._thread_ok():
            return None
        for e in self._match(_ALL, None, None):
            self._end_entry(e, final=True)
        self._ensure_armed()
        return None

    def flush(self, owner, slot):
        """Run a pending later() fn now (dropped silently if its owner is dead)."""
        if self.stopped or not self._thread_ok():
            return None
        if not self._slot_ok(slot, "flush"):
            return None
        e = self._entries.get(self._key(owner, slot))
        if e is not None and e.is_later and e.owner is owner:
            self._run_later(e)
        self._ensure_armed()
        return None

    def run_flush_marked(self):
        """Run every pending flush=True later() now (teardown step 1)."""
        if self.stopped or not self._thread_ok():
            return None
        for e in [x for x in self._entries.values() if x.is_later and x.flush]:
            if not e.dead:
                self._run_later(e)
        self._ensure_armed()
        return None

    def active(self):
        """Pending entry count (tweens + loops + laters; tokens not included)."""
        if self.stopped:
            return 0
        return len(self._entries)

    def pending_tokens(self):
        return 0 if self.stopped else len(self._tokens)

    # --- tokens (A2 / AM-2) ----------------------------------------------------
    def token(self, on_abort):
        """A token that is aborted (on_abort() called) on the next clock TIMER
        tick unless end_token() is called first. Never aborted by after_idle /
        update_idletasks processing."""
        if self.stopped or not self._thread_ok():
            return None
        tok = _Token(on_abort if callable(on_abort) else None)
        self._tokens.append(tok)
        self._ensure_armed()
        return tok

    def end_token(self, tok):
        """True if `tok` was still live (now ended); False otherwise."""
        if self.stopped or not self._thread_ok():
            return False
        if not isinstance(tok, _Token) or not tok.live:
            return False
        tok.live = False
        try:
            self._tokens.remove(tok)
        except ValueError:
            pass        # a running tick already moved it out of _tokens (ended from a sibling's on_abort)
        self._ensure_armed()
        return True

    # --- lock guard (A8) -------------------------------------------------------
    def exempt(self, widget):
        """Entries owned by `widget` or any descendant are lock-exempt."""
        if self.stopped or not self._thread_ok() or widget is None:
            return None
        try:
            ref = weakref.ref(widget)
        except TypeError:
            ref = (lambda w=widget: w)
        for r in self._exempt:
            if r() is widget:
                return None
        self._exempt.append(ref)
        return None

    def _is_exempt(self, owner):
        if owner is None or not self._exempt:
            return False
        live = []
        hit = False
        for r in self._exempt:
            w = r()
            if w is None:
                continue
            live.append(r)
            if not hit:
                node, depth = owner, 0
                while node is not None and depth < 64:
                    if node is w:
                        hit = True
                        break
                    node = getattr(node, "master", None)
                    depth += 1
        self._exempt = live
        return hit

    # --- input recorder ----------------------------------------------------------
    def install_recorder(self):
        """Generic <KeyPress>/<ButtonPress> bindings (add='+') on a dedicated
        bindtag (_REC_TAG) PREPENDED to every widget's bindtags, so the input is
        recorded before ANY handler of that event runs: widget-level ones (rail
        cells' <Button-1> -> show_screen), class, toplevel ('.') and keymap's
        bind_all ('all') ones. A specific binding elsewhere (root.bind('<Escape>'))
        cannot shadow it - Tk picks the most specific binding per tag, and nothing
        else binds this tag. Existing widgets are tagged now; widgets created
        later are tagged by pure-Tcl 'all'-tag <Map>/<FocusIn> bindings (add='+')
        before they can be clicked or typed into. Idempotent. Never unbound (tkinter 3.12's
        unbind(seq, funcid) would drop every other binding on that sequence);
        after stop() the handlers simply do nothing."""
        if self.stopped or not self._thread_ok() or self._recorder_installed or self.root is None:
            return None
        try:
            tkapp = self.root.tk
            tkapp.eval(_REC_TCL)
            self.root.bind_class(_REC_TAG, "<KeyPress>", self._on_key, add="+")
            self.root.bind_class(_REC_TAG, "<ButtonPress>", self._on_button, add="+")
            # <Map>: before a widget can be clicked. <FocusIn>: before an unmapped
            # widget (e.g. a pack slave squeezed to zero size) can get a key.
            for seq in ("<Map>", "<FocusIn>"):
                cur = tkapp.call("bind", "all", seq)
                if _REC_MAP_SCRIPT not in str(cur):
                    tkapp.call("bind", "all", seq, "+" + _REC_MAP_SCRIPT)
            tkapp.call("::fx_clock_rec_walk", str(self.root))
            self._recorder_installed = True
        except Exception as e:
            self._log_once(("recorder",), "fx.clock: input recorder not installed: %r" % (e,))
        return None

    def _on_key(self, event):
        if self.stopped:
            return None
        try:
            ks = event.keysym
            st = event.state if isinstance(event.state, int) else 0
            if ks and (st & _ALT_MASK) and not str(ks).startswith("Alt"):
                ks = "Alt-" + str(ks)
            self._last_input_t = self._now()
            self._last_input_key = ks
        except Exception:
            pass
        if self._block_input:
            return "break"
        self._finish_interruptible()
        return None

    def _on_button(self, event):
        if self.stopped:
            return None
        try:
            self._last_input_t = self._now()
            self._last_input_key = None
        except Exception:
            pass
        if self._block_input:
            return "break"
        self._finish_interruptible()
        return None

    def _finish_interruptible(self):
        """B10 (GAP_FX 1.4): a key or button press finishes every running interruptible tween (its final
        state, then done) before the input acts. Other entries (latches, the power-down, loops) are untouched.
        Never raises into the binding."""
        if self.stopped or not self._entries:
            return
        try:
            hit = [e for e in list(self._entries.values())
                   if e.is_tween and not e.dead and getattr(e, "interruptible", False)]
            if not hit:
                return
            for e in hit:
                self._end_entry(e, final=True, why="input")
            self._ensure_armed()
        except Exception as ex:
            self._log_once(("interrupt",), "fx.clock: finishing interruptible entries raised %r" % (ex,))

    def block_input(self, on=True):
        """B10: while on, the input recorder swallows every key and button press (returns 'break', so no
        other handler of that event runs). The power-down sets it; stop() ends it with everything else."""
        if not self._thread_ok():
            return None
        self._block_input = bool(on) and not self.stopped
        return None

    @property
    def input_blocked(self):
        return bool(self._block_input) and not self.stopped

    def note_input(self, keysym=None, t=None):
        """Harness hook: record an input as the recorder would."""
        if self.stopped or not self._thread_ok():
            return None
        self._last_input_t = self._now() if t is None else float(t)
        self._last_input_key = keysym
        return None

    def last_input(self):
        """(t_seconds or None, keysym or None). A key pressed with Alt held is
        reported as 'Alt-<keysym>' (e.g. 'Alt-1'); a mouse press as None."""
        return (self._last_input_t, self._last_input_key)

    # --- lifecycle ---------------------------------------------------------------
    def _hard_stop(self):
        self.stopped = True
        self._block_input = False
        self._disarm()
        for e in list(self._entries.values()):
            e.dead = True
        self._entries.clear()
        for tok in self._tokens:
            tok.live = False
        self._tokens = []
        self._exempt = []

    def stop(self):
        """Cancel the tick and drop everything without running it. Afterwards
        tween/loop/later/token (and every other entry point) are no-ops that
        return None, and nothing ever re-arms (AM-3)."""
        if self.stopped:
            return None
        if threading.get_ident() != self._thread_ident:
            if self.strict:
                raise AssertionError("fx.clock.stop called off the Tk thread")
            # Still refuse new work; the pending Tk timer is left for the owner's sweep.
            self.stopped = True
            self._after_id = None
            self._entries.clear()
            self._tokens = []
            return None
        self._hard_stop()
        return None

    # --- entry termination -------------------------------------------------------
    def _remove(self, e):
        if self._entries.get(e.key) is e:
            del self._entries[e.key]

    def _end_entry(self, e, final, why="finish"):
        """Terminate `e`. final=True finishes a tween (final step + done);
        loops/laters are always just dropped."""
        if e.dead:
            return
        e.dead = True
        self._remove(e)
        if final and e.is_tween:
            e.finished_by = why
            self._run_final(e)
        elif e.is_tween:
            e.finished_by = "cancel"

    def _apply_final(self, e, why):
        e.dead = True
        e.finished_by = why
        self._run_final(e)

    def _run_final(self, e):
        """step(1.0) then done(), each isolated; skipped entirely for a dead owner."""
        if not self._alive(e.owner):
            return
        e.p = 1.0
        try:
            if e.step is not None:
                e.step(1.0)
            e.n_steps += 1
        except Exception as ex:
            self._err(e.step, e.slot, ex, "final step")
            return
        if e.done is not None:
            try:
                e.done()
            except Exception as ex:
                self._err(e.done, e.slot, ex, "done")

    def _run_later(self, e):
        if e.dead:
            return
        e.dead = True
        self._remove(e)
        if not self._alive(e.owner):
            return
        try:
            e.fn()
        except Exception as ex:
            self._err(e.fn, e.slot, ex, "later fn")

    # --- the tick ----------------------------------------------------------------
    def _tick(self):
        self._after_id = None
        self._armed_due = None
        if self.stopped:
            return
        self._in_tick = True
        try:
            self._tick_body()
        finally:
            self._in_tick = False
            if not self.stopped:
                self._ensure_armed()

    def _degrade(self, kind, now, detail):
        self.trips[kind] = self.trips.get(kind, 0) + 1
        self.degraded_until = now + DEGRADE_MS / 1000.0
        self._late_hist = []
        self._cost_over = 0
        self._log_once(("trip", kind), "fx.clock: watchdog trip (%s: %s) - spatial/blink finished, "
                                       "effective level reduced for %d ms" % (kind, detail, DEGRADE_MS))
        for e in self._match(_ALL, None, ("spatial", "blink")):
            self._end_entry(e, final=True, why="watchdog")

    def _tick_body(self):
        now = self._now()
        t_cost0 = now
        armed_at = self._armed_at if self._armed_at is not None else now
        interval_ms = (now - armed_at) * 1000.0
        expected_ms = max(FRAME_MS, float(self._armed_ms or TICK_MS))
        self._armed_at = None
        self.ticks += 1
        spatial_active = any(e.is_tween and e.kind == "spatial" for e in self._entries.values())

        # 1. tokens not ended before this TIMER tick are aborted (A2 / AM-2)
        if self._tokens:
            toks, self._tokens = self._tokens, []
            for tok in toks:
                if not tok.live:
                    continue
                tok.live = False
                tok.aborted = True
                if tok.on_abort is not None:
                    try:
                        tok.on_abort()
                    except Exception as ex:
                        self._err(tok.on_abort, "token", ex, "token on_abort")
                if self.stopped:
                    return

        # 2. level switches: OFF finishes every tween, REDUCED spatial+blink ones
        lv = self.effective_level()
        if lv != "full":
            kinds = None if lv == "off" else ("spatial", "blink")
            for e in self._match(_ALL, None, kinds):
                if e.is_tween:
                    self._end_entry(e, final=True, why="level")
            if self.stopped:
                return

        # 3. lock guard (A8)
        if self._entries and self._is_locked():
            for e in list(self._entries.values()):
                if e.is_tween and e.nav and not e.dead and not e.created_locked and not self._is_exempt(e.owner):
                    self._end_entry(e, final=True, why="lock")
            if self.stopped:
                return

        # 4. watchdog (a): lateness, sampled only while a spatial tween is active
        if spatial_active:
            late = (interval_ms - expected_ms) > WATCHDOG_LATE_MS
            self._late_hist.append(late)
            if len(self._late_hist) > WATCHDOG_LATE_WINDOW:
                del self._late_hist[0]
            if sum(self._late_hist) >= WATCHDOG_LATE_OF:
                self._degrade("lateness", now, "interval %.1f ms" % interval_ms)
                if self.stopped:
                    return
        else:
            self._late_hist = []

        # 5. step entries (snapshot; entries added by callbacks run next tick)
        step_cost = 0.0
        for e in list(self._entries.values()):
            if e.dead or self._entries.get(e.key) is not e:
                continue
            if not self._alive(e.owner):
                e.dead = True                     # dropped silently: no step, no done
                self._remove(e)
                continue
            if e.is_tween:
                step_cost += self._step_tween(e, now)
            elif e.is_loop:
                if now + _DUE_SLACK_S >= e.due:
                    t_a = self._now()
                    e.due = e.due + e.interval_s
                    if e.due <= now:              # fell behind: resync, never burst
                        e.due = now + e.interval_s
                    try:
                        e.step(now)
                        e.n_steps += 1
                    except Exception as ex:
                        e.dead = True
                        self._remove(e)
                        self._err(e.step, e.slot, ex, "loop step")
                    step_cost += self._now() - t_a
            else:
                if now + _DUE_SLACK_S >= e.due:
                    self._run_later(e)
            if self.stopped:
                return

        # 6. watchdog (b): cost of non-final step callbacks on consecutive ticks
        step_ms = step_cost * 1000.0
        self.last_step_cost_ms = step_ms
        if step_ms > WATCHDOG_COST_MS:
            self._cost_over += 1
            if self._cost_over >= WATCHDOG_COST_TICKS:
                self._degrade("cost", self._now(), "steps %.1f ms" % step_ms)
        else:
            self._cost_over = 0

        cost_ms = (self._now() - t_cost0) * 1000.0
        self.tick_log.append((now, round(interval_ms, 3), round(cost_ms, 3), len(self._entries)))
        if len(self.tick_log) > TICK_LOG_CAP:
            del self.tick_log[:len(self.tick_log) - TICK_LOG_CAP]

    def _step_tween(self, e, now):
        """Advance one tween. Returns the NON-final step cost in seconds (final
        steps and done() are excluded from the watchdog)."""
        # valid() False -> final state this tick
        if e.valid is not None:
            ok = False
            try:
                ok = bool(e.valid())
            except Exception as ex:
                self._err(e.valid, e.slot, ex, "valid")
            if not ok:
                self._end_entry(e, final=True, why="invalid")
                return 0.0
        if e.t0 is None:
            if e.wait_until is not None:
                if e.waiting_since is None:
                    e.waiting_since = now
                ok = True
                try:
                    ok = bool(e.wait_until())
                except Exception as ex:
                    self._err(e.wait_until, e.slot, ex, "wait_until")
                    ok = True
                if not ok and (now - e.waiting_since) < e.wait_max_s:
                    return 0.0
            e.t0 = now
        dur_s = e.dur_ms / 1000.0
        el = now - e.t0
        raw = 1.0 if dur_s <= 0 else min(1.0, el / dur_s)
        max_adv = (PROGRESS_CLAMP_FRAMES * FRAME_MS / e.dur_ms) if e.dur_ms > 0 else 1.0
        # never backwards: time can step back (freeze_at released, a clock-source quirk)
        p = max(e.p, min(raw, e.p + max_adv))
        if el >= dur_s * DEADLINE_FACTOR - 1e-9:
            p = 1.0
        if p < 0.0:
            p = 0.0
        e.p = p
        if p >= 1.0:
            e.dead = True
            self._remove(e)
            e.finished_by = "natural"
            self._run_final(e)
            return 0.0
        if e.step is None:            # pure timing tween
            return 0.0
        t_a = self._now()
        try:
            e.step(e.ease(p))
            e.n_steps += 1
        except Exception as ex:
            e.dead = True
            self._remove(e)
            self._err(e.step, e.slot, ex, "step")
        return self._now() - t_a
