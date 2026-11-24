"""Mecha-terminal color/type system: a customizable phosphor palette
(persisted to data/desktop_theme.json, applied on restart - no live
re-theming) plus the type scale.

Font policy: Consolas for everything read; one optional display face
(fx/fonts.py) for titles/plates/rail labels only. No font file is
registered here; the uppercase letter-spaced "label" look comes from
letter_spaced().

Persistence (desktop_theme.json is shared with the fx preference store):
- load_theme() accepts only a JSON object; anything else (missing, corrupt,
  a list/number/null, a wrong-typed field) falls back to defaults per field
  and never raises. It is side-effect free and keeps a copy of the raw dict
  for last_loaded_raw() (the fx PrefsStore is seeded from it - no 2nd read).
- save_theme() delegates to the registered fx PrefsStore (memory only; its
  writer thread does the disk work). Without one it does a merge-preserving
  write (unknown keys and the "fx" block survive) via a tmp file +
  os.replace, no fsync, bounded retries, and never raises. Both paths save
  per field and never persist an invalid phosphor/launch_screen; the
  fallback never writes over a file that exists but cannot be read.
- Files are read as utf-8-sig (a UTF-8 BOM is valid, not corruption).
"""

import copy
import json
import os
import sys
import threading
import time

DATA_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "data"
)
THEME_PATH = os.path.join(DATA_DIR, "desktop_theme.json")

SURFACE_BASE = "#0B0A08"
SURFACE_PANEL = "#151209"
SURFACE_PANEL_HOVER = "#1D1810"

# Fixed regardless of chosen phosphor - status meaning must stay legible
# no matter which color the user picked for everything else.
DANGER = "#FF4433"
OK = "#39D353"
WARN = "#FFB000"

PHOSPHOR_PRESETS = {
    "amber": {"bright": "#FFB000", "dim": "#8A5A00"},
    "green": {"bright": "#33FF66", "dim": "#146629"},
    "cyan": {"bright": "#33E6FF", "dim": "#146673"},
    "red": {"bright": "#FF4433", "dim": "#7A1F17"},
    "white": {"bright": "#F0F0F0", "dim": "#7A7A7A"},
}

DEFAULT_PHOSPHOR = "amber"

# Open item resolved during implementation: the app defaults to Home (the
# new landing surface this redesign introduces - its system-status/quick-
# resume/boot-log content would otherwise never be seen) rather than the
# last chat. Both are legitimate; this is a data-level preference, not an
# architectural fork, so it's just a schema key here that a future Settings
# toggle can flip to "last_chat".
LAUNCH_SCREENS = ("home", "last_chat")
DEFAULT_LAUNCH_SCREEN = "home"

FONT_FAMILY = "Consolas"

FONT_BODY = (FONT_FAMILY, 11)
FONT_BODY_BOLD = (FONT_FAMILY, 11, "bold")
FONT_LABEL = (FONT_FAMILY, 10, "bold")
FONT_CAPTION = (FONT_FAMILY, 9)
FONT_CAPTION_LINK = (FONT_FAMILY, 9, "underline")
FONT_HEADER = (FONT_FAMILY, 15, "bold")
FONT_MONO = (FONT_FAMILY, 10)
FONT_INDEX = (FONT_FAMILY, 13, "bold")


def letter_spaced(text, sep=" "):
    """Thin-space-join for the uppercase label look the design calls for,
    e.g. letter_spaced("settings") -> 'S E T T I N G S'.
    """
    return sep.join(text.upper())


# Colour tiers Theme copies from fx.palette.tones() (B1). Kept beside bright/dim/... .
_TIER_KEYS = ("value", "ghost", "warn_eff", "unlit", "rule_out", "rule_in", "bloom_out", "bloom_in")
_palette_mod = None


def _palette_module():
    """fx/palette.py, imported lazily (it imports this module at its own top, so a
    module-level import here would be a cycle). Works whether this file was loaded as
    desktop.theme, as a loose file, or by fx/palette's own by-path fallback."""
    global _palette_mod
    if _palette_mod is not None:
        return _palette_mod
    mod = None
    if __package__:
        try:
            from .fx import palette as mod  # desktop.theme -> desktop.fx.palette
        except ImportError:
            mod = None
    if mod is None:
        mod = sys.modules.get("desktop.fx.palette")
    if mod is None:
        try:
            import desktop.fx.palette as mod  # app/ on sys.path
        except ImportError:
            mod = None
    if mod is None:
        import importlib.util
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fx", "palette.py")
        spec = importlib.util.spec_from_file_location("_theme_fx_palette", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    _palette_mod = mod
    return mod


def _is_choice(value, choices):
    """value in choices, but False (not TypeError) for unhashable/odd values."""
    try:
        return isinstance(value, str) and value in choices
    except Exception:  # noqa: BLE001
        return False


class Theme:
    """Current phosphor selection plus every color/font a screen needs.
    Load once at startup; set_phosphor() + save_theme() persist a change but
    it only takes effect after restart (deliberate - see plan's out-of-scope
    list on live re-theming)."""

    def __init__(self, phosphor=DEFAULT_PHOSPHOR, launch_screen=DEFAULT_LAUNCH_SCREEN):
        self.phosphor_name = phosphor if _is_choice(phosphor, PHOSPHOR_PRESETS) else DEFAULT_PHOSPHOR
        # BF-28: the phosphor CHOSEN in Settings (applies after a restart) - save_theme writes this one; the running
        # window keeps phosphor_name
        self.saved_phosphor = self.phosphor_name
        self.launch_screen = launch_screen if _is_choice(launch_screen, LAUNCH_SCREENS) else DEFAULT_LAUNCH_SCREEN
        self._apply_colours()

    def _apply_colours(self):
        """(Re)compute every colour field from phosphor_name. Touches nothing else."""
        preset = PHOSPHOR_PRESETS[self.phosphor_name]
        self.bright = preset["bright"]
        self.dim = preset["dim"]

        self.bg = SURFACE_BASE
        self.panel = SURFACE_PANEL
        self.panel_hover = SURFACE_PANEL_HOVER
        self.text = self.bright
        self.text_dim = self.dim
        self.border = self.dim
        self.danger = DANGER
        self.ok = OK
        self.warn = WARN

        # B1 tiers from the fx tone ladder (one source of truth: fx/palette.py). The palette
        # imports this module at its top, so it is imported HERE, at call time, never at
        # module level. Any failure keeps the old two-tier look rather than breaking a Theme.
        try:
            tones = _palette_module().tones(self.phosphor_name)
            for key in _TIER_KEYS:
                setattr(self, key, tones[key])
        except Exception as e:  # noqa: BLE001 - a Theme must always build
            _log_once(("tiers", type(e).__name__), f"derived colour tiers unavailable, using fallbacks: {e!r}")
            fallback = {"value": self.bright, "ghost": self.dim, "warn_eff": WARN, "unlit": self.panel,
                        "rule_out": self.dim, "rule_in": self.dim, "bloom_out": self.panel, "bloom_in": self.dim}
            for key in _TIER_KEYS:
                setattr(self, key, fallback[key])

    def set_phosphor(self, name):
        if not _is_choice(name, PHOSPHOR_PRESETS):
            raise ValueError(f"unknown phosphor preset: {name}")
        # No __init__ re-run: launch_screen and any attribute added by others stay untouched.
        self.phosphor_name = name
        self._apply_colours()

    def set_launch_screen(self, name):
        if not _is_choice(name, LAUNCH_SCREENS):
            raise ValueError(f"unknown launch screen: {name}")
        self.launch_screen = name


# ---- persistence --------------------------------------------------------------------------------------------------

_last_raw = {}          # deep copy of the dict the last load_theme() read ({} when it was not a valid object)
_store = None           # the fx PrefsStore while fx is installed (register_store), else None
_save_lock = threading.Lock()   # serialises in-process fallback writers
_logged = set()
_REPLACE_ATTEMPTS = 10
_REPLACE_DELAYS_S = (0.002, 0.004, 0.008, 0.012, 0.016, 0.02, 0.02, 0.024, 0.024)   # sum 0.15 s between 10 tries


def _log_once(key, msg):
    if key in _logged:
        return
    _logged.add(key)
    try:
        sys.stderr.write(f"theme: {msg}\n")
        sys.stderr.flush()
    except Exception:  # noqa: BLE001
        pass


_READ_SHARING_RETRIES = 4      # PermissionError retries (5 ms apart) - see _read_raw
# B13-H4: at start-up (before the first readable read of this process) a sharing violation is retried for this long,
# 10 ms apart, ONCE: a backup / sync / antivirus program holding the file for a moment no longer starts the session on
# the defaults (audible sound for a user who switched it off; default colours). A later read keeps the ~20 ms rule.
_STARTUP_READ_RETRY_S = 1.5
_startup_read_pending = True   # False after the first read that was not 'err', or once the start-up budget was spent
# test counters (fallback writer + reads); plain ints, never used for control flow
_stats = {"fallback_writes": 0, "fallback_failures": 0, "replace_retries": 0, "read_sharing_retries": 0}


def _read_raw_status():
    """-> (kind, dict): ('ok', obj) | ('missing', {}) | ('bad', {}) unparseable / not an object | ('err', {}) could not
    be read. Never raises.

    utf-8-sig: a UTF-8 BOM (Notepad 'UTF-8 with BOM', PowerShell 5 Set-Content -Encoding UTF8) is a valid file.
    On Windows an open() that lands inside another writer's os.replace() fails with a transient PermissionError
    (sharing violation), which would otherwise silently start the session on default colours; it is retried a few
    times (<= ~20 ms total), or for up to _STARTUP_READ_RETRY_S at start-up (B13-H4: a file held without sharing by
    another program). A persistent PermissionError (e.g. a directory at the path) ends in 'err'."""
    global _startup_read_pending
    budget = _STARTUP_READ_RETRY_S if _startup_read_pending else None
    t0 = time.monotonic()
    attempt = 0
    while True:
        try:
            with open(THEME_PATH, "r", encoding="utf-8-sig") as f:
                data = json.load(f)
        except FileNotFoundError:
            _startup_read_pending = False
            return "missing", {}
        except PermissionError:
            attempt += 1
            if budget is not None:
                if time.monotonic() - t0 < budget:
                    _stats["read_sharing_retries"] += 1
                    time.sleep(0.01)
                    continue
                _startup_read_pending = False      # spent: a later read never stalls the Tk thread for it again
            elif attempt <= _READ_SHARING_RETRIES:
                _stats["read_sharing_retries"] += 1
                time.sleep(0.005)
                continue
            return "err", {}
        except OSError:
            return "err", {}
        except Exception:  # noqa: BLE001 - ValueError, UnicodeDecodeError, RecursionError, ...
            _startup_read_pending = False
            return "bad", {}
        _startup_read_pending = False
        return ("ok", data) if isinstance(data, dict) else ("bad", {})


def _read_raw():
    """The theme file as a dict; {} when missing, unreadable, unparseable or not a JSON object. Never raises."""
    return _read_raw_status()[1]


def _safe_copy(obj):
    """Deep copy of a JSON-shaped value; never raises. copy.deepcopy needs ~2 Python frames per nesting level, so a
    valid file with an unknown key nested ~500 deep raises RecursionError - fall back to a C-level JSON round trip."""
    try:
        return copy.deepcopy(obj)
    except Exception:  # noqa: BLE001
        pass
    try:
        return json.loads(json.dumps(obj))
    except Exception:  # noqa: BLE001
        return {}


def load_theme():
    """Theme from desktop_theme.json; per-field defaults for anything missing/invalid. Never raises, never writes."""
    global _last_raw
    raw = _read_raw()
    _last_raw = raw     # a fresh parse nobody else holds; last_loaded_raw() hands out copies
    try:
        phosphor = raw.get("phosphor", DEFAULT_PHOSPHOR)
        launch = raw.get("launch_screen", DEFAULT_LAUNCH_SCREEN)
        return Theme(phosphor if _is_choice(phosphor, PHOSPHOR_PRESETS) else DEFAULT_PHOSPHOR,
                     launch if _is_choice(launch, LAUNCH_SCREENS) else DEFAULT_LAUNCH_SCREEN)
    except Exception:  # noqa: BLE001
        return Theme()


def last_loaded_raw():
    """Deep copy of the raw dict the most recent load_theme() read ({} if none / invalid)."""
    out = _safe_copy(_last_raw)
    return out if isinstance(out, dict) else {}


def load_fx_status():
    """(kind, fx): a fresh, read-only read of the file's "fx" object ({} if absent or invalid) with the read's verdict as
    _read_raw_status gives it - 'ok' | 'missing' | 'bad' | 'err' (B13-H4: start-up tells a file it could not read from
    one that is not there). Never raises, never writes."""
    try:
        kind, raw = _read_raw_status()     # a fresh parse: no copy needed
        fx = raw.get("fx")
        return kind, (fx if isinstance(fx, dict) else {})
    except Exception:  # noqa: BLE001
        return "err", {}


def load_fx_raw():
    """A fresh, read-only read of the file's "fx" object ({} if absent or invalid). Never raises, never writes."""
    return load_fx_status()[1]


def register_store(store):
    """fx.install registers its PrefsStore here (fx.teardown passes None). While one is registered, save_theme is
    memory-only and the store's writer thread is the single writer of the file."""
    global _store
    _store = store


def save_theme(theme):
    """Persist phosphor + launch_screen. Never raises.

    With a store registered: store.set_root(...) x2, memory only (no Tk-thread I/O).
    Without one (NullFX / kill switch / harness): read-merge-write under an in-process lock - unknown keys and the "fx"
    block are preserved, a non-object or corrupt base becomes {} - written to a tmp file and os.replace'd (no fsync,
    AM-8), retried ~10x within ~150 ms if the target is held open; on final failure the tmp file is removed and the
    previous file stays intact."""
    # Per field, like load_theme: one invalid attribute never costs the other field its save.
    chosen = getattr(theme, "saved_phosphor", None)          # BF-28: the pending choice, not the running palette
    fields = (("phosphor", chosen if chosen is not None else getattr(theme, "phosphor_name", None), PHOSPHOR_PRESETS),
              ("launch_screen", getattr(theme, "launch_screen", None), LAUNCH_SCREENS))
    store = _store
    if store is not None:
        for key, value, _choices in fields:
            try:
                store.set_root(key, value)
            except Exception as e:  # noqa: BLE001
                _log_once(("store", key, type(e).__name__), f"save_theme via prefs store failed for {key}: {e!r}")
        return
    valid = [(key, value) for key, value, choices in fields if _is_choice(value, choices)]
    for key, value, choices in fields:
        if not _is_choice(value, choices):
            _log_once(("invalid", key), f"save_theme: not saving invalid {key} {value!r}")
    if not valid:
        return
    with _save_lock:
        path = THEME_PATH
        tmp = f"{path}.tmp-{os.getpid()}-{threading.get_ident()}"
        kind, data = _read_raw_status()
        if kind == "err":
            # Present but unreadable (held exclusively, access denied, ...): merging onto {} would wipe the fx
            # block and every unknown key, so do not write at all.
            _stats["fallback_failures"] += 1
            _log_once(("read", "err"), f"not saving: {path} exists but could not be read")
            return
        try:
            for key, value in valid:
                data[key] = value
            folder = os.path.dirname(path) or DATA_DIR
            os.makedirs(folder, exist_ok=True)
            text = json.dumps(data)     # one-shot C encoder (json.dump streams via the recursive pure-Python one)
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(text)
        except Exception as e:  # noqa: BLE001
            _stats["fallback_failures"] += 1
            _remove_quietly(tmp)
            _log_once(("write", type(e).__name__), f"could not write {tmp}: {e!r}")
            return
        last = None
        for attempt in range(_REPLACE_ATTEMPTS):
            try:
                os.replace(tmp, path)
                _stats["fallback_writes"] += 1
                return
            except OSError as e:  # PermissionError while a reader/zipper holds the file, ...
                last = e
                if attempt < len(_REPLACE_DELAYS_S):
                    _stats["replace_retries"] += 1
                    time.sleep(_REPLACE_DELAYS_S[attempt])
        _stats["fallback_failures"] += 1
        _remove_quietly(tmp)
        _log_once(("replace", type(last).__name__), f"could not replace {path} after {_REPLACE_ATTEMPTS} tries: {last!r}")


def _remove_quietly(path):
    try:
        os.remove(path)
    except OSError:
        pass
