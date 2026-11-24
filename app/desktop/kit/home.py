"""kit/home.py - the r3 Home pieces (BUILD_PLAN §4 B4, D-18; geometry from the approved renderer
r3/r3_home.py, see design archive: draw_label_in_gap_rule, draw_bar_mark, record_row, option_box).

    GapRule(master, theme, label)               a 1 px dim rule with the label (C8 value, upper-cased)
                                                knocked into it 12 px from the left (SYSTEM STATUS,
                                                QUICK RESUME). 18 px tall, the rule 8 px down.
    BarMark(master, theme)                      the static bar-built mark beside the status card
                                                (76 px tall, 4 px below the canvas top)
    RecordRow(master, theme, key, text="", max_lines=None)
                                                KEY . . . . VALUE: key C9 value, dim C9 dot leaders
                                                from 62 px, value C9B bright right-aligned at the
                                                row's right edge, one 20 px row
    OptionCell(parent, theme, number, title, caption, ...)
                                                one enlarged Home directory option: an index canvas
                                                (cell.index) and the box (cell.box, a Canvas) holding
                                                the title and caption Labels (cell.title / .caption)

RecordRow: a value that does not fit beside its key (it would come closer than 24 px to the key, the
renderer's "crowds its key" rule) WRAPS from the leader column onto as many lines as it needs, and the
row grows by one line height per extra line; a word longer than the line is broken at characters.
max_lines=None (the default) never shortens the value. max_lines=N (B4 fix ADV1-01: Home bounds its
System status card so the option grid and the action column below it keep their room) wraps onto at
most N lines and elides the rest in the MIDDLE (head and tail kept); nothing is lost (D-27 #14): the
whole value is in cget("text") and in a hover tip (the kit.StatusLine tip: placed in the toplevel, no
timer, removed on <Leave> and when the row stops being viewable). .elided says whether it is cut.
textvariable= is traced through configure(text=). Override rule (X-1): config / __getitem__ re-bound.
B10 (D-24 "values printing in", the lab's power-on): a value that replaces a placeholder (Home's
'checking...' / '-', PENDING, LOADING, an empty slot: fx.motion.is_pending) prints into its final slot
through fx.motion.print_value - cget at once, only the drawing types (the row is a motion "typeable":
fx_type_text / fx_type_show / fx_type_gen / fx_ink). A value replacing a value, a wrapped value and the
kill switch draw it whole at once.

OptionCell: the title (Consolas 20 bold) and caption (Consolas 12) are real Labels placed in the box
(so a layout test can see them), 20 px in from its left edge, the pair centred vertically; the
caption wraps inside the box (never cut), and the gap between them shrinks before anything can
leave a short box. .text_need(width) is the box height that shows both whole at that box width
(Home sizes its grid with it, B4 fix ADV1-03). The index 'NN' (Consolas 16 bold, value) is drawn on cell.index, right-aligned
12 px left of the box on the title's row, with the 10 px dim pin stub. The box fills whatever its
grid cell gives it (it requests only MIN_BOX_H tall and 1 px wide). States:
    primary   bright fill, title + caption in bg
    other     panel fill, 1 px dim edge; pointer hover 1 px bright; keyboard focus 2 px bright
    focus     + the index as an inverse chip (bright fill, bg digits). On the PRIMARY box (already
              bright) focus also draws a 2 px bg ring 2 px inside the edge, so focus is visible on
              the box itself there too (it replaces the dashed ring, which was invisible on the
              primary box: the harness dir_focus finding).
Every pixel constant is in approved pixels at 100 % and grows with the display ratio
(kit.geom_ratio); the fonts are fixed point sizes.

Static: no timers (no after()), no I/O, Tk thread only.
"""

import tkinter as tk

from . import C8, C9, C9B, geom_ratio, tokens
from .painters import dot_leader_count, draw_bar_mark, draw_label_in_gap_rule
from .widgets import StatusLine as _StatusLine
from .widgets import _split_cnf, _TextVarLink, elide_middle, one_line

try:        # B10: the placeholder words of a value print-in (fx/motion.py)
    from ..fx.motion import is_pending as _is_pending
except Exception:  # noqa: BLE001
    _is_pending = None

__all__ = ["GapRule", "BarMark", "RecordRow", "OptionCell", "TILE_TITLE", "TILE_CAP", "TILE_INDEX",
           "MIN_BOX_H", "INDEX_COL_FIRST", "INDEX_COL"]

# ---- card (approved r3_home: rule at window y 104, records at y 124 + 20 i, bar mark at 62,118, h 76)
GAP_RULE_H = 18          # the rule row (window y 96..114)
GAP_RULE_Y = 8           # the rule (window y 104)
ROW_H = 20               # one record row
LEAD_DX = 62             # the leaders start 62 px right of the key's left edge
CROWD_GAP = 24           # a value closer than this to its key wraps instead
LEAD_PAD = 8             # the leaders stop 8 px before the value
KEY_FONT = C9
VALUE_FONT = C9B
BAR_H = 76
BAR_TOP = 4

# ---- the enlarged option box
TILE_TITLE = ("Consolas", 20, "bold")
TILE_CAP = ("Consolas", 12)
TILE_INDEX = ("Consolas", 16, "bold")
MIN_BOX_H = 62           # the box row's floor (B3h hand-off: every box at least 62 px tall)
BOX_TEXT_X = 20          # title / caption left inset; the same room is kept free on the right
NARROW_BOX_W = 270       # below this width (the 820 px window floor gives 241) the insets tighten
NARROW_TEXT_X = 12       # ... to 12 px on the left
NARROW_RIGHT = 8         # ... and 8 px on the right, so a caption needs one line fewer
TEXT_GAP = 8             # title -> caption (approved: centres 34 px apart)
INDEX_DX = 12            # the index's right edge = box left - 12
PIN_W = 10               # the dim pin stub: box left - 10 .. box left
CHIP_PAD = 4
FOCUS_W = 2
PRIMARY_RING_INSET = 2   # the primary box's focus: bright edge, a 2 px bg ring 2 px in, then the fill
INDEX_COL_FIRST = 54     # the first column's index gutter (window x 58..112)
INDEX_COL = 53           # the gutter between two columns, holding the next column's index (472..525)


def _measure(widget, font, text):
    try:
        return int(widget.tk.call("font", "measure", font, text))
    except tk.TclError:
        return 7 * len(text)


def _linespace(widget, font):
    try:
        return max(1, int(widget.tk.call("font", "metrics", font, "-linespace")))
    except tk.TclError:
        return 14


def _fill(cv, x0, y0, x1, y1, col, tag):
    """Filled pixels [x0, x1) x [y0, y1)."""
    if x1 > x0 and y1 > y0:
        cv.create_rectangle(x0, y0, x1, y1, fill=col, outline="", tags=tag)


def _edge(cv, x0, y0, x1, y1, col, w, tag):
    """A w px edge drawn INSIDE [x0, x1) x [y0, y1) (exact pixels, as the renderer's _box)."""
    _fill(cv, x0, y0, x1, y0 + w, col, tag)
    _fill(cv, x0, y1 - w, x1, y1, col, tag)
    _fill(cv, x0, y0, x0 + w, y1, col, tag)
    _fill(cv, x1 - w, y0, x1, y1, col, tag)


# ====================================================================== GapRule

class GapRule(tk.Canvas):
    """GapRule(master, theme, label="", ground=None): the full-width structure rule with the label
    knocked into it (fx/draw.py draw_label_in_gap_rule). Redrawn on <Configure>. .label is the text
    as given; the canvas draws it upper-cased."""

    def __init__(self, master, theme, label="", ground=None, **kw):
        self._t = tokens(theme)
        self._label = "" if label is None else str(label)
        r = geom_ratio(master)
        self._rule_y = int(round(GAP_RULE_Y * r))
        kw.setdefault("highlightthickness", 0)
        kw.setdefault("bd", 0)
        tk.Canvas.__init__(self, master, bg=ground or self._t["bg"], height=int(round(GAP_RULE_H * r)), width=1,
                           **kw)
        self.bind("<Configure>", lambda _e: self._redraw(), add="+")
        self._redraw()

    @property
    def label(self):
        return self._label

    def _redraw(self):
        try:
            self.delete("gap")
            w = max(self.winfo_width(), 2)
            draw_label_in_gap_rule(self, self._t, 0, w, self._rule_y, " ".join(self._label.split()).upper(),
                                   font=C8, tag="gap")
        except tk.TclError:
            pass


# ====================================================================== BarMark

class BarMark(tk.Canvas):
    """BarMark(master, theme, ground=None): the static bar-built mark (fx/draw.py draw_bar_mark,
    bright), BAR_TOP px below the canvas top. .mark_width is its width in px."""

    def __init__(self, master, theme, ground=None, **kw):
        t = tokens(theme)
        r = geom_ratio(master)
        h = int(round(BAR_H * r))
        top = int(round(BAR_TOP * r))
        kw.setdefault("highlightthickness", 0)
        kw.setdefault("bd", 0)
        tk.Canvas.__init__(self, master, bg=ground or t["bg"], height=h + top, width=1, **kw)
        _ids, bw = draw_bar_mark(self, 0, top, h, t["bright"], tag="bar")
        self.mark_width = int(bw)
        tk.Canvas.configure(self, width=self.mark_width)


# ====================================================================== RecordRow

class RecordRow(tk.Canvas):
    """RecordRow(master, theme, key="", text="", ground=None, max_lines=None, **canvas_kw):
    KEY . . . . VALUE (see the module docstring). Pack / grid it with fill="x": the layout decides
    the width. .wrapped, .lines and .line_height read back what is drawn; .shown is the drawn value
    (one paragraph; the whole value unless max_lines elided it: .elided)."""

    def __init__(self, master, theme, key="", text="", ground=None, max_lines=None, **kw):
        self._t = tokens(theme)
        self._max_lines = self._norm_lines(max_lines)
        self._tip = None
        self._key = "" if key is None else " ".join(str(key).split())
        self._full = "" if text is None else text
        self._ground = ground or self._t["bg"]
        self._tv = _TextVarLink(self)
        tvar = kw.pop("textvariable", None)
        kw.setdefault("highlightthickness", 0)
        kw.setdefault("bd", 0)
        r = geom_ratio(master)
        self._r = r
        self._row_h = int(round(ROW_H * r))
        tk.Canvas.__init__(self, master, bg=self._ground, height=self._row_h, width=1, **kw)
        self.line_height = _linespace(self, VALUE_FONT)
        self.wrapped = False
        self.lines = 1
        self.shown = ""
        self.elided = False
        self._last_w = None
        self._type_k = None          # B10: the printed-in frame (first k characters) while a print-in runs
        self._type_cursor = False
        self._type_gen = 0
        self._vleft = None           # B10: the one-line value's slot (its left edge), None while it wraps
        self._vright = None
        if tvar:
            v = self._tv.attach(tvar)
            if v is not None:
                self._full = v
        self.bind("<Configure>", self._on_configure, add="+")
        self.bind("<Enter>", self._show_tip, add="+")
        self.bind("<Leave>", self._on_leave, add="+")
        self._redraw()

    def _s(self, n):
        return int(round(n * self._r))

    @staticmethod
    def _norm_lines(v):
        if v in (None, "", 0, "0"):
            return None
        try:
            return max(1, int(v))
        except (TypeError, ValueError):
            return None

    def destroy(self):
        self._hide_tip()
        self._tv.detach()
        tk.Canvas.destroy(self)

    # ---------------------------------------------------------------- the hover tip (kit.StatusLine's)
    def is_elided(self):
        return self.elided

    _show_tip = _StatusLine._show_tip
    _place_tip = _StatusLine._place_tip
    _over = staticmethod(_StatusLine._over)
    _on_leave = _StatusLine._on_leave
    _on_tip_leave = _StatusLine._on_tip_leave
    _hide_tip = _StatusLine._hide_tip

    @property
    def tip(self):
        """The tip Label while it is shown, else None."""
        return self._tip

    def _sync_tip(self):
        """After a repaint: an open tip follows the new value while it is still elided, else it goes."""
        if self._tip is None:
            return
        try:
            viewable = bool(self.winfo_viewable())
        except tk.TclError:
            viewable = False
        if self.elided and viewable:
            try:
                self._place_tip(self._tip)
            except tk.TclError:
                self._hide_tip()
        else:
            self._hide_tip()

    # ---------------------------------------------------------------- Override rule
    def configure(self, cnf=None, **kw):
        if isinstance(cnf, str):
            if cnf == "text":
                return ("text", "text", "Text", "", self._full)
            if cnf == "textvariable":
                return self._tv.describe()
            if cnf == "key":
                return ("key", "key", "Key", "", self._key)
            if cnf == "max_lines":
                return ("max_lines", "max_lines", "MaxLines", "", self._max_lines)
            return tk.Canvas.configure(self, cnf)
        kw = _split_cnf(cnf, kw)
        if not kw:
            return tk.Canvas.configure(self)
        redraw = False
        changed = None
        if "textvariable" in kw:
            v = self._tv.attach(kw.pop("textvariable"))
            if v is not None:
                kw["text"] = v
        if "text" in kw:
            v = kw.pop("text")
            new = "" if v is None else v
            if new != self._full:
                changed = self._full     # B10: what showed before (a placeholder -> the value prints in)
                self._type_gen += 1
                self._type_k = None
            self._full = new
            redraw = True
        if "key" in kw:
            v = kw.pop("key")
            self._key = "" if v is None else " ".join(str(v).split())
            redraw = True
        if "max_lines" in kw:
            v = self._norm_lines(kw.pop("max_lines"))
            if v != self._max_lines:
                self._max_lines = v
                redraw = True
        if "ground" in kw:
            self._ground = kw.pop("ground") or self._t["bg"]
            kw["bg"] = self._ground
        result = tk.Canvas.configure(self, **kw) if kw else None
        if redraw:
            self._redraw()
        if changed is not None:
            self._fx_value_changed(changed)
        return result

    def cget(self, key):
        if key == "text":
            return self._full
        if key == "textvariable":
            return self._tv.name
        if key == "key":
            return self._key
        if key == "max_lines":
            return self._max_lines
        return tk.Canvas.cget(self, key)

    config = configure
    __getitem__ = cget

    # ---------------------------------------------------------------- B10: the value print-in
    # The value is drawn whole first (cget at once); then fx.motion animates only the drawing: a value
    # replacing a placeholder (checking..., -, PENDING, LOADING, ...: fx.motion.is_pending) prints into its
    # final slot, left to right behind the approved 5 x 10 block, max(3, ceil(n / 6)) characters a frame
    # (the lab's power-on: the SYSTEM STATUS values arrive). Never on a revisit (a value replacing a value),
    # never while the value wraps (it is drawn whole), never under the kill switch. No timer here.
    def _fx_value_changed(self, prev):
        try:
            from .. import fx as _fx
            if _is_pending is not None and _is_pending(self.shown):
                return                   # a placeholder replacing a placeholder ('checking...' -> '-'): drawn plainly
            _fx.get(self).motion.print_value(self, was=one_line(prev if isinstance(prev, str) else str(prev)))
        except Exception:  # noqa: BLE001 - motion never breaks a value write
            pass

    def fx_type_text(self):
        """The value as it prints in: the drawn one-line value; "" while it wraps (nothing types then)."""
        return "" if self.wrapped or self._vleft is None else self.shown

    def fx_type_gen(self):
        return self._type_gen

    def fx_type_show(self, k, cursor=False):
        """Draw the first k characters of the value in its final slot (+ the block); k None = the whole value."""
        if k is None or self._vleft is None or k >= len(self.shown):
            self._type_k, self._type_cursor = None, False
        else:
            self._type_k, self._type_cursor = max(0, int(k)), bool(cursor)
        self._draw_typed()

    @property
    def fx_ink(self):
        """(getter, setter) of the value's ink (the REDUCED colour settle)."""
        def get():
            try:
                items = self.find_withtag("value")
                return self.itemcget(items[0], "fill") if items else ""
            except tk.TclError:
                return ""

        def put(c):
            self.itemconfigure("value", fill=c)
        return (get, put)

    def _draw_typed(self):
        try:
            self.delete("typecur")
            items = self.find_withtag("value")
            if not items or self.wrapped or self._vleft is None:
                return
            item = items[0]
            cy = self._row_h / 2.0
            if self._type_k is None:
                self.itemconfigure(item, text=self.shown, anchor="e")
                self.coords(item, self._vright, cy)
                return
            self.itemconfigure(item, text=self.shown[:self._type_k], anchor="w")
            self.coords(item, self._vleft, cy)
            if self._type_cursor:
                bb = self.bbox(item)
                x0 = (bb[2] - 1 if bb and self.shown[:self._type_k] else self._vleft) + self._s(2)
                y0 = int(round(cy - self._s(5)))
                self.create_rectangle(x0, y0, x0 + self._s(5), y0 + self._s(10), fill=self.itemcget(item, "fill"),
                                      outline="", width=0, tags=("rec", "typecur"))
        except tk.TclError:
            pass

    @property
    def full_text(self):
        return self._full

    def _text(self):
        """The value as drawn: one paragraph (line breaks become two spaces), never shortened."""
        return one_line(self._full if isinstance(self._full, str) else str(self._full))

    # ---------------------------------------------------------------- drawing
    def _on_configure(self, _e=None):
        try:
            w = self.winfo_width()
        except tk.TclError:
            return
        if w != self._last_w:
            self._redraw()

    def _redraw(self):
        try:
            self._paint()
        except tk.TclError:
            pass

    def _paint(self):
        t = self._t
        self.delete("rec")
        w = self.winfo_width()
        self._last_w = w
        sized = w > 1                      # before the first <Configure> the row has no width yet: one line
        row_h = self._row_h
        cy = row_h / 2.0
        key = self._key.upper()
        self.create_text(0, cy, text=key, anchor="w", fill=t["value"], font=KEY_FONT, tags=("rec", "key"))
        kr = _measure(self, KEY_FONT, key)
        text = self._text()
        vw = _measure(self, VALUE_FONT, text)
        ls = self.line_height
        if sized and text and w - vw < kr + self._s(CROWD_GAP):
            # wraps from the leader column (or right after the key when the row is very narrow): never cut
            lead_x0 = self._s(LEAD_DX)
            x0 = lead_x0 if (lead_x0 > kr and w - lead_x0 >= self._s(40)) else min(kr + self._s(8), max(0, w - 8))
            room = max(8, w - x0)
            top = (row_h - ls) // 2
            item = self.create_text(x0, top, text=text, anchor="nw", width=room, justify="left",
                                    fill=t["bright"], font=VALUE_FONT, tags=("rec", "value"))
            bb = self.bbox(item)
            n = 1 if not bb else max(1, int(round((bb[3] - bb[1]) / float(ls))))
            if self._max_lines and n > self._max_lines:
                text, n = self._elide_to_lines(item, text, room, ls)
                bb = self.bbox(item)
            self.wrapped = True
            self.lines = n
            height = (bb[3] - bb[1] if bb else ls) + (row_h - ls)
            self._vleft = None          # B10: a wrapped value never prints in (drawn whole)
            self._type_k = None
        else:
            vid = self.create_text(w, cy, text=text, anchor="e", fill=t["bright"], font=VALUE_FONT,
                                   tags=("rec", "value"))
            bb = self.bbox(vid)
            vleft = bb[0] if bb else w - vw
            self._vleft = (bb[0] + 1) if bb else w - vw      # B10: the value's slot (text bboxes pad 1 px)
            self._vright = w
            lead_x0 = self._s(LEAD_DX)
            n = dot_leader_count(vleft - self._s(LEAD_PAD) - lead_x0, _measure(self, KEY_FONT, ". "))
            if n > 0:
                self.create_text(lead_x0, cy, text=". " * n, anchor="w", fill=t["dim"], font=KEY_FONT,
                                 tags=("rec", "leader"))
            self.wrapped = False
            self.lines = 1
            height = row_h
        self.shown = text
        self.elided = text != self._text()
        height = max(row_h, int(height))
        if int(float(tk.Canvas.cget(self, "height"))) != height:
            tk.Canvas.configure(self, height=height)
        if self._type_k is not None:
            self._draw_typed()          # B10: a repaint mid-print (resize) keeps the printed frame and its block
        self._sync_tip()

    def _elide_to_lines(self, item, text, room, ls):
        """Shorten `text` in the middle until the wrapped item takes at most max_lines lines; returns
        (shown text, lines). A monospace line holds at most room / char-width characters, so the text
        is first bounded by that (a 2 kB error body never needs a 2 kB measuring loop)."""
        cap_lines = self._max_lines
        cw = max(1, _measure(self, VALUE_FONT, "0"))
        cap = max(1, cap_lines * max(1, room // cw))
        if len(text) > cap:
            text = elide_middle(text, lambda s_: len(s_) <= cap)

        def lines_of(s_):
            self.itemconfigure(item, text=s_)
            b = self.bbox(item)
            return 1 if not b else max(1, int(round((b[3] - b[1]) / float(ls))))

        shown = elide_middle(text, lambda s_: lines_of(s_) <= cap_lines)
        n = lines_of(shown)
        return shown, n


# ====================================================================== OptionCell

class OptionCell:
    """OptionCell(parent, theme, number, title, caption="", primary=False, on_activate=None,
                  index_width=INDEX_COL_FIRST, ground=None)

    Not a widget: it makes two siblings in `parent` for the caller to grid side by side, the index
    canvas (cell.index, index_width px wide) and the box (cell.box, a focusable Canvas), and the
    title / caption Labels inside the box. Click (box or its labels), Return and space activate;
    the box takes the keyboard focus (Tab). .focused / .hover / .primary read the state."""

    def __init__(self, parent, theme, number, title, caption="", primary=False, on_activate=None,
                 index_width=INDEX_COL_FIRST, ground=None):
        t = self._t = tokens(theme)
        self._r = geom_ratio(parent)
        self.number = int(number)
        self.primary = bool(primary)
        self.focused = False
        self.hover = False
        self._on_activate = on_activate
        self._ground = ground or t["bg"]
        self._title_cy = None
        fill = t["bright"] if self.primary else t["panel"]
        ti, ci = (t["bg"], t["bg"]) if self.primary else (t["bright"], t["value"])
        self._fill_col = fill
        self.index = tk.Canvas(parent, width=self._s(index_width), height=1, bg=self._ground,
                               highlightthickness=0, bd=0)
        self.box = tk.Canvas(parent, width=1, height=self._s(MIN_BOX_H), bg=fill, highlightthickness=0, bd=0,
                             takefocus=1, cursor="hand2")
        self.title = tk.Label(self.box, text=title, font=TILE_TITLE, bg=fill, fg=ti, anchor="w", justify="left",
                              bd=0, padx=0, pady=0, highlightthickness=0, cursor="hand2")
        self.caption = tk.Label(self.box, text=caption or "", font=TILE_CAP, bg=fill, fg=ci, anchor="w",
                                justify="left", bd=0, padx=0, pady=0, highlightthickness=0, cursor="hand2")
        self.box.bind("<Configure>", self._layout, add="+")
        self.index.bind("<Configure>", lambda _e: self._paint_index(), add="+")
        self.box.bind("<FocusIn>", self._focus_in, add="+")
        self.box.bind("<FocusOut>", self._focus_out, add="+")
        self.box.bind("<Return>", self._activate, add="+")
        self.box.bind("<KP_Enter>", self._activate, add="+")
        self.box.bind("<space>", self._activate, add="+")
        self.box.bind("<Enter>", self._enter, add="+")
        self.box.bind("<Leave>", self._leave, add="+")
        for w in (self.box, self.title, self.caption):
            w.bind("<Button-1>", self._click, add="+")
        self._layout()

    def _s(self, n):
        return int(round(n * self._r))

    # ---------------------------------------------------------------- events
    def activate(self):
        if self._on_activate is not None:
            self._on_activate()

    def _activate(self, _e=None):
        self.activate()
        return "break"

    def _click(self, _e=None):
        try:
            self.box.focus_set()
        except tk.TclError:
            pass
        self.activate()

    def _focus_in(self, _e=None):
        self.focused = True
        self._paint()

    def _focus_out(self, _e=None):
        self.focused = False
        self._paint()

    def _enter(self, _e=None):
        if not self.hover:
            self.hover = True
            self._paint_box()

    def _leave(self, e=None):
        # moving onto the title / caption Label (a child) is still "on the box": that <Leave> comes with
        # the pointer still inside the box (tkinter's Event carries no `detail` to tell them apart)
        if e is not None:
            try:
                if 0 <= int(e.x) < self.box.winfo_width() and 0 <= int(e.y) < self.box.winfo_height():
                    return
            except (TypeError, ValueError, tk.TclError):
                pass
        if self.hover:
            self.hover = False
            self._paint_box()

    # ---------------------------------------------------------------- layout + paint
    def _layout(self, _e=None):
        try:
            box = self.box
            w, h = box.winfo_width(), box.winfo_height()
            if w <= 1 or h <= 1:
                w, h = max(w, box.winfo_reqwidth()), max(h, box.winfo_reqheight())
            # the labels never cover the focus marks (a child window always paints over canvas items):
            # they stay inside `zone`, the edge band the focus look draws in (the primary box's bg ring)
            zone = self._zone()
            tw = self.title.winfo_reqwidth()
            x, wrap = self._text_x_wrap(w)
            if str(self.caption.cget("wraplength")) != str(wrap):
                self.caption.configure(wraplength=wrap)
            th = self.title.winfo_reqheight()
            ch = self.caption.winfo_reqheight() if self.caption.cget("text") else 0
            gap = self._s(TEXT_GAP) if ch else 0
            room = h - 2 * zone
            if th + gap + ch > room:
                gap = max(0, room - th - ch)          # shrink the gap before the text can leave the box
            block = th + gap + ch
            top = max(zone, (h - block) // 2)
            fit_w = max(1, w - zone - x)
            # only a box smaller than its text (far below the 820x560 floor, or a large display scaling)
            # clips a label, and then inside the zone, so focus stays visible
            self.title.place(x=x, y=top, width=tw if tw <= fit_w else fit_w,
                             height=th if top + th <= h - zone else max(1, h - zone - top))
            cy = top + th + gap
            if ch and cy < h - zone:
                cw = self.caption.winfo_reqwidth()
                self.caption.place(x=x, y=cy, width=cw if cw <= fit_w else fit_w,
                                   height=ch if cy + ch <= h - zone else max(1, h - zone - cy))
            else:
                self.caption.place_forget()
            self._title_cy = top + min(th, max(1, h - zone - top)) / 2.0
        except tk.TclError:
            return
        self._paint()

    def _text_x_wrap(self, w):
        """(title / caption x, caption wraplength) in a box w px wide."""
        if w < self._s(NARROW_BOX_W):
            inset, right = self._s(NARROW_TEXT_X), self._s(NARROW_RIGHT)
        else:
            inset = right = self._s(BOX_TEXT_X)
        zone = self._zone()
        tw = self.title.winfo_reqwidth()
        # 20 px in from the left; a box too narrow for that keeps the title clear of its right edge
        x = inset if inset + tw <= w - zone else max(zone, w - zone - tw)
        return x, max(self._s(40), w - x - max(right, zone))

    def text_need(self, width):
        """The box height (px) that shows the title and the WHOLE caption, wrapped as it is at a box
        `width` px wide, inside the focus band (the title -> caption gap may shrink to 0 first, as
        _layout does). Measuring only: the caption's own wraplength is put back (_layout owns it)."""
        try:
            _x, wrap = self._text_x_wrap(max(1, int(width)))
            th = self.title.winfo_reqheight()
            ch = 0
            if self.caption.cget("text"):
                prev = self.caption.cget("wraplength")
                if str(prev) != str(wrap):
                    self.caption.configure(wraplength=wrap)
                    ch = self.caption.winfo_reqheight()
                    self.caption.configure(wraplength=prev)
                else:
                    ch = self.caption.winfo_reqheight()
            return th + ch + 2 * self._zone()
        except tk.TclError:
            return self._s(MIN_BOX_H)

    def _zone(self):
        """The edge band the focus look draws in: the 2 px edge, or on the primary box its bg ring."""
        if self.primary:
            return self._s(PRIMARY_RING_INSET + FOCUS_W) + 1
        return self._s(FOCUS_W) + 1

    def _paint(self):
        self._paint_box()
        self._paint_index()

    def _paint_box(self):
        cv, t = self.box, self._t
        try:
            cv.delete("box")
            w, h = cv.winfo_width(), cv.winfo_height()
            if w <= 1 or h <= 1:
                w, h = max(w, cv.winfo_reqwidth()), max(h, cv.winfo_reqheight())
            _fill(cv, 0, 0, w, h, self._fill_col, ("box", "fill"))
            fw = self._s(FOCUS_W)
            if self.primary:
                if self.focused:
                    i = self._s(PRIMARY_RING_INSET)
                    _edge(cv, i, i, w - i, h - i, t["bg"], fw, ("box", "focus"))
            elif self.focused:
                _edge(cv, 0, 0, w, h, t["bright"], fw, ("box", "focus"))
            else:
                _edge(cv, 0, 0, w, h, t["bright"] if self.hover else t["dim"], max(1, self._s(1)), ("box", "edge"))
        except tk.TclError:
            pass

    def _paint_index(self):
        cv, t = self.index, self._t
        try:
            cv.delete("idx")
            w = cv.winfo_width()
            if w <= 1:
                w = cv.winfo_reqwidth()
            ty = self._title_cy if self._title_cy is not None else self._s(MIN_BOX_H) / 2.0
            ix = w - self._s(INDEX_DX)
            _fill(cv, w - self._s(PIN_W), int(round(ty)), w, int(round(ty)) + max(1, self._s(1)), t["dim"],
                  ("idx", "pin"))
            text = "%02d" % self.number
            if self.focused:
                iw = _measure(cv, TILE_INDEX, text)
                ils = _linespace(cv, TILE_INDEX)
                cx0, cy0 = ix - iw - self._s(CHIP_PAD), int(round(ty - ils / 2.0)) - 1
                _fill(cv, max(0, cx0), cy0, ix + self._s(CHIP_PAD), cy0 + ils + 2, t["bright"], ("idx", "chip"))
                cv.create_text(ix, ty, text=text, anchor="e", fill=t["bg"], font=TILE_INDEX, tags=("idx", "index"))
            else:
                cv.create_text(ix, ty, text=text, anchor="e", fill=t["value"], font=TILE_INDEX,
                               tags=("idx", "index"))
        except tk.TclError:
            pass
