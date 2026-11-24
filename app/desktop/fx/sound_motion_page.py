"""fx/sound_motion_page.py - Settings 08 "Sound & motion": a FRAME plus a SECTION REGISTRY (B10, first part).

BUILD_PLAN §4 B10 "Settings 08, first part" (X-19: motion ships with its control), GAP_FX 4.1; approved
r2_settings_sound / r2_settings_sound_end. The page is built once, inside the Settings screen's own scrollable pane
"fx" (frameless, the thin scrollbar, content x 290..1060), from an ordered registry of sections. B10 registers only
DISPLAY MOTION (fx/page08/motion.py). B11 adds its CRT section and B13 the sound sections, each as a NEW module in
fx/page08/ whose last line registers it: nobody edits another phase's code, and nothing here changes.

    install_fx_pane(screen)               SettingsScreen.__init__, one line after _build_data_pane() (guarded import).
                                          Never raises: any failure shows UNAVAILABLE in the pane instead.
    register_section(key, order, builder) a section module's one-line registration (replaces an earlier `key`)
    unregister_section(key)               -> True when it was registered
    sections()                            the registered SectionSpecs in page order (order, then registration)
    discover()                            imports every module of the fx/page08 package once (each registers itself);
                                          a module that fails to import is logged once and named in failed_modules()
    write_once(store, items)              several prefs leaves in ONE store commit (one file write)
    SoundMotionPage                       the page frame (screen.fx_page)
    Section                               the base class a builder returns (every hook optional)

Registry contract (for B11 / B13)
---------------------------------
* A section module lives in fx/page08/ and ends with ONE line:
      register_section("crt", 310, CrtSection)
  `builder(page, parent) -> Section`: `parent` is a tk.Frame the page made and packed (fill x) for this section,
  at its place in the order. The builder draws into it and returns the object the page talks to.
* Order keys: SIGNAL OUTPUT 100, CUE POLICY 200 (B13); the opt-in cue lamps 210 (B14, "optin", no title: they
  continue CUE POLICY); DISPLAY MOTION 300 (B10, "motion"); the CRT rows 310 (B11, no title of their own: they
  continue DISPLAY MOTION). DEFAULTS is the page's own and always last.
* The page offers the builder (all on the Tk thread):
      page.screen / page.theme / page.t (kit tokens) / page.fx (FX or NullFX)
      page.prefs   the live PrefsStore, or None (kill switch / fx did not start)
      page.live    True when the controls may write (page.prefs is not None)
      page.view    a store to READ values from: the live store, or (read-only page) a memory-only shadow
                   PrefsStore(None, theme.last_loaded_raw(), theme) - no file I/O, no thread
      page.title(parent, text, tag=None)   the section row (the first thing on the page has no gap above it)
      page.option_row(parent) / page.label(parent, text, dim=False) / page.caption(parent, text, indent=0)
                   the Settings screen's own helpers (same look as pages 01-07; captions wrap with the pane)
      page.string_var(value)   a StringVar OWNED by the page: finalised on the Tk thread (release_var) once the
                               page's widgets are gone, whoever still holds the object (the F-1 gc hazard)
      page.set_pref(leaf, value) -> bool   prefs.set when live (never raises; False when read-only or refused)
      page.subscribe(prefix, cb, owner)    prefs.subscribe(prefix, cb, owner=owner) when live, else a no-op
      page.default(leaf)       that leaf's schema default (None for fx.motion = follow Windows)
* A Section may define (the base class has no-op defaults):
      noun          the word RESTORE DEFAULTS' caption uses ("motion", "CRT", "sound"); None = not listed
      defaults()    -> {dotted leaf: value} for the rows it SHOWS (RESTORE DEFAULTS resets only those)
      refresh()     repaint from page.view (after RESTORE DEFAULTS; the page calls it)
      on_show()     page 08 became the shown category (called again on every show)
      on_hide()     page 08 stopped being shown (another category, or Settings hidden)
      release()     drop its own Variables / images / widget commands (the page calls it before its widgets die)
* RESTORE DEFAULTS... (plain confirm) merges every section's defaults() and writes them with write_once(): ONE
  prefs commit, one file write, whatever the number of sections. Leaves no section shows are never touched. A pair
  prefs refuses (unknown leaf, invalid value) is skipped and logged once; the other pairs are still written.
* A builder that raises: its frame is emptied and shows UNAVAILABLE in its place (logged once); the other sections
  keep working. No section at all: the page is UNAVAILABLE alone.

Kill switch (D-31): the page is read-only - every control drawn disabled with the stored setting - under one line
saying that effects are switched off and how to turn them on. No timer anywhere here (no after()); nothing touches
Tk off the Tk thread; the page makes no HTTP call and reads no file.
"""

import os
import tkinter as tk

from .. import theme as _theme
from . import prefs as _prefs

__all__ = ["FX_KEY", "CATEGORY", "STAMP", "UNAVAILABLE", "Section", "SectionSpec",
           "register_section", "unregister_section", "sections", "discover", "failed_modules", "write_once",
           "release_var", "killswitch_line", "restore_caption", "SoundMotionPage", "install_fx_pane",
           "unavailable_pane"]

FX_KEY = "fx"
CATEGORY = ("fx", "Sound & motion", "cues, motion, CRT")
STAMP = "Changes apply at once"
UNAVAILABLE = "Sound & motion controls are unavailable in this build."
DEFAULTS_TITLE, DEFAULTS_TAG, DEFAULTS_ACTION = "Defaults", "CONFIRMS FIRST", "RESTORE DEFAULTS..."
DEFAULTS_KEPT = "Phosphor and start screen are not changed."
CONFIRM_TITLE = CONFIRM_LABEL = "Restore defaults"
ROW_GAP = 18                # Settings' control-row gap (settings_screen.ROW_GAP)

_SPECS = {}                 # key -> SectionSpec
_SEQ = [0]                  # registration counter (ties in `order` keep registration order)
_DISCOVERED = [False]
_FAILED_MODULES = []        # [(module name, repr(exception))]


def _log_once(key, msg):
    try:
        from . import _log_once as fx_log_once
        fx_log_once(("page08",) + tuple(key), "fx page 08: " + msg)
    except Exception:  # noqa: BLE001 - logging must never break the page
        pass


# ====================================================================== registry

class SectionSpec(object):
    __slots__ = ("key", "order", "builder", "seq", "module")

    def __init__(self, key, order, builder, seq, module):
        self.key, self.order, self.builder, self.seq, self.module = key, order, builder, seq, module

    def __repr__(self):
        return "<SectionSpec %r order=%r from %s>" % (self.key, self.order, self.module)


def register_section(key, order, builder):
    """Register (or replace) page-08 section `key` at sort position `order` (a number). builder(page, parent) ->
    Section. Returns the SectionSpec. ValueError on a bad key / order / builder."""
    if not isinstance(key, str) or not key:
        raise ValueError("section key must be a non-empty string, not %r" % (key,))
    if isinstance(order, bool) or not isinstance(order, (int, float)) or order != order:
        raise ValueError("section order must be a number, not %r" % (order,))
    if not callable(builder):
        raise ValueError("section builder must be callable")
    _SEQ[0] += 1
    spec = SectionSpec(key, order, builder, _SEQ[0], getattr(builder, "__module__", "?"))
    _SPECS[key] = spec
    return spec


def unregister_section(key):
    return _SPECS.pop(key, None) is not None


def sections():
    return sorted(_SPECS.values(), key=lambda s: (s.order, s.seq))


def failed_modules():
    return list(_FAILED_MODULES)


def discover():
    """Import every module of the fx/page08 package, once per process (each module registers its section at import).
    A module that fails is logged once and listed by failed_modules(); the others still load. Never raises."""
    if _DISCOVERED[0]:
        return
    _DISCOVERED[0] = True
    try:
        import importlib
        import pkgutil
        from . import page08 as pkg
        names = sorted(m.name for m in pkgutil.iter_modules(pkg.__path__) if not m.name.startswith("_"))
    except Exception as e:  # noqa: BLE001
        _FAILED_MODULES.append(("page08", repr(e)))
        _log_once(("discover", type(e).__name__), "the section package could not be listed: %r" % (e,))
        return
    for name in names:
        try:
            importlib.import_module(pkg.__name__ + "." + name)
        except Exception as e:  # noqa: BLE001 - one broken section module never takes the page down
            _FAILED_MODULES.append((name, repr(e)))
            _log_once(("import", name), "section module %s failed to import: %r" % (name, e))


# ====================================================================== the base section

class Section(object):
    """What a builder returns. Every hook is optional; these are the no-op defaults."""

    noun = None

    def defaults(self):
        return {}

    def refresh(self):
        return None

    def on_show(self):
        return None

    def on_hide(self):
        return None

    def release(self):
        return None


# ====================================================================== prefs helpers

def write_once(store, items):
    """Write several (dotted leaf, value) pairs in ONE store commit, so the writer thread makes one file write
    (RESTORE DEFAULTS, however many sections). Every pair is validated first with prefs' own rules (ValueError on a
    bad one, nothing written). Every leaf is marked dirty even when its value is unchanged (as
    prefs.restore_fx_defaults does), so the FILE ends at these values after an external edit too. -> True when a value
    changed in memory. Uses a public set_many() when the store has one; otherwise the store's own one-burst commit
    (the same call restore_fx_defaults makes; this module is part of the fx package)."""
    items = list(items)
    clean = []
    for leaf, value in items:
        path = _prefs._norm(leaf)
        if path is None or path not in _prefs._SPEC:
            raise ValueError("unknown preference path %r" % (leaf,))
        ok, cv = _prefs._validate(path, value)
        if not ok:
            raise ValueError("invalid value for %s: %r" % (leaf, value))
        clean.append((path, cv))
    if not clean:
        return False
    batch = getattr(store, "set_many", None)
    if callable(batch):
        return bool(batch(items))
    commit = getattr(store, "_commit", None)
    if callable(commit):
        return bool(commit(clean, force_dirty=True))
    changed = False
    for leaf, value in items:                    # last resort: the writer's 20 ms coalesce usually makes it one write
        changed = bool(store.set(leaf, value)) or changed
    return changed


def release_var(var):
    """F-1: finalise a tkinter Variable NOW, on the Tk thread - its Tcl variable unset, its trace commands deleted -
    so that whenever Python frees the object later (a reference cycle the cyclic gc breaks, on any thread) its __del__
    makes no Tcl call (Variable.__del__ returns at once when _tk is None). Needed because a kit Segmented keeps its
    variable in a cycle (Segmented._cells <-> cell.master) that reference counting never frees. Call it only after
    every widget using the variable is destroyed. Never raises."""
    tk_ = getattr(var, "_tk", None)
    if tk_ is None:
        return
    try:
        if tk_.getboolean(tk_.call("info", "exists", var._name)):
            tk_.globalunsetvar(var._name)
        for name in (getattr(var, "_tclCommands", None) or ()):
            tk_.deletecommand(name)
        var._tclCommands = None
    except Exception:  # noqa: BLE001 - an interpreter already gone: nothing left to release
        pass
    finally:
        var._tk = None


def default_of(leaf):
    path = _prefs._norm(leaf)
    spec = _prefs._SPEC.get(path) if path is not None else None
    return None if spec is None else spec[2]


def killswitch_line(fx_mod=None):
    """The read-only page's one line: effects are off, why, and how to turn them on (D-31: LOCALAI_FX=0 and/or the
    marker file <data>/fx_disabled; else fx did not start in this session)."""
    if fx_mod is None:
        from .. import fx as fx_mod  # noqa: PLC0415
    env = None
    try:
        v = os.environ.get("LOCALAI_FX")
        if v is not None and v.strip().lower() in getattr(fx_mod, "_KILL_VALUES", ("0",)):
            env = "LOCALAI_FX=%s" % v.strip()
    except Exception:  # noqa: BLE001
        env = None
    marker = False
    try:
        data_dir = getattr(_theme, "DATA_DIR", None)
        if isinstance(data_dir, (str, os.PathLike)) and data_dir:
            marker = os.path.exists(os.path.join(data_dir, fx_mod.MARKER_NAME))
    except Exception:  # noqa: BLE001
        marker = False
    name = "data\\" + getattr(fx_mod, "MARKER_NAME", "fx_disabled")
    if env and marker:
        return ("Effects are switched off (%s and the file %s), so these settings are read-only. Remove both and "
                "restart the app to turn the effects back on." % (env, name))
    if marker:
        return ("Effects are switched off by the file %s, so these settings are read-only. Delete that file and "
                "restart the app to turn the effects back on." % name)
    if env:
        return ("Effects are switched off (%s), so these settings are read-only. Start the app without %s to turn "
                "the effects back on." % (env, env))
    return ("Effects did not start in this session, so these settings are read-only. Restart the app to try again "
            "(desktop.log names the cause).")


def restore_caption(nouns):
    """RESTORE DEFAULTS' caption, naming only the rows that exist: 'Motion goes back to its default.' /
    'Sound, motion and CRT go back to their defaults.' + the approved 'Phosphor and start screen are not changed.'"""
    words = [str(n) for n in nouns if n]
    if not words:
        return DEFAULTS_KEPT
    words = [words[0][:1].upper() + words[0][1:]] + words[1:]
    if len(words) == 1:
        head = "%s goes back to its default." % words[0]
    else:
        head = "%s and %s go back to their defaults." % (", ".join(words[:-1]), words[-1])
    return head + " " + DEFAULTS_KEPT


# ====================================================================== the page

class SoundMotionPage(tk.Frame):
    """The page frame, packed into the Settings pane "fx". See the module docstring for what a section builder gets.
    .sections: [(SectionSpec, Section)] of the sections that built; .failed: [(key, repr)] of those that did not.
    set_shown(bool) is driven by SettingsScreen._show_category / on_hide. rebuild() builds again from the registry
    (a section registered late; tests)."""

    def __init__(self, master, screen, **kw):
        self.screen = screen
        self.theme = screen.ctx.theme
        try:
            self.t = dict(screen._t)
        except Exception:  # noqa: BLE001
            from ..kit import tokens
            self.t = tokens(self.theme)
        kw.setdefault("bg", self.t["bg"])
        tk.Frame.__init__(self, master, **kw)
        from .. import fx as fx_mod
        self._fx_mod = fx_mod
        self.fx = fx_mod.get(screen)
        store = getattr(self.fx, "prefs", None)
        self.prefs = store if (store is not None and not getattr(store, "closed", False)) else None
        self.live = self.prefs is not None
        self.view = self.prefs if self.live else self._shadow_store()
        self.sections = []
        self.failed = []
        self.shown = False
        self.killswitch_label = None
        self.notice_label = None
        self.restore_action = None
        self.restore_caption_label = None
        self._vars = []
        self._first = True
        self._build()

    # ---------------------------------------------------------------- builder helpers
    def _shadow_store(self):
        """Read-only page: the stored values, read from the dict theme.load_theme() already read (no file I/O,
        no writer thread: path None)."""
        try:
            raw = _theme.last_loaded_raw()
        except Exception:  # noqa: BLE001
            raw = {}
        try:
            return _prefs.PrefsStore(None, raw if isinstance(raw, dict) else {}, self.theme)
        except Exception:  # noqa: BLE001
            return None

    def _gap(self):
        first, self._first = self._first, False
        return first

    def title(self, parent, text, tag=None):
        first = self._gap()
        section = getattr(self.screen, "_section", None)
        if callable(section):
            return section(parent, text, tag=tag, first=first)
        row = tk.Frame(parent, bg=self.t["bg"])
        tk.Label(row, text=str(text).upper(), bg=self.t["bg"], fg=self.t["bright"]).pack(side="left")
        row.pack(fill="x", pady=(0 if first else 26, 0))
        return row

    def option_row(self, parent):
        self._first = False
        helper = getattr(self.screen, "_option_row", None)
        if callable(helper):
            return helper(parent)
        row = tk.Frame(parent, bg=self.t["bg"])
        row.pack(fill="x", pady=(ROW_GAP, 0))
        row.columnconfigure(0, minsize=230)
        return row

    def label(self, parent, text, dim=False, **kw):
        helper = getattr(self.screen, "_label", None)
        if callable(helper):
            return helper(parent, text, dim=dim, **kw)
        kw.setdefault("bg", self.t["bg"])
        kw.setdefault("fg", self.t["value"] if dim else self.t["bright"])
        return tk.Label(parent, text=text, anchor="w", justify="left", **kw)

    def caption(self, parent, text, indent=0):
        self._first = False
        helper = getattr(self.screen, "_caption", None)
        if callable(helper):
            return helper(parent, text, indent=indent)
        lab = tk.Label(parent, text=text, bg=self.t["bg"], fg=self.t["value"], anchor="w", justify="left",
                       wraplength=560)
        lab.pack(fill="x", padx=(indent, 0), pady=(4, 0))
        return lab

    def string_var(self, value=""):
        var = tk.StringVar(master=self, value=value)
        self._vars.append(var)
        return var

    def set_pref(self, leaf, value):
        if not self.live:
            return False
        try:
            return bool(self.prefs.set(leaf, value))
        except Exception as e:  # noqa: BLE001 - a refused value is logged, never raised into a click
            _log_once(("set", leaf, type(e).__name__), "prefs.set(%r, %r) refused: %r" % (leaf, value, e))
            return False

    def subscribe(self, prefix, cb, owner):
        if not self.live:
            return lambda: None
        try:
            return self.prefs.subscribe(prefix, cb, owner=owner)
        except Exception as e:  # noqa: BLE001
            _log_once(("subscribe", prefix, type(e).__name__), "prefs.subscribe(%r) failed: %r" % (prefix, e))
            return lambda: None

    @staticmethod
    def default(leaf):
        return default_of(leaf)

    def content_width(self):
        """The pane's content width in px: the Settings screen's caption wrap width, which follows the WINDOW (with
        the scrollbar's room kept), never this page's own size - so a layout decided from it cannot feed back into
        it. 770 (the content width at 1100 x 800) when the screen has none."""
        try:
            w = int(getattr(getattr(self.screen, "_wrap", None), "width", 0) or 0)
        except (TypeError, ValueError):
            w = 0
        return w if w > 40 else 770

    # ---------------------------------------------------------------- build
    def _build(self):
        self._first = True
        discover()
        if not self.live:
            self.killswitch_label = self.label(self, killswitch_line(self._fx_mod), dim=True, fg=self.t["bright"])
            self.killswitch_label.pack(fill="x")
            self._first = False
        if failed_modules():
            self._notice(self)
        for spec in sections():
            holder = tk.Frame(self, bg=self.t["bg"])
            holder.pack(fill="x")
            first = self._first
            try:
                section = spec.builder(self, holder)
                if section is None:
                    raise TypeError("the builder returned None")
                self.sections.append((spec, section))
            except Exception as e:  # noqa: BLE001 - one broken section never takes the page (or Settings) down
                self.failed.append((spec.key, repr(e)))
                _log_once(("build", spec.key, type(e).__name__), "section %r failed to build: %r" % (spec.key, e))
                for child in holder.winfo_children():
                    child.destroy()
                self._first = first         # B10-H18: the destroyed title gives the flush slot back to the notice
                self._notice(holder)
        if not self.sections:
            for child in self.winfo_children():
                child.destroy()
            self.killswitch_label = self.notice_label = None
            self._first = True
            self._notice(self)
            return
        self._build_defaults()

    def _notice(self, parent):
        lab = self.label(parent, UNAVAILABLE, dim=True)
        lab.pack(fill="x", pady=(0 if self._gap() else ROW_GAP, 0))
        if parent is self:
            self.notice_label = lab
        return lab

    def _build_defaults(self):
        from ..kit.widgets import TextAction
        holder = tk.Frame(self, bg=self.t["bg"])
        holder.pack(fill="x")
        self.title(holder, DEFAULTS_TITLE, tag=DEFAULTS_TAG)
        can = self.live and bool(self.reset_items())
        self.restore_action = TextAction(holder, self.theme, text=DEFAULTS_ACTION, command=self._ask_restore,
                                         ground=self.t["bg"], state="normal" if can else "disabled")
        self.restore_action.pack(anchor="w", pady=(ROW_GAP // 2, 0))
        self.restore_caption_label = self.caption(holder, restore_caption(self.nouns()))

    # ---------------------------------------------------------------- RESTORE DEFAULTS
    def nouns(self):
        """The RESTORE caption's nouns, in page order, each once (B13: SIGNAL OUTPUT and CUE POLICY are both "sound").
        A section whose `noun` raises is logged once and left out (ADV2-02: one broken section never takes the page
        down)."""
        out = []
        for spec, sec in self.sections:
            try:
                n = getattr(sec, "noun", None)
                if n and n not in out:
                    out.append(n)
            except Exception as e:  # noqa: BLE001
                _log_once(("noun", spec.key, type(e).__name__), "section %r noun raised %r" % (spec.key, e))
        return out

    def reset_items(self):
        """Every (leaf, default) the built sections show, in page order (a leaf named twice: the first wins). A pair
        prefs would refuse (unknown leaf, invalid value) is skipped and logged once naming its section (ADV1-02), so
        one broken section never stops RESTORE DEFAULTS for the others."""
        out, seen = [], set()
        for spec, sec in self.sections:
            try:
                items = dict(sec.defaults() or {})
            except Exception as e:  # noqa: BLE001
                _log_once(("defaults", spec.key, type(e).__name__), "section %r defaults() raised %r" % (spec.key, e))
                continue
            for leaf, value in items.items():
                try:
                    path = _prefs._norm(leaf)
                    ok = path is not None and path in _prefs._SPEC and _prefs._validate(path, value)[0]
                except Exception:  # noqa: BLE001
                    ok = False
                if not ok:
                    _log_once(("defaults_bad", spec.key, repr(leaf)[:60]),
                              "section %r defaults() gave a pair prefs refuses: %r = %r - skipped"
                              % (spec.key, leaf, value))
                    continue
                if path not in seen:                   # the normalised path: "fx.crt" and ("fx", "crt") are one leaf
                    seen.add(path)
                    out.append((leaf, value))
        return out

    def _ask_restore(self):
        if not self.live or not self.reset_items():
            return None
        from ..screens import base
        return base.show_inline_confirm(self, self.theme, restore_caption(self.nouns()), self.restore_defaults,
                                        confirm_label=CONFIRM_LABEL, danger=False, title=CONFIRM_TITLE)

    def restore_defaults(self):
        """RESTORE DEFAULTS' confirmed act: every built section's rows back to their defaults in ONE prefs commit
        (one file write), then the sections repainted (the rows show the result; no notice: a new show_toast site
        would need a sound-bridge rule, B12/B13's). -> True when it wrote."""
        items = self.reset_items()
        if not self.live or not items:
            return False
        try:
            write_once(self.prefs, items)
        except Exception as e:  # noqa: BLE001
            _log_once(("restore", type(e).__name__), "RESTORE DEFAULTS failed: %r" % (e,))
            return False
        self.refresh()
        return True

    # ---------------------------------------------------------------- lifecycle
    def refresh(self):
        for spec, sec in list(self.sections):
            self._call(spec, sec, "refresh")

    def set_shown(self, shown):
        """SettingsScreen: page 08 is (True) / is no longer (False) the shown category. on_show runs on every show;
        on_hide only when the page was shown."""
        shown = bool(shown)
        was, self.shown = self.shown, shown
        if shown:
            for spec, sec in list(self.sections):
                self._call(spec, sec, "on_show")
        elif was:
            for spec, sec in list(self.sections):
                self._call(spec, sec, "on_hide")

    def _call(self, spec, sec, name):
        try:
            fn = getattr(sec, name, None)
            if callable(fn):
                fn()
        except Exception as e:  # noqa: BLE001
            _log_once((name, spec.key, type(e).__name__), "section %r %s() raised %r" % (spec.key, name, e))

    def _release(self):
        for spec, sec in list(self.sections):
            self._call(spec, sec, "release")
        self.sections = []
        if self.restore_action is not None:
            try:
                self.restore_action.configure(command=None)
            except tk.TclError:
                pass
        self.restore_action = None
        self.view = None

    def _finalise_vars(self):
        """After the widgets that used them are gone: every Variable the page made is finalised on the Tk thread
        (release_var), whoever still holds the object."""
        made, self._vars = self._vars, []
        for var in made:
            release_var(var)

    def rebuild(self):
        """Build the page again from the current registry (the old widgets and Variables are released first)."""
        self._release()
        for child in self.winfo_children():
            child.destroy()
        self._finalise_vars()
        self.failed = []
        self.killswitch_label = self.notice_label = self.restore_caption_label = None
        self.view = self.prefs if self.live else self._shadow_store()
        self._build()
        if self.shown:
            self.set_shown(True)
        return self

    def destroy(self):
        self._release()
        try:
            tk.Frame.destroy(self)
        finally:
            self._finalise_vars()


# ====================================================================== install

def install_fx_pane(screen):
    """Create Settings page 08 in `screen` (a SettingsScreen): its scrollable pane "fx" holds a SoundMotionPage, kept
    as screen.fx_page. Never raises: if the page cannot be built, the pane shows UNAVAILABLE (logged once) and the
    app opens as usual. -> the page, or None."""
    body = None
    try:
        body = screen._pane(FX_KEY)
        page = SoundMotionPage(body, screen)
        page.pack(fill="x", anchor="n")
        screen.fx_page = page
        return page
    except Exception as e:  # noqa: BLE001 - Settings (and the app) must open whatever happens here
        _log_once(("install", type(e).__name__), "the page could not be built: %r" % (e,))
        screen.fx_page = None
        unavailable_pane(screen, body)
        return None


def unavailable_pane(screen, body=None):
    """Page 08 as the one-line notice (the page failed, or this module could not be imported). Never raises."""
    try:
        if body is None or not body.winfo_exists():
            body = screen._pane(FX_KEY)
        for child in body.winfo_children():
            child.destroy()
        helper = getattr(screen, "_label", None)
        if callable(helper):
            lab = helper(body, UNAVAILABLE, dim=True)
        else:
            lab = tk.Label(body, text=UNAVAILABLE, anchor="w")
        lab.pack(fill="x")
        return lab
    except Exception as e:  # noqa: BLE001
        _log_once(("unavailable", type(e).__name__), "the notice could not be shown either: %r" % (e,))
        return None
