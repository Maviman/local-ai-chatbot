"""kit/rail.py - the shell's rail cells (BUILD_PLAN §4 B2; approved shared_glyphs.rail_cell / power_cell,
frame r2_rail_states.png).

    RailCell(master, theme, glyph_kind, word, code, command=None)    one 40x60 screen cell
    PowerCell(master, theme, command=None)                           the 40x34 power cell

Each is a tk.Canvas painted from the approved geometry (cell-local pixels at 100 %; the whole drawing is
scaled by kit.geom_ratio, so at 200 % the cell is 80x120 and its fonts, which are in points, still fit):

    plate / outline footprint  (2, 1)-(39, 59)   (power: (2, 1)-(39, 33))
    glyph                      20x20 at (10, 6)  (kit.glyphs)
    word                       display XS, centred on (20, 37)
    code                       C7, centred on (20, 51)

Visual states (.state):
    idle           glyph, word and code in `value`
    hover          1 px `bright` outline, ink `bright`              (pointer over an idle cell)
    active         `bright` plate, ink `panel`                      (the current screen; hover changes nothing)
    locked         ink `dim`                                        (navigation locked during a model load)
    locked_active  ink `dim` + the cell's box in `ghost`            (the current screen while locked)
The power cell: idle = the power glyph in `danger`; hover = + a 1 px `danger` outline; locked = glyph `dim`.
There is no keyboard-focus state (X-16): the cells are not focusable, Alt+1..4 reach the screens.

Hover uses <Enter> / <Leave> only (no timer). A click runs `command` in every state: the router itself
refuses navigation while locked (and emits its nav.denied cue), exactly as the old Label rail did.
"""

import tkinter as tk

from . import C7, display_font, ensure_fonts, geom_ratio, tokens
from .glyphs import glyph

CELL_W, CELL_H = 40, 60
POWER_H = 34
RAIL_X0 = 1              # cell x inside the rail
RAIL_Y0, RAIL_PITCH = 12, 64
POWER_BOTTOM = 46        # the power cell's top sits this far above the rail's bottom edge (12 px gap below it)
GLYPH_XY = (10, 6)
WORD_XY = (20, 37)
CODE_XY = (20, 51)
STATES = ("idle", "hover", "active", "locked", "locked_active")


class _Cell(tk.Canvas):
    """Shared state machine of the rail cells. Subclasses implement _draw(state, t)."""

    _H = CELL_H

    def __init__(self, master, theme, command=None, ground=None, **kw):
        self._t = tokens(theme)
        self.command = command
        self._active = False
        self._locked = False
        self._hover = False
        ensure_fonts(master)
        self.ratio = geom_ratio(master)
        kw.setdefault("highlightthickness", 0)
        kw.setdefault("bd", 0)
        kw.setdefault("cursor", "hand2")
        tk.Canvas.__init__(self, master, width=self._s(CELL_W), height=self._s(self._H),
                           bg=ground or self._t["panel"], **kw)
        self._cursor = str(self.cget("cursor"))
        self.bind("<Enter>", self._on_enter, add="+")
        self.bind("<Leave>", self._on_leave, add="+")
        self.bind("<Button-1>", self._on_click, add="+")
        self._paint()

    def _s(self, n):
        return int(round(n * self.ratio))

    # ---------------------------------------------------------------- state
    @property
    def active(self):
        return self._active

    @property
    def locked(self):
        return self._locked

    @property
    def hovered(self):
        return self._hover

    def set_active(self, flag):
        flag = bool(flag)
        if flag != self._active:
            self._active = flag
            self._paint()

    def set_locked(self, flag):
        flag = bool(flag)
        if flag != self._locked:
            self._locked = flag
            try:
                tk.Canvas.configure(self, cursor="" if flag else self._cursor)
            except tk.TclError:
                pass
            self._paint()

    @property
    def state(self):
        raise NotImplementedError

    # ---------------------------------------------------------------- events
    def _on_enter(self, _e=None):
        self._hover = True
        self._paint()

    def _on_leave(self, _e=None):
        self._hover = False
        self._paint()

    def _on_click(self, _e=None):
        if self.command is not None:
            self.command()

    # ---------------------------------------------------------------- paint
    def _paint(self):
        try:
            self.delete("cell")
            self._draw(self.state, self._t)
            if self.ratio != 1.0:
                self.scale("cell", 0, 0, self.ratio, self.ratio)
                stroke = max(1, int(round(self.ratio)))         # 1 px strokes grow with the display too
                for item in self.find_withtag("cell"):
                    if self.type(item) == "line":
                        self.itemconfigure(item, width=stroke)
        except tk.TclError:
            pass

    def _fill(self, x0, y0, x1, y1, col):
        """Filled pixels [x0, x1) x [y0, y1) (approved shared_glyphs._fill)."""
        self.create_rectangle(x0, y0, x1, y1, fill=col, outline="", tags=("cell", "plate"))

    def _box(self, x0, y0, x1, y1, col):
        """A 1 px outline over pixels [x0, x1) x [y0, y1) (approved shared_glyphs._box, w=1)."""
        for r in ((x0, y0, x1, y0 + 1), (x0, y1 - 1, x1, y1), (x0, y0, x0 + 1, y1), (x1 - 1, y0, x1, y1)):
            self.create_rectangle(*r, fill=col, outline="", tags=("cell", "box"))


class RailCell(_Cell):
    """RailCell(master, theme, glyph_kind, word, code, command=None, ground=None)

    One screen cell of the rail: glyph_kind one of kit.glyphs.GLYPHS ('home' / 'chats' / 'proj' /
    'config'), word the short display word (HOME / CHATS / PROJ / CONFIG, D-6), code A1..A4."""

    def __init__(self, master, theme, glyph_kind, word, code, command=None, ground=None, **kw):
        self.glyph_kind = glyph_kind
        self.word = str(word)
        self.code = str(code)
        _Cell.__init__(self, master, theme, command=command, ground=ground, **kw)

    @property
    def state(self):
        if self._locked:
            return "locked_active" if self._active else "locked"
        if self._active:
            return "active"
        return "hover" if self._hover else "idle"

    @property
    def ink(self):
        """The ink of the glyph, word and code in the current state."""
        return {"idle": self._t["value"], "hover": self._t["bright"], "active": self._t["panel"],
                "locked": self._t["dim"], "locked_active": self._t["dim"]}[self.state]

    @property
    def fx_fill(self):
        """B10 (GAP_FX 1.7): (getter, setter) of the active plate's fill, for fx.motion's rail latch (the
        110 ms settle from hot to bright). The getter reads "" when no plate is drawn (not the active cell);
        any repaint redraws the plate in its final colour, so a latch then stops (the app wins)."""
        def get():
            try:
                items = self.find_withtag("plate")
                return self.itemcget(items[0], "fill") if items else ""
            except tk.TclError:
                return ""

        def put(colour):
            self.itemconfigure("plate", fill=colour)
        return (get, put)

    def _draw(self, state, t):
        px0, py0, px1, py1 = 2, 1, CELL_W - 1, CELL_H - 1
        if state == "active":
            self._fill(px0, py0, px1, py1, t["bright"])
        elif state == "hover":
            self._box(px0, py0, px1, py1, t["bright"])
        elif state == "locked_active":
            self._box(px0, py0, px1, py1, t["ghost"])
        c = self.ink
        glyph(self, self.glyph_kind, GLYPH_XY[0], GLYPH_XY[1], c, tag=("cell",))
        self.create_text(WORD_XY[0], WORD_XY[1], text=self.word, fill=c, font=display_font(self, "XS"),
                         tags=("cell", "word"))
        self.create_text(CODE_XY[0], CODE_XY[1], text=self.code, fill=c, font=C7, tags=("cell", "code"))


class PowerCell(_Cell):
    """PowerCell(master, theme, command=None, ground=None): the 40x34 power cell (power glyph in danger)."""

    _H = POWER_H

    @property
    def state(self):
        if self._locked:
            return "locked"
        return "hover" if self._hover else "idle"

    @property
    def ink(self):
        return self._t["dim"] if self._locked else self._t["danger"]

    def _draw(self, state, t):
        if state == "hover":
            self._box(2, 1, CELL_W - 1, POWER_H - 1, t["danger"])
        glyph(self, "power", GLYPH_XY[0], GLYPH_XY[1], self.ink, tag=("cell",))


def cell_xy(index, ratio=1.0):
    """(x, y) of rail cell `index` (0-based) inside the rail, at `ratio`."""
    return int(round(RAIL_X0 * ratio)), int(round((RAIL_Y0 + RAIL_PITCH * index) * ratio))
