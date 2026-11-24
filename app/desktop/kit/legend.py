"""kit/legend.py - the key legend line (BUILD_PLAN §1.3; approved shared_glyphs.keycap / legend /
message_line_idle, and r3_home.keycap for the Home chips).

    paint_legend(cv, theme, items, x=16, cy=12, chips=False, stamp=None, x_right=None, ink=None,
                 more=False) -> right x
    fit_items(cv, items, room, chips=False) -> (shown, dropped): the whole items that fit in room px
    LegendLine(master, theme, items=(), chips=False, stamp=None, rule=False, dim=False)  a 24 px Canvas row

rule=True adds the r3 Home form's 1 px dim rule along the row's top edge (the shell turns chips and
the rule on for Home only, B2; every other screen is the chipless, ruleless R2 form).

An item is (key, verb): the key in C9B and the lower-case verb in C9, 6 px apart, items
18 px apart, all in the value tier, no chip fill and no rule (R2 7.6). chips=True draws the
r3 Home form instead: the key as a value-filled chip with bg text (C8B, 5 px padding).
The optional stamp (X-4) is right-aligned at x_right in C9 value; D-32 dropped the header
date stamps, so screens pass none, but the slot stays for any later use.
Every word is a Canvas text item, so the D-27 snapshot's Canvas walk reads it.
Never cut (B2 fix ADV1-02, D-27): when the row is narrower than its items, LegendLine drops WHOLE
trailing items (never half of one; an ESC item is always kept), draws a "…" marker after the last
one shown, and holds the full list in a hover tip above the row (<Enter> / <Leave>, no timer, X-23).
.shown_items / .dropped / is_elided() read back what is painted.
dim=True paints every key and verb in `dim` (B2 fix ADV1-05: the shell dims the legend while
navigation is locked during a model load, as it dims the rail).
The pixel constants are approved pixels at 100 %; they grow with the display ratio
(kit.geom_ratio, 1.0 at 100 %), because the fonts are in points (B1 fix F11).
"""

import tkinter as tk

from . import C8B, C9, C9B, geom_ratio, tokens
from .widgets import ELLIPSIS, TIP_GAP, _tip_registry

LEGEND_H = 24            # the legend row (window y 776-800 at 1100x800)
LEGEND_X0 = 16           # first key: 16 px into the content column (window x 58)
LEGEND_GAP = 18          # between items
KEY_VERB_GAP = 6
KEY_FONT = C9B
VERB_FONT = C9
CHIP_FONT = C8B
CHIP_PAD = 5


def _measure(cv, font, text):
    return int(cv.tk.call("font", "measure", font, text))


def _items(items):
    out = []
    for it in items or ():
        if isinstance(it, (tuple, list)) and len(it) >= 2:
            out.append((str(it[0]), "" if it[1] is None else str(it[1])))
        elif isinstance(it, (tuple, list)) and len(it) == 1:
            out.append((str(it[0]), ""))
        elif it is not None:
            out.append((str(it), ""))
    return out


def _s_at(r):
    def s(n):
        return int(round(n * r))
    return s


def item_width(cv, key, verb, chips=False):
    """The painted width of one (key, verb) item on cv (no gap), as paint_legend draws it."""
    s = _s_at(geom_ratio(cv))
    if chips:
        w = _measure(cv, CHIP_FONT, key) + 2 * s(CHIP_PAD)
    else:
        w = _measure(cv, KEY_FONT, key)
    if verb:
        w += s(KEY_VERB_GAP) + _measure(cv, VERB_FONT, verb)
    return w


def _row_width(cv, items, chips, more):
    s = _s_at(geom_ratio(cv))
    widths = [item_width(cv, k, v, chips) for k, v in items]
    if more:
        widths.append(_measure(cv, VERB_FONT, ELLIPSIS))
    return sum(widths) + s(LEGEND_GAP) * max(0, len(widths) - 1)


def fit_items(cv, items, room, chips=False):
    """(shown, dropped): the items that fit in `room` px when painted from one x, dropping WHOLE items
    from the end (never half of one) and never an ESC item; when anything is dropped, room is also
    kept for the "…" marker. room None = no limit."""
    items = _items(items)
    if room is None or _row_width(cv, items, chips, False) <= room:
        return items, []
    shown = list(items)
    dropped = []
    while _row_width(cv, shown, chips, True) > room:
        idx = next((i for i in range(len(shown) - 1, -1, -1) if shown[i][0].strip().upper() != "ESC"), None)
        if idx is None:
            break
        dropped.insert(0, shown.pop(idx))
    return shown, dropped


def paint_legend(cv, theme, items, x=LEGEND_X0, cy=LEGEND_H // 2, chips=False, stamp=None, x_right=None,
                 tag="legend", ink=None, more=False):
    """Paint a key legend on canvas cv, first key at x, centred on row cy.
    theme: a Theme, a tones dict or None. ink: override the value ink (the shell's locked dim).
    more: append the "…" marker (items were dropped to fit). -> the x after the last item."""
    t = theme if (isinstance(theme, dict) and "value" in theme and "bg" in theme) else tokens(theme)
    ink = ink or t["value"]
    s = _s_at(geom_ratio(cv))
    tags = (tag, "legend_item")
    n = 0
    for n, (key, verb) in enumerate(_items(items), start=1):
        if n > 1:
            x += s(LEGEND_GAP)
        if chips:
            kw_ = _measure(cv, CHIP_FONT, key)
            y0 = int(round(cy - s(8)))
            cv.create_rectangle(x, y0, x + kw_ + 2 * s(CHIP_PAD), y0 + s(16), fill=ink, outline="", tags=tags)
            cv.create_text(x + s(CHIP_PAD), y0 + s(8), text=key, anchor="w", fill=t["bg"], font=CHIP_FONT,
                           tags=tags + ("key",))
            x += kw_ + 2 * s(CHIP_PAD)
        else:
            cv.create_text(x, cy, text=key, anchor="w", fill=ink, font=KEY_FONT, tags=tags + ("key",))
            x += _measure(cv, KEY_FONT, key)
        if verb:
            cv.create_text(x + s(KEY_VERB_GAP), cy, text=verb, anchor="w", fill=ink, font=VERB_FONT,
                           tags=tags + ("verb",))
            x += s(KEY_VERB_GAP) + _measure(cv, VERB_FONT, verb)
    if more:
        if n:
            x += s(LEGEND_GAP)
        cv.create_text(x, cy, text=ELLIPSIS, anchor="w", fill=ink, font=VERB_FONT, tags=(tag, "more"))
        x += _measure(cv, VERB_FONT, ELLIPSIS)
    if stamp and x_right is not None:
        cv.create_text(x_right, cy, text=str(stamp), anchor="e", fill=ink, font=VERB_FONT,
                       tags=(tag, "stamp"))
    return x


class LegendLine(tk.Canvas):
    """LegendLine(master, theme, items=(), chips=False, stamp=None, ground=None, x0=16, right_pad=16, rule=False,
    dim=False)

    The shell's 24 px legend row. set_items(items, chips=None, rule=None, dim=None) / set_stamp(text) repaint
    it; it also repaints on resize (the stamp is right-aligned, and the fit follows the width). .items /
    .stamp / .chips / .rule / .dim read back what was asked; .shown_items / .dropped what is painted.
    When items had to be dropped, <Enter> shows the full list in .tip above the row; <Leave> removes it."""

    def __init__(self, master, theme, items=(), chips=False, stamp=None, ground=None, x0=LEGEND_X0,
                 right_pad=16, rule=False, dim=False, **kw):
        self._t = tokens(theme)
        self._items = _items(items)
        self._chips = bool(chips)
        self._rule = bool(rule)
        self._dim = bool(dim)
        self._stamp = stamp
        self._x0 = x0
        self._right_pad = right_pad
        self._shown = list(self._items)
        self._dropped = []
        self._tip = None
        kw.setdefault("highlightthickness", 0)
        kw.setdefault("bd", 0)
        self.ratio = geom_ratio(master)
        kw.setdefault("height", int(round(LEGEND_H * self.ratio)))
        tk.Canvas.__init__(self, master, bg=ground or self._t["bg"], **kw)
        self.bind("<Configure>", lambda _e: self._paint(), add="+")
        self.bind("<Enter>", self._show_tip, add="+")
        self.bind("<Leave>", self._hide_tip, add="+")
        self._paint()

    @property
    def items(self):
        return list(self._items)

    @property
    def shown_items(self):
        """The items painted now (all of them unless the row is too narrow)."""
        return list(self._shown)

    @property
    def dropped(self):
        """The items left out to fit the width (they are in the hover tip)."""
        return list(self._dropped)

    def is_elided(self):
        return bool(self._dropped)

    @property
    def stamp(self):
        return self._stamp

    @property
    def chips(self):
        return self._chips

    @property
    def rule(self):
        return self._rule

    @property
    def dim(self):
        return self._dim

    @property
    def tip(self):
        """The full-list tip Label while it is shown, else None."""
        return self._tip

    def set_items(self, items, chips=None, rule=None, dim=None):
        """Repaint with these items. chips / rule / dim: None keeps the current form."""
        self._items = _items(items)
        if chips is not None:
            self._chips = bool(chips)
        if rule is not None:
            self._rule = bool(rule)
        if dim is not None:
            self._dim = bool(dim)
        self._paint()

    def set_stamp(self, text):
        self._stamp = text
        self._paint()

    def _paint(self):
        try:
            self.delete("legend")
            w = self.winfo_width()
            if w <= 1:
                w = int(float(self.cget("width") or 0))
            x_right = (w - int(round(self._right_pad * self.ratio))) if w > 1 else None
            h = int(float(self.cget("height") or LEGEND_H))
            x0 = int(round(self._x0 * self.ratio))
            if self._rule and w > 1:
                # r3 Home form: a 1 px dim rule along the row's top edge, full width
                self.create_rectangle(0, 0, w, max(1, int(round(self.ratio))), fill=self._t["dim"], outline="",
                                      tags=("legend", "rule"))
            room = None
            if x_right is not None:
                room = x_right - x0
                if self._stamp:
                    room -= _measure(self, VERB_FONT, str(self._stamp)) + int(round(LEGEND_GAP * self.ratio))
            self._shown, self._dropped = fit_items(self, self._items, room, self._chips)
            paint_legend(self, self._t, self._shown, x=x0, cy=h // 2, chips=self._chips,
                         stamp=self._stamp, x_right=x_right, ink=self._t["dim"] if self._dim else None,
                         more=bool(self._dropped))
        except tk.TclError:
            pass
        if self._tip is not None:
            # a repaint never leaves a stale tip: it follows the new list, or goes when nothing is dropped
            if self._dropped:
                try:
                    self._place_tip(self._tip)
                except tk.TclError:
                    self._hide_tip()
            else:
                self._hide_tip()

    # ---------------------------------------------------------------- full-list tip (no timer)
    def _tip_text(self):
        return "   ".join(("%s %s" % (k, v)).strip() for k, v in self._items)

    def _show_tip(self, _e=None):
        if self._tip is not None or not self._dropped:
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
        """Above the row (never over it), from the first key's x, inside the window."""
        top = tip.winfo_toplevel()
        top_w = top.winfo_width()
        tip.configure(text=self._tip_text(), wraplength=max(120, min(640, top_w - 40)))
        tw, th = tip.winfo_reqwidth(), tip.winfo_reqheight()
        lx = self.winfo_rootx() - top.winfo_rootx() + int(round(self._x0 * self.ratio))
        y = self.winfo_rooty() - top.winfo_rooty() - TIP_GAP - th
        tip.place(x=max(0, min(lx, top_w - tw)), y=max(0, y))

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
        tk.Canvas.destroy(self)
