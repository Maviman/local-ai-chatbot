"""kit/widgets.py - the static widgets of the approved look (BUILD_PLAN §1.3).

    KitButton(tk.Button)    primary | secondary | danger (= danger_fill) | danger_outline
    TextAction(tk.Label)    a secondary function drawn as a word (takefocus, Return / space)
    lamp_images(w, theme)   (off, on) PhotoImages for tk.Checkbutton(indicatoron=0, ...)
    lamp_check(...)         a plain tk.Checkbutton wearing the lamp images
    Segmented(tk.Frame)     option words, selected = bright + 2 px underbar; StringVar; state=
    Well(tk.Frame)          input well: panel, 1 px dim edge, focus = 2 px bright inside
    SectionRow(tk.Frame)    settings section title (+ optional exception tag)
    StatusLine(tk.Label)    one displayed line, cget("text") = the original text, hover tip
    EmptyState(tk.Frame)    title + block-cursor mark (B10 animates the mark)

House rules: no after() anywhere (tips show on <Enter>/<FocusIn> and hide on
<Leave>/<FocusOut>), no Tk off the Tk thread, no I/O. Every class that overrides
configure/cget follows the Override rule (see kit/__init__): config / __getitem__ are
re-bound after the override, Misc.__setitem__'s dict form works, mixed calls work and
non-text options pass through.

Geometry and inks follow the approved renderers (see design archive): r2/shared_glyphs.py
(button, text_action, input_well) and r2/screens/r2kit.py (lamp, segmented, section_row).
"""

import tkinter as tk

from .. import theme as _theme_mod
from ..fx import palette as _palette
from . import C9, C9B, C10B, C11, C11B, C13B, ensure_fonts, scale_factor, tokens
from .glyphs import POWER_MARK_W
from .painters import unspace

try:        # B10: the real-wait cursor (fx/motion.py); without it a status line just shows its text
    from ..fx.motion import WaitCursor as _WaitCursor, is_wait_text as _is_wait_text
except Exception:  # noqa: BLE001
    _WaitCursor = _is_wait_text = None

__all__ = ["KitButton", "TextAction", "lamp_images", "lamp_check", "Segmented", "Well", "SectionRow",
           "StatusLine", "EmptyState", "display_text", "KINDS"]


_SYMBOLS = set("[](){}<>+-/|")


def _looks_letter_spaced(text, u):
    """True when `text` (whose unspace() is u, u != text) should be read as letter_spaced(u).
    Short odd strings with spaces at the odd positions are ambiguous ("[ x ]", "A / B",
    "x y", "+ -" are labels in their own right), so (B1 fix F6):
      * the un-spaced form needs at least 2 letters or digits ("[ x ]" -> kept);
      * with fewer than 3 of them, a bracket / operator symbol keeps the text ("A / B");
      * with fewer than 3 of them, only genuine letter_spaced output, which is always
        upper-case, is un-spaced ("U P" -> "UP", but "x y" is kept)."""
    alnum = sum(1 for c in u if c.isalnum())
    if alnum < 2:
        return False
    if alnum < 3 and (any(c in _SYMBOLS for c in u) or text != text.upper()):
        return False
    return True


def display_text(text, upper=False, raw=False):
    """The label a kit button shows for `text`.
    A letter-spaced string that round-trips EXACTLY (letter_spaced(unspace(t)) == t, the
    plan's literal rule) is un-spaced ("E X P O R T I N G . . ." -> "EXPORTING..."), unless
    it is a short symbol label (_looks_letter_spaced: "[ x ]", "A / B" are kept). The rule is
    case-sensitive (B1 fix A5): letter_spaced output is always upper-case, so ordinary lower /
    mixed-case text such as "a b c d" or "v 1 . 2" is never rewritten. raw=True never
    un-spaces: use it for labels that carry user text (a chat title, project or model name
    such as "Q 4 K M" or "1 2 3"). Anything else keeps its case ("Up" stays "Up") unless
    upper=True. Non-strings pass through untouched."""
    if not isinstance(text, str):
        return text
    out = text
    u = text if raw else unspace(text)
    if u != text and _theme_mod.letter_spaced(u) == text and _looks_letter_spaced(text, u):
        out = u
    if upper:
        out = out.upper()
    return out


class _TextVarLink:
    """textvariable= for a kit class whose text goes through its own configure(text=) (B1 fix F5).

    Tk would draw a -textvariable directly, bypassing the class's text rules (one line,
    un-spacing, upper=, the face size). So the option never reaches Tk: the class pops it,
    and this link traces the Tcl variable and writes each new value through
    owner.configure(text=value). cget("textvariable") reads the variable's name back.
    The trace command lives on the root (not the widget), so a write after the widget died
    finds a live command that just removes the trace. Tk thread only (as any var.set)."""

    def __init__(self, owner):
        self.owner = owner
        self.name = ""
        self._cmd = None

    def attach(self, var):
        """Trace var (a tk.Variable or a Tcl variable name). '' / None detaches.
        -> the variable's current value, or None when detached."""
        self.detach()
        name = "" if var is None else str(var)
        if not name:
            return None
        o = self.owner
        cmd = o._root()._register(self._fire)
        try:
            o.tk.call("trace", "add", "variable", name, "write", cmd)
        except tk.TclError:
            try:
                o._root().deletecommand(cmd)
            except (tk.TclError, ValueError):
                pass
            raise
        self.name, self._cmd = name, cmd
        return self.value()

    def value(self):
        try:
            v = self.owner.tk.globalgetvar(self.name)
        except tk.TclError:
            return ""
        return v if isinstance(v, (str, int, float)) else str(v)

    def _fire(self, *_a):
        o = self.owner
        try:
            if not o.winfo_exists():
                self._untrace()
                return
            o.configure(text=self.value())
        except tk.TclError:
            pass

    def _untrace(self):
        if self._cmd is not None:
            try:
                self.owner.tk.call("trace", "remove", "variable", self.name, "write", self._cmd)
            except tk.TclError:
                pass

    def detach(self):
        if self._cmd is None:
            return
        self._untrace()
        try:
            self.owner._root().deletecommand(self._cmd)
        except (tk.TclError, ValueError):
            pass
        self.name, self._cmd = "", None

    def describe(self):
        return ("textvariable", "textVariable", "Variable", "", self.name)


def _split_cnf(cnf, kw):
    """Merge the dict form Misc.__setitem__ / old callers pass with the keywords."""
    if cnf:
        merged = dict(cnf)
        merged.update(kw)
        return merged
    return dict(kw)


# ====================================================================== KitButton

KINDS = ("primary", "secondary", "danger", "danger_outline")
_KIND_ALIASES = {"danger_fill": "danger", "danger_inverse": "danger", "danger_double": "danger_outline"}
EDGE_W = 4               # the stop edge: a 4 px solid left edge on every outline danger button
EDGE_GAP = 14            # stop edge -> label clearance
MARK_GAP = 12            # power mark -> label clearance
BTN_FONT = C9B
BTN_PADX, BTN_PADY = 14, 6
# B1 fix F1: Tk's Windows button/photo path dies with an access violation (no traceback) once the
# face is >= 32768 px on a side (16-bit overflow). The face is capped far below that, and a label
# too long for the cap is elided in the middle (too many lines: cut, with an ellipsis line).
FACE_MAX_W = 4096
FACE_MAX_H = 1024
# B1 fix F3: colours are the kit's (the face is an image under the label). These options are
# accepted and swallowed, never passed to Tk, so a legacy bg=/fg= cannot make the label invisible
# until the next repaint. Mark a chosen option with selected=True instead. A caller's image= /
# bitmap= is REFUSED (TypeError), not swallowed: the face would silently replace the icon, and a
# lost icon is lost content (D-27). Passing "" / None (no image) is accepted.
_BTN_SWALLOWED = ("bg", "background", "fg", "foreground", "activebackground", "activeforeground",
                  "disabledforeground", "compound")
_BTN_REFUSED = ("image", "bitmap")


def _btn_options(kw):
    """Drop the swallowed options from kw; raise TypeError for a real image= / bitmap=."""
    for name in _BTN_REFUSED:
        if name in kw:
            if kw[name] not in (None, ""):
                raise TypeError("KitButton draws its own face: %s= is not supported (use a plain "
                                "tk.Button for an icon button)" % name)
            kw.pop(name)
    for name in _BTN_SWALLOWED:
        kw.pop(name, None)
_BTN_OWN = ("kind", "upper", "raw", "ground", "padx", "pady", "mark", "selected", "explain")

# The 15x15 power mark as pixels (glyphs.power_mark's octagon + bar), rows -1..14 from its top.
_POWER_POLY = (5, 0, 4, 0, 0, 4, 0, 10, 4, 14, 10, 14, 14, 10, 14, 4, 10, 0, 9, 0)


def _line_px(x0, y0, x1, y1):
    """Bresenham pixels of a 1 px segment, both ends included."""
    pts = []
    dx, dy = abs(x1 - x0), -abs(y1 - y0)
    sx, sy = (1 if x0 < x1 else -1), (1 if y0 < y1 else -1)
    err = dx + dy
    while True:
        pts.append((x0, y0))
        if x0 == x1 and y0 == y1:
            return pts
        e2 = 2 * err
        if e2 >= dy:
            err += dy
            x0 += sx
        if e2 <= dx:
            err += dx
            y0 += sy


def _power_pixels():
    px = set()
    p = _POWER_POLY
    for i in range(0, len(p) - 2, 2):
        px.update(_line_px(p[i], p[i + 1], p[i + 2], p[i + 3]))
    for yy in range(-1, 7):                  # the bar: column 7, rows -1..6
        px.add((7, yy))
    return sorted(px)


_POWER_PX = _power_pixels()


def _mark_kind(label, mark):
    if mark in ("power", "stop"):
        return mark
    u = str(label or "").upper()
    return "power" if ("SHUT" in u or "POWER" in u) else "stop"


class KitButton(tk.Button):
    """KitButton(master, theme, text="", kind="secondary", upper=False, mark=None, ground=None,
                 selected=False, raw=False, **button_kw)

    Still a tk.Button (tests find buttons by class and text; .invoke(), state=, command=,
    Return / space keep working). Case is preserved by default (X-9); upper=True upper-cases;
    a letter-spaced text that round-trips exactly is un-spaced (display_text); raw=True keeps
    user text verbatim. cget("text") is the displayed label, never clipped (A8); a bad state=
    raises TclError and changes nothing (A4).

    Look (approved shared_glyphs.button), painted as the button's own face image under the
    centred text (compound="center"), because a Windows tk.Button ignores its highlight ring
    at rest and clips child widgets:
      primary         bright fill, bg text; focus = the fill itself
      secondary       no fill (the ground), 1 px dim, value text; hover = bright outline + text;
                      keyboard focus = 2 px bright outline
      danger_outline  no fill, 1 px danger + the 4 px stop edge, danger text; focus 2 px danger;
                      a power-down label (SHUT / POWER, or mark="power") also carries the 15 px
                      power mark
      danger          danger fill, bg text, + the bg power mark on power-down labels; the
                      focused (Tab-selected) danger act is never filled: outline look instead
      selected        (selected=True; primary / secondary) the inverse chip: bright fill, bg text
                      (shared_glyphs "selected"); for a chosen option in a picker row
      disabled        ghost outline, dim text (danger kinds: danger at 45 %)
      disabled + explain=callable (B6-H1): the same ghost face, but a real Tab stop - takefocus
                      stays 1, the ring is 2 px dim while focused - and Return / KP_Enter / space /
                      a click inside run explain() (a "why not" action) in place of the parked
                      command; invoke() still does nothing and cget("state") still reads "disabled"
    Size: text + 2 x padx (14) wide, font line + 2 x pady (6) tall, plus room for a mark on
    both sides so the label stays centred and clear of it. width= / height= are PIXELS (the
    face is an image), capped at FACE_MAX_W x FACE_MAX_H (a longer label is elided). The face
    follows the real size on <Configure>, so fill="x" works; an explicit width= / height= stays
    the requested size even while the face is painted smaller (a squeezed button grows back).
    bg / fg / active* / disabledforeground / compound are swallowed (the face owns them);
    image= / bitmap= raise TypeError (an icon would vanish under the face); textvariable= is
    traced through configure(text=) (display_text applies)."""

    def __init__(self, master, theme, text="", kind="secondary", upper=False, mark=None, ground=None,
                 selected=False, raw=False, explain=None, **kw):
        self._theme = theme
        self._explain = explain if callable(explain) else None
        self._t = tokens(theme)
        k = _KIND_ALIASES.get(kind, kind)
        self._kind = k if k in KINDS else "secondary"
        self._upper = bool(upper)
        self._raw = bool(raw)
        self._mark_opt = mark
        self._ground = ground or self._t["bg"]
        self._selected = bool(selected)
        self._hover = False
        self._focus = False
        self._raw_text = "" if text is None else text
        self._padx = kw.pop("padx", BTN_PADX)
        self._pady = kw.pop("pady", BTN_PADY)
        self._req_w = kw.pop("width", None)
        self._req_h = kw.pop("height", None)
        self._face = None
        self._face_key = None
        state = kw.pop("state", "normal")
        self._dis = False
        self._saved_cmd = None
        self._saved_takefocus = None
        self._tv = _TextVarLink(self)
        tvar = kw.pop("textvariable", None)
        _btn_options(kw)
        kw.setdefault("font", BTN_FONT)
        kw.setdefault("cursor", "hand2")
        for opt, val in (("relief", "flat"), ("overrelief", "flat"), ("bd", 0), ("highlightthickness", 0),
                         ("takefocus", 1), ("default", "disabled")):
            kw.setdefault(opt, val)
        tk.Button.__init__(self, master, text="", padx=0, pady=0, compound="center", **kw)
        try:
            state = self._check_state(state)          # A4: a bad state= fails as on a stock tk.Button
        except tk.TclError:
            tk.Button.destroy(self)
            raise
        if state == "active":
            tk.Button.configure(self, state="active")
        self._relabel()
        self.bind("<Enter>", self._on_enter, add="+")
        self.bind("<Leave>", self._on_leave, add="+")
        self.bind("<FocusIn>", self._on_focus_in, add="+")
        self.bind("<FocusOut>", self._on_focus_out, add="+")
        self.bind("<Return>", self._on_key_invoke, add="+")
        self.bind("<KP_Enter>", self._on_key_invoke, add="+")
        self.bind("<space>", self._on_key_space, add="+")
        self.bind("<ButtonRelease-1>", self._on_explain_release, add="+")
        self.bind("<Configure>", self._on_configure, add="+")
        if state == "disabled":
            self._set_disabled(True, force=True)
        if tvar:
            v = self._tv.attach(tvar)
            if v is not None:
                self._raw_text = v
                self._relabel()
        self._apply_look(natural=True)

    # ---------------------------------------------------------------- the label (F1: never wider than the cap)
    def _relabel(self):
        """Put display_text(raw text) on the button, elided to fit FACE_MAX_W x FACE_MAX_H."""
        s = display_text(self._raw_text, self._upper, self._raw)
        if isinstance(s, str) and s:
            s = self._clip_label(s)
        tk.Button.configure(self, text=s)

    def _clip_label(self, s):
        font = tk.Button.cget(self, "font")
        try:
            ls = max(1, int(self.tk.call("font", "metrics", font, "-linespace")))
        except tk.TclError:
            ls = 14
        lines = s.split("\n")
        max_lines = max(1, (FACE_MAX_H - 2 * int(self._pady)) // ls)
        if len(lines) > max_lines:
            lines = lines[:max_lines - 1] + [ELLIPSIS] if max_lines > 1 else [lines[0] + ELLIPSIS]
        side = max(int(self._padx), self._lead(max(1, scale_factor(self))))
        avail = max(1, FACE_MAX_W - 2 * side)

        def fits(t):
            try:
                return int(self.tk.call("font", "measure", font, t)) <= avail
            except tk.TclError:
                return len(t) * 7 <= avail
        return "\n".join(elide_middle(ln, fits) for ln in lines)

    def _check_state(self, value):
        """The canonical Tk state for value ("dis" -> "disabled"), or the TclError a stock
        tk.Button raises for it. Nothing changes either way: the Tk state is put back at once
        (B1 fix A4: a rejected state= must never unpark a disabled button's command)."""
        prev = tk.Button.cget(self, "state")
        try:
            tk.Button.configure(self, state=value)
            return str(tk.Button.cget(self, "state"))
        finally:
            try:
                tk.Button.configure(self, state=prev)
            except tk.TclError:
                pass

    # ---------------------------------------------------------------- disabled, without Tk's stipple
    # A disabled tk.Button that shows an image is stippled all over by Tk (every platform), which
    # would hatch the whole face. So the Tk state stays normal and "disabled" is kept here: the
    # Tcl -command is parked (clicks, space, Return and invoke() then do nothing), takefocus is
    # 0, the face shows the disabled look, and cget("state") / w["state"] read "disabled".
    def _set_disabled(self, on, force=False):
        on = bool(on)
        if on == self._dis and not force:
            return
        self._dis = on
        if on:
            if self._saved_cmd is None:
                self._saved_cmd = tk.Button.cget(self, "command")
                self._saved_takefocus = tk.Button.cget(self, "takefocus")
            tk.Button.configure(self, command="", takefocus=1 if self._explain else 0)
            if str(tk.Button.cget(self, "state")) == "active":
                tk.Button.configure(self, state="normal")
        else:
            cmd, tf = self._saved_cmd, self._saved_takefocus
            self._saved_cmd = self._saved_takefocus = None
            tk.Button.configure(self, command=cmd if cmd is not None else "",
                                takefocus=tf if tf not in (None, "") else 1)

    # ---------------------------------------------------------------- Override rule
    def configure(self, cnf=None, **kw):
        if isinstance(cnf, str):
            if cnf == "textvariable":
                return self._tv.describe()
            if cnf in _BTN_OWN or (self._dis and cnf in ("state", "command", "takefocus")):
                return (cnf, cnf, cnf.capitalize(), "", self.cget(cnf))
            if cnf == "text":                             # A8: the un-clipped label
                return tuple(tk.Button.configure(self, cnf)[:-1]) + (self.cget("text"),)
            return tk.Button.configure(self, cnf)
        kw = _split_cnf(cnf, kw)
        if not kw:
            return tk.Button.configure(self)
        _btn_options(kw)                                  # F3: the face owns colours; no caller image
        new_state = kw.pop("state", None)
        if new_state is not None:                         # A4: validated before anything changes
            new_state = self._check_state(new_state)
        relabel = any(k in kw for k in ("font", "kind", "mark"))     # they move the clip limit
        if "textvariable" in kw:                          # F5: traced, never handed to Tk
            v = self._tv.attach(kw.pop("textvariable"))
            if v is not None:
                kw["text"] = v
        if "text" in kw and kw["text"] is None:           # F8: Tk drops None; so do we (mark stays in step)
            kw.pop("text")
        if "upper" in kw:
            self._upper = bool(kw.pop("upper"))
            relabel = True
        if "raw" in kw:
            self._raw = bool(kw.pop("raw"))
            relabel = True
        if "text" in kw:
            self._raw_text = kw.pop("text")
            relabel = True
        if "selected" in kw:
            self._selected = bool(kw.pop("selected"))
        if "kind" in kw:
            k = kw.pop("kind")
            k = _KIND_ALIASES.get(k, k)
            if k in KINDS:
                self._kind = k
        if "mark" in kw:
            self._mark_opt = kw.pop("mark")
        if "ground" in kw:
            self._ground = kw.pop("ground") or self._t["bg"]
        if "explain" in kw:                               # B6-H1: a disabled button's "why not" action
            e = kw.pop("explain")
            self._explain = e if callable(e) else None
            if self._dis:
                tk.Button.configure(self, takefocus=1 if self._explain else 0)
        if "padx" in kw:
            self._padx = kw.pop("padx")
            relabel = True
        if "pady" in kw:
            self._pady = kw.pop("pady")
            relabel = True
        if "width" in kw:
            self._req_w = kw.pop("width")
        if "height" in kw:
            self._req_h = kw.pop("height")
        if self._dis:
            # disabled: a new command / takefocus is parked, whatever the call also does to the
            # state (F2: in .config(state="normal", command=new) the NEW command must win, as on
            # a stock tk.Button); _set_disabled(False) below then restores the parked values.
            if "command" in kw:
                tk.Button.configure(self, command=kw.pop("command"))
                self._saved_cmd = tk.Button.cget(self, "command")
                tk.Button.configure(self, command="")
            if "takefocus" in kw:
                self._saved_takefocus = kw.pop("takefocus")
        result = tk.Button.configure(self, **kw) if kw else None
        if relabel:
            self._relabel()
        if new_state is not None:
            if new_state == "disabled":
                self._set_disabled(True)
            else:
                self._set_disabled(False)
                tk.Button.configure(self, state=new_state)     # validated above: cannot fail
        self._apply_look(natural=True)
        return result

    def cget(self, key):
        if key == "kind":
            return self._kind
        if key == "upper":
            return self._upper
        if key == "raw":
            return self._raw
        if key == "text" and isinstance(self._raw_text, str):
            # A8: the label as displayed but never clipped (Tk holds only the elided string it
            # draws), so a D-27 read or w["text"] = w["text"] loses nothing
            return display_text(self._raw_text, self._upper, self._raw)
        if key == "ground":
            return self._ground
        if key == "padx":
            return self._padx
        if key == "pady":
            return self._pady
        if key == "mark":
            return self._mark()
        if key == "selected":
            return self._selected
        if key == "explain":
            return self._explain
        if key == "textvariable":
            return self._tv.name
        if key == "state" and self._dis:
            return "disabled"
        if key == "command" and self._dis:
            return self._saved_cmd if self._saved_cmd is not None else ""
        if key == "takefocus" and self._dis:
            return self._saved_takefocus if self._saved_takefocus is not None else ""
        return tk.Button.cget(self, key)

    config = configure
    __getitem__ = cget

    # ---------------------------------------------------------------- look
    @property
    def kind(self):
        return self._kind

    def _disabled(self):
        return self._dis

    def invoke(self):
        """Like tk.Button.invoke; does nothing while the button is disabled."""
        if self._dis:
            return ""
        return tk.Button.invoke(self)

    def destroy(self):
        self._tv.detach()
        tk.Button.destroy(self)
        # B6 (BF-01 follow-up, the F-1 freeze shape): a KitButton is cyclic garbage once destroyed
        # (self -> _tv -> owner), so the face PhotoImage it held would be finalised by the cyclic gc,
        # on whatever thread runs it - Image.__del__ then calls Tcl off the Tk thread. Drop it here,
        # on the Tk thread: the button no longer shows it, so the refcount frees it at once.
        self._face = None
        self._face_key = None

    def _mark(self):
        if self._kind not in ("danger", "danger_outline"):
            return None
        return _mark_kind(self._raw_text if isinstance(self._raw_text, str) else "", self._mark_opt)

    def _filled_danger(self):
        return self._kind == "danger" and not self._disabled() and not self._focus

    def look(self):
        """(fill, ink, ring, ring_w, edge, mark_ink): the face for the current state.
        edge None = no stop edge; mark_ink None = no power mark."""
        t = self._t
        dis = self._disabled()
        k = self._kind
        mark = self._mark()
        ring_w = 2 if (self._focus and (not dis or self._explain)) else 1
        edge = None
        if (k == "primary" or (self._selected and k == "secondary")) and not dis:
            fill, ink, ring, ring_w = t["bright"], t["bg"], t["bright"], 1
        elif k == "danger" and self._filled_danger():
            fill, ink, ring, ring_w = t["danger"], t["bg"], t["danger"], 1
        elif k in ("danger", "danger_outline"):
            col = _palette.mix(t["bg"], t["danger"], 0.45) if dis else t["danger"]
            fill, ink, ring, edge = self._ground, col, col, col
        elif dis:          # secondary, and the disabled primary; focused + explain=: a dim ring (B6-H1)
            fill, ink, ring = self._ground, t["dim"], (t["dim"] if ring_w == 2 else t["ghost"])
        elif self._hover or self._focus:
            fill, ink, ring = self._ground, t["bright"], t["bright"]
        else:
            fill, ink, ring = self._ground, t["value"], t["dim"]
        return fill, ink, ring, ring_w, edge, (ink if mark == "power" else None)

    def _lead(self, f):
        """Room the mark needs from the left edge to the label (0 without a mark)."""
        mark = self._mark()
        if mark == "power":
            if self._filled_danger():
                return (10 + POWER_MARK_W + MARK_GAP) * f
            return (EDGE_W + 8 + POWER_MARK_W + MARK_GAP) * f
        if mark == "stop":
            return (EDGE_W + EDGE_GAP) * f
        return 0

    def natural_size(self):
        """(w, h) the button asks for: text + padding (+ mark room on both sides), or the
        width= / height= given (pixels)."""
        f = scale_factor(self)
        font = tk.Button.cget(self, "font")
        text = str(tk.Button.cget(self, "text"))
        lines = text.split("\n") or [""]
        try:
            tw = max(int(self.tk.call("font", "measure", font, ln)) for ln in lines)
            lh = int(self.tk.call("font", "metrics", font, "-linespace")) * len(lines)
        except tk.TclError:
            tw, lh = 7 * len(text), 14
        side = max(int(self._padx), self._lead(max(1, f)))
        w = tw + 2 * side
        h = lh + 2 * int(self._pady)
        if self._req_w:
            w = int(self._req_w)
        if self._req_h:
            h = int(self._req_h)
        return max(4, min(FACE_MAX_W, w)), max(4, min(FACE_MAX_H, h))

    def _apply_look(self, natural=False):
        if natural or self._face is None:
            w, h = self.natural_size()
            fw, fh = self._face_size(fallback=False)
            # a managed button keeps the size its geometry manager gave it (fill="x")
            if fw > 1 and fh > 1 and self._face_key is not None and (fw, fh) != self._face_key[:2]:
                w, h = fw, fh
        else:
            w, h = self._face_size()
        self._paint(w, h)

    def _face_size(self, fallback=True):
        try:
            w, h = self.winfo_width(), self.winfo_height()
        except tk.TclError:
            w = h = 1
        if (w <= 1 or h <= 1) and fallback:
            return self.natural_size()
        return w, h

    def _paint(self, w, h):
        w = max(4, min(FACE_MAX_W, int(w)))      # F1: never near Tk's 32768 px crash
        h = max(4, min(FACE_MAX_H, int(h)))
        fill, ink, ring, ring_w, edge, mark_ink = self.look()
        f = scale_factor(self)
        rw = max(4, min(FACE_MAX_W, int(self._req_w))) if self._req_w else w
        rh = max(4, min(FACE_MAX_H, int(self._req_h))) if self._req_h else h
        key = (w, h, fill, ink, ring, ring_w, edge, mark_ink, f, self._filled_danger(), rw, rh)
        if key == self._face_key and self._face is not None:
            return
        try:
            img = self._face
            if img is None or img.width() != w or img.height() != h:
                img = tk.PhotoImage(master=self, width=w, height=h)
            img.blank()
            img.put(fill, to=(0, 0, w, h))
            _put_box(img, 0, 0, w, h, ring, ring_w * f)
            if edge:
                img.put(edge, to=(0, 0, min(w, EDGE_W * f), h))
            if mark_ink:
                mx = (10 if self._filled_danger() else EDGE_W + 8) * f
                my = h // 2 - 7 * f
                for (px, py) in _POWER_PX:
                    x0, y0 = mx + px * f, my + py * f
                    if 0 <= x0 and x0 + f <= w and 0 <= y0 and y0 + f <= h:
                        img.put(mark_ink, to=(x0, y0, x0 + f, y0 + f))
            # B4 fix ADV1-02: the face is painted at the managed size, but an explicit width= / height=
            # stays the REQUESTED size. Requesting the painted size made a squeeze permanent: a button
            # pack shrank once (a short row) then asked for that small size forever, so the room coming
            # back never restored it. Without width= / height= the request follows the face, as before.
            tk.Button.configure(self, image=img, bg=fill, fg=ink, activebackground=fill, activeforeground=ink,
                                disabledforeground=ink, width=rw, height=rh)
            self._face = img
            self._face_key = key
        except tk.TclError:
            pass

    def _on_configure(self, _e=None):
        w, h = self._face_size()
        if self._face_key is None or (w, h) != self._face_key[:2]:
            self._paint(w, h)

    def _on_enter(self, _e=None):
        self._hover = True
        self._apply_look()

    def _on_leave(self, _e=None):
        self._hover = False
        self._apply_look()

    def _on_focus_in(self, _e=None):
        self._focus = True
        self._apply_look()

    def _on_focus_out(self, _e=None):
        self._focus = False
        self._apply_look()

    def _on_key_invoke(self, _e=None):
        if not self._disabled():
            self.invoke()
        elif self._explain:
            self._explain()
        return "break"

    def _on_key_space(self, _e=None):
        """B6-H1: space on a disabled button with explain= runs it (the class binding's invoke is
        parked); otherwise the stock space press (tk::ButtonInvoke) goes on as before."""
        if self._disabled() and self._explain:
            self._explain()
            return "break"
        return None

    def _on_explain_release(self, e=None):
        """B6-H1: a click released inside a disabled button with explain= runs it."""
        if e is None or not (self._disabled() and self._explain):
            return None
        try:
            inside = 0 <= e.x < self.winfo_width() and 0 <= e.y < self.winfo_height()
        except tk.TclError:
            return None
        if inside:
            self._explain()
        return None


# ====================================================================== TextAction

TA_FONT = C9B
TA_PADX, TA_PADY = 6, 4


def _ta_colour_opts(kw):
    """TextAction: bg / background -> ground, fg / foreground -> ink (honoured on every repaint);
    the active / disabled colours are the look's and are swallowed."""
    for name, own in (("bg", "ground"), ("background", "ground"), ("fg", "ink"), ("foreground", "ink")):
        if name in kw:
            v = kw.pop(name)
            if v:
                kw[own] = v
    for name in ("activebackground", "activeforeground", "disabledforeground"):
        kw.pop(name, None)


class TextAction(tk.Label):
    """TextAction(master, theme, text="", command=None, upper=False, ground=None, ink=None, **label_kw)

    A secondary function drawn as a word (approved shared_glyphs.text_action): C9B value
    at rest with no box; hover = 1 px bright box on the hit area (text + padx 6, pady 4) and
    bright text; focus = 2 px; pressed = bright fill, bg text; disabled = dim text.
    takefocus=1; Return and space invoke it like a click. The box is the highlight ring,
    and the padding gives the pixel back, so no state moves the word.
    bg= / fg= (and background= / foreground=) are read as ground= / ink=, so they survive
    every repaint; active* / disabledforeground are swallowed (the look owns them).
    textvariable= is traced through configure(text=), so upper= applies to it."""

    def __init__(self, master, theme, text="", command=None, upper=False, ground=None, ink=None, **kw):
        self._t = tokens(theme)
        self._command = command
        self._upper = bool(upper)
        _ta_colour_opts(kw)
        self._ground = ground or kw.pop("ground", None) or self._t["bg"]
        self._ink = ink or kw.pop("ink", None)
        self._hover = self._focus = self._pressed = False
        self._raw_text = text
        self._tv = _TextVarLink(self)
        tvar = kw.pop("textvariable", None)
        kw.setdefault("font", TA_FONT)
        kw.setdefault("cursor", "hand2")
        state = kw.pop("state", "normal")
        kw.setdefault("takefocus", 1)
        kw.setdefault("bd", 0)
        tk.Label.__init__(self, master, text=self._shown(text), **kw)
        tk.Label.configure(self, state=state)
        for seq, fn in (("<Enter>", self._on_enter), ("<Leave>", self._on_leave), ("<FocusIn>", self._on_fin),
                        ("<FocusOut>", self._on_fout), ("<ButtonPress-1>", self._on_press),
                        ("<ButtonRelease-1>", self._on_release), ("<Return>", self._on_key),
                        ("<KP_Enter>", self._on_key), ("<space>", self._on_key)):
            self.bind(seq, fn, add="+")
        if tvar:
            v = self._tv.attach(tvar)
            if v is not None:
                self._raw_text = v
                tk.Label.configure(self, text=self._shown(v))
        self._apply_look()

    def _shown(self, text):
        return text.upper() if (self._upper and isinstance(text, str)) else text

    def configure(self, cnf=None, **kw):
        if isinstance(cnf, str):
            if cnf == "command":
                return ("command", "command", "Command", "", self._command)
            if cnf == "textvariable":
                return self._tv.describe()
            return tk.Label.configure(self, cnf)
        kw = _split_cnf(cnf, kw)
        if not kw:
            return tk.Label.configure(self)
        _ta_colour_opts(kw)
        if "textvariable" in kw:
            v = self._tv.attach(kw.pop("textvariable"))
            if v is not None:
                kw["text"] = v
        if "command" in kw:
            self._command = kw.pop("command")
        if "upper" in kw:
            self._upper = bool(kw.pop("upper"))
            kw.setdefault("text", self._raw_text)
        if "ground" in kw:
            self._ground = kw.pop("ground") or self._t["bg"]
        if "ink" in kw:
            self._ink = kw.pop("ink")
        if "text" in kw:
            self._raw_text = kw["text"]
            kw["text"] = self._shown(kw["text"])
        result = tk.Label.configure(self, **kw) if kw else None
        self._apply_look()
        return result

    def cget(self, key):
        if key == "command":
            return self._command
        if key == "upper":
            return self._upper
        if key == "ground":
            return self._ground
        if key == "ink":
            return self._ink
        if key == "textvariable":
            return self._tv.name
        return tk.Label.cget(self, key)

    def destroy(self):
        self._tv.detach()
        tk.Label.destroy(self)

    config = configure
    __getitem__ = cget

    def _disabled(self):
        try:
            return str(tk.Label.cget(self, "state")) == "disabled"
        except tk.TclError:
            return False

    def _apply_look(self):
        t = self._t
        dis = self._disabled()
        fill, ink, ring, ring_w = self._ground, (self._ink or t["value"]), self._ground, 2
        if dis:
            ink = t["dim"]
        elif self._pressed:
            fill, ink, ring = t["bright"], t["bg"], t["bright"]
        elif self._focus:
            ink, ring = t["bright"], t["bright"]
        elif self._hover:
            ink, ring, ring_w = t["bright"], t["bright"], 1
        try:
            tk.Label.configure(self, bg=fill, fg=ink, activebackground=fill, activeforeground=ink,
                               disabledforeground=ink, highlightthickness=ring_w, highlightbackground=ring,
                               highlightcolor=ring, padx=TA_PADX - ring_w, pady=TA_PADY - ring_w)
        except tk.TclError:
            pass

    def invoke(self):
        """Run the command unless disabled. -> its result (None when there is none)."""
        if self._disabled() or self._command is None:
            return None
        return self._command()

    def _on_enter(self, _e=None):
        self._hover = True
        self._apply_look()

    def _on_leave(self, _e=None):
        self._hover = False
        self._pressed = False
        self._apply_look()

    def _on_fin(self, _e=None):
        self._focus = True
        self._apply_look()

    def _on_fout(self, _e=None):
        self._focus = False
        self._apply_look()

    def _on_press(self, _e=None):
        if self._disabled():
            return None
        self._pressed = True
        self._apply_look()
        return None

    def _on_release(self, e=None):
        was = self._pressed
        self._pressed = False
        self._apply_look()
        if not was or self._disabled():
            return None
        inside = True
        if e is not None:
            try:
                inside = 0 <= e.x < self.winfo_width() and 0 <= e.y < self.winfo_height()
            except tk.TclError:
                inside = False
        if inside:
            self.invoke()
        return None

    def _on_key(self, _e=None):
        self.invoke()
        return "break"


# ====================================================================== lamps

LAMP_BOX = 14            # 14x14 box, 1 px
LAMP_DOT = (4, 10)       # the 6x6 inner dot (checked)
LAMP_TEXT_GAP = 8        # box -> text (approved: text at x + 22)
_LAMP_ATTR = "_kit_lamp_images"


def lamp_images(widget, theme=None, disabled=False):
    """(off, on) PhotoImages for a lamp checkbox (approved r2kit.lamp): a 14x14 1 px box
    with a transparent core; checked = bright box + 6x6 bright dot, unchecked = dim box;
    disabled draws both in dim. Each image is LAMP_BOX + LAMP_TEXT_GAP wide (transparent
    right part), so compound="left" puts the text 8 px after the box. Scaled by the integer
    kit factor. Cached per interpreter and palette; never drawn twice."""
    t = tokens(theme) if theme is not None else tokens(None)
    root = widget._root() if hasattr(widget, "_root") else widget
    f = scale_factor(root)
    key = (f, t["bright"], t["dim"], bool(disabled))
    cache = getattr(root, _LAMP_ATTR, None)
    if cache is None:
        cache = {}
        try:
            setattr(root, _LAMP_ATTR, cache)
        except Exception:  # noqa: BLE001
            pass
    hit = cache.get(key)
    if hit is not None:
        return hit
    box, w = LAMP_BOX * f, (LAMP_BOX + LAMP_TEXT_GAP) * f
    imgs = []
    for on in (False, True):
        img = tk.PhotoImage(master=root, width=w, height=box)
        col = t["dim"] if (disabled or not on) else t["bright"]
        _put_box(img, 0, 0, box, box, col, f)
        if on:
            a, b = LAMP_DOT[0] * f, LAMP_DOT[1] * f
            img.put(col, to=(a, a, b, b))
        imgs.append(img)
    cache[key] = tuple(imgs)
    return cache[key]


def _put_box(img, x0, y0, x1, y1, col, w=1):
    img.put(col, to=(x0, y0, x1, y0 + w))
    img.put(col, to=(x0, y1 - w, x1, y1))
    img.put(col, to=(x0, y0, x0 + w, y1))
    img.put(col, to=(x1 - w, y0, x1, y1))


def lamp_check(master, theme, text="", variable=None, command=None, ground=None, font=C11, **kw):
    """A plain tk.Checkbutton with the lamp images (indicatoron=0, image=, selectimage=,
    compound="left"). Its text is exactly the text given: a lamp never changes the text
    option or its case. -> the Checkbutton."""
    t = tokens(theme)
    ground = ground or t["bg"]
    off, on = lamp_images(master, theme)
    opts = dict(text=text, image=off, selectimage=on, compound="left", indicatoron=0, anchor="w",
                bg=ground, fg=t["bright"], activebackground=ground, activeforeground=t["bright"],
                selectcolor=ground, disabledforeground=t["dim"], relief="flat", offrelief="flat",
                overrelief="flat", bd=0, highlightthickness=1, highlightbackground=ground,
                highlightcolor=t["bright"], font=font, padx=0, pady=2, cursor="hand2")
    if variable is not None:
        opts["variable"] = variable
    if command is not None:
        opts["command"] = command
    opts.update(kw)
    cb = tk.Checkbutton(master, **opts)
    cb._kit_lamp_images = (off, on)      # keep the images alive with the widget
    return cb


# ====================================================================== Segmented

SEG_FONT = C10B
SEG_PADX, SEG_H = 12, 24


class Segmented(tk.Frame):
    """Segmented(master, theme, options, variable=None, command=None, state="normal", ground=None, font=C10B)

    Option words (approved r2kit.segmented): no group box, no fill; each word value ink,
    centred in a cell of text + 2 x 12; selected = bright word + a 2 px bright underbar under
    the word; keyboard focus = a 2 px bright outline inside the focused cell (the ring is
    always 2 px, ground-coloured at rest, so focus never moves anything). options: strings,
    or (value, label) pairs. variable: a StringVar (one is made when None); external writes
    to it repaint. Left / Right move focus, Return / space select. configure(state=
    "disabled") dims every word and ignores input; cget("state") reads it back."""

    def __init__(self, master, theme, options=(), variable=None, command=None, state="normal", ground=None,
                 font=SEG_FONT, **kw):
        self._t = tokens(theme)
        self._ground = ground or self._t["bg"]
        self._font = font
        self._command = command
        self._state = "disabled" if state == "disabled" else "normal"
        self._opts = []
        self._cells = []
        self._var = variable if variable is not None else tk.StringVar(master)
        self._trace = None
        kw.setdefault("bg", self._ground)
        tk.Frame.__init__(self, master, **kw)
        self._set_options(options)
        self._attach_var()

    # ---------------------------------------------------------------- Override rule
    def configure(self, cnf=None, **kw):
        if isinstance(cnf, str):
            if cnf in ("state", "variable", "command", "options"):
                return (cnf, cnf, cnf.capitalize(), "", self.cget(cnf))
            return tk.Frame.configure(self, cnf)
        kw = _split_cnf(cnf, kw)
        if not kw:
            return tk.Frame.configure(self)
        repaint = False
        if "state" in kw:
            self._state = "disabled" if kw.pop("state") == "disabled" else "normal"
            repaint = True
        if "command" in kw:
            self._command = kw.pop("command")
        if "variable" in kw:
            self._detach_var()
            self._var = kw.pop("variable") or tk.StringVar(self)
            self._attach_var()
            repaint = True
        if "options" in kw:
            self._set_options(kw.pop("options"))
            repaint = True
        if "text" in kw:          # a segmented control has no single text: ignored, never raised
            kw.pop("text")
        result = tk.Frame.configure(self, **kw) if kw else None
        if repaint:
            self._repaint()
        return result

    def cget(self, key):
        if key == "state":
            return self._state
        if key == "variable":
            return self._var
        if key == "command":
            return self._command
        if key == "options":
            return tuple(v for v, _l in self._opts)
        return tk.Frame.cget(self, key)

    config = configure
    __getitem__ = cget

    # ---------------------------------------------------------------- value
    def get(self):
        try:
            return self._var.get()
        except (tk.TclError, ValueError):
            return ""

    def _selected_value(self):
        """The variable's value as an option value (a str): an IntVar / BooleanVar / DoubleVar
        holds a number, the options are stored as strings (B1 fix F7)."""
        v = self.get()
        return v if isinstance(v, str) else str(v)

    def set(self, value):
        self._var.set(value)

    def _attach_var(self):
        try:
            self._trace = self._var.trace_add("write", lambda *_a: self._repaint())
        except (tk.TclError, AttributeError):
            self._trace = None
        self._repaint()

    def _detach_var(self):
        if self._trace is not None:
            try:
                self._var.trace_remove("write", self._trace)
            except (tk.TclError, ValueError):
                pass
            self._trace = None

    def destroy(self):
        self._detach_var()
        tk.Frame.destroy(self)
        # B10-H10: the cells' master is this frame, so self._cells kept a reference cycle (Segmented -> cells ->
        # master) that only the cyclic gc freed - on whatever thread ran it (F-1 class); dropped, the control
        # and its variable go by reference count on the Tk thread
        self._cells = []
        self._opts = []
        self._command = None

    # ---------------------------------------------------------------- cells
    def _set_options(self, options):
        for cell, _lab, _bar in self._cells:
            cell.destroy()
        self._cells = []
        self._opts = []
        for o in options or ():
            if isinstance(o, (tuple, list)) and len(o) == 2:
                value, label = str(o[0]), str(o[1])
            else:
                value = label = str(o)
            self._opts.append((value, label))
        for i, (value, label) in enumerate(self._opts):
            cell = tk.Frame(self, bg=self._ground, highlightthickness=2, highlightbackground=self._ground,
                            highlightcolor=self._t["bright"], takefocus=1, cursor="hand2")
            cell.pack(side="left")
            lab = tk.Label(cell, text=label, font=self._font, bg=self._ground, fg=self._t["value"], bd=0,
                           padx=SEG_PADX - 2, pady=0, cursor="hand2")
            lab.pack(side="top", pady=(2, 0))
            tw = int(self.tk.call("font", "measure", self._font, label))
            bar = tk.Frame(cell, width=max(1, tw), height=2, bg=self._ground, bd=0, highlightthickness=0)
            bar.pack(side="top", pady=(1, 2))
            for w in (cell, lab, bar):
                w.bind("<Button-1>", lambda _e, v=value, c=cell: self._click(v, c), add="+")
            cell.bind("<Return>", lambda _e, v=value: self._choose(v), add="+")
            cell.bind("<KP_Enter>", lambda _e, v=value: self._choose(v), add="+")
            cell.bind("<space>", lambda _e, v=value: self._choose(v), add="+")
            cell.bind("<Left>", lambda _e, i=i: self._move(i, -1), add="+")
            cell.bind("<Right>", lambda _e, i=i: self._move(i, 1), add="+")
            self._cells.append((cell, lab, bar))
        self._repaint()

    def _click(self, value, cell):
        if self._state == "disabled":
            return "break"
        try:
            cell.focus_set()
        except tk.TclError:
            pass
        self._choose(value)
        return None

    def _move(self, i, d):
        if self._state == "disabled" or not self._cells:
            return "break"
        j = max(0, min(len(self._cells) - 1, i + d))
        try:
            self._cells[j][0].focus_set()
        except tk.TclError:
            pass
        return "break"

    def _choose(self, value):
        if self._state == "disabled":
            return "break"
        if self._selected_value() != value:
            self._var.set(value)
        else:
            self._repaint()
        if self._command is not None:
            self._command()
        return "break"

    def _repaint(self):
        t = self._t
        sel = self._selected_value()
        dis = self._state == "disabled"
        for (value, _label), (cell, lab, bar) in zip(self._opts, self._cells):
            try:
                on = value == sel
                ink = t["dim"] if dis else (t["bright"] if on else t["value"])
                lab.configure(fg=ink)
                bar.configure(bg=(t["dim"] if dis else t["bright"]) if on else self._ground)
                cell.configure(takefocus=0 if dis else 1,
                               highlightcolor=self._ground if dis else t["bright"])
            except tk.TclError:
                pass


# ====================================================================== Well

class Well(tk.Frame):
    """Well(master, theme, ground=None) - an input well (approved shared_glyphs.input_well).

    Three nested frames, so focus never moves the content: the outer edge (1 px), an inner
    ring (1 px) and `body` (panel). Rest: dim edge + panel ring (1 px dim). Focus: both
    bright (2 px bright INSIDE the edge). Disabled: ghost edge. Put the Entry / Text in
    well.body and call well.attach(widget) so its focus drives the well."""

    def __init__(self, master, theme, ground=None, **kw):
        self._t = tokens(theme)
        self._focus = False
        self._disabled = False
        kw.setdefault("bd", 0)
        kw.setdefault("highlightthickness", 0)
        tk.Frame.__init__(self, master, bg=self._t["dim"], **kw)
        self._ring = tk.Frame(self, bg=self._t["panel"], bd=0, highlightthickness=0)
        self._ring.pack(fill="both", expand=True, padx=1, pady=1)
        self.body = tk.Frame(self._ring, bg=self._t["panel"], bd=0, highlightthickness=0)
        self.body.pack(fill="both", expand=True, padx=1, pady=1)
        self._attached = []

    def attach(self, widget):
        """Let widget's focus drive the well (FocusIn -> 2 px bright, FocusOut -> rest)."""
        widget.bind("<FocusIn>", lambda _e: self.set_focus(True), add="+")
        widget.bind("<FocusOut>", lambda _e: self.set_focus(False), add="+")
        self._attached.append(widget)
        return widget

    def set_focus(self, on):
        self._focus = bool(on)
        self._paint()

    def set_disabled(self, on):
        self._disabled = bool(on)
        self._paint()

    @property
    def focused(self):
        return self._focus

    def _paint(self):
        t = self._t
        if self._focus and not self._disabled:
            edge = ring = t["bright"]
        else:
            edge, ring = (t["ghost"] if self._disabled else t["dim"]), t["panel"]
        try:
            tk.Frame.configure(self, bg=edge)
            self._ring.configure(bg=ring)
        except tk.TclError:
            pass


# ====================================================================== SectionRow

class SectionRow(tk.Frame):
    """SectionRow(master, theme, title, tag=None, ground=None, upper=True)
    A settings section title (approved r2kit.section_row): the title C10B bright (upper-cased
    by default), then an optional exception tag in C9 value 12 px after it. No code, no rule,
    no box. .title_label / .tag_label are plain tk.Labels."""

    def __init__(self, master, theme, title, tag=None, ground=None, upper=True, **kw):
        t = tokens(theme)
        ground = ground or t["bg"]
        kw.setdefault("bg", ground)
        tk.Frame.__init__(self, master, **kw)
        text = title.upper() if (upper and isinstance(title, str)) else title
        self.title_label = tk.Label(self, text=text, font=C10B, bg=ground, fg=t["bright"], bd=0, padx=0, pady=0)
        self.title_label.pack(side="left")
        self.tag_label = None
        if tag:
            self.tag_label = tk.Label(self, text=tag, font=C9, bg=ground, fg=t["value"], bd=0, padx=0, pady=0)
            self.tag_label.pack(side="left", padx=(12, 0))


# ====================================================================== StatusLine

ELLIPSIS = "…"


# B1 fix F13: every line break Tk or Unicode knows (LF, CR, VT, FF, the C0 separators FS/GS/RS,
# NEL, LINE / PARAGRAPH SEPARATOR) becomes two spaces; every other C0 control and DEL one space.
_BREAKS = "\n\x0b\x0c\x1c\x1d\x1e\x85  "
_ONE_LINE = {ord(c): "  " for c in _BREAKS}
_ONE_LINE.update({c: " " for c in range(0x20) if chr(c) not in _BREAKS})
_ONE_LINE[0x7F] = " "


def one_line(text):
    """The single displayed line for text: every line break (CR/LF/CRLF, VT, FF, NEL, U+2028,
    U+2029, ...) becomes two spaces, a tab or any other control character one space."""
    if not isinstance(text, str):
        text = "" if text is None else str(text)
    s = text.replace("\r\n", "\n").replace("\r", "\n")
    return s.translate(_ONE_LINE)


def elide_middle(text, fits):
    """The longest 'head…tail' form of text for which fits(s) is true (text itself when it
    fits; ELLIPSIS alone when nothing does). fits must be monotone in length."""
    if fits(text):
        return text
    n = len(text)
    lo, hi = 0, n            # number of characters kept
    while lo < hi:
        mid = (lo + hi + 1) // 2
        head = (mid + 1) // 2
        cand = text[:head] + ELLIPSIS + text[n - (mid - head):] if mid - head > 0 else text[:head] + ELLIPSIS
        if fits(cand):
            lo = mid
        else:
            hi = mid - 1
    keep = lo
    if keep <= 0:
        return ELLIPSIS
    head = (keep + 1) // 2
    tail = keep - head
    return text[:head] + ELLIPSIS + (text[n - tail:] if tail > 0 else "")


_TIPS_ATTR = "_kit_open_tips"
_UNMAP_CMD_ATTR = "_kit_unmap_cmd"
TIP_GAP = 2              # line edge -> tip edge


def _tip_registry(top):
    """The set of StatusLines whose tip is up on toplevel `top`. The first use binds ONE
    <Unmap> handler on the toplevel (add="+", so no binding of the app's is replaced): every
    descendant carries the toplevel in its bindtags, so a screen hidden with pack_forget
    reports its <Unmap> there, and each open tip whose line is no longer viewable is removed
    (B1 fix F4; a hidden line never gets its <Leave>). No timer.
    Self-healing (B1 fix A3): a later plain top.bind("<Unmap>", f) by app code (no add="+")
    replaces the handler, so every call checks that the toplevel's <Unmap> script still runs
    the kit's command and re-adds it (add="+", the app's binding kept) when it does not."""
    reg = getattr(top, _TIPS_ATTR, None)
    if reg is None:
        reg = set()
        try:
            setattr(top, _TIPS_ATTR, reg)
        except AttributeError:
            return reg
    try:
        cmd = getattr(top, _UNMAP_CMD_ATTR, None)
        if cmd is None or cmd not in str(top.bind("<Unmap>")):
            new = top.bind("<Unmap>", lambda _e, r=reg: _drop_hidden_tips(r), add="+")
            setattr(top, _UNMAP_CMD_ATTR, new)
            if cmd is not None:                       # the replaced command: no binding runs it now
                try:
                    top.deletecommand(cmd)
                except (tk.TclError, ValueError):
                    pass
    except (tk.TclError, AttributeError):
        pass
    return reg


def _drop_hidden_tips(reg):
    for line in list(reg):
        try:
            gone = not line.winfo_exists() or not line.winfo_viewable()
        except tk.TclError:
            gone = True
        if gone:
            reg.discard(line)
            line._hide_tip()


class StatusLine(tk.Label):
    """StatusLine(master, theme=None, text="", max_chars=400, **label_kw)

    Shows ONE line: configure(text=) turns line breaks into two spaces and, when the line is
    wider than the label (once it has a real width) or longer than max_chars, elides it in the
    middle. cget("text") returns the ORIGINAL text, so nothing is lost; the full text is
    shown in a hover tip (a Label placed in the toplevel on <Enter> / <FocusIn>, removed on
    <Leave> / <FocusOut>; no timer, no window). .shown is the displayed string.
    The tip also goes when the line stops being viewable (its screen is pack_forget-ed: no
    <Leave> reaches a hidden line), through one <Unmap> hook on the toplevel (B1 fix F4), and
    whenever the line re-renders. textvariable= is traced through configure(text=) (F5), so a
    variable's multi-line value still shows as one line and cget("text") reads it.

    Width model: the label requests `width` characters (default 1), so its text never drives
    its size and re-eliding on <Configure> can never feed back into the layout. Pack it with
    fill="x" (or grid sticky="ew"): the layout decides the width, the text fits into it.

    B10: the real-wait cursor lives in the setter. When the text is a wait text (SENDING REQUEST,
    WAITING FOR RESPONSE - fx.motion.is_wait_text), however it arrives (.configure, .config,
    w["text"] = ..., a textvariable), the line's fx.motion.WaitCursor starts (the block cell after
    the drawn text; the text itself is never touched); any other text stops it at once. A writer
    that wants the cursor on another text (boot_fx.SignalAcquiring) starts the same one through
    .fx_wait_cursor, so a line never shows two. The last write or start / stop wins."""

    def __init__(self, master, theme=None, text="", max_chars=400, **kw):
        self._t = tokens(theme)
        self._full = "" if text is None else text
        self._max_chars = max(8, int(max_chars or 400))
        self._last_width = None
        self._tip = None
        self._wait = None
        self._tv = _TextVarLink(self)
        tvar = kw.pop("textvariable", None)
        kw.setdefault("width", 1)
        kw.setdefault("anchor", "w")
        tk.Label.__init__(self, master, text="", **kw)
        self._shown = ""
        if tvar:
            v = self._tv.attach(tvar)
            if v is not None:
                self._full = v
        self._render()
        self.bind("<Configure>", self._on_configure, add="+")
        for seq, fn in (("<Enter>", self._show_tip), ("<FocusIn>", self._show_tip),
                        ("<Leave>", self._on_leave), ("<FocusOut>", self._hide_tip)):
            self.bind(seq, fn, add="+")
        self._sync_wait()

    def configure(self, cnf=None, **kw):
        if isinstance(cnf, str):
            if cnf == "text":
                return ("text", "text", "Text", "", self._full)
            if cnf == "textvariable":
                return self._tv.describe()
            return tk.Label.configure(self, cnf)
        kw = _split_cnf(cnf, kw)
        if not kw:
            return tk.Label.configure(self)
        rerender = False
        text_set = False
        if "textvariable" in kw:
            v = self._tv.attach(kw.pop("textvariable"))
            if v is not None:
                kw["text"] = v
        if "text" in kw:
            v = kw.pop("text")
            self._full = "" if v is None else v
            rerender = True
            text_set = True
        if "max_chars" in kw:
            self._max_chars = max(8, int(kw.pop("max_chars") or 400))
            rerender = True
        if any(k in kw for k in ("font", "padx", "bd", "borderwidth", "highlightthickness")):
            rerender = True
        result = tk.Label.configure(self, **kw) if kw else None
        if rerender:
            self._render()          # an open tip follows the new text (A2), or goes when nothing is hidden
        if text_set:
            self._sync_wait()
        elif rerender and self._wait is not None:
            self._wait.refresh()    # a new font / padding moves the drawn text's end
        return result

    # ---------------------------------------------------------------- B10: the real-wait cursor
    @property
    def fx_wait_cursor(self):
        """This line's fx.motion.WaitCursor (made on first use), or None when fx/motion is unavailable."""
        if self._wait is None and _WaitCursor is not None:
            self._wait = _WaitCursor(None, self)
        return self._wait

    def _sync_wait(self):
        """A wait text starts the cursor (or re-places it after the new text); any other text stops it."""
        if _is_wait_text is None:
            return
        try:
            if _is_wait_text(self._full):
                wc = self.fx_wait_cursor
                if wc is not None:
                    wc.start()
            elif self._wait is not None and self._wait.running:
                self._wait.stop()
        except tk.TclError:
            pass

    def cget(self, key):
        if key == "text":
            return self._full
        if key == "max_chars":
            return self._max_chars
        if key == "textvariable":
            return self._tv.name
        return tk.Label.cget(self, key)

    config = configure
    __getitem__ = cget

    @property
    def shown(self):
        return self._shown

    @property
    def full_text(self):
        return self._full

    def is_elided(self):
        return self._shown != (self._full if isinstance(self._full, str) else str(self._full))

    # ---------------------------------------------------------------- rendering
    def _avail(self):
        try:
            w = self.winfo_width()
        except tk.TclError:
            return None
        if w <= 1:
            return None
        try:
            pad = int(float(tk.Label.cget(self, "padx"))) + int(float(tk.Label.cget(self, "bd"))) + int(
                float(tk.Label.cget(self, "highlightthickness")))
        except (tk.TclError, ValueError):
            pad = 0
        return max(1, w - 2 * pad)

    def _render(self):
        line = one_line(self._full)
        if len(line) > self._max_chars:
            line = elide_middle(line, lambda s: len(s) <= self._max_chars)
        avail = self._avail()
        if avail is not None:
            font = tk.Label.cget(self, "font")

            def fits(s):
                return int(self.tk.call("font", "measure", font, s)) <= avail
            line = elide_middle(line, fits)
            self._last_width = avail
        self._shown = line
        try:
            tk.Label.configure(self, text=line)
        except tk.TclError:
            pass
        if self._tip is not None:
            # F4: a re-render never leaves a stale tip up. A2: while the line is still elided
            # (a stream status written every tick under a resting pointer) the open tip is
            # updated in place and re-placed; it goes only when nothing is hidden any more.
            if self.is_elided() and self._viewable():
                try:
                    self._place_tip(self._tip)
                    _tip_registry(self._tip.winfo_toplevel()).add(self)      # A3: re-heal the hook
                except tk.TclError:
                    self._hide_tip()
            else:
                self._hide_tip()

    def _viewable(self):
        try:
            return bool(self.winfo_viewable())
        except tk.TclError:
            return False

    def _on_configure(self, _e=None):
        avail = self._avail()
        if avail is not None and avail != self._last_width:
            self._render()

    # ---------------------------------------------------------------- tip (no timer)
    def _show_tip(self, _e=None):
        if self._tip is not None or not self.is_elided():
            return
        tip = None
        try:
            top = self.winfo_toplevel()
            t = self._t
            tip = tk.Label(top, text="", justify="left", anchor="w", font=C9, bg=t["panel"],
                           fg=t["bright"], bd=0, padx=6, pady=4, highlightthickness=1,
                           highlightbackground=t["dim"])
            tip.bind("<Leave>", self._on_tip_leave, add="+")
            self._place_tip(tip)
            tip.lift()
            self._tip = tip
            _tip_registry(top).add(self)
        except tk.TclError:
            self._tip = None
            if tip is not None:
                try:
                    tip.destroy()
                except tk.TclError:
                    pass

    def _place_tip(self, tip):
        """Fill `tip` with the full text and place it beside the line, never over it (B1 fix A1).
        Below the line when it fits there, else above; when it fits on neither side the tip takes
        the larger side and its text is elided in the middle to that height, so the whole tip
        stays inside the window and never covers the line (a covering tip takes the pointer:
        <Leave> on the line, then <Enter> again, a flicker on every mouse move)."""
        top = tip.winfo_toplevel()
        top_w, top_h = top.winfo_width(), top.winfo_height()
        lx = self.winfo_rootx() - top.winfo_rootx()
        ly0 = self.winfo_rooty() - top.winfo_rooty()
        ly1 = ly0 + self.winfo_height()
        below = top_h - (ly1 + TIP_GAP)
        above = ly0 - TIP_GAP
        wrap = max(200, min(640, top_w - 40))
        full = str(self._full)
        # Bound the text first by what could ever show (a monospace line holds at most
        # wrap / char-width characters), so a huge text never builds a giant label.
        try:
            ls = max(1, int(self.tk.call("font", "metrics", C9, "-linespace")))
            cw = max(1, int(self.tk.call("font", "measure", C9, "0")))
        except tk.TclError:
            ls, cw = 14, 7
        cap = (max(above, below, ls) // ls + 1) * (wrap // cw + 1)
        if len(full) > cap:
            full = elide_middle(full, lambda s: len(s) <= cap)
        tip.configure(text=full, wraplength=wrap)
        th = tip.winfo_reqheight()
        if th <= below:
            y = ly1 + TIP_GAP
        else:
            room = max(above, below)
            if th > room:

                def fits(s):
                    tip.configure(text=s)
                    return tip.winfo_reqheight() <= room
                tip.configure(text=elide_middle(full, fits))
                th = tip.winfo_reqheight()
            y = ly1 + TIP_GAP if below >= above else ly0 - TIP_GAP - th
        tw = tip.winfo_reqwidth()
        tip.place(x=max(0, min(lx, top_w - tw)), y=y)

    @staticmethod
    def _over(w, e):
        """True when event e's root point lies on widget w (by geometry: winfo_containing cannot
        see through a layered / transparent window)."""
        if w is None or e is None:
            return False
        try:
            x, y = int(e.x_root), int(e.y_root)
            if not w.winfo_ismapped():
                return False
            rx, ry = w.winfo_rootx(), w.winfo_rooty()
            return rx <= x < rx + w.winfo_width() and ry <= y < ry + w.winfo_height()
        except (tk.TclError, TypeError, ValueError, AttributeError):
            return False

    def _on_leave(self, e=None):
        """<Leave> on the line hides the tip, unless the pointer went onto the tip itself
        (defensive, A1: the tip then hides on its own <Leave>)."""
        if self._over(self._tip, e):
            return
        self._hide_tip()

    def _on_tip_leave(self, e=None):
        if self._over(self, e):
            return                  # back on the line: the tip stays (no hide / show cycle)
        self._hide_tip()

    def _hide_tip(self, _e=None):
        tip, self._tip = self._tip, None
        if tip is not None:
            try:
                _tip_registry(tip.winfo_toplevel()).discard(self)
            except tk.TclError:
                pass
            try:
                tip.destroy()
            except tk.TclError:
                pass

    @property
    def tip(self):
        """The tip Label while it is shown, else None."""
        return self._tip

    def destroy(self):
        self._hide_tip()
        self._tv.detach()
        wc, self._wait = self._wait, None
        if wc is not None:
            try:
                wc.destroy()        # its cell is the master's child: it goes with the line
            except Exception:  # noqa: BLE001
                pass
        tk.Label.destroy(self)


# ====================================================================== EmptyState

class EmptyState(tk.Frame):
    """EmptyState(master, theme, title, caption=None, ground=None, font=C13B)

    The empty-screen statement (approved r2_chat_empty): the title in bright with a solid
    block-cursor mark right after it, and an optional one-line caption in value under it.
    The mark is a plain Frame (.mark); B10 animates it through fx without touching the
    screen that owns this widget (X-15). Static here: no timer.

    B10: the mark is the real-wait cursor of an empty transcript (AWAITING INPUT): .wait_cursor
    is an fx.motion.WaitCursor that adopts it, started while this widget is mapped and stopped
    when it is unmapped or destroyed, so its blink (fx.clock, CRT FULL only: B11's WAIT_BLINK)
    runs only while it shows. The mark itself always shows (state, not motion)."""

    def __init__(self, master, theme, title, caption=None, ground=None, font=C13B, **kw):
        t = tokens(theme)
        ground = ground or t["bg"]
        kw.setdefault("bg", ground)
        tk.Frame.__init__(self, master, **kw)
        ensure_fonts(self)
        row = tk.Frame(self, bg=ground)
        row.pack(side="top", anchor="w")
        self.title_label = tk.Label(row, text=title, font=font, bg=ground, fg=t["bright"], bd=0, padx=0, pady=0)
        self.title_label.pack(side="left")
        try:
            ls = int(self.tk.call("font", "metrics", font, "-linespace"))
            cw = int(self.tk.call("font", "measure", font, "M"))
        except tk.TclError:
            ls, cw = 16, 9
        self.mark = tk.Frame(row, bg=t["bright"], width=max(5, cw), height=max(10, int(round(ls * 0.85))),
                             bd=0, highlightthickness=0)
        self.mark.pack(side="left", padx=(2, 0))
        self.caption_label = None
        if caption:
            self.caption_label = tk.Label(self, text=caption, font=C9, bg=ground, fg=t["value"], bd=0, padx=0,
                                          pady=0, justify="left", anchor="w")
            self.caption_label.pack(side="top", anchor="w", pady=(6, 0))
        self.wait_cursor = None
        if _WaitCursor is not None:
            try:
                self.wait_cursor = _WaitCursor(None, self.title_label, cell=self.mark)
                self.bind("<Map>", self._on_map_wait, add="+")
                self.bind("<Unmap>", self._on_unmap_wait, add="+")
            except Exception:  # noqa: BLE001 - the mark stays a steady block
                self.wait_cursor = None

    def _on_map_wait(self, e=None):
        if self.wait_cursor is not None and (e is None or e.widget is self):
            self.wait_cursor.start()

    def _on_unmap_wait(self, e=None):
        if self.wait_cursor is not None and (e is None or e.widget is self):
            self.wait_cursor.stop()

    def destroy(self):
        wc, self.wait_cursor = getattr(self, "wait_cursor", None), None
        if wc is not None:
            try:
                wc.stop()
            except Exception:  # noqa: BLE001
                pass
        tk.Frame.destroy(self)

    def set_title(self, text):
        self.title_label.configure(text=text)
