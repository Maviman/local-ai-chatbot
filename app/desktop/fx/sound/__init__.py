"""fx.sound - the sound bus seam: NullEngine, and (B12) boot / shutdown / get for the real engine.

P1 shipped the silent engine surface every fx hook can call unconditionally. B12 adds the real engine (engine.py: one
"fx-sound" thread; synth.py: the lab-exact cues; bridge.py: events -> cues; _win.py: the only device calls).
get() returns the shared NullEngine until boot() installs a real one. desktop_main calls boot() from B13 on (FD-9);
until then nothing boots it and the app keeps the NullEngine.

    boot(prefs=None, silent=False, ready_probe=None, log_path=None, server=None) -> Engine | NULL_ENGINE
        Reads the kill switches NOW, itself (X-26: it runs in desktop_main before MainWindow / fx.install):
        LOCALAI_FX=0 or <theme.DATA_DIR>/fx_disabled (fx.kill_switch_active()), and LOCALAI_SFX=0, give the NullEngine,
        and so do a platform other than Windows, an unimportable fx package (fail closed) and any start-up error.
        A second boot() while the engine is live returns the same engine (one closed by app.shutdown, or dead, is
        shut down and replaced by a new one); a switch that became active between two
        calls shuts the live engine down and returns the NullEngine (the switch always wins).
        ready_probe / log_path / server are accepted for the B14b watches and unused in v1.
    shutdown(timeout=0.25)   closes the booted engine (joins its thread for at most `timeout` s); get() -> NullEngine.
    get()                    the booted engine, or the NullEngine.

Rules (FOUNDATION_CONTRACT section 0): stdlib only, and nothing under fx/sound ever imports the Tk binding (static grep
test). Every NullEngine method is a no-op that never raises, whatever it is passed. boot/shutdown/get never raise.
"""
import os
import sys
import threading

__all__ = ["NullEngine", "NULL_ENGINE", "get", "boot", "shutdown"]


class NullEngine(object):
    """The engine surface with no device, no thread and no state.

    emit/attach_window/detach/bind_store/shutdown do nothing; enabled_for()
    is always False (so callers keep their legacy feedback, e.g. bell());
    status() is 'off'."""

    __slots__ = ()

    enabled = False

    def emit(self, /, *args, **kwargs):
        return None

    def attach_window(self, /, *args, **kwargs):
        return None

    def detach(self, /, *args, **kwargs):
        return None

    def bind_store(self, /, *args, **kwargs):
        return None

    def shutdown(self, /, *args, **kwargs):
        return None

    def enabled_for(self, /, *args, **kwargs):
        return False

    def status(self, /, *args, **kwargs):
        return "off"

    def __repr__(self):
        return "<fx.sound.NullEngine>"


NULL_ENGINE = NullEngine()

_engine = NULL_ENGINE
_lock = threading.Lock()
_SFX_OFF_VALUES = ("0", "false", "off", "no")
_logged = set()


def get():
    """The module's engine: the NullEngine until boot() installs a real one."""
    return _engine


def _log_once(key, msg):
    if key in _logged:
        return
    _logged.add(key)
    try:
        sys.stderr.write("fx sound: " + str(msg) + "\n")
        sys.stderr.flush()
    except Exception:
        pass


def _switch_reason():
    """Why the engine must stay off right now (a short reason), or None. Read NOW, never cached. Fails closed."""
    try:
        v = os.environ.get("LOCALAI_SFX")
        if v is not None and v.strip().lower() in _SFX_OFF_VALUES:
            return "LOCALAI_SFX=%s" % v.strip()
    except Exception:
        return "environment unreadable"
    if sys.platform != "win32":
        return "not Windows"
    try:
        from .. import kill_switch_active      # the fx package: LOCALAI_FX=0 or <theme.DATA_DIR>/fx_disabled
    except Exception as e:
        return "fx kill switch unavailable (%s)" % (type(e).__name__,)
    try:
        if kill_switch_active():
            return "fx kill switch"
    except Exception as e:
        return "fx kill switch failed (%s)" % (type(e).__name__,)
    return None


def boot(prefs=None, silent=False, ready_probe=None, log_path=None, server=None):
    """Start the sound engine (see the module docstring). -> the Engine, or NULL_ENGINE. Never raises."""
    global _engine
    try:
        with _lock:
            reason = _switch_reason()
            if reason is not None:
                old, _engine = _engine, NULL_ENGINE
                if old is not NULL_ENGINE:
                    try:
                        old.shutdown(0.25)
                    except Exception:
                        pass
                _log_once(("off", reason), "sound off (%s)" % (reason,))
                return NULL_ENGINE
            cur = _engine
            if cur is not NULL_ENGINE and getattr(cur, "is_live", lambda: False)():
                return cur
            if cur is not NULL_ENGINE:
                # fix ADV1-04: an engine closed by app.shutdown (or dead) is replaced; close it first so its thread
                # does not outlive it
                _engine = NULL_ENGINE
                try:
                    cur.shutdown(0.25)
                except Exception:
                    pass
            from . import engine as _engine_mod
            _engine = _engine_mod.Engine(prefs, silent=bool(silent))
            return _engine
    except Exception as e:
        _log_once(("boot", type(e).__name__), "boot failed (%r) - sound off" % (e,))
        _engine = NULL_ENGINE
        return NULL_ENGINE


def shutdown(timeout=0.25):
    """Close the booted engine (bounded join); get() returns the NullEngine afterwards. -> True when no engine thread
    is left running. Never raises."""
    global _engine
    try:
        with _lock:
            old, _engine = _engine, NULL_ENGINE
        if old is not NULL_ENGINE:
            return bool(old.shutdown(timeout))
        return True
    except Exception:
        return False
