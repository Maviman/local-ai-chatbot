"""kit/header.py - the screen headers (BUILD_PLAN §1.3; approved shared_glyphs.header_full,
r2kit.header_compact, r3_home.header_full).

    full_header(parent, theme, title, actions=None, screen=None)          title only, display XL
    compact_header(parent, theme, title="CHAT", subtitle=None, screen=None)  Chat: display L + subtitle
    home_header(parent, theme, title="HOME", code="A1", clock="", screen=None)
        Home (r3): code box, title, boxed clock, the rule_out / rule_in double rule and the
        2 px bright title bar.

Each returns a Header (tk.Frame). The title is Canvas text (header.canvas, header.title_item),
so B11 can slice it for the CRT and the D-27 Canvas walk reads it; when `screen` is given,
screen.fx_title is set to the Header. header.actions is a Frame at the right end for text
actions (empty until a screen fills it). Coordinates are relative to the content column:
x 16 here is window x 58 (the 42 px rail + 16). Static: no timers.

Every constant below is in approved pixels at 100 %; the header multiplies them by the display
ratio (kit.geom_ratio: 1.0 at 100 % and in the DPI-unaware app today), because its fonts are in
points (B1 fix F11): at 200 % the title stays inside its band and above the double rule.
header.band_h / header.rule_y / header.ratio give the values in use.

The title and the compact subtitle (user text in B7: a model file or chat title) are drawn on
one line and elided in the middle so they end 16 px before header.actions (or the clock box on
Home, or the right gutter), re-fitted on every canvas / actions <Configure> (B1 fix A9).
header.title / header.subtitle keep the full text; header.shown_title / shown_subtitle are drawn.
When either is elided (header.is_elided()), the full text shows in a tip below the band while the
pointer is on the header: <Enter> / <Leave>, no timer (B2; D-27 "never cut"). header.tip is that Label.

B10: the Header is a motion "typeable" (fx/motion.py): fx_type_text() is the drawn title (shown_title),
fx_type_show(k, cursor) draws its first k characters behind the block cursor (k None = whole; a repaint
keeps the frame, so a resize mid-type never flashes the whole title). The cursor is a drawn box (one '0'
cell wide, on the baseline: fx.motion.show_block_cursor), not the U+2588 glyph, which Bahnschrift lacks.
fx_type_gen() changes when set_title brings a NEW title (the app wins over a running type-on; the same
title again keeps it), fx_ink is the title's ink (the REDUCED settle). Only the title
types: the subtitle, the code box, the clock and the rules are drawn whole at once. The header itself
still schedules nothing; fx.motion drives the frames through fx.clock.

B11 (CRT on the glass, R2 R-10): while the fx is live the header asks it for its title canvas's CRT glass
(fx.get(canvas).motion.crt_glass -> fx/draw.CrtGlass, header.fx_glass; None under the kill switch) and repaints it
after every paint and every type-on frame, so at CRT FULL the title (a display word >= 15 pt: XL 22, the compact L
15) is sliced by ground-coloured scanlines; the subtitle, the code box and the clock (Consolas < 15 pt) never are.
LOW / OFF: nothing (a title has no outline to bloom). A live CRT change repaints it through the FxCanvas registry.
"""

import tkinter as tk

from . import C9, C9B, C10B, display_font, ensure_fonts, geom_ratio, tokens
from .painters import draw_double_rule, rect_px
from .widgets import ELLIPSIS, TIP_GAP, _tip_registry, elide_middle

FULL_H = 56              # empty space ends the header band at y 56
COMPACT_H = 44
TITLE_X = 16
TITLE_CY = 30
COMPACT_CY = 22
SUBTITLE_GAP = 14
RIGHT_PAD = 16           # right gutter (window x 1084 at 1100 wide)

# r3 Home header geometry (window coords minus the 42 px rail)
HOME_CODE_BOX = (16, 18, 50, 42)
HOME_TITLE_X = 62
HOME_RULE_Y = 52         # rule_out row; rule_in 3 rows below; title bar rows 52-53
HOME_CLOCK_RIGHT = 22    # clock text right edge = width - 22 (window x 1078)
CLOCK_FONT = C10B

try:        # B10: the type-on block cursor is a drawn box (fx/motion.py); without it a type-on shows no cursor
    from ..fx.motion import CURSOR_TAG as _CURSOR_TAG, show_block_cursor as _show_block_cursor
except Exception:  # noqa: BLE001
    _CURSOR_TAG, _show_block_cursor = "fx_type_cursor", None


def _crt_glass(canvas, **spec):
    """B11: the CRT glass of `canvas`, asked of the live fx (fx.get(canvas).motion.crt_glass -> fx/draw.CrtGlass), or
    None (no fx, the kill switch: NullFX's no-op namespace). Never raises."""
    try:
        from .. import fx as _fx
        return _fx.get(canvas).motion.crt_glass(canvas, **spec)
    except Exception:  # noqa: BLE001
        return None


class Header(tk.Frame):
    """One screen header. kind: 'full' | 'compact' | 'home'."""

    def __init__(self, parent, theme, title, kind="full", subtitle=None, code="A1", clock="", actions=None,
                 screen=None, ground=None, **kw):
        self._t = tokens(theme)
        self.kind = kind if kind in ("full", "compact", "home") else "full"
        self._title = "" if title is None else str(title)
        self._subtitle = subtitle
        self._code = code
        self._clock = clock
        ground = ground or self._t["bg"]
        kw.setdefault("bg", ground)
        tk.Frame.__init__(self, parent, **kw)
        ensure_fonts(self)
        self.ratio = geom_ratio(self)
        h = self._s(COMPACT_H if self.kind == "compact" else FULL_H)
        self.band_h = h
        self.rule_y = self._s(HOME_RULE_Y)
        self.canvas = tk.Canvas(self, height=h, bg=ground, highlightthickness=0, bd=0)
        self.canvas.pack(fill="x", expand=True)
        self.actions = tk.Frame(self, bg=ground)
        cy = self._s(COMPACT_CY if self.kind == "compact" else TITLE_CY)
        self.actions.place(relx=1.0, x=-self._s(RIGHT_PAD), y=cy, anchor="e")
        if actions:
            for a in actions:
                try:
                    a.pack(in_=self.actions, side="left", padx=(16, 0))
                    # an action owned by another parent and created before this header sits below
                    # it in the stacking order, so the canvas would cover it: raise it
                    if a.master is not self.actions:
                        a.lift()
                except tk.TclError:
                    pass
        self.title_item = None
        self.subtitle_item = None
        self.clock_item = None
        self.title_right = TITLE_X
        self.shown_title = ""
        self.shown_subtitle = ""
        self._tip = None
        self._type_k = None          # B10: the typed-on frame (first k characters) while a type-on runs
        self._type_cursor = False
        self._type_gen = 0
        self.fx_glass = None         # B11: the title canvas's CRT glass (made below, only while fx is live)
        self.canvas.bind("<Configure>", lambda _e: self._paint(), add="+")
        # B2: an elided title is never cut (D-27): the full text shows in a tip while the pointer is on
        # the header (no timer, X-23), like kit.StatusLine's
        self.canvas.bind("<Enter>", self._show_tip, add="+")
        self.canvas.bind("<Leave>", self._hide_tip, add="+")
        # a screen filling header.actions later changes the room left for the title (A9)
        self.actions.bind("<Configure>", lambda _e: self._paint(), add="+")
        self._paint()
        self.fx_glass = _crt_glass(self.canvas, words=self._glass_words)
        if screen is not None:
            try:
                screen.fx_title = self
            except Exception:  # noqa: BLE001
                pass

    def _s(self, n):
        """n approved pixels at this header's display ratio."""
        return int(round(n * self.ratio))

    # ---------------------------------------------------------------- API
    @property
    def title(self):
        return self._title

    def set_title(self, text):
        text = "" if text is None else str(text)
        if text != self._title:
            self._type_gen += 1      # B10: a NEW title wins over a running type-on; the same title again keeps it
            self._type_k = None
        self._title = text
        self._paint()

    # ---------------------------------------------------------------- B10: the motion typeable
    def fx_type_text(self):
        """The title as drawn when no type-on runs (upper-cased, one line, elided to fit)."""
        return self.shown_title

    def fx_type_gen(self):
        return self._type_gen

    def fx_type_show(self, k, cursor=False):
        """Draw the first k characters of the title (+ the block cursor); k None = the whole title."""
        if k is None or k >= len(self.shown_title):
            self._type_k, self._type_cursor = None, False
        else:
            self._type_k, self._type_cursor = max(0, int(k)), bool(cursor)
        if self.title_item is not None:
            try:
                self.canvas.itemconfigure(self.title_item, text=self._drawn_title(self.shown_title))
            except tk.TclError:
                pass
        self._draw_type_cursor()
        self._paint_glass("type")    # B11: the scanlines follow the typed frame's box

    # ---------------------------------------------------------------- B11: the CRT glass
    def _glass_words(self):
        """The canvas text the glass may slice: the title only (the glass itself keeps text < 15 pt out)."""
        return (self.title_item,) if self.title_item is not None else ()

    def _paint_glass(self, reason):
        g = self.fx_glass
        if g is not None:
            try:
                g.paint(reason)
            except Exception:  # noqa: BLE001 - an effect never breaks the header
                pass

    def _draw_type_cursor(self):
        """The type-on block cursor right after the typed title: a drawn box in the title's ink (one '0' cell wide,
        standing on the baseline; fx.motion.show_block_cursor), never a glyph - Bahnschrift has no U+2588, so Tk
        drew a fallback font's 40 px block. Removed whenever no type-on runs."""
        cv = self.canvas
        try:
            cv.delete(_CURSOR_TAG)
        except tk.TclError:
            return
        if (self._type_k is not None and self._type_cursor and self.title_item is not None
                and _show_block_cursor is not None):
            _show_block_cursor(cv, self.title_item, True)

    @property
    def fx_ink(self):
        """(getter, setter) of the title's ink, for the REDUCED colour settle."""
        def get():
            try:
                return self.canvas.itemcget(self.title_item, "fill") if self.title_item is not None else ""
            except tk.TclError:
                return ""

        def put(c):
            if self.title_item is not None:
                self.canvas.itemconfigure(self.title_item, fill=c)
        return (get, put)

    def _drawn_title(self, shown):
        """The title text as drawn now: its first k characters while a type-on runs (the cursor is a box)."""
        if self._type_k is None:
            return shown
        return shown[:self._type_k]

    def set_subtitle(self, text):
        self._subtitle = text
        self._paint()

    def set_clock(self, text):
        self._clock = text
        self._paint()

    @property
    def subtitle(self):
        """The subtitle as given (the canvas may show it elided)."""
        return self._subtitle

    def title_font(self):
        return display_font(self.canvas, "L" if self.kind == "compact" else "XL")

    def _right_limit(self, w):
        """The x the title / subtitle must end before: the actions frame's left edge minus a
        16 px gap when it holds anything, else the right gutter (B1 fix A9)."""
        limit = w - self._s(RIGHT_PAD)
        try:
            a = self.actions          # slaves, not children: actions= packs other parents' widgets in_= it
            if a.pack_slaves() or a.grid_slaves() or a.place_slaves():
                aw = max(self.actions.winfo_reqwidth(), self.actions.winfo_width()
                         if self.actions.winfo_ismapped() else 0)
                limit -= aw + self._s(16)
        except tk.TclError:
            pass
        return limit

    def _fit(self, font, text, x0, limit):
        """text elided in the middle so that it ends at or before `limit` when drawn from x0."""
        cv = self.canvas
        room = limit - x0

        def fits(s):
            return int(cv.tk.call("font", "measure", font, s)) <= room
        if not text or fits(text):
            return text
        return elide_middle(text, fits) if fits(ELLIPSIS) else ""

    # ---------------------------------------------------------------- title tip (no timer)
    def is_elided(self):
        """True when the drawn title (or compact subtitle) is shorter than the text it stands for."""
        full = " ".join(self._title.split()).upper()
        if self.shown_title != full:
            return True
        if self.kind == "compact" and self._subtitle:
            return self.shown_subtitle != " ".join(str(self._subtitle).split())
        return False

    @property
    def tip(self):
        """The title tip Label while it is shown, else None."""
        return self._tip

    def _tip_text(self):
        text = self._title
        if self.kind == "compact" and self._subtitle:
            text = "%s\n%s" % (text, self._subtitle)
        return text

    def _show_tip(self, _e=None):
        if self._tip is not None or not self.is_elided():
            return
        tip = None
        try:
            top = self.winfo_toplevel()
            t = self._t
            tip = tk.Label(top, text="", justify="left", anchor="w", font=C9, bg=t["panel"], fg=t["bright"], bd=0,
                           padx=6, pady=4, highlightthickness=1, highlightbackground=t["dim"])
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
        """Below the header band, from the title's x, inside the window (never over the title)."""
        top = tip.winfo_toplevel()
        top_w = top.winfo_width()
        cv = self.canvas
        lx = cv.winfo_rootx() - top.winfo_rootx() + self._s(HOME_TITLE_X if self.kind == "home" else TITLE_X)
        y = cv.winfo_rooty() - top.winfo_rooty() + cv.winfo_height() + TIP_GAP
        tip.configure(text=self._tip_text(), wraplength=max(200, min(640, top_w - 40)))
        tw = tip.winfo_reqwidth()
        tip.place(x=max(0, min(lx, top_w - tw)), y=y)

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

    def destroy(self):
        self._hide_tip()
        tk.Frame.destroy(self)

    # ---------------------------------------------------------------- paint
    def _paint(self):
        self._paint_items()
        self._draw_type_cursor()     # B10: a repaint mid-type keeps the typed frame and its cursor
        self._paint_glass("paint")   # B11: the new title item sits above the old scanlines: slice it again
        if self._tip is not None:
            # a repaint never leaves a stale tip: it follows the new text, or goes when nothing is hidden
            try:
                if self.is_elided() and self.winfo_viewable():
                    self._place_tip(self._tip)
                else:
                    self._hide_tip()
            except tk.TclError:
                self._hide_tip()

    def _paint_items(self):
        cv = self.canvas
        t = self._t
        try:
            cv.delete("hdr")
            w = cv.winfo_width()
            if w <= 1:
                w = cv.winfo_reqwidth()
            font = self.title_font()
            shown = " ".join(self._title.split()).upper()          # one line: never out of the band
            if self.kind == "home":
                self._paint_home(cv, t, w, font, shown)
                return
            s = self._s
            cy = s(COMPACT_CY if self.kind == "compact" else TITLE_CY)
            limit = self._right_limit(w)
            shown = self._fit(font, shown, s(TITLE_X), limit)          # A9: never under the actions
            self.shown_title = shown
            self.title_item = cv.create_text(s(TITLE_X), cy, text=self._drawn_title(shown), anchor="w",
                                             fill=t["bright"], font=font, tags=("hdr", "title"))
            self.title_right = s(TITLE_X) + int(cv.tk.call("font", "measure", font, shown))
            self.subtitle_item = None
            self.shown_subtitle = ""
            if self.kind == "compact" and self._subtitle:
                sx = self.title_right + s(SUBTITLE_GAP)
                sub = self._fit(C10B, " ".join(str(self._subtitle).split()), sx, limit)
                self.shown_subtitle = sub
                self.subtitle_item = cv.create_text(sx, cy, text=sub, anchor="w", fill=t["value"], font=C10B,
                                                    tags=("hdr", "subtitle"))
        except tk.TclError:
            pass

    def _paint_home(self, cv, t, w, font, shown):
        s = self._s
        bx0, by0, bx1, by1 = (s(v) for v in HOME_CODE_BOX)
        cy = s(TITLE_CY)
        rect_px(cv, bx0, by0, bx1 - bx0, by1 - by0, outline=t["bright"], tag="hdr", kind="codebox")
        cv.create_text((bx0 + bx1) / 2.0, cy, text=str(self._code or ""), fill=t["bright"], font=C9B,
                       tags=("hdr", "code"))
        x_end = w - s(RIGHT_PAD) + 1
        limit = self._right_limit(w)
        cr = w - s(HOME_CLOCK_RIGHT)
        if self._clock:
            cw = int(cv.tk.call("font", "measure", CLOCK_FONT, str(self._clock)))
            limit = min(limit, cr - cw - s(6) - s(16))
        shown = self._fit(font, shown, s(HOME_TITLE_X), limit)            # A9: never under the clock box
        self.shown_title = shown
        self.title_item = cv.create_text(s(HOME_TITLE_X), cy, text=self._drawn_title(shown), anchor="w",
                                         fill=t["bright"], font=font, tags=("hdr", "title"))
        full_r = s(HOME_TITLE_X) + int(cv.tk.call("font", "measure", font, shown))
        self.title_right = full_r
        self.clock_item = None
        if self._clock:
            self.clock_item = cv.create_text(cr, cy, text=str(self._clock), anchor="e", fill=t["bright"],
                                             font=CLOCK_FONT, tags=("hdr", "clock"))
            bb = cv.bbox(self.clock_item)
            if bb:
                x0 = cr - cw - s(6)
                rect_px(cv, x0, bb[1] - s(2), x_end - x0, bb[3] - bb[1] + s(4), outline=t["dim"], tag="hdr",
                        kind="clockbox")
        rule_y = s(HOME_RULE_Y)
        draw_double_rule(cv, t, s(TITLE_X), x_end, rule_y, color=t["rule_out"], inner=t["rule_in"], sep=s(3),
                         tag="hdr")
        rect_px(cv, s(TITLE_X), rule_y, max(1, full_r - s(TITLE_X)), s(2), fill=t["bright"], tag="hdr",
                kind="bar")


def full_header(parent, theme, title, actions=None, screen=None, **kw):
    """The full header: the title only, display XL bright at (16, 30). -> Header."""
    return Header(parent, theme, title, kind="full", actions=actions, screen=screen, **kw)


def compact_header(parent, theme, title="CHAT", subtitle=None, actions=None, screen=None, **kw):
    """The Chat header: display L title at (16, 22) + an optional C10B value subtitle. -> Header."""
    return Header(parent, theme, title, kind="compact", subtitle=subtitle, actions=actions, screen=screen, **kw)


def home_header(parent, theme, title="HOME", code="A1", clock="", screen=None, **kw):
    """The r3 Home header: code box, title, boxed clock, double rule + title bar. -> Header."""
    return Header(parent, theme, title, kind="home", code=code, clock=clock, screen=screen, **kw)
