"""kit/settings.py - the Settings pieces of the approved look (B8: BUILD_PLAN §4 B8; approved r2
mock_settings_general / _advanced / _data and their cs_local.py helpers).

    CategoryRow(tk.Frame)       one unboxed category row: the index, the plate (selected = the inverse
                                plate, hover = 1 px bright, keyboard focus = 2 px bright + an inverse
                                index chip) and the category's caption under it, always shown (OI-10)
    SettingsFooter(tk.Frame)    SAVE (primary) with the pane's timing stamp left of it; the transient
                                status (FooterStatus) takes the stamp's place while it has text
    FooterStatus(StatusLine)    that status line: one line, elided in the middle with the whole text in
                                its hover tip; cget("text") is the text set (the Settings status_label)
    ActionButton(KitButton)     upper-case on screen, but cget("text") is the text it was given, so code
                                that writes and reads back its own label ("Reset app data...") still reads it
    LampCheck(tk.Checkbutton)   the lamp check whose DISABLED look is the dim lamp and dim text: Tk would
                                stipple a disabled image all over, so the Tk state stays normal and input is
                                swallowed by a bindtag; cget("state") reads "disabled"; the text never changes
    KitScale(ttk.Scale)         a ttk.Scale whose state also inks its row label and readout (disabled:
                                label value, readout dim); the slider look is ttk_controls' (B1)
    InkSegmented(Segmented)     option words each in its own ink (the phosphor picker: each preset's bright)
    ResizeGrip(tk.Canvas)       the drag grip on a well's corner: three 1 px steps
    RecordRow(tk.Frame)         RUNNING record row: KEY C9 value, VALUE C9B value right-aligned at value_x
    WrapGroup                   captions that wrap to the pane width (never cut, D-27)
    meter_style(widget, theme)  the thin (6 px) flat read-only meter style (default-theme trough + pbar)

House rules (kit/__init__): no after(), no I/O, Tk thread only. Classes that override configure / cget
re-bind config / __getitem__ (the Override rule). Every image comes from a per-root cache (lamp_images)
or is drawn on a Canvas; no PhotoImage is created per widget here.
"""

import tkinter as tk
from tkinter import ttk

from . import C9, C9B, C10, C11, px, tokens
from .widgets import KitButton, Segmented, StatusLine, _TextVarLink, _split_cnf, display_text, lamp_images

__all__ = ["CategoryRow", "SettingsFooter", "FooterStatus", "ActionButton", "LampCheck", "KitScale",
           "InkSegmented", "ResizeGrip", "RecordRow", "WrapGroup", "meter_style", "METER_STYLE",
           "CAT_INDEX_W", "CAT_PLATE_W", "CAT_H", "CAT_GAP", "CAT_CAPTION_X", "CAT_COL_W", "CAT_ROW_GAP",
           "SAVE_W", "SAVE_H", "STAMP_GAP"]

# ---------------------------------------------------------------- approved geometry (100 %, window px)
# cs_local.py: rows from y 70 at a 36 px pitch, plate (86, y)-(230, y + 30), index C9 value anchored e at
# x 80, title C10 at x 92, the help caption C9 value at x 76 under its row (18 px). The screen's x 0 is the
# window's x 42 (the rail), so the column below is screen-local.
CAT_INDEX_W = 42           # index column: the digits end 4 px inside it (window x 80)
CAT_GAP = 2                # index column -> plate (window x 86)
CAT_PLATE_W = 144          # plate 86..230
CAT_H = 30                 # plate height
CAT_TITLE_PAD = 4          # edge 1 + ring 1 + 4 = the title at plate x + 6 (window x 92)
CAT_CAPTION_X = 34         # window x 76
CAT_COL_W = 220            # the whole category column (window x 42..262): a caption wraps inside it
CAT_ROW_GAP = 6            # 36 px pitch - 30 px plate
SAVE_W, SAVE_H = 93, 29    # SAVE (992,738)-(1085,767)
STAMP_GAP = 16             # stamp right edge -> SAVE


# ====================================================================== CategoryRow

class CategoryRow(tk.Frame):
    """CategoryRow(master, theme, number, label, caption, command=None, selected=False, ground=None)

    The plate is focusable (takefocus=1): Return / KP_Enter / space and a click run `command`; a click
    also takes the focus. .plate / .title_label / .index_label / .caption_label are plain Tk widgets.
    set_selected(bool) restyles in place (no rebuild); .label / .caption / .selected read back."""

    def __init__(self, master, theme, number, label, caption, command=None, selected=False, ground=None, **kw):
        t = self._t = tokens(theme)
        g = self._g = ground or t["bg"]
        kw.setdefault("bg", g)
        tk.Frame.__init__(self, master, **kw)
        self.label = label
        self.caption = caption
        self.command = command
        self.selected = bool(selected)
        self._hover = False
        self._focus = False
        line = tk.Frame(self, bg=g)
        line.pack(fill="x")
        idx_box = tk.Frame(line, bg=g, width=px(self, CAT_INDEX_W), height=px(self, CAT_H))
        idx_box.pack(side="left")
        idx_box.pack_propagate(False)
        self.index_label = tk.Label(idx_box, text="%02d" % number, font=C9, bg=g, fg=t["value"], bd=0,
                                    padx=4, pady=0)
        self.index_label.pack(side="right")
        self.plate = tk.Frame(line, bg=g, width=px(self, CAT_PLATE_W), height=px(self, CAT_H), takefocus=1,
                              cursor="hand2", bd=0, highlightthickness=0)
        self.plate.pack(side="left", padx=(px(self, CAT_GAP), 0))
        self.plate.pack_propagate(False)
        self._ring = tk.Frame(self.plate, bg=g, bd=0, highlightthickness=0)
        self._ring.pack(fill="both", expand=True, padx=1, pady=1)
        self._body = tk.Frame(self._ring, bg=g, bd=0, highlightthickness=0)
        self._body.pack(fill="both", expand=True, padx=1, pady=1)
        self.title_label = tk.Label(self._body, text=label, font=C10, bg=g, fg=t["value"], anchor="w", bd=0,
                                    padx=CAT_TITLE_PAD, pady=0, cursor="hand2")
        self.title_label.pack(fill="both", expand=True)
        # the caption is the category's help line, always shown (OI-10), full text: it wraps inside the
        # column rather than ever being cut (D-27)
        self.caption_label = tk.Label(self, text=caption, font=C9, bg=g, fg=t["value"], anchor="w", justify="left",
                                      bd=0, padx=0, pady=0,
                                      wraplength=px(self, CAT_COL_W - CAT_CAPTION_X))
        self.caption_label.pack(anchor="w", padx=(px(self, CAT_CAPTION_X), 0), pady=(1, 3))
        for w in (self.plate, self._ring, self._body, self.title_label, self.index_label, self.caption_label):
            w.bind("<Button-1>", self._on_click, add="+")
            w.bind("<Enter>", self._on_enter, add="+")
            w.bind("<Leave>", self._on_leave, add="+")
        for seq in ("<Return>", "<KP_Enter>", "<space>"):
            self.plate.bind(seq, self._on_key, add="+")
        self.plate.bind("<FocusIn>", lambda _e: self._set(focus=True), add="+")
        self.plate.bind("<FocusOut>", lambda _e: self._set(focus=False), add="+")
        self._paint()

    # ---------------------------------------------------------------- state
    def set_selected(self, on):
        on = bool(on)
        if on != self.selected:
            self.selected = on
            self._paint()

    def _set(self, hover=None, focus=None):
        if hover is not None:
            self._hover = bool(hover)
        if focus is not None:
            self._focus = bool(focus)
        self._paint()

    def state(self):
        """'selected' | 'focus' | 'hover' | 'rest' (the plate's look; a selected row with the keyboard focus
        also shows the inverse index chip)."""
        if self.selected:
            return "selected"
        if self._focus:
            return "focus"
        if self._hover:
            return "hover"
        return "rest"

    def _paint(self):
        t, g = self._t, self._g
        st = self.state()
        edge = ring = body = g
        ink = t["value"]
        if st == "selected":
            edge = ring = body = t["bright"]
            ink = t["bg"]
        elif st == "focus":
            edge = ring = t["bright"]
        elif st == "hover":
            edge = t["bright"]
        try:
            self.plate.configure(bg=edge)
            self._ring.configure(bg=ring)
            self._body.configure(bg=body)
            self.title_label.configure(bg=body, fg=ink)
            if self._focus:
                self.index_label.configure(bg=t["bright"], fg=t["bg"])
            else:
                self.index_label.configure(bg=g, fg=t["value"])
        except tk.TclError:
            pass

    # ---------------------------------------------------------------- input
    def invoke(self):
        if self.command is not None:
            return self.command()
        return None

    def _on_click(self, _e=None):
        try:
            self.plate.focus_set()
        except tk.TclError:
            pass
        self.invoke()

    def _on_key(self, _e=None):
        self.invoke()
        return "break"

    def _on_enter(self, _e=None):
        if not self._hover:
            self._set(hover=True)

    def _on_leave(self, e=None):
        # Leave also fires when the pointer moves onto a child; the row only loses its hover when the pointer
        # is outside the whole row (geometry, not the event's detail: tkinter's Event has none)
        try:
            x, y = (int(e.x_root), int(e.y_root)) if e is not None else self.winfo_pointerxy()
            rx, ry = self.winfo_rootx(), self.winfo_rooty()
            inside = rx <= x < rx + self.winfo_width() and ry <= y < ry + self.winfo_height()
        except (tk.TclError, TypeError, ValueError, AttributeError):
            inside = False
        if not inside and self._hover:
            self._set(hover=False)


# ====================================================================== footer

class FooterStatus(StatusLine):
    """The Settings footer's transient status (settings_screen.status_label): a kit StatusLine, so a long
    message is one line elided in the middle with the whole text in its hover tip, and cget("text") is the
    text set. Every text write (configure / config / w["text"] = ..) tells `on_change`, which swaps the stamp
    out while there is a message and back when it is cleared."""

    def __init__(self, master, theme=None, on_change=None, **kw):
        self._on_change = None
        StatusLine.__init__(self, master, theme, **kw)
        self._on_change = on_change

    def configure(self, cnf=None, **kw):
        result = StatusLine.configure(self, cnf, **kw)
        if self._on_change is not None and not isinstance(cnf, str):
            keys = set(kw) | (set(cnf) if isinstance(cnf, dict) else set())
            if keys & {"text", "textvariable"}:
                self._on_change()
        return result

    config = configure
    __getitem__ = StatusLine.cget


class SettingsFooter(tk.Frame):
    """SettingsFooter(master, theme, command=None, stamp="Changes apply on save", ground=None)

    No bar: SAVE (a primary KitButton, SAVE_W x SAVE_H) on the right and the pane's timing stamp (C9 value)
    STAMP_GAP px left of it. .status (FooterStatus) shows a transient message in the stamp's place; the stamp
    returns when the message is cleared. .stamp_row holds .stamp_label (set_stamp() rewrites it), so a later
    phase can add a word to the stamp (B8d: UNSAVED ... + REVERT) without touching the status contract."""

    def __init__(self, master, theme, command=None, stamp="Changes apply on save", save_text="Save", ground=None,
                 **kw):
        t = self._t = tokens(theme)
        g = ground or t["bg"]
        kw.setdefault("bg", g)
        tk.Frame.__init__(self, master, **kw)
        self.save_btn = KitButton(self, theme, text=save_text, kind="primary", upper=True, command=command,
                                  ground=g, width=px(self, SAVE_W), height=px(self, SAVE_H))
        self.save_btn.pack(side="right")
        self.stamp_row = tk.Frame(self, bg=g)
        self.stamp_label = tk.Label(self.stamp_row, text=stamp, font=C9, bg=g, fg=t["value"], bd=0, padx=0, pady=0)
        self.stamp_label.pack(side="right")
        self.status = FooterStatus(self, theme, on_change=self._sync, font=C9, bg=g, fg=t["value"], anchor="e",
                                   bd=0, padx=0, pady=0)
        self._sync()

    def set_stamp(self, text):
        self.stamp_label.configure(text=text)

    def stamp_shown(self):
        try:
            return self.stamp_row.winfo_manager() == "pack"
        except tk.TclError:
            return False

    def set_save_shown(self, shown):
        """B10 (Settings 08 applies every change at once): hide or show SAVE. Without it the stamp (or the status)
        ends at the footer's right edge, where SAVE's right edge was (approved r2_settings_sound: x 1084), and a 1 px
        spacer keeps the footer SAVE's height, so the stamp stays on the same line as on the other pages."""
        try:
            spacer = getattr(self, "_save_spacer", None)
            if spacer is None:
                spacer = self._save_spacer = tk.Frame(self, width=1, height=px(self, SAVE_H), bd=0,
                                                      highlightthickness=0, bg=self.cget("bg"))
            first = next(iter(s for s in self.pack_slaves() if s not in (self.save_btn, spacer)), None)
            keep, drop = (self.save_btn, spacer) if shown else (spacer, self.save_btn)
            if drop.winfo_manager():
                drop.pack_forget()
            if not keep.winfo_manager():
                if first is not None:
                    keep.pack(side="right", before=first)
                else:
                    keep.pack(side="right")
        except tk.TclError:
            pass
        self._sync()

    def _sync(self):
        try:
            busy = bool(str(self.status.cget("text") or ""))
            gap = (0, px(self, STAMP_GAP) if self.save_btn.winfo_manager() else 0)   # no gap for a hidden SAVE
            if busy:
                if self.stamp_row.winfo_manager():
                    self.stamp_row.pack_forget()
                if not self.status.winfo_manager():
                    self.status.pack(side="right", fill="x", expand=True, padx=gap)
                elif getattr(self, "_gap", gap) != gap:
                    self.status.pack_configure(padx=gap)
            else:
                if self.status.winfo_manager():
                    self.status.pack_forget()
                if not self.stamp_row.winfo_manager():
                    self.stamp_row.pack(side="right", padx=gap)
                elif getattr(self, "_gap", gap) != gap:
                    self.stamp_row.pack_configure(padx=gap)
            self._gap = gap
        except tk.TclError:
            pass


# ====================================================================== ActionButton

class ActionButton(KitButton):
    """A KitButton that shows its label upper-case (the approved button look) while cget("text") returns the
    text as given ("Reset app data..." / "Resetting..."): code that sets a label and reads it back, and the
    tests that pin those strings, keep working. Upper-case is display only, the content is the text given."""

    def __init__(self, master, theme, text="", kind="secondary", **kw):
        kw.setdefault("upper", True)
        KitButton.__init__(self, master, theme, text=text, kind=kind, **kw)

    def cget(self, key):
        if key == "text" and isinstance(self._raw_text, str):
            return display_text(self._raw_text, False, self._raw)
        return KitButton.cget(self, key)

    config = KitButton.configure
    __getitem__ = cget


# ====================================================================== LampCheck

_LAMP_BLOCK_TAG = "KitLampDisabled"
_LAMP_BLOCKED = ("<Button-1>", "<ButtonRelease-1>", "<B1-Leave>", "<B1-Enter>", "<Enter>", "<Leave>", "<space>",
                 "<Return>", "<KP_Enter>", "<Key-space>")


def _ensure_lamp_block(widget):
    """The class bindings of the 'disabled lamp' bindtag, once per interpreter: every input a Checkbutton's own
    class bindings react to is swallowed (break) while the tag is on the widget."""
    root = widget._root()
    if getattr(root, "_kit_lamp_block_ready", False):
        return
    for seq in _LAMP_BLOCKED:
        try:
            root.bind_class(_LAMP_BLOCK_TAG, seq, "break")
        except tk.TclError:
            pass
    try:
        root._kit_lamp_block_ready = True
    except AttributeError:
        pass


class LampCheck(tk.Checkbutton):
    """LampCheck(master, theme, text="", variable=None, command=None, ground=None, font=C11, **checkbutton_kw)

    widgets.lamp_check's tk.Checkbutton (indicatoron=0, the cached lamp images, compound="left", text exactly
    as given: a lamp never rewrites its text or its case). Disabled: Tk would stipple a disabled IMAGE all
    over, so configure(state="disabled") keeps the Tk state normal, shows the cached DIM lamp images and dim
    text, takes takefocus 0, puts a bindtag that swallows clicks / space / Enter / hover in front, and
    invoke() does nothing; cget("state") / w["state"] read "disabled". state="normal" undoes all of it."""

    def __init__(self, master, theme, text="", variable=None, command=None, ground=None, font=C11, **kw):
        t = self._t = tokens(theme)
        self._ground = ground or t["bg"]
        self._imgs = lamp_images(master, theme)
        self._dimgs = lamp_images(master, theme, disabled=True)
        self._dis = False
        self._saved_takefocus = None
        self._ink = kw.pop("fg", kw.pop("foreground", t["bright"]))
        state = kw.pop("state", "normal")
        g = self._ground
        opts = dict(text=text, image=self._imgs[0], selectimage=self._imgs[1], compound="left", indicatoron=0,
                    anchor="w", bg=g, fg=self._ink, activebackground=g, activeforeground=self._ink, selectcolor=g,
                    disabledforeground=t["dim"], relief="flat", offrelief="flat", overrelief="flat", bd=0,
                    highlightthickness=1, highlightbackground=g, highlightcolor=t["bright"], font=font, padx=0,
                    pady=2, cursor="hand2", justify="left")
        if variable is not None:
            opts["variable"] = variable
        if command is not None:
            opts["command"] = command
        opts.update(kw)
        tk.Checkbutton.__init__(self, master, **opts)
        self._kit_lamp_images = self._imgs          # the images live with the widget (and in the root cache)
        if state is not None and str(state).startswith("dis"):
            self._set_disabled(True)

    @staticmethod
    def _norm_state(value):
        s = str(value)
        for full in ("normal", "active", "disabled"):
            if s and full.startswith(s):
                return full
        raise tk.TclError('bad state "%s": must be active, disabled, or normal' % s)

    def _set_disabled(self, on):
        on = bool(on)
        if on == self._dis:
            return
        self._dis = on
        t, g = self._t, self._ground
        tags = list(self.bindtags())
        if on:
            _ensure_lamp_block(self)
            self._saved_takefocus = tk.Checkbutton.cget(self, "takefocus")
            if _LAMP_BLOCK_TAG not in tags:
                # B10-H17: the clock's input recorder (fx/clock.py, prepended on <Map>) stays FIRST, so a click /
                # Space / Return on a dimmed lamp still finishes a running type-on; the block tag goes right after it
                at = 1 if tags and tags[0] == "FxClockInputRec" else 0
                self.bindtags(tuple(tags[:at] + [_LAMP_BLOCK_TAG] + tags[at:]))
            tk.Checkbutton.configure(self, image=self._dimgs[0], selectimage=self._dimgs[1], fg=t["dim"],
                                     activeforeground=t["dim"], takefocus=0, cursor="", state="normal")
        else:
            if _LAMP_BLOCK_TAG in tags:
                tags.remove(_LAMP_BLOCK_TAG)
                self.bindtags(tuple(tags))
            tf = self._saved_takefocus
            self._saved_takefocus = None
            tk.Checkbutton.configure(self, image=self._imgs[0], selectimage=self._imgs[1], fg=self._ink,
                                     activeforeground=self._ink, takefocus=tf if tf not in (None, "") else "",
                                     cursor="hand2")

    def configure(self, cnf=None, **kw):
        if isinstance(cnf, str):
            if cnf in ("state", "fg", "foreground") or (self._dis and cnf == "takefocus"):
                return (cnf, cnf, cnf.capitalize(), "", self.cget(cnf))
            return tk.Checkbutton.configure(self, cnf)
        kw = _split_cnf(cnf, kw)
        if not kw:
            return tk.Checkbutton.configure(self)
        new_state = kw.pop("state", None)
        if new_state is not None:
            new_state = self._norm_state(new_state)
        for name in ("fg", "foreground"):
            if name in kw:
                self._ink = kw.pop(name)
                if not self._dis:
                    kw["fg"] = self._ink
                    kw["activeforeground"] = self._ink
        if self._dis and "takefocus" in kw:
            self._saved_takefocus = kw.pop("takefocus")
        result = tk.Checkbutton.configure(self, **kw) if kw else None
        if new_state is not None:
            self._set_disabled(new_state == "disabled")
            if new_state == "active":
                tk.Checkbutton.configure(self, state="active")
        return result

    def cget(self, key):
        if key == "state" and self._dis:
            return "disabled"
        if key in ("fg", "foreground"):
            return self._ink
        if key == "takefocus" and self._dis:
            return self._saved_takefocus if self._saved_takefocus is not None else ""
        return tk.Checkbutton.cget(self, key)

    config = configure
    __getitem__ = cget

    def invoke(self):
        if self._dis:
            return ""
        return tk.Checkbutton.invoke(self)


# ====================================================================== KitScale

class KitScale(ttk.Scale):
    """ttk.Scale (the approved slider look comes from ttk_controls' Horizontal.TScale) that inks its row:
    attach(label, readout) once; then every configure(state=...) - and construction - sets the label to
    bright (value when disabled) and the readout to bright (dim when disabled), as r2kit.slider_row draws it.
    Everything else is a stock ttk.Scale (variable=, command=, from_/to, .get/.set)."""

    def __init__(self, master, theme=None, step=None, **kw):
        self._t = tokens(theme)
        self._row_label = None
        self._readout = None
        self._step = step
        ttk.Scale.__init__(self, master, **kw)
        # B8-H4: ttk::scale steps one UNIT of the range per arrow key (Temperature 0.7 -> 1.5, Reply limit +1 token);
        # here a key moves one step (step=, else a fraction of the range), and "break" keeps the class binding out
        for seq, d in (("<Left>", -1), ("<Down>", -1), ("<Right>", 1), ("<Up>", 1)):
            self.bind(seq, lambda _e, d=d: self._key_step(d), add="+")

    def key_step(self):
        """The size of one arrow-key step: step= when given; else 0.05 on a range up to 2, 1 up to 200, and a fiftieth
        of the range above that."""
        if self._step:
            return float(self._step)
        try:
            rng = abs(float(ttk.Scale.cget(self, "to")) - float(ttk.Scale.cget(self, "from")))
        except (tk.TclError, ValueError):
            return 1.0
        return 0.05 if rng <= 2 else (1.0 if rng <= 200 else max(1.0, round(rng / 50)))

    def _key_step(self, direction):
        if self.disabled():
            return "break"
        try:
            lo, hi = float(ttk.Scale.cget(self, "from")), float(ttk.Scale.cget(self, "to"))
            step = self.key_step()
            cur = float(self.get())
            new = (round((cur - lo) / step) + direction) * step + lo
            self.set(round(min(hi, max(lo, new)), 6))
        except (tk.TclError, ValueError):
            pass
        return "break"

    def attach(self, label=None, readout=None):
        self._row_label = label
        self._readout = readout
        self._sync_inks()
        return self

    def configure(self, cnf=None, **kw):
        result = ttk.Scale.configure(self, cnf, **kw)
        if not isinstance(cnf, str):
            keys = set(kw) | (set(cnf) if isinstance(cnf, dict) else set())
            if "state" in keys:
                self._sync_inks()
        return result

    config = configure

    def disabled(self):
        try:
            return str(ttk.Scale.cget(self, "state")) == "disabled"
        except tk.TclError:
            return False

    def _sync_inks(self):
        t = self._t
        dis = self.disabled()
        for w, on, off in ((self._row_label, t["bright"], t["value"]), (self._readout, t["bright"], t["dim"])):
            if w is not None:
                try:
                    w.configure(fg=off if dis else on)
                except tk.TclError:
                    pass


# ====================================================================== InkSegmented

class InkSegmented(Segmented):
    """Segmented whose option words each carry their own ink (inks={value: colour}): the phosphor picker shows
    every preset's word in that preset's bright. Selected = the 2 px underbar, in the word's own ink. An option
    without an ink keeps Segmented's look; disabled dims every word as Segmented does."""

    def __init__(self, master, theme, options=(), inks=None, **kw):
        self._inks = dict(inks or {})
        Segmented.__init__(self, master, theme, options=options, **kw)

    def _repaint(self):
        Segmented._repaint(self)
        inks = getattr(self, "_inks", None)
        if not inks:
            return
        sel = self._selected_value()
        dis = self._state == "disabled"
        for (value, _label), (_cell, lab, bar) in zip(self._opts, self._cells):
            ink = inks.get(value)
            if not ink or dis:
                continue
            try:
                lab.configure(fg=ink)
                bar.configure(bg=ink if value == sel else self._ground)
            except tk.TclError:
                pass


# ====================================================================== meters

METER_STYLE = "KitMeter.Horizontal.TProgressbar"
METER_BAR = 6              # the approved meter: a 6 px flat bar, the fill edge to edge, no border
METER_ELEMENTS = ("KitMeter.trough", "KitMeter.pbar")


def meter_style(widget, theme=None, bar=METER_BAR, ink="value"):
    """The name of a thin read-only meter style (approved r2 Advanced: 'Live memory usage' is a short flat 6 px
    bar, the fill in `value` ink on `unlit`, because it is a meter, not a control). ink="bright" (B8 builder 2:
    the asset manager's download progress, 'bright on unlit') is a style of its own, KitMeterBright.*.

    It is NOT a derived clam style: clam's pbar element insets its fill by a 2 px smooth border and draws nothing
    at all below ~9 px (measured 2026-10-08: an arrowsize-4 clam bar showed the trough only, its fill invisible).
    So the style has its own layout of the DEFAULT theme's trough + pbar elements, copied into the current theme
    once per interpreter (KitMeter.trough / KitMeter.pbar): a flat trough with no border and a flat pbar whose
    `thickness` is the bar's height, filled edge to edge. Configured on every call (cheap, idempotent); a Tk
    refusal falls back to the stock style name."""
    t = tokens(theme)
    name_ = METER_STYLE if ink == "value" else "KitMeter%s.Horizontal.TProgressbar" % str(ink).capitalize()
    try:
        style = ttk.Style(widget)
        have = set(str(n) for n in style.element_names())
        for name, src in zip(METER_ELEMENTS, ("trough", "pbar")):
            if name not in have:
                style.element_create(name, "from", "default", src)
        style.layout(name_, [(METER_ELEMENTS[0], {"sticky": "nswe", "children": [
            (METER_ELEMENTS[1], {"side": "left", "sticky": "ns"})]})])
        style.configure(name_, troughcolor=t["unlit"], troughrelief="flat", borderwidth=0,
                        thickness=px(widget, bar), pbarrelief="flat", background=t.get(ink, t["value"]))
    except tk.TclError:
        return "Horizontal.TProgressbar"
    return name_


# ====================================================================== ResizeGrip

GRIP = 12                  # the grip's square, px


class ResizeGrip(tk.Canvas):
    """The drag-to-resize grip drawn as three 1 px steps (the approved grip is one 1 px mark, BUILD_PLAN §4 B8:
    "three 1 px steps"), dim on the well's panel, 12 x 12, hugging its bottom-right corner, cursor
    sb_v_double_arrow. The caller binds the drag."""

    def __init__(self, master, theme, ground=None, **kw):
        t = tokens(theme)
        g = ground or t["panel"]
        n = px(master, GRIP)
        kw.setdefault("cursor", "sb_v_double_arrow")
        tk.Canvas.__init__(self, master, width=n, height=n, bg=g, highlightthickness=0, bd=0, **kw)
        s = max(1, (n - 1) // 3)
        x, y = n - 1 - 3 * s, n - 1
        pts = [x, y]
        for _ in range(3):
            pts += [x + s, y, x + s, y - s]
            x, y = x + s, y - s
        self.create_line(*pts, fill=t["dim"], width=1)


# ====================================================================== RecordRow

class _ValueLabel(tk.Label):
    """RecordRow's value Label (B10-H2): its textvariable is traced here (kit.widgets._TextVarLink) instead of
    being handed to Tk, so every write of the variable comes through configure(text=): the value is drawn whole
    at once (cget at once), then fx.motion.print_value gets the previous text - a value replacing a placeholder
    (motion.PENDING_WORDS: "(unknown)", "", ...) prints into its slot (approved r2_motion_type_on section 2,
    the Settings RUNNING record); a value replacing a value, a placeholder replacing one, or no fx (NullFX):
    drawn plainly. The print-in frames are written through Tcl, so they never come back through here.
    cget("textvariable") answers the variable's name (the Override rule; test_settings_b8 A1)."""

    def __init__(self, master, textvariable=None, **kw):
        self._tv = _TextVarLink(self)
        tk.Label.__init__(self, master, **kw)
        if textvariable is not None:
            v = self._tv.attach(textvariable)
            if v is not None:
                tk.Label.configure(self, text=v)

    def configure(self, cnf=None, **kw):
        if isinstance(cnf, str):
            if cnf == "textvariable":
                return self._tv.describe()
            return tk.Label.configure(self, cnf)
        kw = _split_cnf(cnf, kw)
        if "textvariable" in kw:
            v = self._tv.attach(kw.pop("textvariable"))
            if v is not None and "text" not in kw:
                kw["text"] = v
        prev = str(tk.Label.cget(self, "text")) if "text" in kw else None
        result = tk.Label.configure(self, **kw) if kw else tk.Label.configure(self)
        if prev is not None and str(kw["text"]) != prev:
            self._print_in(prev)
        return result

    config = configure

    def cget(self, key):
        if key == "textvariable":
            return self._tv.name
        return tk.Label.cget(self, key)

    __getitem__ = cget

    def _print_in(self, prev):
        try:
            from .. import fx as _fx
            from ..fx.motion import is_pending
            text = str(tk.Label.cget(self, "text"))
            if is_pending(text):
                return                     # a placeholder replacing a placeholder: drawn plainly
            _fx.get(self).motion.print_value(self, text, was=prev)
        except Exception:  # noqa: BLE001 - motion never breaks a value write
            pass

    def destroy(self):
        self._tv.detach()
        tk.Label.destroy(self)


class RecordRow(tk.Frame):
    """RecordRow(master, theme, key, textvariable=None, text="", value_x=400, ground=None)

    One row of the RUNNING record (cs_local.record_row_quiet): KEY in C9 value at the left, the value in C9B
    value RIGHT-aligned at value_x px from the row's left edge, no dot leaders, a 20 px pitch (C9's 14 px line
    + 2 x ROW_PADY). A value wider than its column wraps (right-aligned), never cut. .key_label is a plain
    tk.Label; .value_label is a kit _ValueLabel (a tk.Label whose textvariable prints in, B10-H2)."""

    KEY_W = 120
    ROW_PADY = 3

    def __init__(self, master, theme, key, textvariable=None, text="", value_x=400, ground=None, **kw):
        t = tokens(theme)
        g = ground or t["bg"]
        kw.setdefault("bg", g)
        tk.Frame.__init__(self, master, **kw)
        kw_w = px(self, self.KEY_W)
        vw = max(40, px(self, value_x) - kw_w)
        pad = px(self, self.ROW_PADY)
        self.columnconfigure(0, minsize=kw_w)
        self.columnconfigure(1, minsize=vw)
        self.key_label = tk.Label(self, text=key, font=C9, bg=g, fg=t["value"], bd=0, padx=0, pady=pad, anchor="w")
        self.key_label.grid(row=0, column=0, sticky="nw")
        opts = dict(font=C9B, bg=g, fg=t["value"], bd=0, padx=0, pady=pad, anchor="e", justify="right",
                    wraplength=vw)
        if textvariable is not None:
            opts["textvariable"] = textvariable
        else:
            opts["text"] = text
        self.value_label = _ValueLabel(self, **opts)
        self.value_label.grid(row=0, column=1, sticky="ne")


# ====================================================================== WrapGroup

class WrapGroup:
    """Labels whose wraplength follows one width (the Settings pane's content width), so every caption wraps to
    the pane and is never cut at any window size (D-27 #1). add(label, indent=0) -> label; set_width(w)
    re-wraps each to w - indent (at least `minimum`); labels that died are dropped. No timer: the owner
    calls set_width from its <Configure>."""

    def __init__(self, width=770, minimum=120):
        self.width = int(width)
        self.minimum = int(minimum)
        self._labels = []

    def add(self, label, indent=0):
        self._labels.append((label, int(indent)))
        try:
            label.configure(wraplength=max(self.minimum, self.width - int(indent)))
        except tk.TclError:
            pass
        if len(self._labels) % 64 == 0:
            self.prune()      # labels of rows that are rebuilt (GPU rows) die between two width changes
        return label

    def prune(self):
        alive = []
        for label, indent in self._labels:
            try:
                if label.winfo_exists():
                    alive.append((label, indent))
            except tk.TclError:
                pass
        self._labels = alive

    def __len__(self):
        return len(self._labels)

    def set_width(self, width):
        width = int(width)
        if width == self.width:
            return False
        self.width = width
        alive = []
        for label, indent in self._labels:
            want = max(self.minimum, width - indent)
            try:
                if int(float(str(label.cget("wraplength")))) != want:
                    label.configure(wraplength=want)
                alive.append((label, indent))
            except (tk.TclError, ValueError):
                pass
        self._labels = alive
        return True
