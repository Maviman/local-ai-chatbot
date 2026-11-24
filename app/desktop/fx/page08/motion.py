"""fx/page08/motion.py - Settings 08 DISPLAY MOTION (B10; BUILD_PLAN §4 B10 "Settings 08, first part", GAP_FX 4.1,
spec ST-04; approved r2_settings_sound): the control that ships with the motion (X-19).

    DISPLAY MOTION
    Motion            OFF  REDUCED  FULL   following Windows animation setting
                                           following default (Windows setting unreadable)
                                           set here  FOLLOW WINDOWS
    FULL: titles and new values type on in under 0.15 s and never hold up typing. REDUCED: nothing moves - colour
    cues only. OFF: completely static.

* The words show the EFFECTIVE level (prefs.effective_motion(): fx.motion when set here, else Windows "Show
  animations", else full). A pick writes prefs.set("fx.motion", level) - one change, one file write; re-picking the
  level already shown writes nothing (ST-04 "0 on re-select"), so a click never silently pins the Windows level.
  FOLLOW WINDOWS (only while set here) writes prefs.set("fx.motion", None): the key leaves the file.
* Every change applies at once (the clock reads the level when each effect starts). The row follows the store
  (prefs.subscribe("fx.motion"), owner = the row): RESTORE DEFAULTS, a backup restore, the Windows setting
  re-read on each show of the page (prefs.refresh_windows_motion()).
* The note sits right of the words while it fits on that line, else it moves under them and wraps (the 820 px
  floor). Re-laid out on the row's <Configure> (no timer), decided from the pane's content width
  (page.content_width(), set by the window), never from the row's own width, which would feed back into itself.
* Read-only page (kill switch): the same row, disabled, showing the stored setting; no FOLLOW WINDOWS.
* RESTORE DEFAULTS resets this row only: {"fx.motion": None} (follow Windows, D-4).
"""

import tkinter as tk

from ..sound_motion_page import Section, register_section

LEAF = "fx.motion"
LEVELS = (("off", "OFF"), ("reduced", "REDUCED"), ("full", "FULL"))
TITLE = "Display motion"
LABEL = "Motion"
CAPTION = ("FULL: titles and new values type on in under 0.15 s and never hold up typing. "
           "REDUCED: nothing moves - colour cues only. OFF: completely static.")
NOTES = {
    "windows": "following Windows animation setting",
    "default": "following default (Windows setting unreadable)",
    "explicit": "set here",
}
FOLLOW = "FOLLOW WINDOWS"
NOTE_GAP = 12               # the words -> the note (approved: the note at window x 704)
OPT_COL = 230               # the words start 230 px into the content (settings_screen.OPT_COL)


class MotionSection(Section):
    noun = "motion"

    def __init__(self, page, parent):
        from ...kit import C10, px
        from ...kit.widgets import SEG_FONT, SEG_PADX, Segmented, TextAction
        self.page = page
        t = page.t
        self._px = lambda n: px(parent, n)
        page.title(parent, TITLE)
        self.row = row = page.option_row(parent)
        # a label beside disabled words reads value, as Settings' own (B8 _sync_thinking_controls)
        self.label = page.label(row, LABEL, fg=t["bright"] if page.live else t["value"])
        self.label.grid(row=0, column=0, sticky="w")
        self.var = page.string_var(self._effective())
        self.seg = Segmented(row, page.theme, options=LEVELS, variable=self.var, command=self._on_pick,
                             ground=t["bg"], state="normal" if page.live else "disabled")
        self.seg.grid(row=0, column=1, sticky="w")
        # the words' width without waiting for a geometry pass: a Segmented cell is its word + 2 x SEG_PADX
        self._seg_w = sum(int(row.tk.call("font", "measure", SEG_FONT, lab)) + 2 * SEG_PADX for _v, lab in LEVELS)
        self.note_box = tk.Frame(row, bg=t["bg"])
        self.note = tk.Label(self.note_box, text="", font=C10, bg=t["bg"], fg=t["value"], bd=0, padx=0, pady=0,
                             anchor="w", justify="left")
        self.note.pack(side="left")
        self.follow = TextAction(self.note_box, page.theme, text=FOLLOW, command=self._on_follow, ground=t["bg"])
        self._below = None
        self.caption = page.caption(parent, CAPTION)
        row.bind("<Configure>", self._layout, add="+")
        page.subscribe(LEAF, self._on_pref, owner=row)
        self.refresh()

    # ---------------------------------------------------------------- reading
    def _view(self):
        return self.page.view

    def _effective(self):
        view = self._view()
        try:
            v = view.effective_motion()
        except Exception:  # noqa: BLE001 - no store at all (the shadow could not be made): the app's default
            v = "full"
        return v if v in ("off", "reduced", "full") else "full"

    def _source(self):
        try:
            s = self._view().motion_source()
        except Exception:  # noqa: BLE001
            s = "default"
        return s if s in NOTES else "default"

    # ---------------------------------------------------------------- writing
    def _on_pick(self):
        page = self.page
        if not page.live or self.seg is None:
            self.refresh()
            return
        value = self.var.get()
        try:
            stored = page.prefs.get(LEAF)
        except Exception:  # noqa: BLE001
            stored = None
        if value == stored or (stored is None and value == self._effective()):
            self.refresh()          # re-picking what is shown: nothing to write (and never pin the Windows level)
            return
        page.set_pref(LEAF, value)
        self.refresh()

    def _on_follow(self):
        self.page.set_pref(LEAF, None)
        self.refresh()

    def _on_pref(self, _path, _value):
        self.refresh()

    # ---------------------------------------------------------------- Section hooks
    def defaults(self):
        return {LEAF: self.page.default(LEAF)}

    def refresh(self):
        if self.seg is None:
            return
        try:
            eff = self._effective()
            if self.var.get() != eff:
                self.var.set(eff)               # the Segmented follows its variable (repaint on write)
            src = self._source()
            if self.note.cget("text") != NOTES[src]:
                self.note.configure(text=NOTES[src])
            want_follow = self.page.live and src == "explicit"
            if want_follow and not self.follow.winfo_manager():
                self.follow.pack(side="left", padx=(self._px(NOTE_GAP), 0))
            elif not want_follow and self.follow.winfo_manager():
                try:
                    focused = self.follow.focus_get() is self.follow
                except KeyError:        # tkinter cannot name a focus widget it did not make (a popdown)
                    focused = False
                if focused:
                    self.seg.focus_set()        # the focus never stays on a word that is about to go
                self.follow.pack_forget()
            self._layout()
        except tk.TclError:
            pass

    def on_show(self):
        page = self.page
        if page.live:
            try:
                page.prefs.refresh_windows_motion()     # GAP_FX 1.1: the page re-reads Windows on each show
            except Exception:  # noqa: BLE001
                pass
        self.refresh()

    def release(self):
        for w in (self.seg, self.follow):
            if w is not None:
                try:
                    w.configure(command=None)
                except tk.TclError:
                    pass
        self.seg = self.follow = self.var = None
        self.page = _Released

    # ---------------------------------------------------------------- the note's place
    def _layout(self, _e=None):
        """The note right of the words when it fits on that line, else under them, wrapped to the content width.
        The width is the PANE's (page.content_width(), set by the window), never this row's own: in a pane that is
        not shown the row's width follows what it holds, so deciding from it fed back into itself and the
        <Configure> events never stopped (found in B10: the kit-less Settings of test_settings_b8 N1 spun)."""
        if self.seg is None:
            return
        try:
            width = int(self.page.content_width())
            words_end = max(self._px(OPT_COL), int(self.label.winfo_reqwidth())) + self._seg_w
            room = width - words_end - self._px(NOTE_GAP)
            need = int(self.note.tk.call("font", "measure", self.note.cget("font"), self.note.cget("text")))
            if self.follow.winfo_manager():
                need += self._px(NOTE_GAP) + int(self.follow.winfo_reqwidth())
            below = need > room
            if below != self._below:
                self._below = below
                self.note_box.grid_forget()
                if below:
                    self.note_box.grid(row=1, column=1, columnspan=2, sticky="w", pady=(self._px(4), 0))
                else:
                    self.note_box.grid(row=0, column=2, sticky="w", padx=(self._px(NOTE_GAP), 0))
            wrap = 0                                    # one line beside the words
            if below:
                beside = int(self.follow.winfo_reqwidth()) + self._px(NOTE_GAP) if self.follow.winfo_manager() else 0
                wrap = max(self._px(60), width - self._px(OPT_COL) - beside)
            if int(float(str(self.note.cget("wraplength")))) != wrap:
                self.note.configure(wraplength=wrap)
        except (tk.TclError, ValueError):
            pass


class _ReleasedPage(object):
    """What a released section's page reference becomes: read-only, no store (a late callback does nothing)."""
    live = False
    prefs = None
    view = None

    @staticmethod
    def default(_leaf):
        return None

    @staticmethod
    def set_pref(_leaf, _value):
        return False


_Released = _ReleasedPage()

register_section("motion", 300, MotionSection)
