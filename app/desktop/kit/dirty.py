"""kit/dirty.py - the Settings unsaved-changes guard (B8d: D-7, spec ST-13 in its minimal form; BUILD_PLAN §4 B8d).

    DirtyTracker(screen, collect=None, text_widgets=(), on_change=None)
        A pure snapshot compare, no API call. `clean` is the body Save would send (screen._collect_body()) as it
        was right after the last populate or save. Has-snapshot means `clean is not None` - never _settings_loaded,
        which every load resets. Recomputes SYNCHRONOUSLY on a write of any tk.Variable of the screen (and of
        screen.gpu_section), discovered once by attach(), and on a text widget's <<Modified>> (then
        edit_modified(False)). There is no timer and no debounce: the compare is cheap, and it keeps the timer
        rule of BUILD_PLAN §1.1 under the kill switch.

        dirty_keys()          body keys whose value differs from `clean` (a field holding an invalid number is
                              one of them); [] while there is no snapshot
        user_values()         {key: value} of the dirty keys, read independently: an invalid number is SKIPPED,
                              never raised (it is applied inside a load's apply)
        mark_clean(base, take) snapshot now (after a populate); after a successful save: the body SENT, with the
                              `take` keys (those the answer rewrote on screen) read back, so an edit typed during the
                              save stays UNSAVED
        mark_kept()           the dirty values were kept across a reload (UNSAVED CHANGES KEPT + REVERT)
        discard()             forget the snapshot: the next load populates fresh and keeps nothing
        rebase(fresh, keys)   after a model switch resynced the restart widgets: those keys' on-screen values
                              are the new clean ones (no false UNSAVED), and screen._settings takes fresh's values
                              for them (so a REVERT never restores a stale gpu_layers)
        held()                context: writes inside it recompute once, at the end (a populate sets ~35 fields)
        quiet()               context: what the PROGRAM changes inside it (an async inventory answer) is not an
                              edit - a key it changes that was clean stays clean; an edit already pending stays
        detach()              remove every trace (the screen's destroy)

    DirtyStamp(footer, theme, rest, on_revert)
        Drives SettingsFooter's stamp (kit/settings.py): the rest text (`Changes apply on save`, C9 value) while
        clean, `UNSAVED · n CHANGES` while dirty, `UNSAVED CHANGES KEPT · n` + a REVERT text action after a
        reload that kept the edits; ` · INVALID NUMBER` is added while a field cannot be read as a number. The
        transient status (status_label) still takes the stamp's place while it shows (FooterStatus).

House rules (kit/__init__): no after(), no I/O, Tk thread only. The tracker creates no Tk Variable and no image;
its trace commands are removed by detach() (and with their Variables).
"""

import contextlib
import tkinter as tk

from . import C9, tokens
from .widgets import TextAction

__all__ = ["DirtyTracker", "DirtyStamp", "stamp_text", "REST", "UNSAVED", "KEPT", "INVALID"]

REST = "Changes apply on save"
UNSAVED = "UNSAVED · {n} CHANGES"
KEPT = "UNSAVED CHANGES KEPT · {n}"
INVALID = " · INVALID NUMBER"

_MISSING = object()
# what reading a half-typed number (or a junk value) raises inside _collect_body
_BAD_NUMBER = (tk.TclError, ValueError, TypeError, OverflowError, IndexError, KeyError)
# placeholder readings for an unreadable variable while the other keys are read (see DirtyTracker.current):
# several, so that a key which rounds two of them to the same value is still found
_PROBES = (1, 2, 3, 301, 7777)


class _Fixed:
    """Stands in for a tk.Variable that cannot be read (current()): .get() returns a fixed number."""

    __slots__ = ("value",)

    def __init__(self, value):
        self.value = value

    def get(self):
        return self.value


def _readable(var):
    try:
        var.get()
        return True
    except _BAD_NUMBER:
        return False


def stamp_text(n, kept=False, invalid=False, rest=REST):
    """The footer stamp for n dirty keys: the rest text at 0, else UNSAVED / KEPT (+ INVALID NUMBER)."""
    if n <= 0:
        return rest
    text = (KEPT if kept else UNSAVED).format(n=n)
    return text + (INVALID if invalid else "")


class DirtyTracker:
    def __init__(self, screen, collect=None, text_widgets=(), on_change=None):
        self.screen = screen
        self._collect = collect or screen._collect_body
        self._texts = [w for w in (text_widgets or ()) if w is not None]
        self.on_change = on_change
        self.clean = None              # the snapshot; None = no snapshot (nothing is dirty, nothing is kept)
        self.kept = False
        self.invalid = frozenset()
        self._dirty = frozenset()
        self._hold = 0
        self._traces = []              # (variable, trace name)
        self._vars = []                # (owner, attribute name, variable): what current() may stand in for
        self._attached = False
        self.recomputes = 0            # for tests / perf

    # ------------------------------------------------------------------ wiring
    def _discover(self):
        found, seen = [], set()
        owners = [self.screen, getattr(self.screen, "gpu_section", None)]
        for owner in owners:
            if owner is None:
                continue
            try:
                items = sorted(vars(owner).items())
            except TypeError:
                continue
            for name, v in items:
                if isinstance(v, tk.Variable) and id(v) not in seen:
                    seen.add(id(v))
                    found.append((owner, name, v))
        return found

    def attach(self):
        """Trace every tk.Variable of the screen (and of screen.gpu_section) and the text widgets' <<Modified>>.
        Once; a second call does nothing."""
        if self._attached:
            return self
        self._attached = True
        self._vars = self._discover()
        for _owner, _name, v in self._vars:
            try:
                self._traces.append((v, v.trace_add("write", self._on_write)))
            except tk.TclError:
                pass
        for text in self._texts:
            try:
                text.edit_modified(False)
                text.bind("<<Modified>>", lambda e, w=text: self._on_modified(w), add="+")
            except tk.TclError:
                pass
        return self

    def detach(self):
        """Remove every variable trace (and its Tcl command). The text bindings die with their widgets."""
        traces, self._traces = self._traces, []
        for v, name in traces:
            try:
                v.trace_remove("write", name)
            except (tk.TclError, ValueError, AttributeError):
                pass
        self._vars = []
        self.on_change = None

    def _on_write(self, *_a):
        if self._hold:
            return
        self.recompute()

    def _on_modified(self, text):
        try:
            if not text.edit_modified():
                return None          # the reset below fires <<Modified>> once more: nothing changed
            text.edit_modified(False)
        except tk.TclError:
            return None
        self._on_write()
        return None

    # ------------------------------------------------------------------ reading
    def current(self):
        """(body, invalid keys): the body Save would send now, or (None, frozenset()) when it cannot be read at all
        (the screen is going away). A variable that cannot be read as a number (a half-typed context size) does not
        stop the others: it is stood in for by a fixed number while _collect_body runs - on the Python attribute,
        so no Tcl variable is written and no trace fires - and the keys that follow that number are the invalid
        ones (their values in `body` are placeholders: never used)."""
        try:
            return self._collect(), frozenset()
        except _BAD_NUMBER:
            pass
        except Exception:  # noqa: BLE001 - a broken read must never break the screen
            return None, frozenset()
        bad = [(owner, name) for owner, name, v in self._vars if not _readable(v)]
        if not bad:
            return None, frozenset()
        saved = [(owner, name, getattr(owner, name)) for owner, name in bad]
        bodies = []
        try:
            for probe in _PROBES:
                for owner, name, _v in saved:
                    setattr(owner, name, _Fixed(probe))
                try:
                    bodies.append(self._collect())
                except Exception:  # noqa: BLE001 - this placeholder does not fit a key: try the next
                    continue
                if len(bodies) >= 3:
                    break
        finally:
            for owner, name, v in saved:
                setattr(owner, name, v)
        if len(bodies) < 2:
            return None, frozenset()
        keys = set().union(*bodies)
        invalid = frozenset(k for k in keys if len({repr(b.get(k, _MISSING)) for b in bodies}) > 1)
        if not invalid:
            return None, frozenset()
        return bodies[0], invalid

    def _diff(self, body, invalid):
        if self.clean is None or body is None:
            return frozenset()
        keys = set(self.clean) | set(body)
        return frozenset(k for k in keys
                         if k in invalid or self.clean.get(k, _MISSING) != body.get(k, _MISSING))

    def dirty_keys(self):
        body, invalid = self.current()
        return sorted(self._diff(body, invalid))

    def user_values(self):
        """{key: value} of every dirty key that can be read ({} without a snapshot or when nothing differs)."""
        if self.clean is None:
            return {}
        body, invalid = self.current()
        if body is None:
            return {}
        return {k: body[k] for k in sorted(self._diff(body, invalid)) if k in body and k not in invalid}

    def count(self):
        return len(self._dirty)

    # ------------------------------------------------------------------ state changes
    def recompute(self):
        """Compare now; tell on_change when the count, the kept flag or the invalid flag moved."""
        self.recomputes += 1
        body, invalid = self.current()
        if body is None and self.clean is not None:
            return
        dirty = self._diff(body, invalid)
        kept = self.kept and bool(dirty)
        changed = (dirty, invalid, kept) != (self._dirty, self.invalid, self.kept)
        self._dirty, self.invalid, self.kept = dirty, (invalid if self.clean is not None else frozenset()), kept
        if changed:
            self._notify()

    def _notify(self):
        if self.on_change is not None:
            try:
                self.on_change(self)
            except tk.TclError:
                pass

    def mark_clean(self, base=None, take=()):
        """Snapshot now. base= (a successful save): the body that was SENT is the clean state, except the keys in
        `take`, whose widgets the save's answer has just rewritten (clamped restart keys, the GPU section): those are
        read from the screen. So an edit typed while the save was in flight stays UNSAVED instead of being taken as
        saved."""
        body, invalid = self.current()
        if body is None:
            return
        if isinstance(base, dict):
            clean = dict(base)
            for k in take:
                if k in body and k not in invalid:
                    clean[k] = body[k]
            self.clean = clean
        else:
            self.clean = {k: v for k, v in body.items() if k not in invalid}
        self.kept = False
        self._dirty = frozenset()
        self.invalid = frozenset()
        self.recompute()
        self._notify()

    def mark_kept(self):
        """The edits on screen survived a reload (the _load hunk): flag them KEPT while they stay dirty."""
        self.recompute()
        self.kept = bool(self._dirty)
        self._notify()

    def discard(self):
        self.clean = None
        self.kept = False
        self._dirty = frozenset()
        self.invalid = frozenset()
        self._notify()

    def rebase(self, fresh, keys, before=None):
        """A model switch resynced the restart widgets from `fresh` (_sync_restart_widgets): for each of `keys`,
        the on-screen value is the new clean one when the sync moved it (it differs from `before`, the body read
        just before the sync) or when it equals fresh's value. A key the sync did not touch and that the user had
        changed (a GPU placement) stays dirty. screen._settings takes fresh's values for those keys."""
        if not isinstance(fresh, dict):
            return
        settings = getattr(self.screen, "_settings", None)
        if isinstance(settings, dict):
            settings.update({k: fresh[k] for k in keys if k in fresh})
        if self.clean is not None:
            body, invalid = self.current()
            if body is not None:
                for k in keys:
                    if k not in body or k in invalid or k not in self.clean:
                        continue
                    moved = before is not None and before.get(k, _MISSING) != body[k]
                    same = k in fresh and fresh[k] == body[k] and type(fresh[k]) is type(body[k])
                    if moved or same:
                        self.clean[k] = body[k]
        self.recompute()

    @contextlib.contextmanager
    def held(self):
        self._hold += 1
        try:
            yield self
        finally:
            self._hold -= 1
            if not self._hold:
                self.recompute()

    @contextlib.contextmanager
    def quiet(self):
        pre, _pre_invalid = self.current()
        was_dirty = set(self._dirty)
        self._hold += 1
        try:
            yield self
        finally:
            self._hold -= 1
            if self.clean is not None and pre is not None:
                body, invalid = self.current()
                if body is not None:
                    for k, v in body.items():
                        if k in invalid or k in was_dirty:
                            continue
                        if pre.get(k, _MISSING) != v and self.clean.get(k, _MISSING) != v:
                            self.clean[k] = v           # changed by the program, not by an edit
            if not self._hold:
                self.recompute()


class DirtyStamp:
    """The footer stamp of a SettingsFooter (kit/settings.py): show(n, kept, invalid). `rest` is the pane's timing
    stamp; REVERT (a kit TextAction, value ink, its own focus) runs on_revert and is shown only with KEPT. A footer
    without a stamp row (the kit-less stand-in) gets the stamp packed into the footer itself while dirty."""

    def __init__(self, footer, theme, rest=REST, on_revert=None):
        self.footer = footer
        self.rest = rest
        self._t = tokens(theme)
        self._row = getattr(footer, "stamp_row", None)
        self.label = footer.stamp_label
        host = self._row if self._row is not None else footer
        try:
            ground = str(host.cget("bg"))
        except tk.TclError:
            ground = self._t["bg"]
        self._on_revert = on_revert
        # B8 FIX2 (ADV2-04): REVERT acts only while it is on screen - a transient status hides the stamp row, REVERT
        # included, but the keyboard focus can stay on it
        self.revert = TextAction(host, theme, text="REVERT", command=self._revert_if_shown, ground=ground)
        self.text = None
        self.show(0)

    def _revert_if_shown(self):
        try:
            shown = bool(self.revert.winfo_ismapped())
        except tk.TclError:
            shown = False
        if shown and self._on_revert is not None:
            return self._on_revert()
        return None

    def show(self, n, kept=False, invalid=False):
        text = stamp_text(n, kept, invalid, self.rest)
        want_revert = n > 0 and kept
        ink = self._t["value"] if n <= 0 else self._t["bright"]
        try:
            if text != self.text:
                if hasattr(self.footer, "set_stamp"):
                    self.footer.set_stamp(text)
                else:
                    self.label.configure(text=text)
                self.label.configure(fg=ink)
                self.text = text
            if want_revert and not self.revert.winfo_manager():
                if self._row is not None:
                    self.revert.pack(side="right", before=self.label, padx=(12, 0))
                else:
                    self.revert.pack(side="right", padx=(0, 8))
            elif not want_revert and self.revert.winfo_manager():
                try:      # B8 FIX1 (ADV1-05): the focus must not stay on a word that is about to be hidden
                    has_focus = self.revert.focus_get() is self.revert
                except KeyError:      # tkinter cannot name a focus widget it did not make (a popdown)
                    has_focus = False
                if has_focus:
                    target = getattr(self.footer, "save_btn", None)
                    (target if target is not None else self.footer).focus_set()
                self.revert.pack_forget()
            if self._row is None:             # the kit-less footer: the stamp shows only while dirty
                if n > 0 and not self.label.winfo_manager():
                    self.label.configure(font=C9)
                    self.label.pack(side="right", padx=(0, 8))
                elif n <= 0 and self.label.winfo_manager():
                    self.label.pack_forget()
        except tk.TclError:
            pass

    def revert_shown(self):
        try:
            return self.revert.winfo_manager() == "pack"
        except tk.TclError:
            return False
