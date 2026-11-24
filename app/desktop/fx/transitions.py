"""fx.transitions - router transitions (P1 subset: the begin/end seam only).

MainWindow.show_screen calls, after its two guards:

    token = self.fx.transitions.begin(self._current_screen_name, name, back=not record_history)
    ... current.on_hide() / pack / on_show ...
    self.fx.transitions.end(token, name)

AM-1 (HANDOFF 5.3, overrides the spec's "emit inside begin"): begin() samples
kind, cause and key at dispatch time - before any blocking on_hide (ChatView's
draft flush can hold the Tk thread for up to 2 s) - and records them on the
token. nav.route is emitted by end() only, and only after the commit check
root._current_screen_name == name, carrying the begin-time fields.

A2 / AM-2: begin() registers a clock token. A begin() whose end() never comes
(an on_show that raised) is aborted on the clock's next TIMER tick - never by
idle processing such as update_idletasks. In P1 the abort only records the
fact; the P3 sweep visuals hang off it.

Never raises into app code. Visuals (router sweep, tag, subview, region
switch, power-down, boot pass) arrive in P3.
"""

import re
import sys

from . import clock as _clock_mod

__all__ = ["RouteToken", "Transitions", "NullTransitions"]

_ALT_KEY_RE = re.compile(r"^Alt-(?:Key-)?([1-4])$")


def _log(msg):
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


class RouteToken(object):
    """What begin() sampled. Opaque to callers; end() consumes it."""

    __slots__ = ("prev", "name", "back", "kind", "cause", "key", "deferred", "input_age_ms",
                 "clock_token", "ended", "aborted")

    def __init__(self, prev, name, back, kind, cause, key, deferred, input_age_ms):
        self.prev = prev
        self.name = name
        self.back = back
        self.kind = kind
        self.cause = cause
        self.key = key
        self.deferred = deferred
        self.input_age_ms = input_age_ms
        self.clock_token = None
        self.ended = False
        self.aborted = False

    def fields(self):
        return {"prev": self.prev, "kind": self.kind, "cause": self.cause, "key": self.key}

    def __repr__(self):
        state = "ended" if self.ended else ("aborted" if self.aborted else "live")
        return "<RouteToken %r->%r kind=%s cause=%s key=%s %s>" % (self.prev, self.name, self.kind, self.cause,
                                                                   self.key, state)


class Transitions(object):
    """The FX's router-transition controller (one per installed FX)."""

    def __init__(self, fx):
        self.fx = fx
        self.begun = 0
        self.ended = 0
        self.aborted = 0
        self.emitted = 0
        self.uncommitted = 0
        self._logged = set()

    # --- helpers -------------------------------------------------------------------
    def _log_once(self, key, msg):
        if key in self._logged:
            return
        self._logged.add(key)
        _log(msg)

    def _strict(self):
        return bool(getattr(self.fx, "strict", False))

    def _clock(self):
        clk = getattr(self.fx, "clock", None)
        if clk is None or getattr(clk, "stopped", False):
            return None
        return clk

    @staticmethod
    def _now(clk):
        # The clock's own notion of now (honours freeze_at), which is also the
        # time base of last_input().
        fn = getattr(clk, "_now", None) or getattr(clk, "now", None)
        if callable(fn):
            return fn()
        return clk.time_fn()

    def _sample(self, clk):
        """-> (deferred, cause, key, age_ms) sampled NOW. Fail-safe: user / no key."""
        d = False
        try:
            d = bool(_clock_mod.deferred())
        except Exception:
            d = False
        cause, key, age_ms = "user", None, None
        try:
            t_in, k_in = clk.last_input()
            if t_in is not None:
                age_ms = (self._now(clk) - float(t_in)) * 1000.0
            recent = age_ms is not None and age_ms <= _clock_mod.USER_INPUT_WINDOW_MS
            cause = "auto" if (d and not recent) else "user"
            if (k_in == "Escape" and age_ms is not None and age_ms <= _clock_mod.ESC_SAME_DISPATCH_MS
                    and not d):
                key = "ESC"
            elif recent and isinstance(k_in, str):
                m = _ALT_KEY_RE.match(k_in)
                if m:
                    key = "ALT+" + m.group(1)
        except Exception as e:
            self._log_once(("sample", type(e).__name__), "fx.transitions: input sampling failed: %r" % (e,))
        return d, cause, key, age_ms

    # --- the seam ------------------------------------------------------------------
    def begin(self, prev, name, back=False):
        """Sample kind/cause/key now and register a clock token. -> token or None."""
        try:
            clk = self._clock()
            if clk is None or not getattr(self.fx, "enabled", False):
                return None
            kind = "initial" if prev is None else ("back" if back else "forward")
            d, cause, key, age_ms = self._sample(clk)
            tok = RouteToken(prev, name, bool(back), kind, cause, key, d, age_ms)

            def on_abort(tok=tok):
                if tok.ended:
                    return
                tok.aborted = True
                self.aborted += 1

            ctok = clk.token(on_abort)
            if ctok is None:
                return None
            tok.clock_token = ctok
            self.begun += 1
            return tok
        except AssertionError:
            if self._strict():
                raise           # LOCALAI_FX_STRICT: the clock's off-Tk-thread assertion must reach the harness
            return None
        except Exception as e:
            self._log_once(("begin", type(e).__name__), "fx.transitions: begin failed: %r" % (e,))
            return None

    def end(self, token, name):
        """End the token; emit nav.route only if the navigation committed."""
        if token is None:
            return None
        try:
            if not isinstance(token, RouteToken):
                self._log_once(("end-type",), "fx.transitions: end() with a foreign token %r - ignored"
                               % (type(token).__name__,))
                return None
            if token.ended:
                return None
            token.ended = True
            self.ended += 1
            clk = getattr(self.fx, "clock", None)
            if clk is not None and token.clock_token is not None:
                try:
                    clk.end_token(token.clock_token)
                except AssertionError:
                    if self._strict():
                        raise
                except Exception as e:
                    self._log_once(("end_token", type(e).__name__), "fx.transitions: end_token failed: %r" % (e,))
            root = getattr(self.fx, "root", None)
            if root is None:
                return None
            current = getattr(root, "_current_screen_name", None)
            if current != name:
                self.uncommitted += 1
                return None
            self.emitted += 1
            self.fx.emit(root, "nav.route", name=name, prev=token.prev, kind=token.kind, cause=token.cause,
                         key=token.key, played=False)
        except AssertionError:
            if self._strict():
                raise           # LOCALAI_FX_STRICT: off-Tk-thread use must reach the harness
        except Exception as e:
            self._log_once(("end", type(e).__name__), "fx.transitions: end failed: %r" % (e,))
        return None

    def __repr__(self):
        return "<fx.Transitions begun=%d ended=%d aborted=%d emitted=%d>" % (self.begun, self.ended, self.aborted,
                                                                            self.emitted)


class NullTransitions(object):
    """NullFX's transitions: every entry point returns None and never raises."""

    __slots__ = ()

    def begin(self, /, *args, **kwargs):
        return None

    def end(self, /, *args, **kwargs):
        return None

    def __getattr__(self, name):
        if name.startswith("__"):
            raise AttributeError(name)
        return _noop

    def __repr__(self):
        return "<fx.NullTransitions>"


def _noop(*args, **kwargs):
    return None
