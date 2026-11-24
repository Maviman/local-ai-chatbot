"""kit/loadcard.py - the model-load card of the approved look (BUILD_PLAN §4 B9; approved r2_model_load.png,
renderer r2/overlays/model_load.py, R2 §2.11, GAP_VISUAL §4.3; see design archive).

    LoadingPlate(parent, theme, word)      the 214x68 plate Canvas: a 3 px bright outline and the word in display XL
    LoadCard(parent, theme, title, label, warning, elapsed_var, make_ledger)   the whole card, placed centred

The card (outer edge 1 px dim, 720 px wide, shrinking to fit a narrower window):
  * the LOADING plate at (x0+24, y0+24) - a Canvas, so B11 can add CRT to it (its items are tagged "plate";
    the word "plate_word"). The word is the overlay's title upper-cased ("LOADING MODEL", D-27 keeps OVL-01's
    title text); it steps down XL -> L -> M only if it would not fit inside the outline (scaling, a narrow face);
  * right of the plate: the label in C10B value, wrapped; under it ELAPSED (C9 value) + the time (C13B bright,
    from the caller's elapsed_var - the card creates NO tkinter Variable: F-1, the owner releases its own);
  * the warning, C9 warn_eff, one paragraph wrapped to the card's inner width;
  * the ledger box: 1 px dim, bg inside, rows at a 16 px pitch, holding the caller's ledger widget (make_ledger(parent) -> widget;
    the overlay passes its BootLog, so overlay.boot_log / set_lines(list) are unchanged).
No moving cells (OI-5), no animation and no timer: the kit never calls after(). The card refits itself on its
parent's <Configure> (width, wrap lengths, and the ledger's height, so the warning, the label and the ledger
are never clipped at 820x560 / 1024x600). The pixel constants are approved pixels at 100 % and grow with
kit.geom_ratio, like every other kit module.

B11 (CRT on the glass; approved r2_model_load: bg | bloom_out 2 | bloom_in 1 | BRIGHT 3 | bloom_in 1 | bloom_out 2):
the plate canvas stays exactly 214x68 at (x0+24, y0+24); two Frames wrapped round it (card.plate_bloom = (outer,
inner), highlight rings 2 px and 1 px, in the ground colour at rest) hold the room for the bloom OUTSIDE the stroke,
and every padding next to the plate gives those 3 px back, so nothing moves at any CRT level or under the kill
switch. While the fx is live the card asks it for the plate's CRT glass (fx.get(plate).motion.crt_glass ->
fx/draw.CrtGlass, plate.fx_glass): LOW / FULL colour the two rings bloom_out / bloom_in and draw the bloom inside the
stroke on the plate; FULL also slices the word with ground-coloured scanlines. The kit itself still draws only its
static plate.
"""

import tkinter as tk

from . import C9, C10, C10B, C13B, display_font, ensure_fonts, px, tokens
from .painters import rect_px

CARD_W = 720              # outer width, the 1 px edge included (G5a model_load.card_720)
CARD_MIN_W = 360          # never narrower than this, whatever the window (the window's minsize is 820)
CARD_MARGIN = 16          # kept free between the card and the window edge when the window is narrow / short
CARD_PAD = 24             # the plate, warning and ledger sit 24 px inside the outer edge
EDGE = 1                  # the card's dim edge
PLATE_W, PLATE_H = 214, 68
PLATE_STROKE = 3
BLOOM_OUT_W, BLOOM_IN_W = 2, 1  # B11: the bloom rings outside the plate's stroke (the room is there at every level)
PLATE_GAP = 24            # plate right edge -> label column
PLATE_WORD_PAD = 10       # the word keeps this much clear of the outline on each side
WORD_SIZES = ("XL", "L", "M")
LABEL_TOP = 6             # label line centred at plate top + 14 (renderer)
ELAPSED_GAP = 14          # label -> ELAPSED line (its centre at plate top + 48)
ELAPSED_KEY_GAP = 10      # 'ELAPSED' -> the time
WARNING_GAP = 16          # plate bottom -> warning (renderer: py + 68 + 16)
LEDGER_GAP = 14           # warning -> ledger box (renderer: y0 + 150 with a 2-line warning)
LEDGER_PADX, LEDGER_PADY = 10, 8
LEDGER_PITCH = 16         # ledger row pitch (R2 2.11: Consolas 10 at a 16 px pitch)
LEDGER_LINES = 10         # the overlay's ledger has always shown 10 lines (BootLog height=10)
LEDGER_MIN_LINES = 3
LABEL_MIN_WRAP = 80
# B9-H1 (bug-fix session, from the B9 hand-off): the focus-traversal keys the card swallows
TAB_SEQUENCES = ("<Tab>", "<Shift-Tab>", "<ISO_Left_Tab>", "<Control-Tab>", "<Control-Shift-Tab>")


def _break(_event=None):
    return "break"


def swallow_tabs(widget):
    """Bind the Tab keys (TAB_SEQUENCES) to "break" on `widget` and on every widget inside it, so Tk's "all"-tag
    focus traversal never walks out of it: the overlay's backdrop covers the widgets under the card but does not
    stop traversal, and a click focuses even a disabled Text on Windows. -> how many widgets were bound."""
    n, stack = 0, [widget]
    while stack:
        w = stack.pop()
        for seq in TAB_SEQUENCES:
            try:
                w.bind(seq, _break, add="+")
            except tk.TclError:
                continue
        n += 1
        try:
            stack.extend(w.winfo_children())
        except tk.TclError:
            pass
    return n


def _measure(widget, font, text):
    try:
        return int(widget.tk.call("font", "measure", font, text))
    except (tk.TclError, ValueError):
        return 7 * len(text)


def _linespace(widget, font):
    try:
        return int(widget.tk.call("font", "metrics", font, "-linespace"))
    except (tk.TclError, ValueError):
        return 15


def _crt_glass(canvas, **spec):
    """B11: the CRT glass of `canvas`, asked of the live fx (fx.get(canvas).motion.crt_glass -> fx/draw.CrtGlass), or
    None (no fx, the kill switch: NullFX's no-op namespace). Never raises."""
    try:
        from .. import fx as _fx
        return _fx.get(canvas).motion.crt_glass(canvas, **spec)
    except Exception:  # noqa: BLE001
        return None


class LoadingPlate(tk.Canvas):
    """The square LOADING plate: 214x68 (at 100 %), no fill, a 3 px bright outline drawn as four exact
    rectangles (so the ink box is exactly 214x68), the word centred in display XL bright. Static: CRT (B11)
    is added on top of these items, never by this class."""

    def __init__(self, parent, theme, word="LOADING", **kw):
        ensure_fonts(parent)
        self._t = tokens(theme)
        self.plate_w, self.plate_h = px(parent, PLATE_W), px(parent, PLATE_H)
        self.stroke = px(parent, PLATE_STROKE)
        self.fx_glass = None            # B11: the CRT glass (LoadCard asks the live fx for it)
        kw.setdefault("bg", self._t["bg"])
        super().__init__(parent, width=self.plate_w, height=self.plate_h, highlightthickness=0, bd=0,
                         takefocus=0, **kw)
        self.word = str(word)
        self.word_size = None
        self.draw()

    def word_font(self):
        """The largest display size whose word fits inside the outline (XL at 100 % with either face)."""
        room = self.plate_w - 2 * (self.stroke + px(self, PLATE_WORD_PAD))
        for size in WORD_SIZES:
            font = display_font(self, size)
            if _measure(self, font, self.word) <= room:
                return size, font
        return WORD_SIZES[-1], display_font(self, WORD_SIZES[-1])

    def draw(self):
        self.delete("plate")
        w, h, s = self.plate_w, self.plate_h, self.stroke
        ink = self._t["bright"]
        for x, y, ww, hh in ((0, 0, w, s), (0, h - s, w, s), (0, 0, s, h), (w - s, 0, s, h)):
            rect_px(self, x, y, ww, hh, fill=ink, tag="plate", kind="plate_outline")
        self.word_size, font = self.word_font()
        self.create_text(w / 2.0, h / 2.0, text=self.word, fill=ink, font=font, anchor="center",
                         tags=("plate", "plate_word"))
        g = self.fx_glass               # B11: a new word sits above the old scanlines - the glass slices it again
        if g is not None:
            try:
                g.paint("plate")
            except Exception:  # noqa: BLE001 - an effect never breaks the plate
                pass

    # ---------------------------------------------------------------- B11: what the CRT glass reads
    def glass_box(self):
        """(x0, y0, x1, y1, stroke, ink) of the outline drawn exactly inside the canvas (the glass blooms inside it)."""
        return (0, 0, self.plate_w, self.plate_h, self.stroke, self._t["bright"])

    def glass_words(self):
        """The plate's display word (the glass slices text >= 15 pt only)."""
        return self.find_withtag("plate_word")


class LoadCard(tk.Frame):
    """The whole model-load card (see the module docstring). It places itself centred in `parent` (the
    overlay's backdrop) and refits on the parent's <Configure>. Public parts: plate, label, elapsed_key,
    elapsed_value, warning, ledger_box, ledger (whatever make_ledger returned)."""

    def __init__(self, parent, theme, title, label, warning, elapsed_var, make_ledger, **kw):
        ensure_fonts(parent)
        t = self._t = tokens(theme)
        bg = t["bg"]
        kw.setdefault("takefocus", 1)
        super().__init__(parent, bg=bg, highlightthickness=EDGE, highlightbackground=t["dim"],
                         highlightcolor=t["dim"], bd=0, **kw)
        pad = px(parent, CARD_PAD) - EDGE
        self._pad = pad
        self._card_w = None
        self._box_h = None

        # B11: the plate sits inside two Frames whose highlight rings are the room for the CRT bloom outside its
        # stroke (g px); the paddings next to it give those g px back, so every part keeps its approved place
        b_out, b_in = px(parent, BLOOM_OUT_W), px(parent, BLOOM_IN_W)
        g = b_out + b_in
        top = tk.Frame(self, bg=bg)
        top.pack(fill="x", padx=(pad - g, pad), pady=(pad - g, 0))
        ring_out = tk.Frame(top, bg=bg, bd=0, highlightthickness=b_out, highlightbackground=bg, highlightcolor=bg)
        ring_in = tk.Frame(ring_out, bg=bg, bd=0, highlightthickness=b_in, highlightbackground=bg, highlightcolor=bg)
        self.plate = LoadingPlate(ring_in, theme, word=str(title).upper())
        self.plate.pack()
        ring_in.pack()
        ring_out.pack(side="left", anchor="n")
        self.plate_bloom = (ring_out, ring_in)
        right = tk.Frame(top, bg=bg)
        right.pack(side="left", anchor="n", fill="x", expand=True, padx=(px(parent, PLATE_GAP) - g, 0), pady=(0, g))
        self.label = tk.Label(right, text=label, bg=bg, fg=t["value"], font=C10B, justify="left", anchor="w",
                              wraplength=px(parent, 400), bd=0, padx=0, pady=0)
        self.label.pack(anchor="w", fill="x", pady=(px(parent, LABEL_TOP) + g, 0))
        erow = tk.Frame(right, bg=bg)
        erow.pack(anchor="w", pady=(px(parent, ELAPSED_GAP), 0))
        self.elapsed_key = tk.Label(erow, text="ELAPSED", bg=bg, fg=t["value"], font=C9, bd=0, padx=0, pady=0)
        self.elapsed_key.pack(side="left")
        self.elapsed_value = tk.Label(erow, textvariable=elapsed_var, bg=bg, fg=t["bright"], font=C13B, bd=0,
                                      padx=0, pady=0)
        self.elapsed_value.pack(side="left", padx=(px(parent, ELAPSED_KEY_GAP), 0))

        self.warning = tk.Label(self, text=warning, bg=bg, fg=t["warn_eff"], font=C9, justify="left", anchor="w",
                                wraplength=px(parent, 640), bd=0, padx=0, pady=0)
        self.warning.pack(fill="x", padx=pad, pady=(px(parent, WARNING_GAP) - g, 0))

        self._pitch = _linespace(self, C10)
        self._box_chrome = 2 * px(parent, LEDGER_PADY) + 2
        self.ledger_box = tk.Frame(self, bg=bg, highlightthickness=1, highlightbackground=t["dim"],
                                   highlightcolor=t["dim"], bd=0, height=self._box_height(LEDGER_LINES))
        self.ledger_box.pack_propagate(False)
        self.ledger_box.pack(fill="x", padx=pad, pady=(px(parent, LEDGER_GAP), pad))
        self.ledger = make_ledger(self.ledger_box)
        self._measure_ledger(parent)
        self.ledger_box.configure(height=self._box_height(LEDGER_LINES))
        self.ledger.pack(fill="both", expand=True)
        swallow_tabs(self)              # B9-H1: Tab never leaves the card (the ledger included)

        self.place(relx=0.5, rely=0.5, anchor="center", width=px(parent, CARD_W))
        parent.bind("<Configure>", self._fit, add="+")
        self._fit()
        # B11: CRT on the plate's glass while the fx is live (None under the kill switch: the static plate only)
        self.plate.fx_glass = _crt_glass(self.plate, words=self.plate.glass_words, plate=self.plate.glass_box,
                                         frames=self.plate_bloom)

    def _measure_ledger(self, parent):
        """The box holds whole rows of the ledger: its padding is set here (approved pixels at the display ratio)
        and the row pitch / chrome are read from the widget itself, so no partial row shows at any scaling."""
        try:
            self.ledger.configure(padx=px(parent, LEDGER_PADX), pady=px(parent, LEDGER_PADY))
        except tk.TclError:
            pass
        try:
            # R2 2.11: the lines sit at a 16 px pitch. Consolas 10's own line is 15 px at 100 %, so the missing pixel
            # goes above every display row (spacing1 for a line's first row, spacing2 for its wrapped rows); a face
            # whose line is already taller keeps its own height.
            line = _linespace(self.ledger, self.ledger.cget("font"))
            extra = max(0, px(parent, LEDGER_PITCH) - line)
            self.ledger.configure(spacing1=extra, spacing2=extra, spacing3=0)
            self._pitch = line + extra
            edge = int(self.ledger.cget("bd")) + int(self.ledger.cget("highlightthickness"))
            self._box_chrome = 2 * (int(self.ledger.cget("pady")) + edge) + 2
        except (tk.TclError, ValueError):
            pass

    def _box_height(self, lines):
        return lines * self._pitch + self._box_chrome

    def _room(self):
        """(width, height) the card may use: the parent's size, or the window's before the parent is laid out."""
        try:
            w, h = self.master.winfo_width(), self.master.winfo_height()
            if w <= 1 or h <= 1:
                top = self.winfo_toplevel()
                w, h = top.winfo_width(), top.winfo_height()
            return w, h
        except tk.TclError:
            return 0, 0

    def _fit(self, event=None):
        try:
            pw, ph = self._room()
            if pw <= 1 or ph <= 1:
                return
            margin = px(self, CARD_MARGIN)
            cw = max(px(self, CARD_MIN_W), min(px(self, CARD_W), pw - 2 * margin))
            inner = cw - 2 * (self._pad + EDGE)
            if cw != self._card_w:
                self._card_w = cw
                self.place_configure(width=cw)
                self.label.configure(wraplength=max(px(self, LABEL_MIN_WRAP),
                                                    inner - self.plate.plate_w - px(self, PLATE_GAP)))
                self.warning.configure(wraplength=max(px(self, LABEL_MIN_WRAP), inner))
            # Label reqheights are recomputed synchronously on configure; the packed frames' are not (idle), so the
            # height is summed from the labels themselves.
            right_h = (px(self, LABEL_TOP) + self.label.winfo_reqheight() + px(self, ELAPSED_GAP)
                       + max(self.elapsed_key.winfo_reqheight(), self.elapsed_value.winfo_reqheight()))
            other = (2 * (self._pad + EDGE) + max(self.plate.plate_h, right_h) + px(self, WARNING_GAP)
                     + self.warning.winfo_reqheight() + px(self, LEDGER_GAP))
            rows = (ph - 2 * margin - other - self._box_chrome) // max(1, self._pitch)   # whole rows only
            box_h = self._box_height(max(LEDGER_MIN_LINES, min(LEDGER_LINES, rows)))
            if box_h != self._box_h:
                self._box_h = box_h
                self.ledger_box.configure(height=box_h)
        except tk.TclError:
            pass  # torn down mid-event (the overlay finished)

    def ledger_lines(self):
        """How many ledger lines the box holds at its current height."""
        h = self._box_h if self._box_h is not None else self._box_height(LEDGER_LINES)
        return (h - self._box_chrome) // max(1, self._pitch)
