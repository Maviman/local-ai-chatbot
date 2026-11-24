"""fx.sound.engine - the sound engine v1 (B12; AESTHETIC_SPEC 5.5 / S-01 / S-03 / S-13 / S-16, GAP_FX 3.2-3.5).

ONE daemon thread, "fx-sound", does everything that takes time or touches Windows: it renders the cue bank (synth,
2 ms yields between recipes), requantises it on a volume change, runs the bridge, arbitrates and plays through _win
(the only module with device calls; LOCALAI_SFX_SILENT=1 turns its device calls into recorded no-ops). The engine never
imports or touches Tk and schedules no Tk timer.

The public methods are called on the Tk thread and only touch memory: emit() checks a few flags and puts a tuple on an
unbounded SimpleQueue whose length it caps itself (QUEUE_MAX; over the cap the event is dropped and counted), so it
never blocks and never raises. Volume changes, mutes, resets and test() auditions travel on the same FIFO as control
items, so a test() issued after a volume change always plays at the new gain (S-13 F6).

The thread loop: wait for an item (or the next deadline: the 150 ms load_online verdict, the error retry, the
no-device recheck) -> coalesce: sleep until the first item's emit time + 8 ms, then drain -> the bridge turns each
event into candidates -> per candidate the filters that belong to it (closed, master / volume, category / muted cue,
max age, the while_closed policy when the window is hidden, DND) -> the batch keeps its single most severe remaining
candidate (highest priority, the latest wins ties; a TEST audition always wins) -> its per-cue min gap and the one-voice
rule (play only over nothing, a lower priority, or an equal-priority cuttable cue) -> waveOutGetNumDevs > 0 ->
PlaySoundW.

Health (GAP_FX 3.2, simplified): no device -> status 'no_device' (emit refuses; rechecked every 5 s);
3 consecutive FALSE results from PlaySoundW -> status 'error' (emit refuses), retried after 60 s (doubling to 600 s
while it keeps failing; a successful play resets it). Recovering from either resets the bridge.

v1 scope (BUILD_PLAN B12, X-16): the emitted cues, volume, master, categories, presence (reply_landed = away,
while_closed = replies) and DND (respect_dnd defaults to True). No watches: the llama-health ready watch (power_on),
the long-load wait pings and the send-variant selector are B14b; transmit always plays the default chirp.
"""
import itertools
import math
import queue
import sys
import threading
import time

from . import bridge as _bridge
from . import synth as _synth

__all__ = ["Engine", "SOUND_DEFAULTS", "normalise_prefs", "snap_volume", "QUEUE_MAX", "COALESCE_S",
           "PLAY_FAIL_LIMIT", "ERROR_RETRY_S", "ERROR_RETRY_MAX_S", "NO_DEVICE_RECHECK_S", "LOG_MAX", "TAIL_GUARD_S"]

# fx/prefs.py FX_DEFAULTS["sound"] (D-1..D-3; respect_dnd True). Mirrored here so fx/sound stays self-contained;
# tests/fx/test_fx_sound.py pins the two equal.
SOUND_DEFAULTS = {
    "enabled": True,
    "volume": 40,
    "cat": {c: True for c in _bridge.CATEGORIES},
    "reply_landed": "away",
    "while_closed": "replies",
    "respect_dnd": True,
    "wait_pings": False,
    "signal_acquired": False,
    "context_alert": False,
    "muted_cues": [],
    "transmit_variant": "chirp",
}
_ENUMS = {"reply_landed": ("away", "always", "off"), "while_closed": ("silent", "replies", "all"),
          "transmit_variant": ("chirp", "blip")}
_BOOLS = ("enabled", "respect_dnd", "wait_pings", "signal_acquired", "context_alert")

QUEUE_MAX = 32                 # pending events; more are dropped and counted (S-01)
COALESCE_S = 0.008             # wall clock from the first emit of a batch (S-03)
RENDER_PAUSE_S = 0.002         # the mandatory yields between cue renders (E5)
PLAY_FAIL_LIMIT = 3            # consecutive FALSE results from PlaySoundW -> 'error'
ERROR_RETRY_S = 60.0
ERROR_RETRY_MAX_S = 600.0
NO_DEVICE_RECHECK_S = 5.0
LOG_MAX = 256                  # decision_log() ring
# Sound nav/cut-off fix (2026-10-12): a cue counts as sounding for DURATION_MS + this long. The tail of an async sound
# is still in Windows' output pipeline after its last sample was handed over (the reason _win pads every image with
# _win.TAIL_PAD_MS of silence; pinned equal by test_fx_sound), and a PlaySoundW for the next cue throws that tail away.
# So a cue of lower (or equal non-cuttable) priority waits out the drain too instead of cutting the one before it.
TAIL_GUARD_S = 0.150
DND_STATES = frozenset((2, 3, 4, 6, 7))
QUNS_NOT_PRESENT = 1
_REFUSE_STATUS = frozenset(("error", "no_device"))


def snap_volume(v):
    """fx/prefs.py snap_volume: the nearest 5, ties up (37 -> 35, 38 -> 40, 12.5 -> 15)."""
    return int(math.floor(v / 5.0 + 0.5)) * 5


def _is_num(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def normalise_prefs(raw):
    """A full, valid sound-prefs dict from anything (the raw 'fx.sound' object of desktop_theme.json, a PrefsStore
    snapshot, None, junk). Per leaf, exactly the store's rules: a wrong type / unknown choice / out-of-range value gives
    that leaf's default; the volume snaps to the nearest 5 within 0..100. Never raises."""
    out = {k: (dict(v) if isinstance(v, dict) else (list(v) if isinstance(v, list) else v))
           for k, v in SOUND_DEFAULTS.items()}
    if not isinstance(raw, dict):
        return out
    try:
        for k in _BOOLS:
            v = raw.get(k)
            if isinstance(v, bool):
                out[k] = v
        v = raw.get("volume")
        try:
            if _is_num(v) and math.isfinite(v) and 0 <= v <= 100:
                out["volume"] = snap_volume(v)
        except Exception:
            pass
        cat = raw.get("cat")
        if isinstance(cat, dict):
            for c in _bridge.CATEGORIES:
                cv = cat.get(c)
                if isinstance(cv, bool):
                    out["cat"][c] = cv
        for k, choices in _ENUMS.items():
            v = raw.get(k)
            if isinstance(v, str) and v in choices:
                out[k] = v
        mc = raw.get("muted_cues")
        if isinstance(mc, (list, tuple)) and all(isinstance(x, str) for x in mc):
            # B12-H3: de-duplicated in order (a hand-edited file with 10**6 copies made every candidate scan them)
            out["muted_cues"] = list(dict.fromkeys(x for x in mc if x in _bridge.CUE_POLICY))
    except Exception:
        pass
    return out


class _Ctl(object):
    """A control item on the engine's FIFO (never counted against QUEUE_MAX)."""

    __slots__ = ("op", "arg", "t")

    def __init__(self, op, arg=None, t=0.0):
        self.op, self.arg, self.t = op, arg, t


_STOP = _Ctl("stop")


class _Sounding(object):
    __slots__ = ("cue", "prio", "cuttable", "end")

    def __init__(self, cue, prio, cuttable, end):
        self.cue, self.prio, self.cuttable, self.end = cue, prio, cuttable, end


def _stderr_log(msg):
    try:
        sys.stderr.write("fx sound: " + str(msg).rstrip("\n") + "\n")
        sys.stderr.flush()
    except Exception:
        pass


class Engine(object):
    """The live engine. Built by fx.sound.boot() (production) or directly by tests.

    prefs: the raw sound prefs (normalised here). silent: latch _win's silent mode for this process.
    Test seams: win (a _win-like object), synth, clock (seconds, perf_counter-like), rate (force 48000 / 44100),
    start=False (no thread: drive it with pump()), coalesce_s, render_pause_s, log."""

    def __init__(self, prefs=None, *, silent=False, win=None, synth=None, clock=None, rate=None, start=True,
                 coalesce_s=COALESCE_S, render_pause_s=RENDER_PAUSE_S, log=None):
        if win is None:
            from . import _win as win
        self._win = win
        if silent:
            fs = getattr(win, "force_silent", None)
            if callable(fs):
                fs()
        self._synth = synth if synth is not None else _synth
        self._now = clock if callable(clock) else time.perf_counter
        self._forced_rate = rate
        self._coalesce_s = float(coalesce_s)
        self._render_pause_s = float(render_pause_s)
        self._log_fn = log if callable(log) else _stderr_log
        self._logged = set()

        self._q = queue.SimpleQueue()
        self._qmax = QUEUE_MAX
        self._prefs = normalise_prefs(prefs)
        self._bridge = _bridge.Bridge()
        self._seq = itertools.count(1)
        self._log = []                       # decision log ring (appended on the fx-sound thread only)
        self._stats = {}
        self._dropped = 0                    # emits refused because the queue was full
        self._emitted = 0
        self._refused = 0

        self._status = "starting"            # starting | ready | no_device | error
        self._closed = False                 # shutdown() was called
        self._closed_play = False            # app.shutdown seen: nothing plays after power_off
        self._shutdown_req = False           # app.shutdown emitted (written by emit() only; ADV1-01)
        self._stop_flag = False
        self._dead = False                   # the thread has exited
        self._prefs_on = True                # master on and volume > 0 (written by _apply_prefs only)
        self._hwnd = None
        self._store = None
        self._unsub = None
        self._rebuild_pending = False
        self._mute_pending = False
        self._reset_pending = False

        self._rate = None
        self._floats = None
        self._bank = {}                      # cue -> device buffer
        self._bank_volume = None
        self._render_s = None
        self._current = None
        self._last_played = {}
        self._fail_run = 0
        self._retry_s = ERROR_RETRY_S
        self._retry_at = None
        self._recheck_at = None
        self._batch_presence = None
        self._batch_quns = None
        self._prefs_on = bool(self._prefs["enabled"] and self._prefs["volume"] > 0)

        self._thread = None
        if start:
            self._thread = threading.Thread(target=self._run, name="fx-sound", daemon=True)
            self._thread.start()

    def __repr__(self):
        return "<fx.sound.Engine %s rate=%s>" % (self.status(), self._rate)

    # ================================================================== Tk-thread API (memory only, never raises)
    enabled = True

    def emit(self, event, data=None, **info):
        """Queue one seam event for the fx-sound thread. -> True when queued; False (nothing queued) when the engine
        is closed or dead, master is off, the volume is 0, there is no device, it is in the error state, the event is
        not a string, or QUEUE_MAX events are already waiting (dropped and counted). Never blocks, never raises.
        'app.shutdown' (at most once) closes the engine for every later emit / test() even when it cannot be queued
        (master off, volume 0, no device, error), and it is never lost to the QUEUE_MAX cap (fix ADV1-01)."""
        try:
            if isinstance(event, str) and event == "app.shutdown" and not self._shutdown_req:
                self._shutdown_req = True
                if not (self._closed or self._closed_play or self._dead or not self._prefs_on
                        or self._status in _REFUSE_STATUS):
                    self._q.put((self._now(), event, dict(data) if isinstance(data, dict) else {}))
                    self._emitted += 1
                    return True
                self._refused += 1
                return False
            if (self._closed or self._closed_play or self._shutdown_req or self._dead or not self._prefs_on
                    or self._status in _REFUSE_STATUS):
                self._refused += 1
                return False
            if not isinstance(event, str):
                self._refused += 1
                return False
            if self._q.qsize() >= self._qmax:
                self._dropped += 1
                return False
            if info:
                payload = dict(data) if isinstance(data, dict) else {}
                payload.update(info)
            else:
                payload = data if data is not None else {}
            self._q.put((self._now(), event, payload))
            self._emitted += 1
            return True
        except Exception:
            return False

    def enabled_for(self, cue):
        """Would `cue` be allowed to play by the settings (master, volume, its category, the muted list) and the
        engine's state? Cheap; used e.g. by MainWindow._on_close to decide on its bell() fallback."""
        try:
            pol = _bridge.CUE_POLICY.get(cue)
            if pol is None or not self._is_open():
                return False
            p = self._prefs
            return bool(p["cat"].get(pol[0], False)) and cue not in p["muted_cues"]
        except Exception:
            return False

    def test(self, what=None):
        """Audition one cue (a cue name, or a category / 'master' -> its representative cue) at the committed volume.
        Bypasses category, muted cues, presence, DND, max age and min gap; never master, volume 0, the device state
        or a closed engine. -> the cue's duration in ms when queued, else None. No Tk call."""
        try:
            cue = _bridge.TEST_CUE.get(what, what) if what is not None else "affirm"
            if cue not in _bridge.CUE_POLICY or not self._is_open() or self._q.qsize() >= self._qmax:
                return None
            self._q.put(_Ctl("test", cue, self._now()))
            return int(_synth.DURATION_MS[cue])
        except Exception:
            return None

    def status(self):
        """'starting' | 'ready' | 'off' (master off, volume 0, shut down) | 'no_device' | 'error' (cached state)."""
        try:
            if self._closed or self._closed_play or self._shutdown_req:
                return "off"
            if self._dead:
                return "error"
            p = self._prefs
            if not p["enabled"] or p["volume"] <= 0:
                return "off"
            return self._status
        except Exception:
            return "error"

    def available(self):
        return self.status() == "ready"

    def describe(self, category):
        return _bridge.CATEGORY_CAPTION.get(category, "")

    def set_prefs(self, prefs):
        """Adopt a raw sound-prefs dict (normalised). A volume change requantises off-thread.

        FULL SNAPSHOT ONLY (fix ADV1-05): the dict REPLACES the prefs, it is not merged onto them; every leaf that is
        missing or invalid takes its default. set_prefs({'volume': 55}) therefore also turns every category back on,
        clears the muted list and respects DND again. Pass the whole 'fx.sound' object (bind_store does)."""
        try:
            self._apply_prefs(normalise_prefs(prefs))
        except Exception as e:
            self._log_once(("set_prefs", type(e).__name__), "set_prefs failed: %r" % (e,))

    def bind_store(self, store):
        """Follow a PrefsStore's 'fx.sound' leaves (fx.install calls this on the Tk thread). The subscription
        callback runs synchronously there and only swaps the snapshot / enqueues control items."""
        try:
            self._unbind_store()
            snapshot = normalise_prefs(store.get("fx.sound"))
            self._store = store
            self._unsub = store.subscribe("fx.sound", self._on_store_change)
            self._apply_prefs(snapshot)
        except Exception as e:
            self._store = self._unsub = None
            self._log_once(("bind_store", type(e).__name__), "bind_store failed: %r" % (e,))

    def attach_window(self, hwnd):
        """The app window's HWND (fx calls this on every root <Map>); presence is read on the fx-sound thread."""
        try:
            h = int(hwnd)
            self._hwnd = h if h > 0 else None
        except Exception:
            self._hwnd = None

    def detach(self, *args, **kwargs):
        """fx.teardown: forget the window and stop following the store."""
        self._hwnd = None
        self._unbind_store()

    def shutdown(self, timeout=0.25):
        """Close (nothing more is queued or played; a cue already playing keeps playing, e.g. power_off) and join the
        thread for at most `timeout` s (also during the initial render: it stops between two recipes). -> True when
        the thread has exited. Idempotent, never raises."""
        try:
            self._closed = True
            self._stop_flag = True
            self.detach()
            self._q.put(_STOP)
            t = self._thread
            if t is None:
                return True
            if t is not threading.current_thread():
                t.join(max(0.0, float(timeout)))
            return not t.is_alive()
        except Exception:
            return False

    def is_live(self):
        """Not shut down, not closed by app.shutdown (fix ADV1-04: such an engine refuses everything for good, so
        boot() must replace it) and the thread (if any) still running."""
        return (not self._closed and not self._closed_play and not self._shutdown_req and not self._dead
                and (self._thread is None or self._thread.is_alive()))

    def stats(self):
        try:
            s = dict(self._stats)
            s.update({"status": self.status(), "rate": self._rate, "render_s": self._render_s,
                      "emitted": self._emitted, "dropped_queue": self._dropped, "refused": self._refused,
                      "queued": self._q.qsize(), "bank_volume": self._bank_volume, "fail_run": self._fail_run,
                      "bridge_resets": self._bridge.resets, "thread_alive": bool(self._thread and
                                                                                  self._thread.is_alive())})
            return s
        except Exception:
            return {}

    def decision_log(self):
        """The last LOG_MAX decisions, oldest first: dicts with t, event, cue, verdict (played / dropped:<why> /
        silent:<why> / ...) and why."""
        try:
            return list(self._log)
        except Exception:
            return []

    # ================================================================== prefs plumbing (Tk thread)
    def _on_store_change(self, path, value):
        try:
            store = self._store
            if store is not None:
                self._apply_prefs(normalise_prefs(store.get("fx.sound")))
        except Exception as e:
            self._log_once(("store_change", type(e).__name__), "prefs change not applied: %r" % (e,))

    def _unbind_store(self):
        unsub, self._unsub, self._store = self._unsub, None, None
        if unsub is not None:
            try:
                unsub()
            except Exception:
                pass

    def _apply_prefs(self, new):
        old = self._prefs
        self._prefs = new
        was_on = old["enabled"] and old["volume"] > 0
        now_on = bool(new["enabled"] and new["volume"] > 0)
        self._prefs_on = now_on
        if new["volume"] != old["volume"] and not self._rebuild_pending:
            self._rebuild_pending = True
            self._q.put(_Ctl("rebuild"))
        # at most one pending mute and one pending reset, however fast master / volume are toggled
        if was_on and not now_on and not self._mute_pending:
            self._mute_pending = True
            self._q.put(_Ctl("mute"))
        elif now_on and not was_on and not self._reset_pending:
            self._reset_pending = True
            self._q.put(_Ctl("reset"))

    def _is_open(self):
        """emit()'s gate, read from its parts each time (each part has ONE writer: closed / prefs / shutdown_req on the
        caller's thread, status / closed_play / dead on the fx-sound thread), so no thread can write back a stale verdict."""
        return not (self._closed or self._closed_play or self._shutdown_req or self._dead or not self._prefs_on
                    or self._status in _REFUSE_STATUS)

    # ================================================================== the fx-sound thread
    def _log_once(self, key, msg):
        if key in self._logged or len(self._logged) > 256:
            return
        self._logged.add(key)
        try:
            self._log_fn(msg)
        except Exception:
            pass

    def _bump(self, key, n=1):
        self._stats[key] = self._stats.get(key, 0) + n

    def _decide(self, t, event, cue, verdict, why=""):
        # event names and reasons can come from any caller: the ring never holds more than a short label of them
        self._log.append({"t": t, "event": event[:64] if isinstance(event, str) else "?", "cue": cue,
                          "verdict": verdict[:96], "why": why[:64] if isinstance(why, str) else ""})
        if len(self._log) > LOG_MAX:
            del self._log[:len(self._log) - LOG_MAX]
        self._bump(verdict.split(":", 1)[0] if verdict.startswith(("silent", "ignored", "armed", "pending",
                                                                     "presence")) else verdict)

    def _set_status(self, status):
        self._status = status

    def _run(self):
        try:
            if not self._bootstrap():
                return
            while True:
                item = self._wait()
                if item is _STOP:
                    break
                batch = []
                stop = False
                if item is not None:
                    batch.append(item)
                    first_t = item.t if isinstance(item, _Ctl) else item[0]
                    wait = first_t + self._coalesce_s - self._now()
                    if wait > 0 and not isinstance(item, _Ctl):
                        time.sleep(min(wait, self._coalesce_s))
                    stop = self._drain(batch)
                self._service(batch)
                if stop:
                    break
        except Exception as e:
            self._log_once(("thread", type(e).__name__), "fx-sound thread failed: %r - sound is off" % (e,))
            self._status = "error"
        finally:
            self._dead = True

    def _wait(self):
        timeout = self._next_timeout()
        try:
            if timeout is None:
                return self._q.get()
            return self._q.get(timeout=max(0.0, timeout))
        except queue.Empty:
            return None

    def _drain(self, batch):
        """Move everything queued now into batch. -> True when a stop item was seen (the batch still runs)."""
        while True:
            try:
                it = self._q.get_nowait()
            except queue.Empty:
                return False
            if it is _STOP:
                return True
            batch.append(it)

    def _next_timeout(self):
        now = self._now()
        deadlines = [d for d in (self._bridge.pending_deadline(),
                                 self._retry_at if self._status == "error" else None,
                                 self._recheck_at if self._status == "no_device" else None) if d is not None]
        if not deadlines:
            return None
        return min(deadlines) - now

    def pump(self, now=None):
        """Tests only (start=False): run one batch of everything queued now, plus due deadlines, synchronously."""
        if self._thread is not None:
            raise RuntimeError("pump() is for an engine built with start=False")
        if self._floats is None and not self._stop_flag and not self._dead:
            if not self._bootstrap():
                self._dead = True
                return False
        batch = []
        stop = self._drain(batch)
        self._service(batch, now)
        if stop:
            self._dead = True
        return True

    # --- render ---------------------------------------------------------------------------------------------
    def _pick_rate(self):
        if self._forced_rate in _synth.RATES:
            return self._forced_rate
        for r in _synth.RATES:
            try:
                if self._win.format_supported(r):
                    return r
            except Exception:
                pass
        return _synth.FALLBACK_RATE

    def _bootstrap(self):
        """Render the bank (on this thread). -> False when shut down during the render or the render failed."""
        t0 = time.perf_counter()
        try:
            rate = self._pick_rate()
            floats = self._synth.render_all(rate, names=_synth.CUES, pause_s=self._render_pause_s,
                                            stop=lambda: self._stop_flag)
            if floats is None or self._stop_flag:
                self._bump("render_stopped")
                return False
            missing = [c for c in _bridge.CUES if c not in floats]
            if missing:
                raise ValueError("render_all returned no %s" % (missing,))
            self._rate, self._floats = rate, floats
            if not self._requantise():
                return False
        except Exception as e:
            self._log_once(("render", type(e).__name__), "cue render failed: %r - sound is off" % (e,))
            self._bump("render_failed")
            self._retry_at = None
            self._set_status("error")
            return False
        self._render_s = time.perf_counter() - t0
        n = self._num_devs()
        if n > 0:
            self._set_status("ready")
        else:
            self._set_status("no_device")
            self._recheck_at = self._now() + NO_DEVICE_RECHECK_S
        self._decide(self._now(), "boot", None, "ready" if n > 0 else "no_device",
                     "rate=%s render=%.3fs" % (self._rate, self._render_s))
        return True

    def _requantise(self):
        """(Re)build the device buffers at the current volume, yielding between cues. -> False if stopped."""
        self._rebuild_pending = False
        vol = self._prefs["volume"]
        if self._floats is None or vol == self._bank_volume:
            return True
        bank, by_src, first = {}, {}, True
        for cue in _bridge.CUES:
            if self._stop_flag:
                return False
            src = id(self._floats[cue])
            buf = by_src.get(src)
            if buf is None:
                if not first and self._render_pause_s:
                    time.sleep(self._render_pause_s)
                first = False
                pcm = self._synth.quantise(self._floats[cue], vol)
                buf = by_src[src] = self._win.make_buffer(self._synth.wav_bytes(pcm, self._rate))
            bank[cue] = buf
        self._bank, self._bank_volume = bank, vol
        self._bump("rebuilds")
        return True

    # --- one batch ------------------------------------------------------------------------------------------
    def _service(self, batch, now=None):
        now = self._now() if now is None else now
        self._batch_presence = self._batch_quns = None
        prefs = self._prefs
        cands, shutdown_seen = [], False
        for item in batch:
            if isinstance(item, _Ctl):
                c = self._control(item)
                if c is not None:
                    cands.append(c)
                prefs = self._prefs
                continue
            t, event, payload = item
            self._bump("events")
            if event == "app.shutdown":
                shutdown_seen = True
            cs, notes = self._bridge.translate(event, payload, t, prefs, self._presence)
            for n in notes:
                if n.startswith(("bad_event", "unknown_event")):
                    self._bump(n.split(":", 1)[0])
                self._decide(t, event, None, n)
            cands.extend(cs)
        cands.extend(self._bridge.due(now))
        if cands:
            self._arbitrate(cands, now)
        if shutdown_seen:
            self._closed_play = True
        self._tick(now)

    def _control(self, item):
        op = item.op
        if op == "rebuild":
            try:
                self._requantise()
            except Exception as e:          # keep the previous bank (still valid) rather than lose the thread
                self._rebuild_pending = False
                self._bump("rebuild_failed")
                self._log_once(("rebuild", type(e).__name__), "volume rebuild failed: %r - kept the old level" % (e,))
        elif op == "mute":
            self._mute_pending = False
            cur = self._current
            if cur is not None and self._now() < cur.end:
                self._win.stop(self._rate or _synth.SAMPLE_RATE)
                self._decide(self._now(), "mute", cur.cue, "stopped")
            self._current = None
        elif op == "reset":
            self._reset_pending = False
            self._bridge.reset()
        elif op == "test":
            return _bridge.Candidate(item.arg, item.t, "test", why="test", test=True)
        return None

    def _tick(self, now):
        if self._status == "error" and self._retry_at is not None and now >= self._retry_at:
            self._retry_at = None
            self._fail_run = 0
            self._bridge.reset()
            n = self._num_devs()
            if n > 0:
                self._set_status("ready")
                self._decide(now, "health", None, "retry:ready")
            else:
                self._set_status("no_device")
                self._recheck_at = now + NO_DEVICE_RECHECK_S
                self._decide(now, "health", None, "retry:no_device")
        elif self._status == "no_device" and self._recheck_at is not None and now >= self._recheck_at:
            if self._num_devs() > 0:
                self._recheck_at = None
                self._bridge.reset()
                self._set_status("ready")
                self._decide(now, "health", None, "device:back")
            else:
                self._recheck_at = now + NO_DEVICE_RECHECK_S

    def _num_devs(self):
        try:
            return int(self._win.num_devs())
        except Exception:
            return 0

    def _presence(self):
        """(hidden, away) for this batch, or None when unknown (no window attached / the query failed). Windows'
        'user not present' notification state counts as away."""
        if self._batch_presence is None:
            pres = None
            if self._hwnd:
                try:
                    pres = self._win.presence(self._hwnd)
                except Exception:
                    pres = None
            if self._quns() == QUNS_NOT_PRESENT:
                pres = (pres[0] if pres else False, True)
            self._batch_presence = pres if pres is not None else False
        return self._batch_presence or None

    def _quns(self):
        if self._batch_quns is None:
            try:
                self._batch_quns = int(self._win.notification_state() or 0)
            except Exception:
                self._batch_quns = 0
        return self._batch_quns

    # --- arbitration ----------------------------------------------------------------------------------------
    def _candidate_verdict(self, c, prefs, now):
        """Filters that belong to the candidate itself (applied to every candidate BEFORE the batch picks one):
        closed, error, master, volume, category, muted cue, max age, the while_closed policy, DND."""
        if self._closed or (self._closed_play and c.cue != "power_off"):
            return "dropped:closed"
        if self._status == "error":
            return "dropped:error"
        if not prefs["enabled"]:
            return "dropped:master"
        if prefs["volume"] <= 0:
            return "dropped:volume"
        if c.test:
            return None
        cat = _bridge.CUE_POLICY[c.cue][0]
        if not prefs["cat"].get(cat, False):
            return "dropped:category"
        if c.cue in prefs["muted_cues"]:
            return "dropped:muted"
        if c.cue == "power_off":
            return None
        max_age = _bridge.CUE_POLICY[c.cue][4]
        if max_age is not None and not c.verdict and now - c.t > max_age:
            return "dropped:stale"
        pres = self._presence()
        if pres is not None and pres[0]:
            policy = prefs["while_closed"]
            if policy == "silent" or (policy == "replies" and c.cue != "landed"):
                return "dropped:hidden"
        if prefs["respect_dnd"] and self._quns() in DND_STATES:
            return "dropped:dnd"
        return None

    def _rate_verdict(self, c, now):
        """The batch's chosen candidate only: its per-cue min gap (insist bypasses) and the one-voice rule."""
        gap = _bridge.CUE_POLICY[c.cue][3]
        if not c.test and not c.insist:
            last = self._last_played.get(c.cue)
            if last is not None and now - last < gap:
                return "dropped:min_gap"
        cur = self._current
        if cur is not None and now < cur.end:
            if cur.cue == "power_off":
                return "dropped:busy"
            if not c.test and not (c.prio > cur.prio or (c.prio == cur.prio and cur.cuttable)):
                return "dropped:busy"
        return None

    def _arbitrate(self, cands, now):
        prefs = self._prefs
        eligible = []
        for c in cands:
            c.seq = next(self._seq)
            v = self._candidate_verdict(c, prefs, now)
            if v:
                self._decide(c.t, c.event, c.cue, v, c.why)
            else:
                eligible.append(c)
        if not eligible:
            return
        best = max(eligible, key=lambda c: (c.test, c.prio, c.seq))
        for c in eligible:
            if c is not best:
                self._decide(c.t, c.event, c.cue, "dropped:batch", c.why)
        v = self._rate_verdict(best, now)
        if v:
            self._decide(best.t, best.event, best.cue, v, best.why)
            return
        if self._num_devs() <= 0:
            self._set_status("no_device")
            self._recheck_at = now + NO_DEVICE_RECHECK_S
            self._decide(best.t, best.event, best.cue, "dropped:no_device", best.why)
            return
        buf = self._bank.get(best.cue)
        if buf is None:
            self._decide(best.t, best.event, best.cue, "dropped:no_bank", best.why)
            return
        try:
            ok = bool(self._win.play_memory(buf))
        except Exception:
            ok = False
        if ok:
            cat, prio, cuttable, gap, max_age = _bridge.CUE_POLICY[best.cue]
            self._current = _Sounding(best.cue, best.prio, cuttable,
                                      now + _synth.DURATION_MS[best.cue] / 1000.0 + TAIL_GUARD_S)
            self._last_played[best.cue] = now
            self._fail_run = 0
            self._retry_s = ERROR_RETRY_S
            self._bridge.note_played(best, now)
            self._decide(best.t, best.event, best.cue, "played", best.why)
            return
        self._fail_run += 1
        self._decide(best.t, best.event, best.cue, "play_failed", "run=%d" % self._fail_run)
        if self._fail_run >= PLAY_FAIL_LIMIT:
            self._retry_at = now + self._retry_s
            self._decide(now, "health", None, "error", "retry in %.0f s" % self._retry_s)
            self._retry_s = min(ERROR_RETRY_MAX_S, self._retry_s * 2)
            self._current = None
            self._set_status("error")
            self._log_once(("play_false",), "PlaySoundW refused %d cues in a row - sound paused, retrying in %.0f s"
                           % (PLAY_FAIL_LIMIT, self._retry_at - now))
