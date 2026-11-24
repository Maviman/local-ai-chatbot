"""PrefsStore: the single client writer of data/desktop_theme.json (ST-01 / FD-2, schema AESTHETIC_SPEC §7.2).

Who calls what
--------------
* The Tk thread only ever touches memory: get/set/get_root/set_root/subscribe/effective_motion/... are dict operations
  under a threading.Lock held for microseconds, never across I/O.
* One daemon thread, 'fx-prefs-writer', is the only thing in this client process that writes the file. It never calls Tk.
* Construction does NO file I/O (AM-5): the store is seeded from the dict theme.load_theme() already read
  (theme.last_loaded_raw()). Only set()/set_root()/restore_fx_defaults() cause writes.

Writer loop (per ST-01)
-----------------------
wait on an Event -> coalesce 20 ms -> snapshot the dirty leaves (leaf -> generation) -> read the file
(missing -> {}; unreadable -> back off; unparseable / not an object -> NO write, back off 50 ms doubling to 2 s, except
under PREFS_CORRUPT_RULE: identical (size, mtime_ns) on >= 3 reads spanning >= 2 s -> rewrite from the session's values,
logged once) -> merge ONLY the dirty leaves into what is on disk now (unknown keys at every level survive) -> write
'<file>.tmp-<pid>-<tid>' + fsync (off the Tk thread) -> re-stat the target; if it changed since the read, re-read and
re-merge -> os.replace (PermissionError -> back off; never an in-place fallback) -> clear only the leaves whose generation
still matches. Every failure path removes its tmp file. Each failure kind is logged once (a replace denial only once
it is persistent - read-only target, or REPLACE_DENIED_LOG_AFTER in a row - since the backup zipper causes brief ones).
The file is decoded as utf-8-sig (a BOM is not corruption). No deepcopy on the writer path (_cow_base).

Validation is per leaf (§7.2): wrong type / unknown enum / out of range -> that leaf's default on load, ValueError on set();
bool is rejected for crt and volume; volume rounds to the nearest 5 within 0..100; fx.v = max(1, existing int);
'fx.motion' absent means "follow Windows" and set('fx.motion', None) removes the key.

Stdlib only. This module never imports tkinter; subscribe(owner=...) uses the owner widget's own bind().
"""
import copy
import json
import math
import os
import stat as _stat_mod
import sys
import threading
import time

try:  # package import (desktop.fx.prefs)
    from .. import theme as _theme
except Exception:  # noqa: BLE001 - loaded by path (tests) or without the package
    try:
        from desktop import theme as _theme
    except Exception:  # noqa: BLE001
        _theme = None

if _theme is not None:
    PHOSPHORS = tuple(_theme.PHOSPHOR_PRESETS)
    LAUNCH_SCREENS = tuple(_theme.LAUNCH_SCREENS)
    DEFAULT_ROOTS = {"phosphor": _theme.DEFAULT_PHOSPHOR, "launch_screen": _theme.DEFAULT_LAUNCH_SCREEN}
else:  # pragma: no cover - mirrors theme.py
    PHOSPHORS = ("amber", "green", "cyan", "red", "white")
    LAUNCH_SCREENS = ("home", "last_chat")
    DEFAULT_ROOTS = {"phosphor": "amber", "launch_screen": "home"}

ROOT_CHOICES = {"phosphor": PHOSPHORS, "launch_screen": LAUNCH_SCREENS}

FX_VERSION = 1
MOTION_LEVELS = ("off", "reduced", "full")
DISPLAY_FACES = ("industrial", "terminal")
INSTRUMENT_INKS = ("phosphor", "reference")
SOUND_CATEGORIES = ("nav", "interface", "comms", "system", "alerts")
REPLY_LANDED = ("away", "always", "off")
WHILE_CLOSED = ("silent", "replies", "all")
TRANSMIT_VARIANTS = ("chirp", "blip")

# Schema §7.2 with DECISIONS D-1..D-8 applied. 'motion' is deliberately ABSENT (= follow Windows, D-4).
# B11: crt is FULL (2) on a first run (D-29 supersedes D-8's LOW). The store writes only dirty leaves, so a file that
# already holds a valid "crt" (e.g. 1, written by P1 testing) keeps it; a file without one, or with an invalid one
# ("2", true, 5), reads as FULL. RESTORE DEFAULTS on Settings 08 sets it back to 2.
FX_DEFAULTS = {
    "v": FX_VERSION,
    "crt": 2,
    "display_face": "industrial",
    "instrument_ink": "phosphor",
    "sound": {
        "enabled": True,
        "volume": 40,
        "cat": {c: True for c in SOUND_CATEGORIES},
        "reply_landed": "away",
        "while_closed": "replies",
        "respect_dnd": True,
        "wait_pings": False,
        "signal_acquired": False,
        "context_alert": False,
        "muted_cues": [],
        "transmit_variant": "chirp",
    },
}

COALESCE_S = 0.020
BACKOFF_START_S, BACKOFF_MAX_S = 0.050, 2.0
FLUSH_BACKOFF_CAP_S = 0.2   # while a flush() waits, a backoff wait is at most this (the doubling itself continues)
# PREFS_CORRUPT_RULE: an unparseable file is only treated as genuinely corrupt (and rewritten from the session's values)
# when it stayed byte-identical by (size, mtime_ns) across >= 3 reads spanning >= 2 s. Anything shorter is a writer
# (e.g. server.py's restore) caught mid-write, and we must not write over it.
CORRUPT_STABLE_READS, CORRUPT_STABLE_SPAN_S = 3, 2.0
# os.replace PermissionError is normally a transient sharing violation (server.py's backup zipper holding the file for
# a few ms) and stays silent; this many denials in a row (~0.35 s of backoff) is a persistent problem and is logged once.
# A read-only target is logged at the first denial.
REPLACE_DENIED_LOG_AFTER = 4
_TIMER_TICK_S = 0.035   # close(): margin for a timed wait overshooting by up to ~2 Windows timer ticks
SPI_GETCLIENTAREAANIMATION = 0x1042

_ENUM, _OPT_ENUM, _INT_RANGE, _VOLUME, _BOOL, _STRLIST = "enum", "opt_enum", "int", "volume", "bool", "strlist"

# leaf path -> (kind, choices/range, default)
_SPEC = {
    ("fx", "motion"): (_OPT_ENUM, MOTION_LEVELS, None),
    ("fx", "crt"): (_INT_RANGE, (0, 2), 2),          # B11: FULL by default (D-29); FX_DEFAULTS["crt"] matches
    ("fx", "display_face"): (_ENUM, DISPLAY_FACES, "industrial"),
    ("fx", "instrument_ink"): (_ENUM, INSTRUMENT_INKS, "phosphor"),
    ("fx", "sound", "enabled"): (_BOOL, None, True),
    ("fx", "sound", "volume"): (_VOLUME, (0, 100), 40),
}
for _c in SOUND_CATEGORIES:
    _SPEC[("fx", "sound", "cat", _c)] = (_BOOL, None, True)
_SPEC.update({
    ("fx", "sound", "reply_landed"): (_ENUM, REPLY_LANDED, "away"),
    ("fx", "sound", "while_closed"): (_ENUM, WHILE_CLOSED, "replies"),
    ("fx", "sound", "respect_dnd"): (_BOOL, None, True),
    ("fx", "sound", "wait_pings"): (_BOOL, None, False),
    ("fx", "sound", "signal_acquired"): (_BOOL, None, False),
    ("fx", "sound", "context_alert"): (_BOOL, None, False),
    ("fx", "sound", "muted_cues"): (_STRLIST, None, []),
    ("fx", "sound", "transmit_variant"): (_ENUM, TRANSMIT_VARIANTS, "chirp"),
})
FX_LEAVES = tuple(_SPEC)
ROOT_LEAVES = (("phosphor",), ("launch_screen",))
# for_test(**leaves): every leaf's last segment is unique across the schema.
_SHORT_NAMES = {p[-1]: p for p in FX_LEAVES}


def _is_num(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def snap_volume(v):
    """The store's volume rule: nearest 5, ties always UP (12.5 -> 15, 17.5 -> 20). round() would be half-to-even
    (12.5 -> 10 but 17.5 -> 20). A UI readout should use THIS so it always shows what set() will store.
    Expects a finite number; no range check."""
    return int(math.floor(v / 5.0 + 0.5)) * 5


def _validate(path, v):
    """-> (ok, clean_value) for an fx leaf or a root key. Never raises."""
    if len(path) == 1 and path[0] in ROOT_CHOICES:
        return (isinstance(v, str) and v in ROOT_CHOICES[path[0]]), v
    spec = _SPEC.get(path)
    if spec is None:
        return False, None
    kind, arg, _default = spec
    try:
        if kind == _ENUM:
            return (isinstance(v, str) and v in arg), v
        if kind == _OPT_ENUM:
            return (v is None or (isinstance(v, str) and v in arg)), v
        if kind == _BOOL:
            return isinstance(v, bool), v
        if kind == _INT_RANGE:
            if not _is_num(v) or not math.isfinite(v) or float(v) != int(v):
                return False, None
            iv = int(v)
            return (arg[0] <= iv <= arg[1]), iv
        if kind == _VOLUME:
            if not _is_num(v) or not math.isfinite(v) or not (arg[0] <= v <= arg[1]):
                return False, None
            return True, snap_volume(v)
        if kind == _STRLIST:
            if isinstance(v, (list, tuple)) and all(isinstance(x, str) for x in v):
                return True, list(v)
            return False, None
    except Exception:  # noqa: BLE001 - e.g. OverflowError on absurd numbers
        return False, None
    return False, None


def _is_root_key(key):
    """key is 'phosphor' / 'launch_screen'; False (never TypeError) for unhashable or odd keys."""
    return isinstance(key, str) and key in ROOT_CHOICES


def _safe_copy(obj):
    """Deep copy of a JSON-shaped value. copy.deepcopy needs ~2 Python frames per nesting level, so a valid file with an
    unknown key nested ~500 deep raises RecursionError; fall back to a C-level JSON round trip, then {}. Never raises."""
    try:
        return copy.deepcopy(obj)
    except Exception:  # noqa: BLE001 - RecursionError, an uncopyable object, ...
        pass
    try:
        return json.loads(json.dumps(obj))
    except Exception:  # noqa: BLE001
        return {}


# The only containers _put() ever mutates: every leaf lives under these.
_LEAF_CONTAINERS = ((), ("fx",), ("fx", "sound"), ("fx", "sound", "cat"))


def _cow_base(base):
    """A copy of `base` that _put() may mutate without touching `base`: only the root / fx / fx.sound / fx.sound.cat
    dicts are copied (shallow); every other subtree is shared, read-only. O(a few dozen keys) whatever the file size and
    nesting depth - so no deepcopy (no RecursionError, no long GIL/lock hold) on the writer's corrupt-rule path."""
    out = dict(base) if isinstance(base, dict) else {}
    for cpath in _LEAF_CONTAINERS[1:]:
        parent_found, parent = _lookup(out, cpath[:-1])
        if not parent_found or not isinstance(parent, dict):
            break
        child = parent.get(cpath[-1])
        if not isinstance(child, dict):
            break
        parent[cpath[-1]] = dict(child)
    return out


def _default(path):
    spec = _SPEC.get(path)
    if spec is None:
        return None
    return copy.copy(spec[2])


def _lookup(d, path):
    """-> (found, value) navigating nested dicts; any non-dict intermediate = not found."""
    for k in path:
        if not isinstance(d, dict) or k not in d:
            return False, None
        d = d[k]
    return True, d


def _put(d, path, value):
    """Set one leaf inside nested dict d, creating/replacing non-dict intermediates. motion None removes the key."""
    for k in path[:-1]:
        nxt = d.get(k)
        if not isinstance(nxt, dict):
            nxt = {}
            d[k] = nxt
        d = nxt
    if value is None and path == ("fx", "motion"):
        d.pop("motion", None)
    else:
        d[path[-1]] = copy.copy(value)


def _norm(path):
    """'fx.sound.volume' | ('fx','sound','volume') -> tuple, or None if it is neither."""
    if isinstance(path, str):
        return tuple(path.split(".")) if path else ()
    if isinstance(path, (tuple, list)) and all(isinstance(p, str) for p in path):
        return tuple(path)
    return None


def _dotted(path):
    return ".".join(path)


def _stderr_log(msg):
    try:
        sys.stderr.write(str(msg) + "\n")
        sys.stderr.flush()
    except Exception:  # noqa: BLE001
        pass


def _spi_client_area_animation():
    """Windows 'Show animations in Windows' (SPI_GETCLIENTAREAANIMATION). True/False, or None when unreadable."""
    try:
        import ctypes
        from ctypes import wintypes
        val = wintypes.BOOL(0)
        ok = ctypes.windll.user32.SystemParametersInfoW(SPI_GETCLIENTAREAANIMATION, 0, ctypes.byref(val), 0)
        if not ok:
            return None
        return bool(val.value)
    except Exception:  # noqa: BLE001
        return None


class PrefsStore:
    def __init__(self, path, raw, running_roots, *, log=None, windows_motion_fn=None):
        self.path = path
        self._log_fn = log if callable(log) else _stderr_log
        self._logged = set()
        self._lock = threading.Lock()
        self._values = {}
        self._dirty = {}            # leaf path -> generation at the time of its set()
        self._gen = 0
        self._epoch = 0             # bumped by reload(): an in-flight write from before a reload is discarded
        self._subs = []             # [sub record dict], registration order
        self._owner_subs = {}       # owner Tk path -> [sub records]
        self._closed = False
        self._stop = False
        self._wake = threading.Event()     # set(): there is something to write
        self._urgent = threading.Event()   # flush/close/reload: cut a backoff wait short
        self._idle = threading.Event()     # nothing pending AND nothing in flight
        self._idle.set()
        self._flushers = 0          # threads currently inside flush() (caps the backoff wait meanwhile)
        self.stats = {"writes": 0, "attempts": 0, "parse_fail": 0, "read_err": 0, "replace_denied": 0,
                      "restat_changed": 0, "corrupt_rewrites": 0, "write_err": 0, "epoch_discards": 0}
        self._write_delay = 0.0     # test hook: seconds to sleep between write and fsync (simulates a slow disk)
        self._ver = {}              # leaf path -> change counter; a dispatch stops once its leaf has changed again
        self._denied_run = 0        # consecutive os.replace PermissionErrors (writer thread only)
        # Owner auto-unsubscribe rides on a bindtag of our own (not the owner's widget-level <Destroy> binding, which
        # any later plain owner.bind('<Destroy>', fn) would silently replace).
        self._owner_tag = f"FxPrefsOwner{id(self):x}"

        raw = raw if isinstance(raw, dict) else {}
        self._last_good = _safe_copy(raw)   # never mutated in place: only ever replaced wholesale
        self._running_roots = self._coerce_roots(running_roots)
        self._load_leaves(raw)
        for (k,) in ROOT_LEAVES:
            found, v = _lookup(raw, (k,))
            ok, _ = _validate((k,), v) if found else (False, None)
            self._values[(k,)] = v if ok else self._running_roots[k]

        self._win_fn = windows_motion_fn if callable(windows_motion_fn) else _spi_client_area_animation
        self._win_motion = self._read_windows_motion()

        self._thread = None
        if path:
            self._thread = threading.Thread(target=self._run, name="fx-prefs-writer", daemon=True)
            self._thread.start()

    # ------------------------------------------------------------------ helpers
    @staticmethod
    def _coerce_roots(running_roots):
        roots = dict(DEFAULT_ROOTS)
        src = {}
        if isinstance(running_roots, dict):
            src = running_roots
        elif running_roots is not None:  # a Theme-like object
            src = {"phosphor": getattr(running_roots, "phosphor_name", None),
                   "launch_screen": getattr(running_roots, "launch_screen", None)}
        for k in roots:
            v = src.get(k)
            if _validate((k,), v)[0]:
                roots[k] = v
        return roots

    def _load_leaves(self, raw):
        for p in FX_LEAVES:
            found, v = _lookup(raw, p)
            ok, cv = _validate(p, v) if found else (False, None)
            self._values[p] = cv if ok else _default(p)

    def _log_once(self, key, msg):
        if key in self._logged:
            return
        self._logged.add(key)
        try:
            self._log_fn("fx prefs: " + str(msg))
        except Exception:  # noqa: BLE001
            _stderr_log("fx prefs: " + str(msg))

    def _read_windows_motion(self):
        try:
            v = self._win_fn()
        except Exception as e:  # noqa: BLE001
            self._log_once(("spi", type(e).__name__), f"Windows animation setting unreadable: {e!r}")
            return None
        return v if isinstance(v, bool) else None

    def _notify(self, changes):
        """changes: [(path tuple, value, version)] - sync on the caller thread, registration order, each subscriber
        isolated. Re-entrancy: when a subscriber changes the same leaf again (a clamp, a dependent toggle), that nested
        set() notifies EVERY subscriber of the newer value; this outer dispatch then stops for that leaf, so no later
        subscriber is handed the stale value last."""
        if not changes:
            return
        subs = list(self._subs)
        for path, value, ver in changes:
            dotted = _dotted(path)
            for rec in subs:
                if self._ver.get(path, 0) != ver:
                    break                   # superseded by a newer change of this leaf (already dispatched)
                if not rec["live"]:
                    continue
                pre = rec["prefix"]
                if pre and path[:len(pre)] != pre:
                    continue
                try:
                    rec["cb"](dotted, copy.copy(value))
                except Exception as e:  # noqa: BLE001
                    cb = rec["cb"]
                    site = getattr(cb, "__module__", "?") + "." + getattr(cb, "__qualname__", repr(cb))
                    self._log_once(("sub", site, type(e).__name__), f"subscriber {site} raised {e!r} on {dotted}")

    def _mark(self, path):
        """Under self._lock: mark one leaf dirty (if a writer exists and we are not closed)."""
        self._gen += 1
        self._dirty[path] = self._gen
        self._idle.clear()

    def _writes_enabled(self):
        return self._thread is not None and not self._closed

    # ------------------------------------------------------------------ Tk-thread API (memory only)
    def get(self, path):
        """Leaf -> its value; a root key -> the stored (next-launch) value; a prefix of leaves ('fx', 'fx.sound',
        'fx.sound.cat') -> a nested dict of those leaves (absent motion omitted); anything else -> None. Never raises."""
        p = _norm(path)
        if p is None:
            return None
        v = self._values.get(p, _MISSING)
        if v is not _MISSING:
            return copy.copy(v)
        if p == ("fx", "v"):
            return FX_VERSION
        out = {}
        n = len(p)
        for leaf in FX_LEAVES:
            if leaf[:n] == p and len(leaf) > n:
                val = self._values.get(leaf)
                if leaf == ("fx", "motion") and val is None:
                    continue
                _put(out, leaf[n:], val)
        return out or None

    def set(self, path, value):
        """Validate + store one fx leaf in memory and queue it for the writer. -> True if the value changed.
        ValueError on an unknown path or invalid value."""
        p = _norm(path)
        if p is None or p not in _SPEC:
            if p is not None and len(p) == 1 and p[0] in ROOT_CHOICES:
                return self.set_root(p[0], value)
            raise ValueError(f"unknown preference path {path!r}")
        ok, cv = _validate(p, value)
        if not ok:
            raise ValueError(f"invalid value for {_dotted(p)}: {value!r}")
        return self._commit([(p, cv)])

    def get_root(self, key):
        return self._values.get((key,)) if _is_root_key(key) else None

    def set_root(self, key, value):
        if not _is_root_key(key):
            raise ValueError(f"unknown root key {key!r}")
        ok, cv = _validate((key,), value)
        if not ok:
            raise ValueError(f"invalid value for {key}: {value!r}")
        return self._commit([((key,), cv)])

    def running_root(self, key):
        """What the app is actually running with (load_theme's result at construction)."""
        return self._running_roots.get(key) if _is_root_key(key) else None

    def _bump(self, p):
        """Under self._lock: a new version of leaf p -> that version."""
        v = self._ver.get(p, 0) + 1
        self._ver[p] = v
        return v

    def _commit(self, items, force_dirty=False):
        changes = []
        closed_drop = False
        with self._lock:
            for p, cv in items:
                changed = self._values.get(p, _MISSING) != cv
                self._values[p] = copy.copy(cv)
                if changed:
                    changes.append((p, cv, self._bump(p)))
                if changed or force_dirty:
                    if self._writes_enabled():
                        self._mark(p)
                    elif self._closed:
                        closed_drop = True
        if closed_drop:
            self._log_once("closed", "store closed - later changes are kept in memory only")
        if self._writes_enabled() and (changes or force_dirty):
            self._wake.set()
        self._notify(changes)
        return bool(changes)

    def subscribe(self, prefix, cb, owner=None):
        """cb(path, value) runs synchronously on the thread that changed the value, for every change of a leaf at or
        under the dotted prefix ('' = everything). Returns an idempotent unsubscribe callable. With owner (a Tk widget)
        the subscription is dropped automatically on the owner's <Destroy> (via this store's own bindtag, so the
        owner's own bind('<Destroy>', fn) cannot clobber it). ValueError for a bad prefix/cb or a non-widget owner;
        an already-dead owner (or one whose Tk is gone) registers nothing and gets a no-op unsubscribe."""
        pre = _norm(prefix)
        if pre is None or not callable(cb):
            raise ValueError(f"bad subscription {prefix!r}")
        rec = {"prefix": pre, "cb": cb, "live": True, "owner": None}

        def unsubscribe():
            if not rec["live"]:
                return
            rec["live"] = False
            try:
                self._subs.remove(rec)
            except ValueError:
                pass
            key = rec["owner"]
            if key is not None:
                lst = self._owner_subs.get(key)
                if lst and rec in lst:
                    lst.remove(rec)

        if owner is not None:
            if not all(callable(getattr(owner, a, None)) for a in ("winfo_exists", "bindtags", "bind_class")):
                # e.g. a controller object: a silently dead subscription would be worse than an error
                raise ValueError(f"subscribe owner must be a Tk widget, not {type(owner).__name__}")
            try:
                key = str(owner)
                if not owner.winfo_exists():
                    rec["live"] = False
                    return unsubscribe
                self._attach_owner_tag(owner)
                rec["owner"] = key
                self._owner_subs.setdefault(key, []).append(rec)
            except Exception as e:  # noqa: BLE001 - a widget whose Tk is gone, ...: do not subscribe at all
                self._log_once(("owner", type(e).__name__), f"subscribe owner unusable: {e!r}")
                rec["live"] = False
                return unsubscribe
        self._subs.append(rec)
        return unsubscribe

    def _attach_owner_tag(self, owner):
        """Route owner's <Destroy> through this store's own bindtag. One class binding (one Tcl command) per Tk
        interpreter per store, however many owners - nothing per owner to leak. Idempotent."""
        tag = self._owner_tag
        if not owner.bind_class(tag, "<Destroy>"):
            owner.bind_class(tag, "<Destroy>", self._on_owner_destroy)
        tags = owner.bindtags()
        if tag not in tags:
            owner.bindtags(tuple(tags) + (tag,))

    def _on_owner_destroy(self, event):
        try:
            key = str(event.widget)   # %W: only owners carry the tag, but be strict anyway
        except Exception:  # noqa: BLE001
            return
        for rec in self._owner_subs.pop(key, []):
            rec["live"] = False
            try:
                self._subs.remove(rec)
            except ValueError:
                pass

    def subscriber_count(self):
        return len(self._subs)

    # -- motion
    def effective_motion(self):
        v = self._values.get(("fx", "motion"))
        if v in MOTION_LEVELS:
            return v
        if self._win_motion is True:
            return "full"
        if self._win_motion is False:
            return "reduced"
        return "full"

    def motion_source(self):
        if self._values.get(("fx", "motion")) in MOTION_LEVELS:
            return "explicit"
        return "windows" if isinstance(self._win_motion, bool) else "default"

    def refresh_windows_motion(self):
        """Re-read the Windows setting (the FX pane calls this on <Map>). Notifies 'fx.motion' subscribers (value None)
        when the EFFECTIVE level changed because of it."""
        before = self.effective_motion()
        self._win_motion = self._read_windows_motion()
        if self.effective_motion() != before:
            p = ("fx", "motion")
            with self._lock:
                ver = self._bump(p)
            self._notify([(p, None, ver)])

    # -- bulk
    def restore_fx_defaults(self):
        """Every fx.* leaf back to its default in one burst (one write); phosphor/launch_screen untouched. All fx leaves
        are written (not only the changed ones) so the file ends fully at defaults even after an external edit."""
        self._commit([(p, _default(p)) for p in FX_LEAVES], force_dirty=True)

    def reload(self, raw):
        """Adopt a dict read (off the Tk thread) from a freshly restored file. Restore wins: pending leaves are dropped,
        an in-flight write from before this call is discarded, subscribers hear about every leaf that changed."""
        raw = raw if isinstance(raw, dict) else {}
        good = _safe_copy(raw)          # outside the lock: the writer must never wait on a big copy (nor vice versa)
        with self._lock:
            before = dict(self._values)
            self._dirty.clear()
            self._epoch += 1
            self._load_leaves(raw)
            for (k,) in ROOT_LEAVES:
                found, v = _lookup(raw, (k,))
                ok, _ = _validate((k,), v) if found else (False, None)
                self._values[(k,)] = v if ok else DEFAULT_ROOTS[k]
            self._last_good = good
            changes = [(p, self._values[p], self._bump(p)) for p in list(FX_LEAVES) + list(ROOT_LEAVES)
                       if before.get(p, _MISSING) != self._values[p]]
        self._wake.set()
        self._urgent.set()
        self._notify(changes)

    # -- lifecycle
    def flush(self, timeout):
        """Wait (bounded) until nothing is pending AND nothing is in flight. -> True when settled."""
        if self._thread is None or not self._thread.is_alive():
            with self._lock:
                return not self._dirty
        with self._lock:
            self._flushers += 1
        try:
            self._wake.set()
            self._urgent.set()
            return self._idle.wait(max(0.0, float(timeout)))
        finally:
            with self._lock:
                self._flushers -= 1

    def close(self, timeout):
        """flush(timeout), then stop the writer. Later set()s are memory-only (logged once). Bounded by ~timeout."""
        if self._closed:
            return
        budget = max(0.0, float(timeout))
        deadline = time.monotonic() + budget
        # Keep a slice of the budget back for the join below, so the whole call stays within timeout (teardown shares
        # the 800 ms shutdown grace: HANDOFF B-5 'prefs.close(0.5) adds at most 0.5 s'). Windows timed waits
        # (Event.wait / join) can each overshoot by one ~15.6 ms timer tick, hence the margins.
        reserve = min(0.05, budget * 0.35)
        ok = self.flush(budget - reserve)
        with self._lock:
            self._closed = True
            self._stop = True
            pending = bool(self._dirty)
        if not ok and pending:
            self._log_once("close-pending", "closed with unsaved preference changes (writer could not finish in time)")
        self._wake.set()
        self._urgent.set()
        t = self._thread
        if t is not None and t is not threading.current_thread():
            # The reserved slice lets a writer woken from a backoff wait exit; a writer stuck in slow I/O is a daemon
            # and is left behind: it finishes at most the attempt already in progress (still tmp + re-stat +
            # os.replace, so still safe) and starts no new one.
            t.join(max(0.0, deadline - time.monotonic() - _TIMER_TICK_S))

    @property
    def closed(self):
        return self._closed

    @classmethod
    def for_test(cls, path=None, *, windows_motion=None, log=None, **leaves):
        """Store seeded with the given leaves as if they were in the file (no write). Leaves by last segment
        (motion=, crt=, volume=, nav=, phosphor=, ...) or dotted with '__' (fx__sound__volume=). path=None -> memory
        only (no writer thread). windows_motion: True/False/None (None = unreadable -> 'full')."""
        raw, roots = {}, dict(DEFAULT_ROOTS)
        for name, value in leaves.items():
            if name in ROOT_CHOICES:
                if not _validate((name,), value)[0]:
                    raise ValueError(f"invalid value for {name}: {value!r}")
                raw[name] = value
                roots[name] = value
                continue
            p = _SHORT_NAMES.get(name) or tuple(name.split("__"))
            if p not in _SPEC:
                raise ValueError(f"unknown preference {name!r}")
            ok, cv = _validate(p, value)
            if not ok:
                raise ValueError(f"invalid value for {_dotted(p)}: {value!r}")
            _put(raw, p, cv)
        return cls(path, raw, roots, log=log, windows_motion_fn=lambda: windows_motion)

    # ------------------------------------------------------------------ writer thread (never touches Tk)
    def _tmp_path(self):
        return f"{self.path}.tmp-{os.getpid()}-{threading.get_ident()}"

    def _remove_tmp(self, tmp):
        try:
            os.remove(tmp)
        except OSError:
            pass

    def _target_readonly(self):
        """True when the target exists with the Windows read-only attribute (no write bit). Never raises."""
        try:
            return not (os.stat(self.path).st_mode & _stat_mod.S_IWRITE)
        except OSError:
            return False

    @staticmethod
    def _stat(path):
        try:
            s = os.stat(path)
            return (s.st_size, s.st_mtime_ns)
        except FileNotFoundError:
            return None
        except OSError:
            return ("err",)

    def _read(self):
        """-> ('ok', dict, stat) | ('missing', {}, None) | ('bad', None, stat) | ('err', None, None)"""
        try:
            s = os.stat(self.path)
            st = (s.st_size, s.st_mtime_ns)
            with open(self.path, "rb") as f:
                data = f.read()
        except FileNotFoundError:
            return "missing", {}, None
        except OSError as e:
            self._log_once(("read", type(e).__name__), f"cannot read {self.path}: {e}")
            return "err", None, None
        try:
            # utf-8-sig: a UTF-8 BOM (Notepad 'UTF-8 with BOM', PowerShell 5 Set-Content -Encoding UTF8) is still a
            # valid file - treating it as corrupt would let PREFS_CORRUPT_RULE rewrite it from session defaults.
            d = json.loads(data.decode("utf-8-sig"))
        except (ValueError, UnicodeDecodeError, RecursionError):
            return "bad", None, st
        if not isinstance(d, dict):
            return "bad", None, st
        return "ok", d, st

    def _run(self):
        backoff = 0.0
        bad_seen = []      # [(monotonic, stat)] consecutive unparseable reads of one unchanged file
        while True:
            try:
                if self._stop:
                    break
                if backoff:
                    # a plain set() does not cut a backoff short; flush/close/reload do, and while a flush() is
                    # waiting the wait is capped (it asked for the data on disk now - e.g. close(0.5) at teardown)
                    self._urgent.wait(min(backoff, FLUSH_BACKOFF_CAP_S) if self._flushers else backoff)
                    self._urgent.clear()
                else:
                    self._wake.wait()
                    self._urgent.clear()         # a stale flush() request must not skip a later backoff
                self._wake.clear()
                if self._stop:
                    break
                with self._lock:
                    if not self._dirty:
                        self._idle.set()
                        backoff, bad_seen = 0.0, []
                        continue
                time.sleep(COALESCE_S)          # coalesce a burst (a slider drag) into one write
                backoff, bad_seen = self._attempt(backoff, bad_seen)
            except Exception as e:  # noqa: BLE001 - the writer must never die
                self._log_once(("loop", type(e).__name__), f"writer error: {e!r}")
                backoff = min(BACKOFF_MAX_S, (backoff * 2) or BACKOFF_START_S)

    def _attempt(self, backoff, bad_seen):
        """One read-merge-write attempt. -> (next backoff, bad_seen)."""
        nxt = min(BACKOFF_MAX_S, (backoff * 2) or BACKOFF_START_S)
        with self._lock:
            todo = dict(self._dirty)
            vals = {p: copy.copy(self._values.get(p)) for p in todo}
            epoch = self._epoch
            if not todo:
                self._idle.set()
                return 0.0, []
        self.stats["attempts"] += 1
        kind, base, st = self._read()
        if kind == "err":
            self.stats["read_err"] += 1
            return nxt, []
        if kind == "bad":
            self.stats["parse_fail"] += 1
            now = time.monotonic()
            bad_seen = [b for b in bad_seen if b[1] == st] + [(now, st)]
            stable = len(bad_seen) >= CORRUPT_STABLE_READS and now - bad_seen[0][0] >= CORRUPT_STABLE_SPAN_S
            if not stable:
                self._log_once("parse", f"{os.path.basename(self.path)} is unparseable - waiting, not writing")
                return nxt, bad_seen               # NEVER write after a failed parse
            self._log_once("corrupt", f"{os.path.basename(self.path)} stayed unreadable - rewriting from session values")
            self.stats["corrupt_rewrites"] += 1
            with self._lock:   # dict ops only: grab the reference; _last_good is never mutated in place
                base = self._last_good
                all_vals = {p: copy.copy(self._values.get(p)) for p in list(FX_LEAVES) + list(ROOT_LEAVES)}
            all_vals.update(vals)
            vals = all_vals
        # base is either our own fresh parse or the shared _last_good: _cow_base copies just the containers _put()
        # mutates (no deepcopy: no RecursionError on deep unknown keys, no long hold of anything).
        merged = _cow_base(base)
        for p, v in vals.items():
            _put(merged, p, v)
        fx = merged.get("fx")
        if isinstance(fx, dict):
            v = fx.get("v")
            fx["v"] = max(FX_VERSION, int(v)) if (_is_num(v) and math.isfinite(v)) else FX_VERSION
        try:
            try:
                text = json.dumps(merged, indent=1)
            except RecursionError:   # the indenting encoder is pure Python (frames per level); the C one is not
                text = json.dumps(merged)
            blob = text.encode("utf-8")
        except Exception as e:  # noqa: BLE001
            self._log_once(("dump", type(e).__name__), f"cannot serialise preferences: {e!r}")
            return nxt, []
        tmp = self._tmp_path()
        try:
            folder = os.path.dirname(self.path)
            if folder:
                os.makedirs(folder, exist_ok=True)
            with open(tmp, "wb") as f:
                f.write(blob)
                f.flush()
                if self._write_delay:
                    time.sleep(self._write_delay)
                os.fsync(f.fileno())
        except OSError as e:
            self.stats["write_err"] += 1
            self._log_once(("write", type(e).__name__), f"cannot write {tmp}: {e}")
            self._remove_tmp(tmp)
            return nxt, []
        if self._stat(self.path) != st:          # someone (a restore) wrote it since our read: re-read, re-merge
            self.stats["restat_changed"] += 1
            self._remove_tmp(tmp)
            self._wake.set()
            return BACKOFF_START_S, []
        with self._lock:
            stale = self._epoch != epoch
        if stale:                                 # reload() ran meanwhile: restore wins, drop this write
            self.stats["epoch_discards"] += 1
            self._remove_tmp(tmp)
            self._wake.set()
            return 0.0, []
        try:
            os.replace(tmp, self.path)
        except PermissionError as e:
            # Usually another handle (backup zipper / restore) holding the file for a moment: back off silently.
            # A read-only target, or a denial that keeps repeating, is persistent and must be diagnosable (logged once).
            self.stats["replace_denied"] += 1
            self._denied_run += 1
            self._remove_tmp(tmp)
            if self._target_readonly():
                self._log_once(("replace", "readonly"),
                               f"cannot replace {self.path}: the file is read-only ({e}) - preference changes are not "
                               f"saved until the read-only attribute is cleared")
            elif self._denied_run >= REPLACE_DENIED_LOG_AFTER:
                self._log_once(("replace", "PermissionError"),
                               f"cannot replace {self.path}: access denied {self._denied_run} times in a row ({e}) - "
                               f"still retrying")
            return nxt, []
        except OSError as e:
            self.stats["write_err"] += 1
            self._log_once(("replace", type(e).__name__), f"cannot replace {self.path}: {e}")
            self._remove_tmp(tmp)
            return nxt, []
        self.stats["writes"] += 1
        self._denied_run = 0
        with self._lock:
            self._last_good = merged
            for p, g in todo.items():
                if self._dirty.get(p) == g:
                    del self._dirty[p]
            if self._dirty:
                self._wake.set()
            else:
                self._idle.set()
        return 0.0, []


class _Missing:
    __slots__ = ()

    def __repr__(self):
        return "<missing>"


_MISSING = _Missing()
