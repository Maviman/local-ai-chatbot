"""kit/flag.py - StatusFlag, the approved bi-hexagon status flag (BUILD_PLAN §1.3).

A tk.Canvas that stands in for the Home status chips (tk.Label today). Its text API is the
chip's: configure(text=) / cget("text") round-trip the SEMANTIC word exactly as written
("Loading...", "Ready", "Offline", "Error", "Online", "Linked", "Off", "..."), which keeps
test_home_status and test_ui_journey 1b reading the same words. The flag DRAWS the word
upper-cased (a busy word loses its trailing dots: the 5x10 block cursor replaces them).

Look (approved r3_home.status_flag, 20 px tall, Consolas 9):
    on     1 px value outline, no fill; key bright; word ok for READY, value otherwise
    busy   1 px dim outline; key + word value + the 5x10 value block cursor
    off    1 px dim outline; all text dim (also "pending" and unknown words)
    fault  danger fill, bg text (the only filled flag)
The state follows the word (WORD_STATES); flag_state= forces one. bg= / fg= (and the other
Label-only options a chip call passes: font, padx, pady, relief) are accepted and swallowed:
the flag derives every ink from its state. ground= sets the colour it sits on. Reading one of
those Label options back answers like the chip did (B1 fix F14): fg / foreground = the word's
drawn ink, font = FLAG_FONT, padx / pady = 0, relief = flat, ...
Geometry (FLAG_H, paddings, the cursor) grows with the display ratio (kit.geom_ratio, 1.0 at
100 %), because the font is in points (B1 fix F11): at 200 % the word still sits inside the flag.

B10 (motion, inside the setter, so the screens stay untouched): configure(text=) - through .configure,
.config, w["text"] = ... or a textvariable alike - sets the semantic word (cget) and draws the final
state at once; then fx.motion animates only the drawing: a word replacing a pending flag ('...', '')
prints in (motion.print_value, first arrival only), a busy flag turning on (Loading... -> Ready) gets
the 110 ms READY colour settle (motion.latch). The same word again, or any other change, is drawn
plainly. Under NullFX (the kill switch) nothing moves. The flag is a motion "typeable": fx_type_text /
fx_type_show / fx_type_gen / fx_ink (see fx/motion.py); its print-in cursor is the approved 5 x 10 block,
drawn (tag "typecur"), not a glyph. The flag's width is the whole word's from the first frame.
"""

import tkinter as tk

from . import C9, geom_ratio, tokens
from .painters import flag_poly
from .widgets import _TextVarLink

FLAG_H = 20
FLAG_FONT = C9
STATES = ("on", "busy", "off", "pending", "fault")
WORD_STATES = {
    "ready": "on", "online": "on", "linked": "on", "on": "on", "ok": "on",
    "loading": "busy", "starting": "busy", "busy": "busy",
    "offline": "off", "off": "off", "down": "off",
    "error": "fault", "fault": "fault", "failed": "fault",
    "pending": "pending", "": "pending",
}
OK_WORDS = ("ready",)            # the ONE positive cue: only READY's word is green
_SWALLOWED = ("bg", "background", "fg", "foreground", "font", "padx", "pady", "relief", "anchor",
              "justify", "wraplength", "activebackground", "activeforeground")


# What a Label chip answered for its Label-only options (F14); bg / background stay the canvas's.
_LABEL_READS = {
    "fg": lambda f: f._word_ink or "", "foreground": lambda f: f._word_ink or "",
    "font": lambda f: FLAG_FONT, "padx": lambda f: 0, "pady": lambda f: 0, "relief": lambda f: "flat",
    "anchor": lambda f: "center", "justify": lambda f: "left", "wraplength": lambda f: 0,
    "activebackground": lambda f: f._ground, "activeforeground": lambda f: f._word_ink or "",
}


_DOTS = ".…"           # ASCII dots and U+2026 (B1 fix A7)


def _motion_of(widget):
    """fx.get(widget).motion (the no-op namespace under NullFX), or None. Never raises."""
    try:
        from .. import fx as _fx
        return _fx.get(widget).motion
    except Exception:  # noqa: BLE001
        return None


def _first_word(key):
    """'error: cuda out of memory' -> 'error' (split on ':' and whitespace)."""
    parts = key.replace(":", " ").split()
    return parts[0].strip(_DOTS) if parts else ""


def state_for(word):
    """The flag state a semantic word maps to; unknown -> 'off'. B1 fix A7: trailing '.' and
    '…' are both stripped ('Loading…' -> busy); otherwise the FIRST word decides
    ('Error: CUDA out of memory' -> fault, 'Ready (cached)' -> on); any other word that ends
    in '...' / '…' is busy ('Testing...', 'Uploading…'). '...' alone stays pending."""
    if not isinstance(word, str):
        return "off"
    raw = " ".join(word.split())
    key = raw.rstrip(_DOTS).strip().lower()
    if key in WORD_STATES:
        return WORD_STATES[key]
    first = _first_word(key)
    if first in WORD_STATES and first:
        return WORD_STATES[first]
    if key and (raw.endswith("...") or raw.endswith("…")):
        return "busy"
    return "off"


def shown_word(word, state):
    """What the flag draws for `word`: upper-cased; a busy word loses its trailing dots
    ('.' or '…': the block cursor stands for them)."""
    s = "" if word is None else str(word)
    s = " ".join(s.split()).upper()
    if state == "busy":
        s = s.rstrip(_DOTS).rstrip()
    return s


def _norm_key(key):
    """The key as one line (B1 fix A15: 'MO\\nDEL' -> 'MO DEL', never two lines out of the flag)."""
    return "" if key is None else " ".join(str(key).split())


class StatusFlag(tk.Canvas):
    """StatusFlag(parent, theme, key="MODEL", text="...", ground=None, flag_state=None, **canvas_kw)

    textvariable= (B1 fix A6) works as on the other kit text classes: it is never handed to
    the canvas, it is traced and each write goes through configure(text=); cget("textvariable")
    reads the name back, "" detaches, destroy() removes the trace."""

    def __init__(self, master, theme, key="", text="...", ground=None, flag_state=None, **kw):
        self._theme = theme
        self._t = tokens(theme)
        self._key = _norm_key(key)
        self._word = "" if text is None else str(text)
        self._forced = flag_state if flag_state in STATES else None
        self._word_ink = None
        self._type_k = None          # B10: the printed-in frame (first k characters) while a print-in runs
        self._type_cursor = False
        self._type_gen = 0
        self._tv = _TextVarLink(self)
        tvar = kw.pop("textvariable", None)
        for name in _SWALLOWED:
            kw.pop(name, None)
        self._ground = ground or self._t["bg"]
        kw.setdefault("highlightthickness", 0)
        kw.setdefault("bd", 0)
        tk.Canvas.__init__(self, master, bg=self._ground, height=FLAG_H + 1, width=1, **kw)
        if tvar:
            v = self._tv.attach(tvar)
            if v is not None:
                self._word = str(v)
        self._redraw()

    def destroy(self):
        self._tv.detach()
        tk.Canvas.destroy(self)

    # ---------------------------------------------------------------- semantic API
    @property
    def state_kind(self):
        """The flag's drawn state: on | busy | off | pending | fault."""
        return self._forced or state_for(self._word)

    @property
    def key(self):
        return self._key

    def configure(self, cnf=None, **kw):
        if isinstance(cnf, str):                      # w.configure("text") -> option description
            if cnf == "text":
                return ("text", "text", "Text", "", self._word)
            if cnf == "textvariable":
                return self._tv.describe()
            if cnf in _LABEL_READS and cnf not in ("bg", "background"):
                return (cnf, cnf, cnf.capitalize(), "", self.cget(cnf))
            return tk.Canvas.configure(self, cnf)
        if cnf:
            kw = dict(cnf, **kw)
        if not kw:
            return tk.Canvas.configure(self)
        redraw = False
        changed = None
        if "textvariable" in kw:                       # A6: traced, never handed to the canvas
            v = self._tv.attach(kw.pop("textvariable"))
            if v is not None:
                kw["text"] = v
        if "text" in kw:
            v = kw.pop("text")
            new = "" if v is None else str(v)
            if new != self._word:
                changed = (self._word, self.state_kind)       # B10: what showed before, for fx.motion
                self._type_gen += 1
                self._type_k = None
            self._word = new
            redraw = True
        if "key" in kw:
            self._key = _norm_key(kw.pop("key"))
            redraw = True
        if "flag_state" in kw:
            v = kw.pop("flag_state")
            self._forced = v if v in STATES else None
            redraw = True
        if "ground" in kw:
            self._ground = kw.pop("ground") or self._t["bg"]
            kw["bg"] = self._ground
            redraw = True
        for name in _SWALLOWED:
            if name in ("bg", "background") and kw.get(name) == self._ground:
                continue
            kw.pop(name, None)
        result = tk.Canvas.configure(self, **kw) if kw else None
        if redraw:
            self._redraw()
        if changed is not None:
            self._fx_word_changed(*changed)
        return result

    # ---------------------------------------------------------------- B10: motion
    def _fx_word_changed(self, prev_word, prev_state):
        """The final state is drawn; fx.motion animates the drawing only (the module docstring)."""
        m = _motion_of(self)
        if m is None:
            return
        new_state = self.state_kind
        try:
            if prev_state == "pending" and new_state != "pending":
                m.print_value(self, was=shown_word(prev_word, prev_state))
            elif prev_state == "busy" and new_state == "on":
                m.latch(self, "fg")
        except Exception:  # noqa: BLE001 - motion never breaks a flag write
            pass

    def fx_type_text(self):
        """The word as drawn when nothing types (upper-cased; a busy word without its dots)."""
        return shown_word(self._word, self.state_kind)

    def fx_type_gen(self):
        return self._type_gen

    def fx_type_show(self, k, cursor=False):
        """Draw the first k characters of the word (+ the block cursor); k None = the whole word."""
        word = self.fx_type_text()
        if k is None or k >= len(word):
            self._type_k, self._type_cursor = None, False
        else:
            self._type_k, self._type_cursor = max(0, int(k)), bool(cursor)
        try:
            self.itemconfigure("word", text=self._drawn_word(word))
        except tk.TclError:
            pass
        self._draw_typecur()

    def _draw_typecur(self):
        """The print-in's block cursor while it types: the approved 5 x 10 block (the busy flag's own cursor), 2 px
        after the typed characters, in the word's ink. A busy flag's own cursor waits at the word's end until the
        print-in is done (one block at a time)."""
        try:
            self.delete("typecur")
            typing = self._type_k is not None and self._type_cursor
            self.itemconfigure("cursor", state="hidden" if typing else "normal")
            if not typing:
                return
            items = self.find_withtag("word")
            bb = self.bbox(items[0]) if items else None
            if not bb:
                return
            r = geom_ratio(self)

            def s(n):
                return int(round(n * r))
            x0 = (bb[2] - 1 if self.itemcget(items[0], "text") else bb[0] + 1) + s(2)
            y0 = int(round(s(FLAG_H) / 2.0 - s(5)))
            self.create_rectangle(x0, y0, x0 + s(5), y0 + s(10), fill=self.itemcget(items[0], "fill"), outline="",
                                  tags=("flag", "typecur"))
        except tk.TclError:
            pass

    @property
    def fx_ink(self):
        """(getter, setter) of the word's ink: the READY settle (motion.latch(flag, "fg"))."""
        def get():
            try:
                items = self.find_withtag("word")
                return self.itemcget(items[0], "fill") if items else ""
            except tk.TclError:
                return ""

        def put(c):
            self.itemconfigure("word", fill=c)
        return (get, put)

    def _drawn_word(self, word):
        """The word as drawn now: its first k characters while a print-in runs (the cursor is a drawn block)."""
        if self._type_k is None:
            return word
        return word[:self._type_k]

    def cget(self, key):
        if key == "text":
            return self._word
        if key == "key":
            return self._key
        if key == "textvariable":
            return self._tv.name
        if key == "flag_state":
            return self.state_kind
        if key in _LABEL_READS and key not in ("bg", "background"):
            return _LABEL_READS[key](self)
        return tk.Canvas.cget(self, key)

    # Override rule (X-1): Misc binds these at class-definition time.
    config = configure
    __getitem__ = cget

    # ---------------------------------------------------------------- drawing
    def _measure(self, text):
        return int(self.tk.call("font", "measure", FLAG_FONT, text))

    def _redraw(self):
        t = self._t
        st = self.state_kind
        word = shown_word(self._word, st)
        key = self._key.upper()
        r = geom_ratio(self)

        def s(n):
            return int(round(n * r))
        fh = s(FLAG_H)
        gap = self._measure("  ") if key and word else 0
        kw_, ww = self._measure(key), self._measure(word)
        p = fh // 2
        cur = s(7) if st == "busy" else 0
        w = p + s(6) + kw_ + gap + ww + cur + s(6) + p
        self.delete("flag")
        tk.Canvas.configure(self, width=w + 1, height=fh + 1)
        cy = fh / 2.0
        poly = flag_poly(0, 0, w, fh, "bi")
        if st == "on":
            wkey = " ".join(self._word.split()).rstrip(_DOTS).strip().lower()
            ok = wkey in OK_WORDS or (wkey not in WORD_STATES and _first_word(wkey) in OK_WORDS)
            word_ink = t["ok"] if ok else t["value"]
            oc, fl, kink, wink = t["value"], "", t["bright"], word_ink
        elif st == "fault":
            oc, fl, kink, wink = t["danger"], t["danger"], t["bg"], t["bg"]
        elif st == "busy":
            oc, fl, kink, wink = t["dim"], "", t["value"], t["value"]
        else:
            oc, fl, kink, wink = t["dim"], "", t["dim"], t["dim"]
        self.create_polygon(poly, fill=fl, outline=oc, tags=("flag", "outline"))
        tx = p + s(6)
        if key:
            self.create_text(tx, cy, text=key, anchor="w", fill=kink, font=FLAG_FONT, tags=("flag", "key"))
        self.create_text(tx + kw_ + gap, cy, text=self._drawn_word(word), anchor="w", fill=wink, font=FLAG_FONT,
                         tags=("flag", "word"))
        if st == "busy":
            bx = tx + kw_ + gap + ww + s(2)
            y0 = int(round(cy - s(5)))
            self.create_rectangle(bx, y0, bx + s(5), y0 + s(10), fill=t["value"], outline="",
                                  tags=("flag", "cursor"))
        self._word_ink = wink
        self._width = w
        if self._type_k is not None:
            self._draw_typecur()        # B10: a redraw mid-print keeps the typed frame and its block
        return w
